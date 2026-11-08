"""Деньги конструктора: всё хранится в копейках (int), показывается в ₽."""
from __future__ import annotations

import re

STARS_TO_KOP = 150  # курс миграции со старой валюты: 1 ⭐ = 1,5 ₽

_RUB_RE = re.compile(r"^([+-]?)(\d{1,9})(?:[.,](\d{1,2}))?$")


def rub(kop: int) -> str:
    """12345 -> «123,45 ₽», 22500 -> «225 ₽»."""
    kop = int(kop)
    sign = "-" if kop < 0 else ""
    whole, frac = divmod(abs(kop), 100)
    whole_s = f"{whole:,}".replace(",", " ")
    return f"{sign}{whole_s} ₽" if not frac else f"{sign}{whole_s},{frac:02d} ₽"


def parse_rub(text: str, *, allow_negative: bool = False) -> int | None:
    """«4,5» / «4.50» / «225» / «-10» -> копейки. None — не похоже на сумму."""
    m = _RUB_RE.match(text.strip().replace(" ", "").replace("₽", ""))
    if not m:
        return None
    sign, whole, frac = m.groups()
    if sign == "-" and not allow_negative:
        return None
    kop = int(whole) * 100 + int((frac or "0").ljust(2, "0"))
    return -kop if sign == "-" else kop


def ceil_rub(kop: int) -> int:
    """Округление вверх до целого рубля (в копейках)."""
    return -(-int(kop) // 100) * 100
