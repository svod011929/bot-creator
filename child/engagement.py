"""Local, optional daily observations; no message texts, tokens, amounts or outbound requests."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

TZ = ZoneInfo("Europe/Moscow")
COMMON = {"start", "access", "interaction"}
EVENTS = COMMON | {
    "tap",
    "mission",
    "expedition_start",
    "expedition_complete",
    "skill",
    "onboard",
    "quiz",
    "link",
    "qualified",
    "checkin",
    "card_open",
    "card_complete",
    "memory_complete",
    "constellation",
}
STEPS = {
    "clicker": (
        ("access", "Подтверждённый допуск"),
        ("tap", "Успешный клик"),
        ("expedition_start", "Начали экспедицию"),
        ("expedition_complete", "Завершили экспедицию"),
    ),
    "refbot": (
        ("access", "Подтверждённый допуск"),
        ("onboard", "Завершили знакомство"),
        ("link", "Получили ссылку кампании"),
        ("qualified", "Получили подтверждённого друга"),
    ),
    "starbot": (
        ("access", "Подтверждённый допуск"),
        ("card_open", "Открыли карточку"),
        ("card_complete", "Верно прошли карточку"),
        ("constellation", "Собрали созвездие"),
    ),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS engagement_meta (
    id INTEGER PRIMARY KEY CHECK(id=1),
    version INTEGER NOT NULL,
    started_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS engagement_people (
    user_id INTEGER PRIMARY KEY,
    first_at INTEGER NOT NULL,
    first_day TEXT NOT NULL,
    registered_here INTEGER NOT NULL CHECK(registered_here IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_engagement_people_day ON engagement_people(first_day,user_id);
CREATE INDEX IF NOT EXISTS ix_engagement_people_time ON engagement_people(first_at,user_id);
CREATE TABLE IF NOT EXISTS engagement_activity (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    first_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,day)
);
CREATE INDEX IF NOT EXISTS ix_engagement_activity_day ON engagement_activity(day,user_id);
CREATE TABLE IF NOT EXISTS engagement_events (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    first_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,kind,day)
);
CREATE INDEX IF NOT EXISTS ix_engagement_events_time ON engagement_events(kind,first_at,user_id);
"""
log = logging.getLogger("engagement")
observer_actor = ContextVar("engagement_actor", default=None)


class ObservationScope(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        message = event.message if isinstance(event, CallbackQuery) else event
        eligible = (
            user is not None
            and not user.is_bot
            and isinstance(message, Message)
            and message.chat.type == "private"
            and message.chat.id == user.id
        )
        token = observer_actor.set(user.id if eligible else None)
        try:
            return await handler(event, data)
        finally:
            observer_actor.reset(token)


def day_key(at):
    return datetime.fromtimestamp(at, TZ).date().isoformat()


class Engagement:
    def __init__(self, db, *, owner_check=None, clock=None):
        self.db = db
        self.owner_check = owner_check or (lambda uid: False)
        self.clock = clock or time.time
        self.available = False
        self.warned = False
        db.engagement = self
        try:
            with db.lock:
                if db.conn.in_transaction:
                    return
                db.conn.executescript(SCHEMA)
                db.exec("INSERT INTO engagement_meta(id,version,started_at) VALUES(1,1,?) ON CONFLICT(id) DO NOTHING", self.now())
                self.available = True
        except sqlite3.Error:
            self._warn()

    def _warn(self):
        if not self.warned:
            log.warning("Local activity observations unavailable; gameplay continues")
            self.warned = True

    def now(self):
        return int(self.clock())

    def record(self, uid, kind, *, active=True, registered=False):
        if not self.available or kind not in EVENTS or type(uid) is not int or uid <= 0:
            return False
        try:
            if self.owner_check(uid) or not self.db.one("SELECT 1 FROM users WHERE id=?", uid):
                return False
            at = self.now()
            day = day_key(at)
            registered = registered is True and kind == "start"
            # Repeated taps/callbacks need no duplicate daily writes.
            if not registered and self.db.one(
                "SELECT 1 FROM engagement_events WHERE user_id=? AND kind=? AND day=?", uid, kind, day
            ):
                return True
            with self.db.tx():
                if active:
                    self.db.exec(
                        "INSERT INTO engagement_people(user_id,first_at,first_day,registered_here) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET registered_here=1 WHERE excluded.registered_here=1",
                        uid,
                        at,
                        day,
                        int(registered),
                    )
                    self.db.exec(
                        "INSERT INTO engagement_activity(user_id,day,first_at) VALUES(?,?,?) ON CONFLICT(user_id,day) DO NOTHING",
                        uid,
                        day,
                        at,
                    )
                self.db.exec(
                    "INSERT INTO engagement_events(user_id,kind,day,first_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,kind,day) DO NOTHING",
                    uid,
                    kind,
                    day,
                    at,
                )
            return True
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, OverflowError):
            self._warn()
            return False


def observe(db, uid, kind, *, active=True, registered=False):
    collector = getattr(db, "engagement", None)
    actor = observer_actor.get()
    if collector is not None and actor is not None and (not active or actor == uid):
        return collector.record(uid, kind, active=active, registered=registered)
    return False


def private_observation(app, message, uid, kind, *, registered=False):
    if message is not None and message.chat.type == "private" and message.chat.id == uid:
        return observe(app.db, uid, kind, registered=registered)
    return False
