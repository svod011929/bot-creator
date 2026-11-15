"""LavePay payout helpers for clicker and refbot child withdrawals.

The child process never receives LavePay keys. It only records a reserved
withdrawal and, when both the platform flag and the per-bot flag are on, a
payout job. The constructor later sends money from the platform merchant.
Flags default off, so the existing manual owner-paid flow stays the default.
"""
from __future__ import annotations

import json
import re
from typing import Any

from child.files import read_config

PAYOUT_TEMPLATES = frozenset({"clicker", "refbot"})
METHODS = ("card", "sbp", "usdt_trc20")
JOB_AWAITING = "awaiting_approval"
JOB_QUEUED = "queued"
JOB_SUBMITTED = "submitted"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

# LavePay documented limits (recipient gets this many rubles / USDT).
CARD_MIN_RUB, CARD_MAX_RUB = 100, 300_000
SBP_MIN_RUB, SBP_MAX_RUB = 500, 100_000
USDT_MIN = "1"
USDT_DECIMALS = 6

RUBLE_UNITS = frozenset({
    "₽", "руб", "руб.", "рубль", "рубля", "рублей", "рубли",
    "rub", "rur", "ruble", "rubles", "rub.",
})
USDT_UNITS = frozenset({
    "usdt", "usdt trc-20", "usdt trc20", "usdttrc20", "usdt-trc20",
})

CARD_RE = re.compile(r"^\d{16,19}$")
TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
SBP_NAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+ [А-ЯЁ]$")
IDEMPOTENCY_RE = re.compile(r"^cwd-([1-9][0-9]{0,18})-([1-9][0-9]{0,18})$")


def _truthy_flag(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def payouts_platform_enabled(cfg: dict[str, Any] | None) -> bool:
    return isinstance(cfg, dict) and _truthy_flag(cfg.get("lavepay_payouts_enabled"))


def payouts_auto_platform_enabled(cfg: dict[str, Any] | None) -> bool:
    return isinstance(cfg, dict) and _truthy_flag(cfg.get("lavepay_payouts_auto"))


def payouts_bot_enabled(settings: dict[str, Any] | None) -> bool:
    return isinstance(settings, dict) and _truthy_flag(settings.get("lavepay_payouts"))


def payouts_auto_bot_enabled(settings: dict[str, Any] | None) -> bool:
    return isinstance(settings, dict) and _truthy_flag(settings.get("lavepay_payouts_auto"))


def template_allows(app) -> bool:
    key = getattr(getattr(app, "info", None), "key", None) or (app.cfg or {}).get("template")
    return key in PAYOUT_TEMPLATES


def payouts_active(app) -> bool:
    """True only when platform + bot flags are on and the template is in scope."""
    if not template_allows(app):
        return False
    return payouts_platform_enabled(read_config(app.dir)) and payouts_bot_enabled(app.s)


def payouts_auto(app) -> bool:
    """Auto-queue only when both platform and bot auto flags are on (and payouts are active)."""
    if not payouts_active(app):
        return False
    return payouts_auto_platform_enabled(read_config(app.dir)) and payouts_auto_bot_enabled(app.s)


def normalize_unit_name(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("₽", "₽").split())


def payout_unit(settings: dict[str, Any] | None, currency: str) -> str | None:
    """Return 'rub', 'usdt', or None when the bot unit must not be paid via LavePay."""
    raw = str((settings or {}).get("lavepay_payout_unit") or "auto").strip().lower()
    if raw in {"rub", "usdt"}:
        return raw
    # "auto" or anything else: infer only from an unambiguous currency label.
    label = normalize_unit_name(currency)
    compact = label.replace(" ", "")
    if label in RUBLE_UNITS or compact in RUBLE_UNITS:
        return "rub"
    if label in USDT_UNITS or compact in USDT_UNITS:
        return "usdt"
    return None


def payouts_ready(app) -> bool:
    """Flags on, and the bot currency (or explicit unit setting) can be paid out."""
    return payouts_active(app) and payout_unit(app.s, app.currency) is not None


def methods_for_amount(unit: str, amount: float) -> list[str]:
    if unit == "usdt":
        try:
            if _usdt_ok(amount):
                return ["usdt_trc20"]
        except ValueError:
            return []
        return []
    if unit != "rub" or not _integer_amount(amount):
        return []
    rub = int(amount)
    methods: list[str] = []
    if CARD_MIN_RUB <= rub <= CARD_MAX_RUB:
        methods.append("card")
    if SBP_MIN_RUB <= rub <= SBP_MAX_RUB:
        methods.append("sbp")
    return methods


def _integer_amount(amount: float) -> bool:
    return amount == int(amount) and int(amount) > 0


def _usdt_ok(amount: float) -> bool:
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid USDT amount") from exc
    if value <= 0:
        raise ValueError("Invalid USDT amount")
    if value < Decimal(USDT_MIN):
        return False
    if value.as_tuple().exponent < -USDT_DECIMALS:
        return False
    return True


def format_usdt(amount: float) -> str:
    from decimal import ROUND_DOWN, Decimal
    value = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    raise ValueError("phone")


def normalize_card(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if not CARD_RE.fullmatch(digits):
        raise ValueError("card")
    return digits


def normalize_trc20(raw: str) -> str:
    address = (raw or "").strip()
    if not TRC20_RE.fullmatch(address):
        raise ValueError("address")
    return address


def normalize_sbp_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not SBP_NAME_RE.fullmatch(name):
        raise ValueError("name")
    return name


def match_sbp_bank(banks: list[dict[str, Any]], raw: str) -> dict[str, str] | None:
    text = " ".join((raw or "").split())
    if not text or not banks:
        return None
    if text.isdigit():
        hits = [b for b in banks if str(b.get("bank_id") or "") == text]
        return _bank(hits[0]) if len(hits) == 1 else None
    needle = text.casefold()
    exact = [b for b in banks if str(b.get("name") or "").casefold() == needle]
    if len(exact) == 1:
        return _bank(exact[0])
    starts = [b for b in banks if str(b.get("name") or "").casefold().startswith(needle)]
    if len(starts) == 1:
        return _bank(starts[0])
    contains = [b for b in banks if needle in str(b.get("name") or "").casefold()]
    if len(contains) == 1:
        return _bank(contains[0])
    return None


def _bank(row: dict[str, Any]) -> dict[str, str]:
    return {"bank_id": str(row.get("bank_id") or ""), "bank_name": str(row.get("name") or "")}


def mask_card(number: str) -> str:
    digits = re.sub(r"\D+", "", number or "")
    if len(digits) < 4:
        return "Карта ••••"
    return f"Карта •••• {digits[-4:]}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) < 4:
        return "СБП ••••"
    return f"+7 ••• •••-{digits[-4:-2]}-{digits[-2:]}"


def mask_address(address: str) -> str:
    value = (address or "").strip()
    if len(value) <= 10:
        return value
    return f"{value[:4]}…{value[-4:]}"


def public_details(method: str, destination: dict[str, Any]) -> str:
    if method == "card":
        return mask_card(str(destination.get("card_number") or ""))
    if method == "sbp":
        name = destination.get("recipient_name") or ""
        bank = destination.get("bank_name") or destination.get("bank") or ""
        return f"СБП, {mask_phone(str(destination.get('phone') or ''))}, {bank}, {name}".strip(", ")
    if method == "usdt_trc20":
        return f"USDT TRC-20 {mask_address(str(destination.get('address') or ''))}"
    return "Реквизиты скрыты"


def idempotency_key(bot_id: int, withdrawal_id: int) -> str:
    key = f"cwd-{int(bot_id)}-{int(withdrawal_id)}"
    if len(key) > 64:
        raise ValueError("idempotency key too long")
    return key


def parse_idempotency_key(key: str) -> tuple[int, int] | None:
    match = IDEMPOTENCY_RE.fullmatch((key or "").strip())
    if not match:
        return None
    bot_id, wid = int(match.group(1)), int(match.group(2))
    if bot_id > 2**31 - 1 or wid > 2**63 - 1:
        return None
    return bot_id, wid


def dumps_destination(destination: dict[str, Any]) -> str:
    return json.dumps(destination, ensure_ascii=False, separators=(",", ":"))


def loads_destination(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


METHOD_LABELS = {
    "card": "💳 На карту",
    "sbp": "🏦 По СБП",
    "usdt_trc20": "🪙 USDT TRC-20",
}

METHOD_PROMPTS = {
    "card": "Введите номер карты (только цифры, 16–19 знаков).",
    "sbp": "Введите российский номер телефона получателя, например +79991234567.",
    "usdt_trc20": "Введите адрес USDT в сети TRC-20 (начинается с T).",
}
