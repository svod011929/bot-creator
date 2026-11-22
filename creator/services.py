"""Бизнес-операции конструктора, общие для оплаты LavePay и оплаты с баланса. Все суммы — копейки."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import Bot

from child import templates
from creator.config import Config
from creator.db import DB, InsufficientFunds
from creator.money import ceil_rub, rub
from creator.supervisor import Supervisor

log = logging.getLogger("services")

TokenValidator = Callable[[str], Awaitable[tuple[str, str] | None]]


async def telegram_validate_token(token: str) -> tuple[str, str] | None:
    """getMe по токену. Возвращает (username, имя) или None."""
    bot = Bot(token)
    try:
        me = await bot.get_me()
        return me.username or "", me.full_name
    except Exception:  # noqa: BLE001
        return None
    finally:
        await bot.session.close()


@dataclass
class Ctx:
    db: DB
    cfg: Config
    sup: Supervisor
    validate_token: TokenValidator = telegram_validate_token
    bot_username: str = "creator_bot"
    started_bots: set[int] = field(default_factory=set)
    lave: Any = None  # creator.lavepay.LavePayService, если задан LAVEPAY_API_KEY
    tgrass: Any = None  # creator.tgrass.TGrassService, если задан TGRASS_FACTORY_TOKEN
    piarflow: Any = None  # creator.piarflow.PiarFlowService, если задан PIARFLOW_TOKEN

    source_inputs: dict = field(default_factory=dict, repr=False)  # expiring candidate secrets, never FSM/DB
    subscription_checks: dict = field(default_factory=dict)
    bot_lists: dict = field(default_factory=dict)  # bounded ephemeral owner list views, no bot rows/credentials

    @property
    def extra_earn(self) -> bool:
        """Есть ли хоть одна сеть «Доп заработка»."""
        return self.tgrass is not None or self.piarflow is not None

    async def delete_bot(self, bot_id: int) -> None:
        """Удаление бота: остановка, последняя синхронизация ОП TGrass, перенос файлов в корзину."""
        await self.sup.stop(bot_id)
        if self.tgrass is not None:
            try:
                self.tgrass.sync_bot(bot_id)
            except Exception:  # noqa: BLE001
                log.exception("tgrass final sync failed for %s", bot_id)
            if self.db.tgrass(bot_id):
                self.db.tgrass_set(bot_id, status="disabled")
        if self.piarflow is not None:
            try:
                self.piarflow.sync_bot(bot_id)
            except Exception:  # noqa: BLE001
                log.exception("piarflow final sync failed for %s", bot_id)
            if self.db.pf(bot_id):
                self.db.pf_set(bot_id, status="disabled")
        self.db.delete_bot(bot_id)
        self.sup.remove_files(bot_id)

    def price(self, key: str) -> int:
        raw = self.db.kv_get(f"price:{key}")
        if raw is not None and raw.lstrip("-").isdigit():
            return int(raw)
        return int(self.cfg.prices[key])

    def template_price(self, template_key: str) -> int:
        return self.price(templates.info(template_key).price_key)

    def hosting_pack_price(self, days: int) -> int:
        """Цена пакета хостинга на days дней, вверх до целого рубля (7/30/90 дн. при 4,50 ₽ = 32/135/405 ₽)."""
        return ceil_rub(days * self.price("HOSTING_DAY_PRICE"))

    def expected_amount(self, uid: int, payload: str) -> tuple[int | None, str]:
        """Сумма (копейки), которую ожидаем за payload, или (None, причина отказа)."""
        from creator.lavepay import MAX_TOPUP_KOP, MIN_TOPUP_KOP
        kind, *rest = payload.split(":")
        try:
            if kind == "topup":
                amount = int(rest[0])
                ok = MIN_TOPUP_KOP <= amount <= MAX_TOPUP_KOP and len(rest) == 1
                return (amount, "") if ok else (None, "Неверная сумма")
            if kind == "create":
                d = self.db.draft(int(rest[0]))
                if not d or d["user_id"] != uid or d["used"]:
                    return None, "Заказ устарел, создайте бота заново"
                return self.template_price(d["template"]), ""
            if kind == "host":
                b = self.db.bot(int(rest[0]))
                if not b or b["owner_id"] != uid:
                    return None, "Бот не найден"
                amount = int(rest[1])
                return (amount, "") if amount > 0 else (None, "Неверная сумма")
            if kind == "vip":
                b = self.db.bot(int(rest[0]))
                if not b or b["owner_id"] != uid:
                    return None, "Бот не найден"
                if b["vip"]:
                    return None, "VIP уже активен"
                return self.price("VIP_PRICE"), ""
        except (ValueError, IndexError):
            pass
        return None, "Неизвестный платёж"

    def is_admin(self, uid: int) -> bool:
        return uid in self.cfg.admin_ids

    # ---------- операции ----------
    async def create_bot(self, draft_id: int) -> int | None:
        start_hosting = self.price("HOSTING_DAY_PRICE") * self.price("START_HOSTING_DAYS")
        bot_id = self.db.materialize_draft(draft_id, start_hosting)
        if bot_id is None:
            return None
        try:
            await self.sup.start(bot_id)
        except Exception:  # noqa: BLE001
            log.exception("start failed for %s", bot_id)
            self.db.update_bot(bot_id, status="error", last_error="Не удалось запустить")
        return bot_id

    async def create_bot_from_balance(self, uid: int, draft_id: int) -> int | None:
        d = self.db.draft(draft_id)
        if not d or d["used"] or d["user_id"] != uid:
            return None
        price = self.template_price(d["template"])
        self.db.change_balance(uid, -price, "create", f"Создание бота @{d['bot_username']}")
        bot_id = await self.create_bot(draft_id)
        if bot_id is None:
            self.db.change_balance(uid, price, "refund", "Возврат: черновик уже использован")
        return bot_id

    async def set_vip(self, bot_id: int) -> None:
        self.db.update_bot(bot_id, vip=1)
        await self.apply_changes(bot_id)

    async def apply_changes(self, bot_id: int) -> None:
        """Переписывает config.json и перезапускает бота, если он запущен."""
        b = self.db.bot(bot_id)
        if not b:
            return
        if b["desired"]:
            await self.sup.restart(bot_id)
        else:
            self.sup.write_files(bot_id)

    async def fulfil_payload(self, uid: int, payload: str, amount: int, via: str = "LavePay") -> str:
        """Выполняет оплаченный счёт (amount — копейки). Возвращает текст для пользователя."""
        kind, *rest = payload.split(":")
        if kind == "topup":
            self.db.change_balance(uid, amount, "topup", f"Пополнение через {via}")
            return f"✅ Баланс пополнен на {rub(amount)}"
        if kind == "create":
            bot_id = await self.create_bot(int(rest[0]))
            if bot_id is None:
                self.db.change_balance(uid, amount, "refund", "Возврат: черновик уже использован")
                return "⚠️ Этот заказ уже был оплачен ранее — сумма зачислена на ваш баланс."
            return f"✅ Оплата получена! Бот ID {bot_id} создан и запускается. Откройте «🤖 Мои боты»."
        if kind == "host":
            bot_id = int(rest[0])
            if not self.db.bot(bot_id):
                self.db.change_balance(uid, amount, "refund", "Возврат: бот не найден")
                return "⚠️ Бот не найден — сумма зачислена на баланс."
            bal = self.db.add_hosting(bot_id, amount)
            return f"✅ Баланс хостинга бота ID {bot_id}: {rub(bal)}"
        if kind == "vip":
            bot_id = int(rest[0])
            if not self.db.bot(bot_id):
                self.db.change_balance(uid, amount, "refund", "Возврат: бот не найден")
                return "⚠️ Бот не найден — сумма зачислена на баланс."
            await self.set_vip(bot_id)
            return f"👑 VIP для бота ID {bot_id} активирован!"
        log.error("unknown payload %s", payload)
        self.db.change_balance(uid, amount, "topup", "Оплата с неизвестным назначением")
        return "✅ Оплата зачислена на баланс."


__all__ = ["Ctx", "InsufficientFunds", "telegram_validate_token"]
