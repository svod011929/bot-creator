"""SQLite дочернего бота (у каждого бота свой файл bot.db)."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    referrer_id INTEGER,
    ref_rewarded INTEGER NOT NULL DEFAULT 0,
    balance REAL NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    clicks INTEGER NOT NULL DEFAULT 0,
    last_daily INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    currency_at_request TEXT,
    resolved_at INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    channel TEXT,
    reward REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS task_done (
    user_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
"""


def now() -> int:
    return int(time.time())


def fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


class ChildDB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if str(exc) != "database is locked" or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            self.conn.executescript(SCHEMA)
            self._migrate_withdrawals()
        except BaseException:
            self.conn.close()
            raise
        self.lock = threading.RLock()

    def _migrate_withdrawals(self) -> None:
        # Recheck schema after acquiring SQLite's writer lock (also protects concurrent openers).
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(withdrawals)")}
            for name, kind in (("currency_at_request", "TEXT"), ("resolved_at", "INTEGER")):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE withdrawals ADD COLUMN {name} {kind}")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def one(self, sql: str, *a: Any) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, a).fetchone()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers across connections and roll back the whole business operation."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, *a: Any) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, a).fetchall()

    def exec(self, sql: str, *a: Any) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, a)

    def scalar(self, sql: str, *a: Any) -> Any:
        r = self.one(sql, *a)
        return r[0] if r else None

    # ---- пользователи ----
    def upsert_user(self, uid: int, username: str | None, full_name: str, referrer_id: int | None) -> bool:
        with self.tx():
            if self.one("SELECT 1 FROM users WHERE id=?", uid):
                self.exec("UPDATE users SET username=?, full_name=?, blocked=0 WHERE id=?", username, full_name, uid)
                return False
            if referrer_id == uid or (referrer_id and not self.one("SELECT 1 FROM users WHERE id=?", referrer_id)):
                referrer_id = None
            self.exec(
                "INSERT INTO users(id,username,full_name,referrer_id,created_at) VALUES(?,?,?,?,?)",
                uid, username, full_name, referrer_id, now(),
            )
            return True

    def user(self, uid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id=?", uid)

    def add_balance(self, uid: int, amount: float) -> float:
        if not math.isfinite(amount):
            raise ValueError("Non-finite amount")
        with self.lock:
            self.exec("UPDATE users SET balance=balance+? WHERE id=?", amount, uid)
            return self.scalar("SELECT balance FROM users WHERE id=?", uid) or 0.0

    def try_spend(self, uid: int, amount: float) -> bool:
        if not math.isfinite(amount) or amount < 0:
            return False
        with self.lock:
            cur = self.exec("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", amount, uid, amount)
            return cur.rowcount == 1

    def reward_referrer_once(self, uid: int, reward: float) -> int | None:
        """Начисляет пригласившему награду за uid (один раз). Возвращает id пригласившего."""
        if not math.isfinite(reward) or reward < 0:
            raise ValueError("Invalid reward")
        with self.tx():
            u = self.user(uid)
            if not u or not u["referrer_id"] or u["ref_rewarded"]:
                return None
            self.exec("UPDATE users SET ref_rewarded=1 WHERE id=?", uid)
            if reward > 0:
                self.add_balance(u["referrer_id"], reward)
            return u["referrer_id"]

    def complete_task(self, uid: int, tid: int, *, expected: tuple) -> float | None:
        """Credit once, only if the checked task is still active and unchanged."""
        with self.tx():
            task = self.one("SELECT * FROM tasks WHERE id=? AND active=1", tid)
            if not task or not self.user(uid) or (task["url"], task["channel"], task["reward"]) != expected:
                return None
            reward = task["reward"]
            if not math.isfinite(reward) or reward <= 0:
                return None
            cur = self.exec("INSERT INTO task_done(user_id, task_id) VALUES(?,?) "
                            "ON CONFLICT(user_id,task_id) DO NOTHING", uid, tid)
            if not cur.rowcount:
                return None
            self.add_balance(uid, reward)
            return reward

    def try_upgrade(self, uid: int, *, level: int, cost: int, max_level: int) -> bool:
        """A single conditional statement changes both balance and level."""
        if cost < 0:
            return False
        with self.lock:
            cur = self.exec("UPDATE users SET balance=balance-?,level=level+1 "
                            "WHERE id=? AND level=? AND level<? AND balance>=?", cost, uid, level, max_level, cost)
            return cur.rowcount == 1

    def referrals(self, uid: int) -> int:
        return self.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=?", uid) or 0

    def active_ids(self) -> list[int]:
        return [r[0] for r in self.all("SELECT id FROM users WHERE blocked=0")]

    def mark_blocked(self, uid: int) -> None:
        self.exec("UPDATE users SET blocked=1 WHERE id=?", uid)

    # ---- выводы ----
    def create_withdrawal(self, uid: int, amount: float, details: str, *, currency: str) -> int | None:
        if not math.isfinite(amount) or amount <= 0:
            return None
        with self.tx():
            if not self.try_spend(uid, amount):
                return None
            cur = self.exec(
                "INSERT INTO withdrawals(user_id,amount,details,created_at,currency_at_request) VALUES(?,?,?,?,?)",
                uid, amount, details, now(), currency,
            )
            return int(cur.lastrowid)

    def resolve_withdrawal(self, wid: int, approve: bool) -> sqlite3.Row | None:
        with self.tx():
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND status='pending'", wid)
            if not w:
                return None
            changed = self.exec(
                "UPDATE withdrawals SET status=?,resolved_at=? WHERE id=? AND status='pending'",
                "paid" if approve else "rejected", now(), wid,
            )
            if changed.rowcount != 1:
                return None
            if not approve:
                self.add_balance(w["user_id"], w["amount"])
            return w

    def withdrawal(self, wid: int, *, uid: int | None = None) -> sqlite3.Row | None:
        if uid is None:
            return self.one("SELECT * FROM withdrawals WHERE id=?", wid)
        return self.one("SELECT * FROM withdrawals WHERE id=? AND user_id=?", wid, uid)

    def withdrawal_page(self, scope: str, page: int, *, uid: int | None = None) -> tuple[list, int, int]:
        if type(page) is not int or not 0 <= page <= 1_000_000:
            raise ValueError("Invalid page")
        if scope == "mine" and uid is not None:
            where, args, direction = "user_id=?", (uid,), "DESC"
        elif scope == "pending":
            where, args, direction = "status='pending'", (), "ASC"
        elif scope == "completed":
            where, args, direction = "status IN ('paid','rejected')", (), "DESC"
        else:
            raise ValueError("Invalid scope")
        with self.lock:
            total = self.scalar(f"SELECT COUNT(*) FROM withdrawals WHERE {where}", *args)
            page = min(page, max(0, (total - 1) // 10))
            rows = self.all(f"SELECT * FROM withdrawals WHERE {where} "
                            f"ORDER BY created_at {direction}, id {direction} LIMIT 10 OFFSET ?", *args, page * 10)
            return rows, page, total

    def paid_by_unit(self, limit: int = 10) -> list[tuple[str | None, int, float]]:
        """Requests marked paid, grouped by the unit stored with each request (never summed across units)."""
        rows = self.all("SELECT currency_at_request, COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals "
                        "WHERE status='paid' GROUP BY currency_at_request ORDER BY COUNT(*) DESC LIMIT ?", limit)
        return [(r[0], int(r[1]), float(r[2])) for r in rows]

    def stats(self) -> dict[str, Any]:
        t = time.localtime()
        today = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
        return {
            "users": self.scalar("SELECT COUNT(*) FROM users") or 0,
            "today": self.scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", today) or 0,
            "blocked": self.scalar("SELECT COUNT(*) FROM users WHERE blocked=1") or 0,
            "pending": self.scalar("SELECT COUNT(*) FROM withdrawals WHERE status='pending'") or 0,
            "paid": self.scalar("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'") or 0,
        }
