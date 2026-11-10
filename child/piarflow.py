"""Задания PiarFlow в дочернем боте: спонсоры из POST /sponsors и проверка POST /sponsors/check.

Бот подключён к PiarFlow по своему Telegram-токену (POST /v1/traffic_bot/add в конструкторе),
поэтому в /sponsors передаются только user_id и chat_id. Засчитывается только ссылка, которую бот
сам показал пользователю невыполненной и которая затем вернулась со статусом «subscribed».
«not_counted» (выполнено, но PiarFlow не заплатил) помечается и владельцу не оплачивается.
Записи попадают в таблицу pf_done, откуда их забирает конструктор.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User

from child import sponsor_log as slog
from child.db import ChildDB

log = logging.getLogger("child.piarflow")

PASS_TTL = 600        # прошёл задания — не беспокоим 10 минут
NO_TASKS_TTL = 300    # заданий нет — повторный запрос через 5 минут
ERROR_TTL = 120       # PiarFlow недоступен — пропускаем пользователя 2 минуты
CHECK_CB = "pf_chk"
DONE_STATUSES = ("subscribed", "not_counted")
_NET = "piarflow"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pf_shown (
    user_id INTEGER NOT NULL,
    link TEXT NOT NULL,
    shown_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, link)
);
CREATE TABLE IF NOT EXISTS pf_done (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    link TEXT NOT NULL,
    counted INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, link)
);
CREATE TABLE IF NOT EXISTS sponsor_gate_reset (
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    reset_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, provider)
);
"""


class PiarFlowGate:
    def __init__(self, api_key: str, db: ChildDB, base_url: str = "https://piarflow.com/v1",
                 timeout: float = 10, max_sponsors: int = 0):
        self.api_key = api_key
        self.db = db
        self.base_url = (base_url or "https://piarflow.com/v1").rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_sponsors = max_sponsors
        self._session: aiohttp.ClientSession | None = None
        self._pass_until: dict[int, float] = {}
        self._pending: dict[int, list[str]] = {}
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.execute(
                "CREATE TABLE IF NOT EXISTS sponsor_gate_reset ("
                "user_id INTEGER NOT NULL, provider TEXT NOT NULL, reset_at INTEGER NOT NULL, "
                "PRIMARY KEY (user_id, provider))")

    async def _post(self, path: str, body: dict) -> tuple[int, dict | None, int]:
        """(HTTP-статус, JSON|None, duration_ms). Успешный done-лог — на стороне вызывающего."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        slog.log_start(log, _NET, path)
        t0 = time.monotonic()
        try:
            async with self._session.post(self.base_url + path, json=body, headers={
                    "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                ms = int((time.monotonic() - t0) * 1000)
                if r.status != 200 or not isinstance(data, dict):
                    err = "bad_json" if r.status == 200 else "http"
                    slog.log_done(log, _NET, path, http_status=r.status, duration_ms=ms, error=err)
                    return r.status, None, ms
                return r.status, data, ms
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            ms = int((time.monotonic() - t0) * 1000)
            slog.log_done(log, _NET, path, http_status=0, duration_ms=ms, error=slog.classify_exc(e))
            return 0, None, ms

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _items(data: dict | None) -> list[tuple[str, str]]:
        out = []
        for s in (data or {}).get("sponsors") or []:
            if isinstance(s, dict) and str(s.get("link") or "").startswith("http"):
                out.append((str(s["link"]), str(s.get("status") or "")))
        return out

    def _was_shown(self, uid: int, link: str) -> bool:
        return self.db.one("SELECT 1 FROM pf_shown WHERE user_id=? AND link=?", uid, link) is not None

    def _is_done(self, uid: int, link: str) -> bool:
        return self.db.one("SELECT 1 FROM pf_done WHERE user_id=? AND link=?", uid, link) is not None

    def _mark_shown(self, uid: int, links: list[str]) -> None:
        ts = int(time.time())
        with self.db.lock:
            for link in links:
                self.db.exec("INSERT OR IGNORE INTO pf_shown(user_id,link,shown_at) VALUES(?,?,?)", uid, link, ts)

    def _mark_done(self, uid: int, items: list[tuple[str, str]]) -> None:
        ts = int(time.time())
        for link, status in items:
            if status in DONE_STATUSES and self._was_shown(uid, link):
                self.db.exec("INSERT OR IGNORE INTO pf_done(user_id,link,counted,created_at) VALUES(?,?,?,?)",
                             uid, link, 1 if status == "subscribed" else 0, ts)

    async def _verify(self, uid: int, links: list[str]) -> list[tuple[str, str]] | None:
        http_status, data, ms = await self._post("/sponsors/check", {"user_id": uid, "links": links})
        if data is None:
            return None
        items = self._items(data)
        self._mark_done(uid, items)
        slog.log_done(
            log, _NET, "/sponsors/check", http_status=http_status, duration_ms=ms,
            tasks=len(items), statuses=slog.status_counts(s for _, s in items),
            result="verified",
        )
        return items

    @staticmethod
    def keyboard(links: list[str]) -> InlineKeyboardMarkup:
        rows = []
        for i, link in enumerate(links, 1):
            label = "🤖 Запустить бота" if "?start=" in link else "📢 Подписаться"
            rows.append([InlineKeyboardButton(text=f"{label} #{i}", url=link)])
        rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data=CHECK_CB)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _pass(self, uid: int, ttl: float) -> None:
        self._pass_until[uid] = time.time() + ttl
        self._pending.pop(uid, None)

    async def check(self, u: User, force: bool = False) -> InlineKeyboardMarkup | None:
        """None — пользователь может пользоваться ботом; иначе клавиатура с заданиями."""
        uid = int(u.id)
        reset = False
        try:
            reset = self.db.one("SELECT 1 FROM sponsor_gate_reset WHERE user_id=? AND provider=?",
                                uid, "piarflow") is not None
        except Exception:  # noqa: BLE001
            reset = False
        if reset:
            self._pass_until.pop(uid, None)
            self._pending.pop(uid, None)
        if not force and not reset and self._pass_until.get(uid, 0) > time.time():
            slog.log_skip(log, _NET, "cache")
            return None
        pending = self._pending.get(uid)
        if force and pending:
            items = await self._verify(uid, pending)
            if items is None:
                self._pass(uid, ERROR_TTL)
                return None
            if reset:
                try:
                    self.db.exec("DELETE FROM sponsor_gate_reset WHERE user_id=? AND provider=?", uid, "piarflow")
                except Exception:  # noqa: BLE001
                    pass
            st = dict(items)
            left = [link for link in pending if st.get(link, "unsubscribed") not in DONE_STATUSES]
            if left:
                self._pending[uid] = left
                return self.keyboard(left)
            self._pass(uid, PASS_TTL)
            return None
        body: dict[str, Any] = {"user_id": uid, "chat_id": uid}
        if self.max_sponsors:
            body["max_sponsors"] = self.max_sponsors
        http_status, data, ms = await self._post("/sponsors", body)
        if data is None:
            # 403 — пользователь не может выполнить задания, 404 — заданий нет
            self._pass(uid, NO_TASKS_TTL if http_status in (403, 404) else ERROR_TTL)
            return None
        if reset:
            try:
                self.db.exec("DELETE FROM sponsor_gate_reset WHERE user_id=? AND provider=?", uid, "piarflow")
            except Exception:  # noqa: BLE001
                pass
        items = self._items(data)
        # показанные раньше и уже выполненные — подтверждаем через /sponsors/check (там PiarFlow засчитывает)
        done_now = [link for link, s in items if s in DONE_STATUSES
                    and self._was_shown(uid, link) and not self._is_done(uid, link)]
        if done_now:
            await self._verify(uid, done_now)
        todo = [link for link, s in items if s not in DONE_STATUSES]
        if todo:
            self._mark_shown(uid, todo)
            self._pending[uid] = todo
            result = "show_gate"
            kb: InlineKeyboardMarkup | None = self.keyboard(todo)
        else:
            result = "pass" if items else "no_tasks"
            self._pass(uid, PASS_TTL if items else NO_TASKS_TTL)
            kb = None
        slog.log_done(
            log, _NET, "/sponsors", http_status=http_status, duration_ms=ms,
            tasks=len(items), statuses=slog.status_counts(s for _, s in items), result=result,
        )
        return kb


def revert_completion(bot_dir, uid: int, link: str) -> None:
    """Drop the local completion so the gate shows again; does not change creator UNIQUE ops."""
    import sqlite3
    import time
    from pathlib import Path
    path = Path(bot_dir) / "bot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM pf_done WHERE user_id=? AND link=?", (uid, link))
        conn.execute("DELETE FROM pf_shown WHERE user_id=? AND link=?", (uid, link))
        conn.execute(
            "INSERT OR REPLACE INTO sponsor_gate_reset(user_id,provider,reset_at) VALUES(?,?,?)",
            (uid, "piarflow", int(time.time())))
        conn.commit()
    finally:
        conn.close()

