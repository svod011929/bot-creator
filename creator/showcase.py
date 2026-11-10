"""E2 (display only): resale showcase paging/filters and partner accrual summary over existing data."""
from __future__ import annotations

import sqlite3
import time

from child import templates
from creator.db import DB

PAGE = 10
FILTERS = ("all", *templates.TEMPLATE_MODULES)
_BASE = "FROM bots WHERE deleted=0 AND sale_price IS NOT NULL AND owner_id<>?"


def page(db: DB, uid: int, flt: str, number: int) -> tuple[list[sqlite3.Row], int, int]:
    """Same visibility as the existing market (not deleted, on sale, not own), newest first."""
    where, args = (_BASE, (uid,)) if flt == "all" else (_BASE + " AND template=?", (uid, flt))
    total = db.scalar(f"SELECT COUNT(*) {where}", *args) or 0
    pages = max(1, -(-total // PAGE))
    number = min(max(0, number), pages - 1)
    rows = db.all(f"SELECT id,template,sale_price {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                  *args, PAGE, number * PAGE)
    return rows, total, number


def seller_income(price: int, fee_percent: int) -> int:
    """Exactly the purchase formula in DB.buy_bot (fee floored)."""
    return price - price * fee_percent // 100


def partner_summary(db: DB, uid: int) -> dict:
    since = int(time.time()) - 30 * 86400
    row = db.one("SELECT COUNT(*), COALESCE(SUM(amount),0), COALESCE(SUM(CASE WHEN created_at>=? THEN amount END),0),"
                 " MAX(created_at) FROM transactions WHERE user_id=? AND kind='ref'", since, uid)
    return {"count": int(row[0]), "total": int(row[1]), "last30": int(row[2]), "last": row[3]}
