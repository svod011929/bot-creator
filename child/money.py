"""Integer minor units for child-bot balances and rewards.

User-facing amounts (settings, typed sums, MGRAM API) stay in major units.
Stored credits and debits are integers: 1 major unit = 100 minor units.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SCALE = 100

# Tables/columns that hold user-facing money and must be converted together.
MONEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "balance"),
    ("withdrawals", "amount"),
    ("tasks", "reward"),
    ("micro_tasks", "reward"),
    ("micro_mgram_shown", "reward"),
    ("micro_mgram_done", "reward"),
)


def to_minor(value: object) -> int:
    """Convert a major-unit amount to integer minor units, rounding half up."""
    if value is None or value is True or value is False:
        raise ValueError("Invalid amount")
    try:
        quantized = (Decimal(str(value)) * SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid amount") from exc
    minor = int(quantized)
    if quantized != minor:
        raise ValueError("Invalid amount")
    return minor


def from_minor(value: object) -> Decimal:
    """Major-unit Decimal for a stored minor-unit integer (exact)."""
    return Decimal(int(value)) / SCALE


def as_minor(value: object) -> int:
    """Accept a stored integer minor amount; reject fractional floats."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid amount")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("Invalid amount")
        return int(value)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid amount") from exc
    if number != number.to_integral_value():
        raise ValueError("Invalid amount")
    return int(number)


def fmt(value: object) -> str:
    """Format a minor-unit integer for display, matching the previous major-unit style."""
    minor = as_minor(value)
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    major, cents = divmod(minor, SCALE)
    if cents == 0:
        return f"{sign}{major}"
    return f"{sign}{major}.{cents:02d}".rstrip("0")
