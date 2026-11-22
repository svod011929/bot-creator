"""Запуск дочернего бота: python -m child --dir data/bots/<id> [--check]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage

from child.core import ChildApp, GateMiddleware, build_admin_router, build_core_router
from child.engagement import ObservationScope
from child.withdrawals import build_history_router

EXIT_BAD_TOKEN = 3


def build_dispatcher(app: ChildApp) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    gate = GateMiddleware(app)
    dp.message.outer_middleware(ObservationScope())
    dp.callback_query.outer_middleware(ObservationScope())
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)
    dp.include_router(build_core_router(app))
    dp.include_router(build_history_router(app))
    dp.include_router(build_admin_router(app))
    if hasattr(app.template, "build_admin_router"):
        dp.include_router(app.template.build_admin_router(app))
    dp.include_router(app.template.build_router(app))
    return dp


async def run(bot_dir: Path) -> int:
    app = ChildApp(bot_dir)
    bot = Bot(app.cfg["token"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            break
        except TelegramUnauthorizedError:
            logging.error("Токен бота недействителен (отозван в BotFather?)")
            await bot.session.close()
            return EXIT_BAD_TOKEN
        except (TelegramNetworkError, TelegramServerError) as e:
            # Telegram недоступен — ждём сами, чтобы супервизор не счёл это падением
            logging.warning("Telegram недоступен (%s), повтор через %s с", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    app.username = me.username or app.username
    dp = build_dispatcher(app)
    logging.info("Бот @%s (шаблон %s) запущен", app.username, app.info.key)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        return EXIT_BAD_TOKEN
    finally:
        await app.close_gates()
        await bot.session.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--check", action="store_true", help="только собрать бота без подключения к Telegram")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check:
        app = ChildApp(args.dir)
        build_dispatcher(app)
        print(f"OK: шаблон {app.info.key}, владелец {app.owner_id}")
        return
    sys.exit(asyncio.run(run(args.dir)))


if __name__ == "__main__":
    main()
