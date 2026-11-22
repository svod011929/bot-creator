"""Точка входа: python -m creator [--dry-run]."""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from child import templates
from child.files import write_config
from creator.app import build_dispatcher
from creator.billing import billing_loop
from creator.config import PROJECT_ROOT, load_config
from creator.db import DB
from creator.lavepay import LavePayClient, LavePayService
from creator.services import Ctx
from creator.supervisor import Supervisor

log = logging.getLogger("creator")


def dry_run() -> int:
    """Проверка без Telegram: БД, роутеры, сборка всех шаблонов дочерних ботов."""
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        cfg.data_dir = Path(tmp)
        db = DB(cfg.db_path)
        ctx = Ctx(db=db, cfg=cfg, sup=Supervisor(db, cfg))
        dp = build_dispatcher(ctx)
        print(f"[ok] конструктор: роутеров {len(dp.sub_routers)}, апдейты {dp.resolve_used_update_types()}")
        print(f"[ok] цены: { {k: ctx.price(k) for k in cfg.prices} }")
        for t in templates.all_infos():
            d = Path(tmp) / "bots" / t.key
            write_config(d, {"token": "0:dry", "template": t.key, "owner_id": 1, "username": "dry_bot"})
            res = subprocess.run([sys.executable, "-m", "child", "--dir", str(d), "--check"],
                                 cwd=PROJECT_ROOT, capture_output=True, text=True)
            status = "ok" if res.returncode == 0 else "FAIL"
            print(f"[{status}] шаблон {t.button}: {res.stdout.strip() or res.stderr.strip()[-300:]}")
            if res.returncode:
                return 1
    print("[ok] токен BOT_TOKEN " + ("задан" if cfg.bot_token else "НЕ задан — для запуска заполните .env"))
    print(f"[ok] админы: {len(cfg.admin_ids)}")
    print("[ok] TGrass: " + ("настроен источник или фабрика" if cfg.tgrass_enabled else "нет настроенного источника/фабрики"))
    print("[ok] PiarFlow: " + ("настроен источник или фабрика" if cfg.piarflow_enabled else "нет настроенного источника/фабрики"))
    return 0


async def run() -> None:
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env (см. .env.example) или запустите с --dry-run")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit("BOT_TOKEN недействителен — проверьте токен в @BotFather и .env") from None

    async def notify(uid: int, text: str) -> None:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # noqa: BLE001
            log.info("notify %s failed: %s", uid, e)

    sup = Supervisor(db, cfg, notifier=notify, creator_username=me.username or "")
    ctx = Ctx(db=db, cfg=cfg, sup=sup, bot_username=me.username or "")
    lave_client = None
    tasks: list[asyncio.Task] = []
    webhook_runner = None
    if cfg.lavepay_enabled:
        lave_client = LavePayClient(cfg.lavepay_api_key, cfg.lavepay_base_url)
        ctx.lave = LavePayService(ctx, lave_client, notify=notify, webhook_secret=cfg.lavepay_webhook_secret)
        try:
            m = await lave_client.me()
            log.info("LavePay: мерчант #%s «%s», статус %s", m.get("id"), m.get("name"), m.get("status"))
        except Exception as e:  # noqa: BLE001
            log.warning("LavePay: проверка ключа не удалась: %s", e)
        tasks.append(asyncio.create_task(ctx.lave.poll_loop()))
        if cfg.lavepay_webhook_port and cfg.lavepay_webhook_secret:
            webhook_runner = await ctx.lave.start_webhook(cfg.lavepay_webhook_host, cfg.lavepay_webhook_port)
    from creator.integrations import ensure_services, wait_and_sync
    ensure_services(ctx)
    for network in ("tgrass", "piarflow"):
        tasks.append(asyncio.create_task(wait_and_sync(ctx, network)))
    dp = build_dispatcher(ctx)
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    started = await sup.start_all()
    log.info("Конструктор @%s запущен, поднято дочерних ботов: %s", me.username, started)
    billing = asyncio.create_task(billing_loop(ctx, notify))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        billing.cancel()
        for t in tasks:
            t.cancel()
        if webhook_runner:
            await webhook_runner.cleanup()
        if lave_client:
            await lave_client.close()
        for network in ("tgrass", "piarflow"):
            service = getattr(ctx, network)
            if service is not None:
                await service.client.close()
        await sup.shutdown()
        await bot.session.close()
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Конструктор Telegram-ботов")
    p.add_argument("--dry-run", action="store_true", help="самопроверка без подключения к Telegram")
    args = p.parse_args()
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        sys.exit(dry_run())
    asyncio.run(run())


if __name__ == "__main__":
    main()
