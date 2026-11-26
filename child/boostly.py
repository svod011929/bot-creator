"""ОП Boostly в дочернем боте: задания и проверка только через локальный прокси конструктора.

Хаб-ключ площадки здесь не хранится и не запрашивается. Если в config.json
оказывается поле с ключом конструктора, клиент отказывается работать.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User

from child import sponsor_log as slog
from child.db import ChildDB

log = logging.getLogger("child.boostly")

PASS_TTL = 600
NO_TASKS_TTL = 300
ERROR_TTL = 120
CHECK_CB = "bl_chk"
TASKS_CHECK_CB = "bl_tsk"
TASKS_REFRESH_CB = "bl_ref"
BTN_TASKS = "🚀 Boostly"
TEXT = "📢 Чтобы пользоваться ботом, выполните задания и нажмите «✅ Проверить»."
TASKS_TEXT = "🚀 <b>Задания Boostly</b>\nВыполните задание и нажмите «✅ Проверить»."
EMPTY_TEXT = "Заданий пока нет, загляните позже."
_NET = "boostly"
REQUEST_TIMEOUT = 12.0
LEAKED_FIELDS = (
    "boostly_key",
    "boostly_hub_api_key",
    "boostly_api_key",
    "BOOSTLY_HUB_API_KEY",
    "mgram_key",
    "mgram_hub_api_key",
    "mgram_partner_api_key",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS boostly_shown (
    user_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    shown_at INTEGER NOT NULL,
    task_url TEXT,
    PRIMARY KEY (user_id, task_id)
);
CREATE TABLE IF NOT EXISTS boostly_done (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    task_url TEXT,
    UNIQUE (user_id, task_id)
);
"""


def _proxy_conf(cfg: dict[str, Any]) -> tuple[str, str] | None:
    sock = str(cfg.get("boostly_proxy") or "").strip()
    token = str(cfg.get("boostly_proxy_token") or "").strip()
    if not sock or not token:
        return None
    if any(key in cfg for key in LEAKED_FIELDS):
        log.error("refusing boostly proxy: unexpected creator key field in child config")
        return None
    return sock, token


def _refuse_reason(cfg: dict[str, Any]) -> str:
    if any(key in cfg for key in LEAKED_FIELDS):
        return "refused"
    return "no_proxy"


async def _post(cfg: dict[str, Any], path: str, body: dict) -> dict:
    conf = _proxy_conf(cfg)
    if conf is None:
        return {"ok": False, "reason": _refuse_reason(cfg), "confirmed": False, "tasks": []}
    sock, token = conf
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        connector = aiohttp.UnixConnector(path=sock)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.post(
                f"http://localhost{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    data = {}
                if resp.status != 200:
                    reason = str(data.get("reason") or "error")
                    return {"ok": False, "reason": reason, "confirmed": False, "tasks": []}
                data.setdefault("ok", False)
                return data
    except TimeoutError:
        return {"ok": False, "reason": "timeout", "confirmed": False, "tasks": []}
    except Exception:  # noqa: BLE001 — no token/path in logs
        log.exception("boostly proxy request failed")
        return {"ok": False, "reason": "error", "confirmed": False, "tasks": []}


async def fetch_pending(cfg: dict[str, Any], bot_id: int, user_id: int, meta: dict | None = None) -> list[dict]:
    body: dict[str, Any] = {"bot_id": bot_id, "user_id": user_id}
    if meta:
        body["meta"] = meta
    data = await _post(cfg, "/tasks", body)
    if not data.get("ok"):
        return []
    rows = data.get("tasks")
    if not isinstance(rows, list):
        return []
    return [t for t in rows if isinstance(t, dict) and t.get("id")]


async def check_completion(cfg: dict[str, Any], bot_id: int, user_id: int, task_id: str) -> tuple[bool, str]:
    data = await _post(cfg, "/check", {"bot_id": bot_id, "user_id": user_id, "task_id": task_id})
    reason = str(data.get("reason") or ("ok" if data.get("ok") else "error"))
    if reason == "timeout" or data.get("reason") == "timeout":
        return False, "timeout"
    if not data.get("ok") or not data.get("confirmed"):
        return False, reason if reason != "ok" else "pending"
    return True, "confirmed"


class BoostlyGate:
    def __init__(self, cfg: dict[str, Any], db: ChildDB, bot_id: int):
        self.cfg = cfg
        self.db = db
        self.bot_id = int(bot_id)
        self._pass_until: dict[int, float] = {}
        with db.lock:
            db.conn.executescript(SCHEMA)

    async def close(self) -> None:
        return None

    def _remember_pass(self, uid: int, until: float) -> None:
        self._pass_until[uid] = until

    def user_meta(self, u: User) -> dict[str, Any] | None:
        meta: dict[str, Any] = {}
        if u.language_code:
            meta["language_code"] = u.language_code
        if u.first_name:
            meta["first_name"] = u.first_name
        if u.username:
            meta["username"] = u.username
        premium = getattr(u, "is_premium", None)
        if premium is not None:
            meta["is_premium"] = bool(premium)
        return meta or None

    def _task_rows(self, tasks: list[dict], done_ids: set[str]) -> list[list[InlineKeyboardButton]]:
        rows = []
        for t in tasks:
            tid = str(t.get("id") or "").strip()
            if not tid or tid in done_ids:
                continue
            url = str(t.get("url") or t.get("link") or "")
            if not url.startswith("http"):
                continue
            title = str(t.get("title") or "Выполнить")[:40]
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
        return rows

    async def render_list(self, u: User, *, checking: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
        """Task list for the «🚀 Boostly» section. Available to the owner as well."""
        now = time.time()
        tasks = await fetch_pending(self.cfg, self.bot_id, u.id, self.user_meta(u))
        for t in tasks:
            tid = str(t.get("id") or "").strip()
            if not tid:
                continue
            url = str(t.get("url") or t.get("link") or "")
            self.db.exec(
                "INSERT OR IGNORE INTO boostly_shown(user_id, task_id, shown_at, task_url) VALUES(?,?,?,?)",
                u.id, tid, int(now), url,
            )
        done_ids = {r["task_id"] for r in self.db.all("SELECT task_id FROM boostly_done WHERE user_id=?", u.id)}
        if checking:
            shown = self.db.all("SELECT task_id FROM boostly_shown WHERE user_id=?", u.id)
            for row in shown:
                tid = row["task_id"]
                if tid in done_ids:
                    continue
                ok, _reason = await check_completion(self.cfg, self.bot_id, u.id, tid)
                if ok:
                    self.db.exec(
                        "INSERT OR IGNORE INTO boostly_done(user_id, task_id, created_at, task_url) VALUES(?,?,?,?)",
                        u.id, tid, int(now), None,
                    )
                    done_ids.add(tid)
        rows = self._task_rows(tasks, done_ids)
        if not rows:
            return EMPTY_TEXT, InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=TASKS_REFRESH_CB)],
            ])
        rows.append([
            InlineKeyboardButton(text="✅ Проверить", callback_data=TASKS_CHECK_CB),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=TASKS_REFRESH_CB),
        ])
        return TASKS_TEXT, InlineKeyboardMarkup(inline_keyboard=rows)

    async def check(self, u: User, force: bool = False) -> InlineKeyboardMarkup | None:
        now = time.time()
        if not force and self._pass_until.get(u.id, 0) > now:
            return None
        meta = self.user_meta(u)
        tasks = await fetch_pending(self.cfg, self.bot_id, u.id, meta)
        conf = _proxy_conf(self.cfg)
        if conf is None:
            reason = _refuse_reason(self.cfg)
            slog.log_done(log, _NET, "/tasks", http_status=0, duration_ms=0, tasks=0, result=reason)
            if reason == "refused":
                return None
            self._remember_pass(u.id, now + ERROR_TTL)
            return None

        shown_now = []
        for t in tasks:
            tid = str(t.get("id") or "").strip()
            if not tid:
                continue
            url = str(t.get("url") or t.get("link") or "")
            self.db.exec(
                "INSERT OR IGNORE INTO boostly_shown(user_id, task_id, shown_at, task_url) VALUES(?,?,?,?)",
                u.id, tid, int(now), url,
            )
            shown_now.append(tid)

        shown = self.db.all("SELECT task_id FROM boostly_shown WHERE user_id=?", u.id)
        done_ids = {r["task_id"] for r in self.db.all("SELECT task_id FROM boostly_done WHERE user_id=?", u.id)}
        for row in shown:
            tid = row["task_id"]
            if tid in done_ids:
                continue
            ok, _reason = await check_completion(self.cfg, self.bot_id, u.id, tid)
            if ok:
                self.db.exec(
                    "INSERT OR IGNORE INTO boostly_done(user_id, task_id, created_at, task_url) VALUES(?,?,?,?)",
                    u.id, tid, int(now), None,
                )
                done_ids.add(tid)

        rows = []
        for t in tasks:
            tid = str(t.get("id") or "").strip()
            if not tid or tid in done_ids:
                continue
            url = str(t.get("url") or t.get("link") or "")
            if not url.startswith("http"):
                continue
            title = str(t.get("title") or "Выполнить")[:40]
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
        if rows:
            rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data=CHECK_CB)])
            slog.log_done(log, _NET, "/tasks", http_status=200, duration_ms=0, tasks=len(tasks), result="show_gate")
            return InlineKeyboardMarkup(inline_keyboard=rows)
        self._remember_pass(u.id, now + (NO_TASKS_TTL if not tasks else PASS_TTL))
        slog.log_done(log, _NET, "/tasks", http_status=200, duration_ms=0, tasks=len(tasks), result="pass")
        return None
