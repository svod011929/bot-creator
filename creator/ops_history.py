"""E1: read-only, paginated history of a user's own creator operations (rubles)."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

PAGE = 10
FILTERS = {
    "all": ("Все", None),
    "topup": ("Пополнения", ("topup",)),
    "bots": ("Боты", ("create", "hosting", "vip")),
    "ref": ("Партнёрские", ("ref",)),
    "resale": ("Перепродажа", ("resale_buy", "resale_sell")),
    "withdraw": ("Вывод", ("withdraw", "withdraw_refund")),
    "other": ("Прочее", None),
}
GROUPED = tuple(k for _, kinds in FILTERS.values() if kinds for k in kinds)
_REF = re.compile(r"^(\d{1,3})% от оплаты реферала -?\d+$")
_LONG_NUMBER = re.compile(r"\d{7,}")


def _where(flt: str) -> tuple[str, tuple]:
    kinds = FILTERS[flt][1]
    if flt == "other":
        return f" AND kind NOT IN ({','.join('?' * len(GROUPED))})", GROUPED
    if kinds:
        return f" AND kind IN ({','.join('?' * len(kinds))})", kinds
    return "", ()


def page(db, uid: int, flt: str, number: int) -> tuple[list, int, int]:
    """Rows of the caller only; page number is clamped to the existing range."""
    where, args = _where(flt)
    total = db.scalar("SELECT COUNT(*) FROM transactions WHERE user_id=?" + where, uid, *args) or 0
    pages = max(1, -(-total // PAGE))
    number = min(max(number, 0), pages - 1)
    rows = db.all("SELECT * FROM transactions WHERE user_id=?" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                  uid, *args, PAGE, number * PAGE)
    return rows, total, number


def operation(db, uid: int, tx_id: int):
    return db.one("SELECT * FROM transactions WHERE id=? AND user_id=?", tx_id, uid)


def when(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def description(kind: str, comment: str | None) -> str:
    """Own operation description without other people's Telegram IDs."""
    text = (comment or "").strip()
    if kind == "ref":
        m = _REF.match(text)
        text = f"{m[1]}% от оплаты приглашённого пользователя" if m else "Начисление от оплаты приглашённого пользователя"
    elif kind == "admin":
        text = "Корректировка администратором"
    text = _LONG_NUMBER.sub("…", text)[:200]
    return html.escape(text) if text else "—"
