"""ОП TGrass в дочернем боте: офферы из POST /offers и проверка подписки тем же запросом.

Засчитывается только оффер, который бот сам показал пользователю неподписанным и который потом
стал «subscribed». Фейки (по `/check` → is_fake) помечаются и не оплачиваются владельцу.
Записи попадают в таблицу tgrass_done, откуда их забирает конструктор.
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

log = logging.getLogger("child.tgrass")

PASS_TTL = 600        # прошёл ОП — не беспокоим 10 минут
NO_OFFERS_TTL = 300   # офферов нет — повторный запрос через 5 минут
ERROR_TTL = 120       # TGrass недоступен — пропускаем пользователя 2 минуты
CHECK_CB = "tg_chk"
TEXT = "📢 Чтобы пользоваться ботом, подпишитесь на спонсоров и нажмите «✅ Проверить»."
_NET = "tgrass"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tgrass_shown (
    user_id INTEGER NOT NULL,
    offer_id INTEGER NOT NULL,
    shown_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, offer_id)
);
CREATE TABLE IF NOT EXISTS tgrass_done (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    offer_id INTEGER NOT NULL,
    fake INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, offer_id)
);
"""


class TGrassGate:
    def __init__(self, api_key: str, db: ChildDB, base_url: str = "https://tgrass.space", timeout: float = 10):
        self.api_key = api_key
        self.db = db
        self.base_url = (base_url or "https://tgrass.space").rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._pass_until: dict[int, float] = {}
        with db.lock:
            db.conn.executescript(SCHEMA)

    async def _post(self, path: str, body: dict) -> tuple[dict | None, int, int]:
        """(data|None, http_status, duration_ms). Успешный done-лог — на стороне вызывающего."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        slog.log_start(log, _NET, path)
        t0 = time.monotonic()
        try:
            async with self._session.post(self.base_url + path, json=body, headers={
                    "Auth": self.api_key, "accept": "application/json"}) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                ms = int((time.monotonic() - t0) * 1000)
                if r.status != 200 or not isinstance(data, dict):
                    err = "bad_json" if r.status == 200 else "http"
                    slog.log_done(log, _NET, path, http_status=r.status, duration_ms=ms, error=err)
                    return None, r.status, ms
                return data, r.status, ms
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            ms = int((time.monotonic() - t0) * 1000)
            slog.log_done(log, _NET, path, http_status=0, duration_ms=ms, error=slog.classify_exc(e))
            return None, 0, ms

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def user_body(u: User) -> dict[str, Any]:
        body: dict[str, Any] = {"tg_user_id": int(u.id), "is_premium": bool(u.is_premium),
                                "lang": (u.language_code or "ru")[:8]}
        if u.username:
            body["tg_login"] = u.username
        return body

    def _record(self, uid: int, offers: list[dict]) -> list[int]:
        """Запоминает показанные неподписанные офферы; возвращает id новых подписок на показанные."""
        ts = int(time.time())
        newly: list[int] = []
        with self.db.lock:
            for o in offers:
                try:
                    oid = int(o.get("offer_id"))
                except (TypeError, ValueError):
                    continue
                shown = self.db.one("SELECT 1 FROM tgrass_shown WHERE user_id=? AND offer_id=?", uid, oid)
                if o.get("subscribed"):
                    if shown and not self.db.one("SELECT 1 FROM tgrass_done WHERE user_id=? AND offer_id=?", uid, oid):
                        newly.append(oid)
                elif not shown:
                    self.db.exec("INSERT OR IGNORE INTO tgrass_shown(user_id,offer_id,shown_at) VALUES(?,?,?)",
                                 uid, oid, ts)
        return newly

    async def _complete(self, uid: int, offer_ids: list[int]) -> None:
        for oid in offer_ids:
            data, http_status, ms = await self._post("/check", {"tg_user_id": uid, "offer_id": oid})
            if data is None:
                continue
            st = str(data.get("status") or "unknown")
            fake = bool(data.get("is_fake"))
            label = f"{st}_fake" if fake and st == "subscribed" else st
            slog.log_done(
                log, _NET, "/check", http_status=http_status, duration_ms=ms, tasks=1,
                statuses=slog.status_counts([label]),
                result="confirmed" if st == "subscribed" else "pending",
            )
            if data.get("status") not in (None, "subscribed"):
                continue  # TGrass не подтвердил — попробуем при следующей проверке
            self.db.exec("INSERT OR IGNORE INTO tgrass_done(user_id,offer_id,fake,created_at) VALUES(?,?,?,?)",
                         uid, oid, 1 if fake else 0, int(time.time()))

    async def check(self, u: User, force: bool = False) -> InlineKeyboardMarkup | None:
        """None — пользователь может пользоваться ботом; иначе клавиатура с офферами."""
        now = time.time()
        if not force and self._pass_until.get(u.id, 0) > now:
            slog.log_skip(log, _NET, "cache")
            return None
        data, http_status, ms = await self._post("/offers", self.user_body(u))
        if data is None:
            self._pass_until[u.id] = now + ERROR_TTL
            return None
        api_status = data.get("status")
        offers = [o for o in (data.get("offers") or []) if isinstance(o, dict)]
        offer_statuses = ["subscribed" if o.get("subscribed") else "unsubscribed" for o in offers]
        newly = self._record(u.id, offers)
        if newly:
            await self._complete(u.id, newly)
        kb: InlineKeyboardMarkup | None = None
        if api_status == "not_ok":
            rows = []
            for o in offers:
                if o.get("subscribed") or not str(o.get("link") or "").startswith("http"):
                    continue
                label = "📢 Подписаться" if o.get("type") == "channel" else "🔗 Перейти"
                name = str(o.get("name") or "").strip()
                rows.append([InlineKeyboardButton(text=f"{label}: {name}"[:60] if name else label, url=o["link"])])
            if rows:
                rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data=CHECK_CB)])
                kb = InlineKeyboardMarkup(inline_keyboard=rows)
                result = "show_gate"
            else:
                result = "pass"
                self._pass_until[u.id] = now + PASS_TTL
        elif api_status == "no_offers":
            result = "no_tasks"
            self._pass_until[u.id] = now + NO_OFFERS_TTL
        else:
            result = "pass"
            self._pass_until[u.id] = now + PASS_TTL
        slog.log_done(
            log, _NET, "/offers", http_status=http_status, duration_ms=ms, tasks=len(offers),
            statuses=slog.status_counts(offer_statuses), result=result,
        )
        return kb
