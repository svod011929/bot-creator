"""Один процесс для всех дочерних ботов.

Запускается супервизором: python -m child.worker --socket <path>
Не импортирует creator и не читает секреты конструктора.
Каждый бот — свой ChildApp, Bot, Dispatcher и bot.db.
Управление: JSON-строки по Unix-сокету.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError

from child.__main__ import EXIT_BAD_TOKEN, build_dispatcher
from child.core import ChildApp

log = logging.getLogger("worker")

FORBIDDEN_ENV = frozenset({
    "BOT_TOKEN", "LAVEPAY_API_KEY", "LAVEPAY_WEBHOOK_SECRET", "OWNER_PAYOUT_KEY",
    "TGRASS_FACTORY_TOKEN", "TGRASS_API_KEY", "TGRASS_HUB_API_KEY",
    "PIARFLOW_TOKEN", "PIARFLOW_HUB_API_KEY",
})


class BotSlot:
    def __init__(self, bot_id: int, app: ChildApp, bot: Bot, task: asyncio.Task):
        self.bot_id = bot_id
        self.app = app
        self.bot = bot
        self.task = task
        self.crashes: list[float] = []


class BotHost:
    """Хост ботов внутри worker. Тестируется без сокета и без Telegram."""

    def __init__(self):
        self.slots: dict[int, BotSlot] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._stopping = False
        self.exits: list[dict] = []

    def _lock(self, bot_id: int) -> asyncio.Lock:
        return self._locks.setdefault(bot_id, asyncio.Lock())

    def is_running(self, bot_id: int) -> bool:
        slot = self.slots.get(bot_id)
        return slot is not None and not slot.task.done()

    def running_ids(self) -> list[int]:
        return sorted(i for i, s in self.slots.items() if not s.task.done())

    async def start(self, bot_dir: Path) -> dict:
        app = ChildApp(bot_dir)
        bot_id = int(app.cfg.get("bot_id") or 0)
        if not bot_id:
            return {"ok": False, "error": "no bot_id in config"}
        async with self._lock(bot_id):
            if self.is_running(bot_id):
                return {"ok": True, "id": bot_id, "already": True}
            bot = Bot(app.cfg["token"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            task = asyncio.create_task(self._run(bot_id, app, bot))
            self.slots[bot_id] = BotSlot(bot_id, app, bot, task)
            log.info("bot %s started", bot_id)
            return {"ok": True, "id": bot_id}

    async def _run(self, bot_id: int, app: ChildApp, bot: Bot) -> None:
        code = 0
        try:
            delay = 5
            while True:
                try:
                    me = await bot.get_me()
                    break
                except TelegramUnauthorizedError:
                    code = EXIT_BAD_TOKEN
                    return
                except (TelegramNetworkError, TelegramServerError) as exc:
                    log.warning("bot %s telegram down (%s), retry %ss", bot_id, exc, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
            app.username = me.username or app.username
            dp = build_dispatcher(app)
            _append_bot_log(Path(app.dir), f"Run polling for bot @{app.username}")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except TelegramUnauthorizedError:
            code = EXIT_BAD_TOKEN
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("bot %s crashed", bot_id)
            code = 1
        finally:
            await self._close(app, bot)
            slot = self.slots.get(bot_id)
            # Intentional stop() pops the slot first — do not auto-restart.
            if not self._stopping and slot is not None and slot.task is asyncio.current_task():
                self.exits.append({"id": bot_id, "code": code})
                self.slots.pop(bot_id, None)

    async def _close(self, app: ChildApp, bot: Bot) -> None:
        try:
            await app.close_gates()
        except Exception:  # noqa: BLE001
            log.exception("close gates")
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass

    async def stop(self, bot_id: int) -> dict:
        async with self._lock(bot_id):
            slot = self.slots.pop(bot_id, None)
            if not slot:
                return {"ok": True, "id": bot_id, "stopped": True}
            slot.task.cancel()
            try:
                await slot.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self._close(slot.app, slot.bot)
            return {"ok": True, "id": bot_id}

    async def restart(self, bot_dir: Path) -> dict:
        app_id = _id_from_dir(bot_dir)
        if app_id:
            await self.stop(app_id)
        return await self.start(bot_dir)

    async def reload(self, bot_dir: Path) -> dict:
        """Перечитать config.json этого бота: стоп + старт только его."""
        return await self.restart(bot_dir)

    async def shutdown(self) -> None:
        self._stopping = True
        for bot_id in list(self.slots):
            await self.stop(bot_id)


def _append_bot_log(bot_dir: Path, line: str) -> None:
    try:
        log_path = bot_dir / "bot.log"
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(bot_dir / "bot.log.1")
        with open(log_path, "ab") as out:
            out.write((line + "\n").encode())
    except OSError:
        pass


def _id_from_dir(bot_dir: Path) -> int:
    cfg = bot_dir / "config.json"
    if not cfg.exists():
        return 0
    try:
        return int(json.loads(cfg.read_text(encoding="utf-8")).get("bot_id") or 0)
    except (ValueError, OSError):
        return 0


async def _handle(host: BotHost, raw: str) -> dict:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad json"}
    cmd = msg.get("cmd")
    if cmd == "ping":
        return {"ok": True, "pong": True}
    if cmd == "status":
        exits = host.exits
        host.exits = []
        return {"ok": True, "running": host.running_ids(), "exits": exits}
    if cmd == "envcheck":
        leaked = sorted(k for k in FORBIDDEN_ENV if os.environ.get(k))
        return {"ok": not leaked, "leaked": leaked}
    bot_dir = Path(msg["dir"]) if msg.get("dir") else None
    bot_id = int(msg.get("id") or 0)
    if cmd == "start" and bot_dir:
        return await host.start(bot_dir)
    if cmd == "stop" and bot_id:
        return await host.stop(bot_id)
    if cmd == "restart" and bot_dir:
        return await host.restart(bot_dir)
    if cmd == "reload" and bot_dir:
        return await host.reload(bot_dir)
    if cmd == "shutdown":
        await host.shutdown()
        return {"ok": True}
    return {"ok": False, "error": "unknown command"}


async def serve(socket_path: Path) -> None:
    host = BotHost()
    if socket_path.exists():
        socket_path.unlink()
    server = await asyncio.start_unix_server(_client(host), path=str(socket_path))
    os.chmod(socket_path, 0o600)
    log.info("worker listening on %s", socket_path)
    try:
        await server.serve_forever()
    finally:
        await host.shutdown()
        server.close()


def _client(host: BotHost):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            reply = await _handle(host, raw.decode())
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
    return handle


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--socket", required=True, type=Path)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    leaked = [k for k in FORBIDDEN_ENV if os.environ.get(k)]
    if leaked:
        log.error("refusing to start: creator secrets in environment: %s", ",".join(leaked))
        raise SystemExit(2)
    asyncio.run(serve(args.socket))


if __name__ == "__main__":
    main()
