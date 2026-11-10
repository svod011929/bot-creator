"""Супервизор дочерних ботов: каждый бот — отдельный процесс `python -m child --dir data/bots/<id>`.

* запускает/останавливает процессы;
* перезапускает упавшие с нарастающей паузой;
* при неверном токене (код выхода 3) или частых падениях помечает бота как «ошибка» и уведомляет владельца;
* при старте конструктора поднимает всех ботов с desired=1 (переживает перезагрузку сервера).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from child.files import write_config
from creator.config import PROJECT_ROOT, Config
from creator.db import DB

log = logging.getLogger("supervisor")

EXIT_BAD_TOKEN = 3
MAX_CRASHES = 5          # столько падений подряд…
CRASH_WINDOW = 300       # …за столько секунд — и бот помечается как ошибка
LOG_MAX_BYTES = 5 * 1024 * 1024
CHILD_ENV_KEYS = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP", "VIRTUAL_ENV",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PYTHONIOENCODING", "PYTHONUTF8",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass
class Child:
    bot_id: int
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None


class Supervisor:
    def __init__(self, db: DB, cfg: Config, notifier: Notifier | None = None,
                 cmd: list[str] | None = None, creator_username: str = ""):
        self.db = db
        self.cfg = cfg
        self.notifier = notifier
        self.cmd = cmd or [sys.executable, "-m", "child"]
        self.creator_username = creator_username
        self.children: dict[int, Child] = {}
        self.crashes: dict[int, list[float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._stopping = False

    def _bot_lock(self, bot_id: int) -> asyncio.Lock:
        return self._locks.setdefault(bot_id, asyncio.Lock())

    def bot_dir(self, bot_id: int) -> Path:
        return self.cfg.bots_dir / str(bot_id)

    def write_files(self, bot_id: int) -> Path:
        b = self.db.bot(bot_id)
        if b is None:
            raise LookupError(bot_id)
        d = self.bot_dir(bot_id)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        tgrass_key = self.db.tgrass_key_for(bot_id)
        piarflow_key = self.db.pf_key_for(bot_id)
        write_config(d, {
            "bot_id": b["id"],
            "tgrass_key": tgrass_key,
            "tgrass_base_url": self.cfg.tgrass_base_url,
            "piarflow_key": piarflow_key,
            "piarflow_base_url": self.cfg.piarflow_base_url,
            "token": b["token"],
            "template": b["template"],
            "owner_id": b["owner_id"],
            "username": b["username"],
            "vip": bool(b["vip"]),
            "creator_username": self.creator_username,
        })
        return d

    def is_running(self, bot_id: int) -> bool:
        c = self.children.get(bot_id)
        return c is not None and c.proc.returncode is None

    async def _notify(self, bot_id: int, text: str) -> None:
        b = self.db.one("SELECT owner_id FROM bots WHERE id=?", bot_id)
        if b and self.notifier:
            try:
                await self.notifier(b["owner_id"], text)
            except Exception:  # noqa: BLE001
                log.exception("notify failed")

    async def _spawn(self, bot_id: int) -> None:
        d = self.write_files(bot_id)
        logfile = d / "bot.log"
        if logfile.exists() and logfile.stat().st_size > LOG_MAX_BYTES:
            shutil.move(logfile, d / "bot.log.1")
        env = {k: v for k, v in os.environ.items() if k in CHILD_ENV_KEYS}
        env["PYTHONUNBUFFERED"] = "1"
        with open(logfile, "ab") as out:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd, "--dir", str(d), cwd=str(PROJECT_ROOT), env=env,
                stdout=out, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
            )
        child = Child(bot_id, proc)
        self.children[bot_id] = child
        child.task = asyncio.create_task(self._watch(child))
        self.db.update_bot(bot_id, status="running", last_error=None)
        log.info("bot %s started pid=%s", bot_id, proc.pid)

    async def start(self, bot_id: int, *, require_desired: bool = False) -> None:
        async with self._bot_lock(bot_id):
            await self._start(bot_id, require_desired=require_desired)

    async def _start(self, bot_id: int, *, require_desired: bool = False) -> None:
        b = self.db.bot(bot_id)
        if self._stopping or not b or (require_desired and not b["desired"]):
            return
        self.db.update_bot(bot_id, desired=1)
        if self.is_running(bot_id):
            return
        self.crashes.pop(bot_id, None)
        await self._spawn(bot_id)

    async def _terminate(self, bot_id: int) -> None:
        child = self.children.pop(bot_id, None)
        if not child or child.proc.returncode is not None:
            return
        try:
            child.proc.terminate()
            await asyncio.wait_for(child.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            child.proc.kill()
            await child.proc.wait()
        except ProcessLookupError:
            pass

    async def stop(self, bot_id: int, status: str = "stopped") -> None:
        async with self._bot_lock(bot_id):
            self.db.update_bot(bot_id, desired=0, status=status)
            await self._terminate(bot_id)

    async def restart(self, bot_id: int, *, require_desired: bool = False) -> None:
        async with self._bot_lock(bot_id):
            b = self.db.bot(bot_id)
            if self._stopping or not b or (require_desired and not b["desired"]):
                return
            await self._terminate(bot_id)
            await self._start(bot_id, require_desired=require_desired)

    async def start_all(self) -> int:
        n = 0
        for b in self.db.bots_to_run():
            try:
                await self.start(b["id"])
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("cannot start bot %s", b["id"])
        return n

    async def shutdown(self) -> None:
        """Остановка конструктора: гасим процессы, но desired не трогаем — после рестарта поднимутся."""
        self._stopping = True
        async def terminate_locked(bot_id):
            async with self._bot_lock(bot_id):
                await self._terminate(bot_id)
        await asyncio.gather(*(terminate_locked(i) for i in set(self.children) | set(self._locks)), return_exceptions=True)

    async def _watch(self, child: Child) -> None:
        code = await child.proc.wait()
        if self._stopping or self.children.get(child.bot_id) is not child:
            return  # остановлен намеренно
        self.children.pop(child.bot_id, None)
        bot_id = child.bot_id
        b = self.db.bot(bot_id)
        if not b or not b["desired"]:
            self.db.update_bot(bot_id, status="stopped")
            return
        if code == EXIT_BAD_TOKEN:
            self.db.update_bot(bot_id, desired=0, status="error", last_error="Неверный токен")
            await self._notify(bot_id, f"⚠️ Бот ID {bot_id} остановлен: токен недействителен. "
                                       "Возможно, его перевыпустили в @BotFather.")
            return
        now = time.monotonic()
        hist = [t for t in self.crashes.get(bot_id, []) if now - t < CRASH_WINDOW] + [now]
        self.crashes[bot_id] = hist
        if len(hist) >= MAX_CRASHES:
            self.db.update_bot(bot_id, desired=0, status="error", last_error=f"Частые падения (код {code})")
            await self._notify(bot_id, f"⚠️ Бот ID {bot_id} несколько раз упал и остановлен. "
                                       "Нажмите «Запустить» в карточке бота или напишите администратору.")
            return
        delay = min(60, 2 ** len(hist))
        log.warning("bot %s exited with %s, restart in %ss", bot_id, code, delay)
        self.db.update_bot(bot_id, status="restarting", last_error=f"Перезапуск после падения (код {code})")
        await asyncio.sleep(delay)
        async with self._bot_lock(bot_id):
            b = self.db.bot(bot_id)
            if b and b["desired"] and not self._stopping and not self.is_running(bot_id):
                await self._spawn(bot_id)

    def remove_files(self, bot_id: int) -> None:
        d = self.bot_dir(bot_id)
        if d.exists():
            trash = self.cfg.data_dir / "deleted"
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / f"{bot_id}-{int(time.time())}"
            shutil.move(str(d), target)
            cfg = target / "config.json"
            if cfg.exists():
                cfg.unlink()  # токен не храним
