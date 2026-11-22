"""Owner-shop LavePay payouts for clicker and refbot (USDT TRC-20 only).

The constructor holds each owner's API key, encrypted at rest. Child processes
never see the key. Money is sent only after the owner confirms a live quote
in the constructor bot. The platform merchant is not used for these payouts.
"""
from __future__ import annotations

import logging
import re
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from child import templates
from child.files import read_config, read_settings, write_config
from child.payouts import PAYOUT_TEMPLATES, format_usdt, payout_unit
from creator.lavepay import LavePayClient, LavePayError
from creator.secretbox import SecretBoxError, box_for, decrypt, encrypt

log = logging.getLogger("owner_payouts")

KEY_RE = re.compile(r"^LAVE-\d+-[A-Za-z0-9_-]{8,}$")
USDT_MIN = Decimal("1")
RATE_MAX_DIGITS = Decimal("0.00000001")


def look_key(raw: str | None) -> str:
    """Normalize a pasted LavePay key; raise ValueError if the shape is wrong."""
    key = "".join((raw or "").split())
    if not KEY_RE.fullmatch(key):
        raise ValueError("key")
    return key


def usdt_balance(me: dict) -> str:
    bals = me.get("balances") if isinstance(me, dict) else None
    if isinstance(bals, dict) and bals.get("USD") is not None:
        return str(bals.get("USD"))
    if isinstance(me, dict) and me.get("balance") is not None:
        return str(me.get("balance"))
    return "0"


def public_me(me: dict) -> dict[str, Any]:
    return {
        "merchant_id": me.get("id"),
        "merchant_name": me.get("name"),
        "status": me.get("status"),
        "payouts_enabled": bool(me.get("payouts_enabled")),
        "usdt_balance": usdt_balance(me),
    }


def parse_rate(raw: str) -> str:
    text = (raw or "").strip().replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("rate") from exc
    if value <= 0 or value > Decimal("1000000"):
        raise ValueError("rate")
    quantized = format(value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN), "f")
    if "." in quantized:
        quantized = quantized.rstrip("0").rstrip(".")
    return quantized or "0"


def unit_ready(template: str, settings: dict, shop: dict | None) -> tuple[bool, str | None, str | None]:
    """Return (ready, kind, rate_string). kind is rub|usdt."""
    if not shop:
        return False, None, None
    kind = shop.get("unit_kind")
    rate = shop.get("unit_rate")
    if kind in {"rub", "usdt"} and rate:
        try:
            if Decimal(str(rate)) > 0:
                return True, kind, str(rate)
        except InvalidOperation:
            pass
    if template == "refbot" and payout_unit(settings, str(settings.get("currency") or "")) == "rub":
        return True, "rub", "1"
    return False, None, None


def child_flags(cfg, db, bot_id: int) -> dict[str, Any]:
    flags = {
        "lavepay_payouts_enabled": False,
        "lavepay_payouts_auto": False,
        "lavepay_unit_kind": None,
        "lavepay_unit_rate": None,
    }
    if not getattr(cfg, "child_lavepay_payouts", False):
        return flags
    row = db.bot(bot_id)
    if not row or row["template"] not in PAYOUT_TEMPLATES:
        return flags
    shop = db.owner_lavepay_public(bot_id)
    defaults = templates.info(row["template"]).defaults()
    settings = read_settings(Path(cfg.bots_dir) / str(bot_id), defaults)
    ready, kind, rate = unit_ready(row["template"], settings, shop)
    flags["lavepay_unit_kind"] = kind
    flags["lavepay_unit_rate"] = rate
    flags["lavepay_payouts_enabled"] = bool(
        shop and shop.get("payouts_enabled") and shop.get("status") == "active" and ready
    )
    return flags


def apply_child_flags(cfg, db, bot_id: int) -> None:
    """Update config.json flags in place without touching the token or other secrets."""
    path = Path(cfg.bots_dir) / str(bot_id)
    if not path.exists():
        return
    current = read_config(path)
    if not current:
        return
    current.update(child_flags(cfg, db, bot_id))
    write_config(path, current)


def shop_plain_key(cfg, db, bot_id: int) -> str | None:
    row = db.owner_lavepay_shop(bot_id)
    if not row:
        return None
    try:
        fernet = box_for(cfg.data_dir, getattr(cfg, "owner_payout_key", "") or "")
        return decrypt(fernet, row["key_blob"])
    except SecretBoxError:
        log.warning("owner lavepay key undecipherable bot=%s", bot_id)
        return None


async def inspect_key(api_key: str, base_url: str) -> dict:
    client = LavePayClient(api_key, base_url)
    try:
        return await client.me()
    finally:
        await client.close()


async def save_shop(cfg, db, bot_id: int, owner_id: int, api_key: str) -> dict:
    me = await inspect_key(api_key, cfg.lavepay_base_url)
    public = public_me(me)
    fernet = box_for(cfg.data_dir, getattr(cfg, "owner_payout_key", "") or "")
    db.upsert_owner_lavepay_shop(
        bot_id, owner_id, encrypt(fernet, api_key),
        merchant_id=public["merchant_id"], merchant_name=public["merchant_name"],
        status=public["status"], payouts_enabled=public["payouts_enabled"],
        usdt_balance=public["usdt_balance"],
    )
    apply_child_flags(cfg, db, bot_id)
    return public


async def refresh_shop(cfg, db, bot_id: int) -> dict | None:
    key = shop_plain_key(cfg, db, bot_id)
    if not key:
        return None
    me = await inspect_key(key, cfg.lavepay_base_url)
    public = public_me(me)
    db.update_owner_lavepay_meta(
        bot_id, merchant_id=public["merchant_id"], merchant_name=public["merchant_name"],
        status=public["status"], payouts_enabled=public["payouts_enabled"],
        usdt_balance=public["usdt_balance"],
    )
    apply_child_flags(cfg, db, bot_id)
    return public


def disconnect_shop(cfg, db, bot_id: int) -> None:
    db.delete_owner_lavepay_shop(bot_id)
    apply_child_flags(cfg, db, bot_id)


def set_rate(cfg, db, bot_id: int, owner_id: int, unit_kind: str, unit_rate: str) -> bool:
    ok = db.set_owner_lavepay_rate(bot_id, owner_id, unit_kind, parse_rate(unit_rate))
    if ok:
        apply_child_flags(cfg, db, bot_id)
    return ok


def clear_rate(cfg, db, bot_id: int, owner_id: int) -> bool:
    ok = db.set_owner_lavepay_rate(bot_id, owner_id, None, None)
    if ok:
        apply_child_flags(cfg, db, bot_id)
    return ok


def convert_amount(amount: float, kind: str, rate: str) -> tuple[str | None, Decimal | None]:
    """If kind is usdt, return USDT string. If rub, return (None, rubles Decimal)."""
    value = Decimal(str(amount)) * Decimal(rate)
    if kind == "usdt":
        if value < USDT_MIN:
            raise ValueError("below_min")
        return format_usdt(float(value)), None
    if kind == "rub":
        if value <= 0:
            raise ValueError("below_min")
        # integer rubles only? allow fractional bot units * rate to a ruble amount with 2 dp
        rubles = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if rubles <= 0:
            raise ValueError("below_min")
        return None, rubles
    raise ValueError("kind")


async def quote_for_job(client: LavePayClient, *, amount_usdt: str | None = None,
                        amount_rub: Decimal | None = None) -> dict[str, Any]:
    rate = None
    if amount_usdt is None:
        if amount_rub is None:
            raise ValueError("amount")
        rate_info = await _fx_rate(client)
        rate = Decimal(str(rate_info["rate"]))
        if rate <= 0:
            raise LavePayError("payout_unavailable", "Нет курса USDT", 409)
        usdt = (amount_rub / rate).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if usdt < USDT_MIN:
            raise ValueError("below_min")
        amount_usdt = format_usdt(float(usdt))
    quote = await client.withdrawals_quote("USDT", "TRX", amount_usdt)
    if quote.get("enough") is False:
        # still return the quote so the owner can see the shortfall
        pass
    return {
        "amount": str(quote.get("amount") or amount_usdt),
        "fee": str(quote.get("fee") or "0"),
        "debit": str(quote.get("debit") or quote.get("fee") or "0"),
        "rate": str(rate) if rate is not None else None,
        "amount_rub": str(amount_rub) if amount_rub is not None else None,
        "enough": bool(quote.get("enough", True)),
        "balance": str(quote.get("balance") or ""),
        "currency": str(quote.get("currency") or "USDT"),
    }


async def _fx_rate(client: LavePayClient) -> dict:
    last_error = None
    for method in ("card_rate", "sbp_rate"):
        try:
            data = await getattr(client, method)()
        except LavePayError as exc:
            last_error = exc
            continue
        if data.get("available") is False:
            last_error = LavePayError("payout_unavailable", str(data.get("reason") or "unavailable"), 409)
            continue
        if data.get("rate"):
            return data
    try:
        data = await client.request("GET", "/buy/rate")
        if data.get("rate"):
            return data
    except LavePayError as exc:
        last_error = exc
    if last_error:
        raise last_error
    raise LavePayError("payout_unavailable", "Нет курса USDT", 409)


async def owner_client(cfg, db, bot_id: int) -> LavePayClient:
    key = shop_plain_key(cfg, db, bot_id)
    if not key:
        raise LavePayError("disabled", "Магазин LavePay не подключён", 0)
    return LavePayClient(key, cfg.lavepay_base_url)
