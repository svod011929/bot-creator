"""SQLite дочернего бота (у каждого бота свой файл bot.db)."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from child.payouts import idempotency_key as payout_idempotency_key

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
    @classmethod
    def open_existing(cls, path: Path | str, *, writable: bool = False) -> ChildDB:
        """Open an existing file without creating tables, migrating data or changing journal mode."""
        db = cls.__new__(cls)
        uri = Path(path).absolute().as_uri() + ("?mode=rw" if writable else "?mode=ro")
        db.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        db.conn.row_factory = sqlite3.Row
        db.lock = threading.RLock()
        db._savepoint_counter = 0
        return db

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
            self._ensure_banned_column()
            self._ensure_owner_actions()
        except BaseException:
            self.conn.close()
            raise
        self.lock = threading.RLock()
        self._savepoint_counter = 0

    def _migrate_withdrawals(self) -> None:
        # Recheck schema after acquiring SQLite's writer lock (also protects concurrent openers).
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(withdrawals)")}
            for name, kind in (("currency_at_request", "TEXT"), ("resolved_at", "INTEGER"), ("method", "TEXT")):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE withdrawals ADD COLUMN {name} {kind}")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payout_jobs (
                    withdrawal_id INTEGER PRIMARY KEY,
                    method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    destination TEXT NOT NULL,
                    amount_rub INTEGER,
                    amount_asset TEXT,
                    provider_id TEXT,
                    debit TEXT,
                    error TEXT,
                    result_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    submitted_at INTEGER,
                    completed_at INTEGER
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_payout_jobs_status ON payout_jobs(status, next_attempt_at)"
            )
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
            nested = self.conn.in_transaction
            self._savepoint_counter += 1
            point = f"child_operation_{self._savepoint_counter}"
            self.conn.execute(f"SAVEPOINT {point}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute(f"RELEASE SAVEPOINT {point}" if nested else "COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    if nested:
                        self.conn.execute(f"ROLLBACK TO SAVEPOINT {point}")
                        self.conn.execute(f"RELEASE SAVEPOINT {point}")
                    else:
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
                uid,
                username,
                full_name,
                referrer_id,
                now(),
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
            cur = self.exec(
                "INSERT INTO task_done(user_id, task_id) VALUES(?,?) ON CONFLICT(user_id,task_id) DO NOTHING", uid, tid
            )
            if not cur.rowcount:
                return None
            self.add_balance(uid, reward)
            return reward

    def try_upgrade(self, uid: int, *, level: int, cost: int, max_level: int) -> bool:
        """A single conditional statement changes both balance and level."""
        if cost < 0:
            return False
        with self.lock:
            cur = self.exec(
                "UPDATE users SET balance=balance-?,level=level+1 WHERE id=? AND level=? AND level<? AND balance>=?",
                cost,
                uid,
                level,
                max_level,
                cost,
            )
            return cur.rowcount == 1

    def referrals(self, uid: int) -> int:
        return self.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=?", uid) or 0

    def active_ids(self) -> list[int]:
        return [r[0] for r in self.all("SELECT id FROM users WHERE blocked=0")]

    def mark_blocked(self, uid: int) -> None:
        self.exec("UPDATE users SET blocked=1 WHERE id=?", uid)

    # ---- выводы ----
    def _ensure_banned_column(self) -> None:
        """Additive: users.banned for owner moderation (distinct from blocked)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "banned" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")

    def _ensure_owner_actions(self) -> None:
        """Additive journal of owner admin actions. Never stores secrets."""
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS owner_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_owner_actions_time ON owner_actions(created_at DESC, id DESC)"
        )

    def log_owner(self, owner_id: int, action: str, target: str = "", detail: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO owner_actions(owner_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                (owner_id, action[:64], (target or "")[:128], (detail or "")[:500], now()),
            )

    def owner_actions(self, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM owner_actions ORDER BY id DESC LIMIT ? OFFSET ?",
            limit, offset,
        )

    def owner_actions_count(self) -> int:
        return self.scalar("SELECT COUNT(*) FROM owner_actions") or 0


    def set_banned(self, uid: int, banned: bool) -> None:
        with self.lock:
            self.conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), uid))

    def is_banned(self, uid: int) -> bool:
        row = self.one("SELECT banned FROM users WHERE id=?", uid)
        return bool(row and row["banned"])

    def user_list(self, limit: int = 10, offset: int = 0, search: str = "") -> list[sqlite3.Row]:
        if search:
            like = f"%{search}%"
            return self.all(
                "SELECT id, username, full_name, balance, banned, blocked, created_at FROM users "
                "WHERE id=? OR username LIKE ? OR full_name LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
                int(search) if search.isdigit() else -1, like, like, limit, offset,
            )
        return self.all(
            "SELECT id, username, full_name, balance, banned, blocked, created_at FROM users "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            limit, offset,
        )

    def user_count(self, search: str = "") -> int:
        if search:
            like = f"%{search}%"
            return self.scalar(
                "SELECT COUNT(*) FROM users WHERE id=? OR username LIKE ? OR full_name LIKE ?",
                int(search) if search.isdigit() else -1, like, like,
            ) or 0
        return self.scalar("SELECT COUNT(*) FROM users") or 0


    def create_withdrawal(self, uid: int, amount: float, details: str, *, currency: str,
                          method: str | None = None, payout: dict | None = None) -> int | None:
        if not math.isfinite(amount) or amount <= 0:
            return None
        with self.tx():
            if not self.try_spend(uid, amount):
                return None
            cur = self.exec(
                "INSERT INTO withdrawals(user_id,amount,details,created_at,currency_at_request,method) "
                "VALUES(?,?,?,?,?,?)",
                uid,
                amount,
                details,
                now(),
                currency,
                method,
            )
            wid = int(cur.lastrowid)
            if payout:
                payload = dict(payout)
                bot_id = int(payload.pop("bot_id", 0) or 0)
                if not payload.get("idempotency_key"):
                    if bot_id <= 0:
                        raise ValueError("payout job requires bot_id")
                    payload["idempotency_key"] = payout_idempotency_key(bot_id, wid)
                self._insert_payout_job(wid, payload)
            return wid

    def _insert_payout_job(self, wid: int, payout: dict) -> None:
        ts = now()
        self.exec(
            "INSERT INTO payout_jobs(withdrawal_id,method,status,idempotency_key,destination,"
            "amount_rub,amount_asset,error,result_json,attempts,next_attempt_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            wid,
            payout["method"],
            payout["status"],
            payout["idempotency_key"],
            payout["destination"],
            payout.get("amount_rub"),
            payout.get("amount_asset"),
            payout.get("error"),
            payout.get("result_json"),
            int(payout.get("attempts") or 0),
            int(payout.get("next_attempt_at") or 0),
            ts,
            ts,
        )

    def payout_job(self, wid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=?", wid)

    def payout_job_by_key(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM payout_jobs WHERE idempotency_key=?", key)

    def payout_jobs_by_status(self, *statuses: str, now_ts: int | None = None) -> list[sqlite3.Row]:
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        if now_ts is None:
            return self.all(f"SELECT * FROM payout_jobs WHERE status IN ({placeholders}) ORDER BY withdrawal_id",
                            *statuses)
        return self.all(
            f"SELECT * FROM payout_jobs WHERE status IN ({placeholders}) AND next_attempt_at<=? "
            "ORDER BY withdrawal_id",
            *statuses,
            now_ts,
        )

    def queue_payout_job(self, wid: int) -> sqlite3.Row | None:
        """Move an awaiting-approval job to queued. Does not change the reserved withdrawal."""
        with self.tx():
            job = self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=?", wid)
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND status='pending'", wid)
            if not job or not w or job["status"] not in ("awaiting_approval", "queued"):
                return None
            if job["status"] == "queued":
                return job
            ts = now()
            cur = self.exec(
                "UPDATE payout_jobs SET status='queued',error=NULL,updated_at=?,next_attempt_at=0 "
                "WHERE withdrawal_id=? AND status='awaiting_approval'",
                ts, wid,
            )
            if cur.rowcount != 1:
                return None
            return self.payout_job(wid)

    def cancel_payout_and_reject(self, wid: int) -> sqlite3.Row | None:
        """Reject a pending withdrawal and cancel a job that has not been submitted yet."""
        with self.tx():
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND status='pending'", wid)
            if not w:
                return None
            job = self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=?", wid)
            if job and job["status"] == "submitted":
                return None
            changed = self.exec(
                "UPDATE withdrawals SET status='rejected',resolved_at=? WHERE id=? AND status='pending'",
                now(), wid,
            )
            if changed.rowcount != 1:
                return None
            if job and job["status"] in ("awaiting_approval", "queued"):
                self.exec(
                    "UPDATE payout_jobs SET status='cancelled',updated_at=? "
                    "WHERE withdrawal_id=? AND status IN ('awaiting_approval','queued')",
                    now(), wid,
                )
            self.add_balance(w["user_id"], w["amount"])
            return w

    def mark_payout_submitted(self, wid: int, provider_id: str, debit: str | None = None,
                              result: dict | None = None) -> sqlite3.Row | None:
        with self.tx():
            job = self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=? AND status='queued'", wid)
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND status='pending'", wid)
            if not job or not w:
                return None
            ts = now()
            import json
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result else None
            cur = self.exec(
                "UPDATE payout_jobs SET status='submitted',provider_id=?,debit=?,result_json=?,"
                "error=NULL,submitted_at=?,updated_at=?,attempts=attempts+1 "
                "WHERE withdrawal_id=? AND status='queued'",
                provider_id, debit, payload, ts, ts, wid,
            )
            return self.payout_job(wid) if cur.rowcount == 1 else None

    def defer_payout_job(self, wid: int, *, error: str, delay: int, expected: str = "queued") -> bool:
        with self.tx():
            ts = now()
            cur = self.exec(
                "UPDATE payout_jobs SET error=?,next_attempt_at=?,updated_at=?,attempts=attempts+1 "
                "WHERE withdrawal_id=? AND status=?",
                error[:500], ts + max(0, delay), ts, wid, expected,
            )
            return cur.rowcount == 1

    def apply_payout_outcome(self, wid: int, *, ok: bool, provider_id: str | None = None,
                             debit: str | None = None, error: str | None = None,
                             result: dict | None = None) -> str | None:
        """Complete or fail a reserved payout exactly once. Returns 'completed', 'failed', or None."""
        import json
        with self.tx():
            w = self.one("SELECT * FROM withdrawals WHERE id=?", wid)
            job = self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=?", wid)
            if not w or not job:
                return None
            if job["status"] == "completed":
                return "completed" if ok else None
            if job["status"] == "failed":
                return "failed" if not ok else None
            if job["status"] not in ("queued", "submitted"):
                return None
            if w["status"] != "pending":
                return None
            ts = now()
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result else job["result_json"]
            if ok:
                cur = self.exec(
                    "UPDATE withdrawals SET status='paid',resolved_at=? WHERE id=? AND status='pending'",
                    ts, wid,
                )
                if cur.rowcount != 1:
                    return None
                self.exec(
                    "UPDATE payout_jobs SET status='completed',provider_id=COALESCE(?,provider_id),"
                    "debit=COALESCE(?,debit),result_json=?,error=NULL,completed_at=?,updated_at=? "
                    "WHERE withdrawal_id=? AND status IN ('queued','submitted')",
                    provider_id, debit, payload, ts, ts, wid,
                )
                return "completed"
            cur = self.exec(
                "UPDATE withdrawals SET status='failed',resolved_at=? WHERE id=? AND status='pending'",
                ts, wid,
            )
            if cur.rowcount != 1:
                return None
            self.exec(
                "UPDATE payout_jobs SET status='failed',provider_id=COALESCE(?,provider_id),"
                "debit=COALESCE(?,debit),result_json=?,error=?,completed_at=?,updated_at=? "
                "WHERE withdrawal_id=? AND status IN ('queued','submitted')",
                provider_id, debit, payload, (error or "failed")[:500], ts, ts, wid,
            )
            self.add_balance(w["user_id"], w["amount"])
            return "failed"

    def paid_or_inflight_rub_since(self, since: int) -> int:
        """Integer rubles already reserved, sent or paid today (for a daily cap)."""
        row = self.one(
            "SELECT COALESCE(SUM(j.amount_rub),0) FROM payout_jobs j "
            "WHERE j.status IN ('queued','submitted','completed') "
            "AND j.amount_rub IS NOT NULL AND j.created_at>=?",
            since,
        )
        return int(row[0] or 0) if row else 0

    def resolve_withdrawal(self, wid: int, approve: bool) -> sqlite3.Row | None:
        with self.tx():
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND status='pending'", wid)
            if not w:
                return None
            job = self.one("SELECT * FROM payout_jobs WHERE withdrawal_id=?", wid)
            if job and job["status"] in ("awaiting_approval", "queued", "submitted"):
                return None
            changed = self.exec(
                "UPDATE withdrawals SET status=?,resolved_at=? WHERE id=? AND status='pending'",
                "paid" if approve else "rejected",
                now(),
                wid,
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
            where, args, direction = "status IN ('paid','rejected','failed')", (), "DESC"
        else:
            raise ValueError("Invalid scope")
        with self.lock:
            total = self.scalar(f"SELECT COUNT(*) FROM withdrawals WHERE {where}", *args)
            page = min(page, max(0, (total - 1) // 10))
            rows = self.all(
                f"SELECT * FROM withdrawals WHERE {where} ORDER BY created_at {direction}, id {direction} LIMIT 10 OFFSET ?",
                *args,
                page * 10,
            )
            return rows, page, total

    def paid_by_unit(self, limit: int = 10) -> list[tuple[str | None, int, float]]:
        """Requests marked paid, grouped by the unit stored with each request (never summed across units)."""
        rows = self.all(
            "SELECT currency_at_request, COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals "
            "WHERE status='paid' GROUP BY currency_at_request ORDER BY COUNT(*) DESC LIMIT ?",
            limit,
        )
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
