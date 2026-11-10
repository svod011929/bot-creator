"""Creator withdrawals of earned money (rules approved by the owner on 2026-11-10).

available = min(balance, max(0, E - C - S - W)), all in kopecks, from the user's own ledger:
  E — earned: positive ref, resale_sell, tgrass, piarflow;
  C — clawbacks: tgrass_penalty, piarflow_penalty (stored as negative amounts);
  S — spending, attributed to earned first: create, hosting, vip, resale_buy and negative admin;
  W — net withdrawn: withdraw debits (incl. the pending one) minus withdraw_refund.
topup, refund and positive admin never count as earned. A negative balance makes available 0.
Payouts are manual; see PayoutProvider.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from creator.db import DB, InsufficientFunds, now

EARNED = ("ref", "resale_sell", "tgrass", "piarflow")
PENALTIES = ("tgrass_penalty", "piarflow_penalty")
SPENDING = ("create", "hosting", "vip", "resale_buy")
METHOD_TITLES = {"card": "Банковская карта", "sbp": "СБП (по номеру телефона)", "usdt_trc20": "USDT TRC-20"}
METHOD_PROMPTS = {
    "card": "Отправьте номер карты (16–19 цифр).",
    "sbp": "Отправьте номер телефона и банк, например: +7 999 123-45-67 Т-Банк",
    "usdt_trc20": "Отправьте адрес кошелька USDT в сети TRC-20 (начинается с T, 34 символа).",
}
STATUS_TITLES = {"pending": "⏳ на рассмотрении", "paid": "✅ отмечена выплаченной", "rejected": "❌ отклонена"}


class WithdrawError(Exception):
    """code: pending | minimum | available | fee | method"""


class PayoutProvider(Protocol):
    """Extension point for an automatic payout API (e.g. LavePay). Not implemented: payouts are manual."""

    async def payout(self, request: sqlite3.Row) -> str: ...


@dataclass(frozen=True)
class Available:
    earned: int
    penalties: int
    spent: int
    withdrawn: int
    balance: int

    @property
    def available(self) -> int:
        return max(0, min(self.balance, self.earned - self.penalties - self.spent - self.withdrawn))


def _available(c: sqlite3.Connection, uid: int) -> Available:
    e = ",".join("?" * len(EARNED))
    p = ",".join("?" * len(PENALTIES))
    s = ",".join("?" * len(SPENDING))
    row = c.execute(
        f"SELECT COALESCE(SUM(CASE WHEN kind IN ({e}) AND amount>0 THEN amount END),0),"
        f" COALESCE(SUM(CASE WHEN kind IN ({p}) AND amount<0 THEN -amount END),0),"
        f" COALESCE(SUM(CASE WHEN (kind IN ({s}) OR kind='admin') AND amount<0 THEN -amount END),0),"
        " COALESCE(SUM(CASE WHEN kind='withdraw' THEN -amount WHEN kind='withdraw_refund' THEN -amount END),0)"
        " FROM transactions WHERE user_id=?", (*EARNED, *PENALTIES, *SPENDING, uid)).fetchone()
    bal = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    return Available(int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(bal[0]) if bal else 0)


def available(db: DB, uid: int) -> Available:
    with db._lock:
        return _available(db.conn, uid)


def fee_split(amount: int, percent: int) -> tuple[int, int]:
    fee = -(-amount * max(0, percent) // 100)
    return fee, amount - fee


def parse_amount(text: str) -> int | None:
    raw = (text or "").strip().replace(" ", "").replace(",", ".").removesuffix("₽")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", raw):
        return None
    try:
        kop = int(Decimal(raw) * 100)
    except InvalidOperation:
        return None
    return kop if kop > 0 else None


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


_TRC20 = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
_PHONE = re.compile(r"^\s*(\+7|8)[\s\-()]*((?:\d[\s\-()]*){10})(.*)$")


def validate(method: str, text: str) -> str | None:
    """Basic format check only; returns normalized details or None."""
    text = (text or "").strip()
    if method == "card":
        digits = re.sub(r"[\s-]", "", text)
        return digits if digits.isdigit() and 16 <= len(digits) <= 19 and _luhn(digits) else None
    if method == "sbp":
        m = _PHONE.match(text)
        if not m:
            return None
        bank = " ".join(m[3].split())[:40]
        phone = "+7" + re.sub(r"\D", "", m[2])
        return f"{phone} {bank}" if re.search(r"[A-Za-zА-Яа-яЁё]{2}", bank) else None
    if method == "usdt_trc20":
        return text if _TRC20.fullmatch(text) else None
    return None


def mask(details: str) -> str:
    core = re.sub(r"[^0-9A-Za-z]", "", details.split(" ")[0]) or details
    return "••••" + core[-4:]


def create_request(db: DB, uid: int, amount: int, method: str, details: str, *,
                   fee_percent: int, minimum: int) -> sqlite3.Row:
    if method not in METHOD_TITLES:
        raise WithdrawError("method")
    fee, net = fee_split(amount, fee_percent)
    if fee_percent < 0 or net <= 0:
        raise WithdrawError("fee")
    if amount < minimum:
        raise WithdrawError("minimum")
    try:
        with db.tx() as c:
            if c.execute("SELECT 1 FROM withdrawal_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone():
                raise WithdrawError("pending")
            if amount > _available(c, uid).available:
                raise WithdrawError("available")
            cur = c.execute("INSERT INTO withdrawal_requests(user_id,amount,fee,net,method,details,created_at) "
                            "VALUES(?,?,?,?,?,?,?)", (uid, amount, fee, net, method, details, now()))
            rid = cur.lastrowid
            db._change_balance(c, uid, -amount, "withdraw", f"Заявка на вывод №{rid}")
            return c.execute("SELECT * FROM withdrawal_requests WHERE id=?", (rid,)).fetchone()
    except sqlite3.IntegrityError:
        raise WithdrawError("pending") from None
    except InsufficientFunds:
        raise WithdrawError("available") from None


def resolve(db: DB, rid: int, *, approve: bool, by: int, reason: str | None = None) -> sqlite3.Row | None:
    """Atomic pending -> paid|rejected; None if already resolved or missing. Reject refunds the full amount."""
    with db.tx() as c:
        row = c.execute("SELECT * FROM withdrawal_requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            return None
        status = "paid" if approve else "rejected"
        reason = (reason or "").strip()[:300] or None
        cur = c.execute("UPDATE withdrawal_requests SET status=?, reason=?, details=?, resolved_at=?, resolved_by=? "
                        "WHERE id=? AND status='pending'",
                        (status, None if approve else reason, mask(row["details"]), now(), by, rid))
        if cur.rowcount != 1:
            return None
        if not approve:
            db._change_balance(c, row["user_id"], row["amount"], "withdraw_refund", f"Возврат по заявке на вывод №{rid}")
        return c.execute("SELECT * FROM withdrawal_requests WHERE id=?", (rid,)).fetchone()


def get(db: DB, rid: int) -> sqlite3.Row | None:
    return db.one("SELECT * FROM withdrawal_requests WHERE id=?", rid)


def pending_of(db: DB, uid: int) -> sqlite3.Row | None:
    return db.one("SELECT * FROM withdrawal_requests WHERE user_id=? AND status='pending'", uid)


def page(db: DB, number: int, uid: int | None = None, size: int = 10) -> tuple[list[sqlite3.Row], int, int]:
    where, args = ("WHERE user_id=?", (uid,)) if uid is not None else ("", ())
    total = db.scalar(f"SELECT COUNT(*) FROM withdrawal_requests {where}", *args) or 0
    pages = max(1, -(-total // size))
    number = min(max(0, number), pages - 1)
    rows = db.all(f"SELECT * FROM withdrawal_requests {where} "
                  "ORDER BY status='pending' DESC, id DESC LIMIT ? OFFSET ?", *args, size, number * size)
    return rows, total, number
