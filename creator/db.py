"""SQLite-хранилище конструктора."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from creator.money import STARS_TO_KOP

# Все деньги — целые копейки: users.balance/ref_earned, transactions.amount, bots.hosting_balance/sale_price,
# payments.amount, lave_invoices.amount, tgrass_ops.reward_kop.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    referrer_id INTEGER,
    balance INTEGER NOT NULL DEFAULT 0,
    ref_earned INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL,
    comment TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_user ON transactions(user_id, id);
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    template TEXT NOT NULL,
    token TEXT NOT NULL,
    username TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'stopped',
    desired INTEGER NOT NULL DEFAULT 0,
    hosting_balance INTEGER NOT NULL DEFAULT 0,
    next_charge_at INTEGER,
    vip INTEGER NOT NULL DEFAULT 0,
    sale_price INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bots_owner ON bots(owner_id, deleted);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    template TEXT NOT NULL,
    token TEXT NOT NULL,
    bot_username TEXT,
    bot_title TEXT,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lave_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT UNIQUE,
    user_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    amount INTEGER NOT NULL,                  -- копейки
    usd TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    pay_url TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    paid_at INTEGER
);
CREATE INDEX IF NOT EXISTS lave_invoices_status ON lave_invoices(status);
CREATE TABLE IF NOT EXISTS payment_jobs (
    local_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'received' CHECK(status IN ('received','applied')),
    result_text TEXT,
    received_at INTEGER NOT NULL,
    applied_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_payment_jobs_status ON payment_jobs(status,local_id);
CREATE TABLE IF NOT EXISTS bot_runtime_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    bot_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('start','sync')),
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_runtime_jobs_pending ON bot_runtime_jobs(id) WHERE completed_at IS NULL;
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS tgrass_bots (
    bot_id INTEGER PRIMARY KEY,
    applicant_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | disabled
    mode TEXT NOT NULL DEFAULT 'factory',     -- relay | factory
    api_key TEXT,                             -- auth_token бота в TGrass (из /new_bot) или хаб при relay
    users_at_apply INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    applied_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER,
    cursor INTEGER NOT NULL DEFAULT 0         -- последний обработанный id из tgrass_done дочернего бота
);
CREATE TABLE IF NOT EXISTS tgrass_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    offer_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    reward_kop INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    offer_link TEXT,
    UNIQUE (bot_id, tg_user_id, offer_id)
);
CREATE INDEX IF NOT EXISTS ix_tgrass_ops_bot ON tgrass_ops(bot_id, created_at);
CREATE TABLE IF NOT EXISTS pf_bots (
    bot_id INTEGER PRIMARY KEY,
    applicant_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | disabled
    mode TEXT NOT NULL DEFAULT 'factory',     -- relay | factory
    api_key TEXT,                             -- API key бота в PiarFlow или хаб при relay
    pf_bot_id INTEGER,                        -- Telegram ID бота в PiarFlow (только factory)
    users_at_apply INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    applied_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER,
    cursor INTEGER NOT NULL DEFAULT 0         -- последний обработанный id из pf_done дочернего бота
);
CREATE TABLE IF NOT EXISTS pf_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    link TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    reward_kop INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (bot_id, tg_user_id, link)
);
CREATE INDEX IF NOT EXISTS ix_pf_ops_bot ON pf_ops(bot_id, created_at);
CREATE TABLE IF NOT EXISTS webhook_secrets (
    bot_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    secret TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, provider)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_secret ON webhook_secrets(secret);
CREATE TABLE IF NOT EXISTS webhook_url_notices (
    bot_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    secret TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, provider, secret)
);
CREATE TABLE IF NOT EXISTS unsubscribe_penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    bot_id INTEGER NOT NULL,
    op_id INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    offer_key TEXT NOT NULL,
    amount INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    withdrawal_request_id INTEGER,
    child_reverted INTEGER NOT NULL DEFAULT 0,
    owner_notified INTEGER NOT NULL DEFAULT 0,
    user_notified INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (provider, bot_id, op_id)
);
CREATE TABLE IF NOT EXISTS withdrawal_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,                  -- requested, kopecks (debited in full)
    fee INTEGER NOT NULL,
    net INTEGER NOT NULL,                     -- to be paid out
    method TEXT NOT NULL,                     -- card | sbp | usdt_trc20
    details TEXT NOT NULL,                    -- full while pending, masked after resolution
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | rejected
    reason TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    resolved_by INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_withdrawal_pending ON withdrawal_requests(user_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS ix_withdrawal_user ON withdrawal_requests(user_id, id);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT,
    created_at INTEGER NOT NULL
);
"""


class InsufficientFunds(Exception):
    pass


def now() -> int:
    return int(time.time())


def day_start(ts: int | None = None) -> int:
    t = time.localtime(ts or now())
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        existed = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._savepoint_counter = 0
        self._ensure_mode_columns()
        self._ensure_unsubscribe_schema()
        if self.kv_get("created_at") is None:
            self.kv_set("created_at", str(now()))
        if self.kv_get("currency") != "rub":
            if existed:
                self.migrate_stars_to_rub()
            else:
                self.kv_set("currency", "rub")

    # ---------- миграция ⭐ -> ₽ (копейки) ----------
    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_mode_columns(self) -> None:
        """Добавляет mode (relay|factory) в заявки доп. заработка на старых базах."""
        with self._lock:
            for table in ("tgrass_bots", "pf_bots"):
                if table not in {r[0] for r in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
                    continue
                if "mode" not in self._columns(table):
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN mode TEXT NOT NULL DEFAULT 'factory'")

    def _ensure_unsubscribe_schema(self) -> None:
        """Additive columns/tables for unsubscribe penalties (existing rows stay)."""
        with self._lock:
            if "tgrass_ops" in {r[0] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")} and "offer_link" not in self._columns("tgrass_ops"):
                self.conn.execute("ALTER TABLE tgrass_ops ADD COLUMN offer_link TEXT")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS webhook_url_notices ("
                "bot_id INTEGER NOT NULL, provider TEXT NOT NULL, secret TEXT NOT NULL, "
                "owner_id INTEGER NOT NULL, sent_at INTEGER NOT NULL, "
                "PRIMARY KEY (bot_id, provider, secret))")

    def migrate_stars_to_rub(self) -> None:
        """Однократный перевод старой базы (суммы в Telegram Stars) в копейки по курсу 1 ⭐ = 1,5 ₽."""
        k = STARS_TO_KOP
        with self.tx() as c:
            c.execute("UPDATE users SET balance=balance*?, ref_earned=ref_earned*?", (k, k))
            c.execute("UPDATE transactions SET amount=amount*?", (k,))
            c.execute("UPDATE bots SET hosting_balance=hosting_balance*?, sale_price=sale_price*?", (k, k))
            c.execute("UPDATE payments SET amount=amount*?", (k,))
            cols = self._columns("lave_invoices")
            if "stars" in cols:
                c.execute("ALTER TABLE lave_invoices RENAME COLUMN stars TO amount")
                c.execute("UPDATE lave_invoices SET amount=rub*100")
                for r in c.execute("SELECT id, payload, rub FROM lave_invoices WHERE status<>'paid'").fetchall():
                    kind, *rest = r["payload"].split(":")
                    new = None
                    if kind == "topup":
                        new = f"topup:{r['rub'] * 100}"
                    elif kind == "host" and rest:
                        new = f"host:{rest[0]}:{r['rub'] * 100}"
                    if new:
                        c.execute("UPDATE lave_invoices SET payload=? WHERE id=?", (new, r["id"]))
            if "rub" in self._columns("lave_invoices"):
                c.execute("ALTER TABLE lave_invoices DROP COLUMN rub")
            if "reward_centi" in self._columns("tgrass_ops"):
                c.execute("ALTER TABLE tgrass_ops RENAME COLUMN reward_centi TO reward_kop")
                c.execute("UPDATE tgrass_ops SET reward_kop=(reward_kop*3+1)/2")
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tgrass_frac'").fetchone():
                for r in c.execute("SELECT user_id, centi FROM tgrass_frac WHERE centi>0").fetchall():
                    kop = (r["centi"] * 3 + 1) // 2
                    if kop > 0 and c.execute("SELECT 1 FROM users WHERE id=?", (r["user_id"],)).fetchone():
                        self._change_balance(c, r["user_id"], kop, "tgrass", "TGrass: остаток долей при переходе на ₽")
                c.execute("DROP TABLE tgrass_frac")
            for key in ("PRICE_CLICKER", "PRICE_REFBOT", "PRICE_STARBOT", "HOSTING_DAY_PRICE", "VIP_PRICE"):
                c.execute("UPDATE kv SET value=CAST(CAST(value AS INTEGER)*? AS TEXT) WHERE key=?", (k, f"price:{key}"))
            old = c.execute("SELECT value FROM kv WHERE key='price:TGRASS_REWARD_CENTI'").fetchone()
            if old and old[0].lstrip("-").isdigit():
                c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('price:TGRASS_REWARD',?)",
                          (str((int(old[0]) * 3 + 1) // 2),))
            c.execute("DELETE FROM kv WHERE key IN ('price:TGRASS_REWARD_CENTI','price:RUB_PER_100_STARS') "
                      "OR key LIKE 'price:RUB\\_%' ESCAPE '\\'")
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('currency','rub')")
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('migrated_rub_at',?)", (str(now()),))

    # ---------- низкоуровневое ----------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self.conn.in_transaction
            self._savepoint_counter += 1
            point = f"operation_{self._savepoint_counter}"
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

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def scalar(self, sql: str, *args: Any) -> Any:
        row = self.one(sql, *args)
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()

    # ---------- kv / цены ----------
    def kv_get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", key)
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---------- пользователи ----------
    def upsert_user(self, uid: int, username: str | None, full_name: str, referrer_id: int | None = None) -> bool:
        """Создаёт пользователя или обновляет имя. Возвращает True, если пользователь новый."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
            if row:
                c.execute("UPDATE users SET username=?, full_name=? WHERE id=?", (username, full_name, uid))
                return False
            if referrer_id == uid:
                referrer_id = None
            if referrer_id is not None and not c.execute("SELECT 1 FROM users WHERE id=?", (referrer_id,)).fetchone():
                referrer_id = None
            c.execute(
                "INSERT INTO users(id,username,full_name,referrer_id,created_at) VALUES(?,?,?,?,?)",
                (uid, username, full_name, referrer_id, now()),
            )
            return True

    def user(self, uid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id=?", uid)

    def set_banned(self, uid: int, banned: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), uid))

    def change_balance(self, uid: int, amount: int, kind: str, comment: str = "", *, allow_negative: bool = False) -> int:
        with self.tx() as c:
            return self._change_balance(c, uid, amount, kind, comment, allow_negative=allow_negative)

    @staticmethod
    def _change_balance(c: sqlite3.Connection, uid: int, amount: int, kind: str, comment: str = "",
                        *, allow_negative: bool = False) -> int:
        row = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(uid)
        new = row[0] + amount
        if new < 0 and not allow_negative:
            raise InsufficientFunds
        c.execute("UPDATE users SET balance=? WHERE id=?", (new, uid))
        c.execute(
            "INSERT INTO transactions(user_id,amount,kind,comment,created_at) VALUES(?,?,?,?,?)",
            (uid, amount, kind, comment, now()),
        )
        return new

    def history(self, uid: int, limit: int = 15) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?", uid, limit)

    def referrals_count(self, uid: int) -> int:
        return self.scalar("SELECT COUNT(*) FROM users WHERE referrer_id=?", uid) or 0

    def credit_referrer(self, payer_id: int, amount: int, percent: int) -> tuple[int, int] | None:
        """Начисляет партнёру процент от оплаты реферала. Возвращает (referrer_id, bonus)."""
        u = self.user(payer_id)
        if not u or not u["referrer_id"] or percent <= 0:
            return None
        bonus = amount * percent // 100
        if bonus <= 0:
            return None
        ref_id = u["referrer_id"]
        with self.tx() as c:
            self._change_balance(c, ref_id, bonus, "ref", f"{percent}% от оплаты реферала {payer_id}")
            c.execute("UPDATE users SET ref_earned=ref_earned+? WHERE id=?", (bonus, ref_id))
        return ref_id, bonus

    def all_user_ids(self) -> list[int]:
        return [r[0] for r in self.all("SELECT id FROM users WHERE banned=0")]

    # ---------- платежи ----------
    def register_payment(self, charge_id: str, uid: int, amount: int, payload: str) -> bool:
        """Идемпотентность: False, если платёж уже обработан."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO payments(charge_id,user_id,amount,payload,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(charge_id) DO NOTHING", (charge_id, uid, amount, payload, now()),
            )
            return cur.rowcount == 1

    # ---------- LavePay ----------
    def lave_new(self, uid: int, payload: str, amount: int, usd: str) -> int:
        """amount — сумма счёта в копейках."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO lave_invoices(user_id,payload,amount,usd,created_at) VALUES(?,?,?,?,?)",
                (uid, payload, amount, usd, now()),
            )
            return int(cur.lastrowid)

    def lave_set_created(self, local_id: int, invoice_id: str, pay_url: str, expires_at: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE lave_invoices SET invoice_id=?, pay_url=?, expires_at=?, status='active' WHERE id=?",
                (invoice_id, pay_url, expires_at, local_id),
            )

    def lave_get(self, local_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM lave_invoices WHERE id=?", local_id)

    def lave_by_invoice(self, invoice_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM lave_invoices WHERE invoice_id=?", invoice_id)

    def lave_active(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM lave_invoices WHERE status='active' ORDER BY id")

    def lave_set_status(self, local_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE lave_invoices SET status=? WHERE id=? AND status NOT IN ('paid','received')",
                              (status, local_id))

    def lave_receive(self, local_id: int) -> bool:
        """Persist a validated paid event before acknowledging it; never replay untracked legacy payments."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM lave_invoices WHERE id=?", (local_id,)).fetchone()
            job = c.execute("SELECT 1 FROM payment_jobs WHERE local_id=?", (local_id,)).fetchone()
            if not row or (row["status"] == "paid" and not job):
                return False
            if not job:
                c.execute("INSERT INTO payment_jobs(local_id,received_at) VALUES(?,?)", (local_id, now()))
                c.execute("UPDATE lave_invoices SET status='received' WHERE id=?", (local_id,))
            return True

    def lave_received(self) -> list[sqlite3.Row]:
        return self.all("SELECT local_id FROM payment_jobs WHERE status='received' ORDER BY local_id LIMIT 100")

    def enqueue_runtime(self, source_key: str, bot_id: int, action: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO bot_runtime_jobs(source_key,bot_id,action,created_at) VALUES(?,?,?,?) "
                              "ON CONFLICT(source_key) DO NOTHING",
                              (source_key, bot_id, action, now()))

    def runtime_pending(self, bot_id: int | None = None) -> list[sqlite3.Row]:
        if bot_id is None:
            return self.all("SELECT * FROM bot_runtime_jobs WHERE completed_at IS NULL ORDER BY id LIMIT 100")
        return self.all("SELECT * FROM bot_runtime_jobs WHERE completed_at IS NULL AND bot_id=? ORDER BY id LIMIT 100", bot_id)

    def runtime_complete(self, job_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE bot_runtime_jobs SET completed_at=? WHERE id=? AND completed_at IS NULL", (now(), job_id))

    def lave_mark_paid(self, local_id: int) -> bool:
        """Атомарно переводит счёт в paid. False — уже был оплачен (повтор)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE lave_invoices SET status='paid', paid_at=? WHERE id=? AND status<>'paid'", (now(), local_id))
            return cur.rowcount == 1

    # ---------- черновики ----------
    def create_draft(self, uid: int, template: str, token: str, username: str, title: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO drafts(user_id,template,token,bot_username,bot_title,created_at) VALUES(?,?,?,?,?,?)",
                (uid, template, token, username, title, now()),
            )
            return int(cur.lastrowid)

    def draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM drafts WHERE id=?", draft_id)

    # ---------- боты ----------
    def token_in_use(self, token: str) -> bool:
        return self.one("SELECT 1 FROM bots WHERE token=? AND deleted=0", token) is not None

    def materialize_draft(self, draft_id: int, start_hosting: int) -> int | None:
        """Превращает оплаченный черновик в бота. None, если черновик уже использован."""
        with self.tx() as c:
            d = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not d or d["used"]:
                return None
            c.execute("UPDATE drafts SET used=1 WHERE id=?", (draft_id,))
            cur = c.execute(
                "INSERT INTO bots(owner_id,template,token,username,title,status,desired,hosting_balance,"
                "next_charge_at,created_at) VALUES(?,?,?,?,?,'stopped',1,?,?,?)",
                (d["user_id"], d["template"], d["token"], d["bot_username"], d["bot_title"],
                 start_hosting, now() + 86400, now()),
            )
            return int(cur.lastrowid)

    def bot(self, bot_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM bots WHERE id=? AND deleted=0", bot_id)

    def user_bots(self, uid: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM bots WHERE owner_id=? AND deleted=0 ORDER BY id", uid)

    def bots_to_run(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM bots WHERE desired=1 AND deleted=0")

    def update_bot(self, bot_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE bots SET {cols} WHERE id=?", (*fields.values(), bot_id))

    def add_hosting(self, bot_id: int, amount: int) -> int:
        with self.tx() as c:
            c.execute("UPDATE bots SET hosting_balance=hosting_balance+? WHERE id=?", (amount, bot_id))
            return c.execute("SELECT hosting_balance FROM bots WHERE id=?", (bot_id,)).fetchone()[0]

    def hosting_from_balance(self, uid: int, bot_id: int, amount: int) -> None:
        with self.tx() as c:
            self._change_balance(c, uid, -amount, "hosting", f"Хостинг бота ID {bot_id}")
            c.execute("UPDATE bots SET hosting_balance=hosting_balance+? WHERE id=?", (amount, bot_id))

    def charge_due_bots(self, day_price: int, ts: int | None = None) -> list[tuple[sqlite3.Row, bool]]:
        """Списывает суточную плату с запущенных ботов.
        Возвращает список (бот, ok) — ok=False значит денег не хватило и бота надо остановить."""
        ts = ts or now()
        result: list[tuple[sqlite3.Row, bool]] = []
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM bots WHERE deleted=0 AND desired=1 AND next_charge_at IS NOT NULL AND next_charge_at<=?",
                (ts,),
            ).fetchall()
            for b in rows:
                if b["hosting_balance"] >= day_price:
                    c.execute(
                        "UPDATE bots SET hosting_balance=hosting_balance-?, next_charge_at=? WHERE id=?",
                        (day_price, max(b["next_charge_at"], ts - 86400) + 86400, b["id"]),
                    )
                    result.append((b, True))
                else:
                    c.execute(
                        "UPDATE bots SET desired=0, status='stopped', last_error='Закончился баланс хостинга' WHERE id=?",
                        (b["id"],),
                    )
                    result.append((b, False))
        return result

    @contextmanager
    def owned_bot(self, bot_id: int, owner_id: int):
        """Synchronous owner-bound mutation, serialized with transfer/delete."""
        with self._lock:
            row = self.bot(bot_id)
            if not row or row["owner_id"] != owner_id:
                raise ValueError("Bot not found")
            yield row

    def transfer_bot(self, bot_id: int, new_owner: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE bots SET owner_id=?, sale_price=NULL WHERE id=?", (new_owner, bot_id))

    def delete_bot(self, bot_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE bots SET deleted=1, desired=0, status='stopped', sale_price=NULL, token='' WHERE id=?",
                (bot_id,),
            )

    # ---------- перепродажа ----------
    def bots_for_sale(self, exclude_owner: int | None = None) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM bots WHERE deleted=0 AND sale_price IS NOT NULL AND owner_id<>? ORDER BY id DESC LIMIT 50",
            exclude_owner or 0,
        )

    def buy_bot(self, buyer_id: int, bot_id: int, fee_percent: int) -> tuple[int, int, int]:
        """Покупка бота за баланс. Возвращает (seller_id, price, seller_income)."""
        with self.tx() as c:
            b = c.execute("SELECT * FROM bots WHERE id=? AND deleted=0", (bot_id,)).fetchone()
            if not b or b["sale_price"] is None:
                raise LookupError("not for sale")
            if b["owner_id"] == buyer_id:
                raise LookupError("own bot")
            price = b["sale_price"]
            income = price - price * fee_percent // 100
            self._change_balance(c, buyer_id, -price, "resale_buy", f"Покупка бота ID {bot_id}")
            self._change_balance(c, b["owner_id"], income, "resale_sell", f"Продажа бота ID {bot_id}")
            c.execute("UPDATE bots SET owner_id=?, sale_price=NULL WHERE id=?", (buyer_id, bot_id))
            return b["owner_id"], price, income

    # ---------- статистика ----------
    def stats(self) -> dict[str, int]:
        today = day_start()
        return {
            "users_total": self.scalar("SELECT COUNT(*) FROM users") or 0,
            "users_today": self.scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", today) or 0,
            "bots_total": self.scalar("SELECT COUNT(*) FROM bots WHERE deleted=0") or 0,
            "bots_running": self.scalar("SELECT COUNT(*) FROM bots WHERE deleted=0 AND status='running'") or 0,
            "payments_total": self.scalar("SELECT COALESCE(SUM(amount),0) FROM payments") or 0,
            "payments_today": self.scalar("SELECT COALESCE(SUM(amount),0) FROM payments WHERE created_at>=?", today) or 0,
            "balances": self.scalar("SELECT COALESCE(SUM(balance),0) FROM users") or 0,
        }

    # ---------- TGrass (Доп заработок) ----------
    def tgrass(self, bot_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM tgrass_bots WHERE bot_id=?", bot_id)

    def tgrass_apply(self, bot_id: int, applicant_id: int, users: int, mode: str = "factory") -> bool:
        """Новая заявка (или повторная после отказа/отключения). False — уже ждёт решения или подключён.
        mode: relay — трансляция спонсоров хаба креатора; factory — свой бот в сети."""
        if mode not in ("relay", "factory"):
            raise ValueError(f"unknown mode: {mode}")
        with self.tx() as c:
            row = c.execute("SELECT status FROM tgrass_bots WHERE bot_id=?", (bot_id,)).fetchone()
            if row and row["status"] in ("pending", "approved"):
                return False
            if row:
                c.execute("UPDATE tgrass_bots SET status='pending', mode=?, applicant_id=?, users_at_apply=?, "
                          "api_key=NULL, error=NULL, applied_at=?, decided_at=NULL, decided_by=NULL WHERE bot_id=?",
                          (mode, applicant_id, users, now(), bot_id))
            else:
                c.execute("INSERT INTO tgrass_bots(bot_id,applicant_id,users_at_apply,applied_at,mode) "
                          "VALUES(?,?,?,?,?)", (bot_id, applicant_id, users, now(), mode))
            return True

    def tgrass_set(self, bot_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE tgrass_bots SET {cols} WHERE bot_id=?", (*fields.values(), bot_id))

    def tgrass_list(self, status: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT t.*, b.username, b.owner_id FROM tgrass_bots t JOIN bots b ON b.id=t.bot_id "
            "WHERE t.status=? AND b.deleted=0 ORDER BY t.applied_at", status)

    def tgrass_key_for(self, bot_id: int) -> str:
        row = self.one("SELECT api_key FROM tgrass_bots WHERE bot_id=? AND status='approved'", bot_id)
        return (row["api_key"] or "") if row else ""

    def tgrass_credit(self, bot_id: int, tg_user_id: int, offer_id: int, reward_kop: int,
                      ts: int | None = None, offer_link: str | None = None) -> bool:
        """Идемпотентно засчитывает одну ОП владельцу бота (reward_kop копеек сразу на баланс).
        False — уже засчитана (или бота нет)."""
        with self.tx() as c:
            b = c.execute("SELECT owner_id FROM bots WHERE id=?", (bot_id,)).fetchone()
            if not b:
                return False
            owner = b["owner_id"]
            reward_kop = max(0, reward_kop)
            cur = c.execute(
                "INSERT OR IGNORE INTO tgrass_ops(bot_id,tg_user_id,offer_id,owner_id,reward_kop,created_at,offer_link) "
                "VALUES(?,?,?,?,?,?,?)", (bot_id, tg_user_id, offer_id, owner, reward_kop, ts or now(), offer_link or None))
            if cur.rowcount != 1:
                return False
            if reward_kop > 0:
                self._change_balance(c, owner, reward_kop, "tgrass", f"TGrass: ОП в боте ID {bot_id}")
            return True

    def tgrass_stats(self, bot_id: int, since: int = 0) -> tuple[int, int]:
        """(число ОП, заработано копеек) по боту начиная с since."""
        row = self.one("SELECT COUNT(*), COALESCE(SUM(reward_kop),0) FROM tgrass_ops WHERE bot_id=? AND created_at>=?",
                       bot_id, since)
        return int(row[0]), int(row[1])

    # ---------- PiarFlow (Доп заработок) ----------
    def pf(self, bot_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM pf_bots WHERE bot_id=?", bot_id)

    def pf_apply(self, bot_id: int, applicant_id: int, users: int, mode: str = "factory") -> bool:
        """Новая заявка (или повторная после отказа/отключения). False — уже ждёт решения или подключён.
        mode: relay — трансляция спонсоров хаба креатора; factory — свой бот в сети."""
        if mode not in ("relay", "factory"):
            raise ValueError(f"unknown mode: {mode}")
        with self.tx() as c:
            row = c.execute("SELECT status FROM pf_bots WHERE bot_id=?", (bot_id,)).fetchone()
            if row and row["status"] in ("pending", "approved"):
                return False
            if row:
                c.execute("UPDATE pf_bots SET status='pending', mode=?, applicant_id=?, users_at_apply=?, "
                          "api_key=NULL, pf_bot_id=NULL, error=NULL, applied_at=?, decided_at=NULL, decided_by=NULL "
                          "WHERE bot_id=?",
                          (mode, applicant_id, users, now(), bot_id))
            else:
                c.execute("INSERT INTO pf_bots(bot_id,applicant_id,users_at_apply,applied_at,mode) "
                          "VALUES(?,?,?,?,?)", (bot_id, applicant_id, users, now(), mode))
            return True

    def pf_set(self, bot_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE pf_bots SET {cols} WHERE bot_id=?", (*fields.values(), bot_id))

    def pf_list(self, status: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT t.*, b.username, b.owner_id FROM pf_bots t JOIN bots b ON b.id=t.bot_id "
            "WHERE t.status=? AND b.deleted=0 ORDER BY t.applied_at", status)

    def pf_key_for(self, bot_id: int) -> str:
        row = self.one("SELECT api_key FROM pf_bots WHERE bot_id=? AND status='approved'", bot_id)
        return (row["api_key"] or "") if row else ""

    def pf_credit(self, bot_id: int, tg_user_id: int, link: str, reward_kop: int, ts: int | None = None) -> bool:
        """Идемпотентно засчитывает одно задание PiarFlow владельцу бота (reward_kop копеек на баланс)."""
        with self.tx() as c:
            b = c.execute("SELECT owner_id FROM bots WHERE id=?", (bot_id,)).fetchone()
            if not b:
                return False
            owner = b["owner_id"]
            reward_kop = max(0, reward_kop)
            cur = c.execute(
                "INSERT OR IGNORE INTO pf_ops(bot_id,tg_user_id,link,owner_id,reward_kop,created_at) "
                "VALUES(?,?,?,?,?,?)", (bot_id, tg_user_id, link, owner, reward_kop, ts or now()))
            if cur.rowcount != 1:
                return False
            if reward_kop > 0:
                self._change_balance(c, owner, reward_kop, "piarflow", f"PiarFlow: задание в боте ID {bot_id}")
            return True

    def pf_stats(self, bot_id: int, since: int = 0) -> tuple[int, int]:
        """(число заданий, заработано копеек) по боту начиная с since."""
        row = self.one("SELECT COUNT(*), COALESCE(SUM(reward_kop),0) FROM pf_ops WHERE bot_id=? AND created_at>=?",
                       bot_id, since)
        return int(row[0]), int(row[1])

    def add_report(self, uid: int, text: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO reports(user_id,text,created_at) VALUES(?,?,?)", (uid, text, now())
            )
            return int(cur.lastrowid)

    def cleanup_drafts(self, max_age: int = 2 * 86400) -> None:
        """Стираем токены из старых/использованных черновиков."""
        with self._lock:
            self.conn.execute("UPDATE drafts SET token='' WHERE used=1 AND token<>''")
            self.conn.execute("DELETE FROM drafts WHERE used=0 AND created_at<?", (now() - max_age,))
