"""Scheduled clicker challenges; progress derives from existing committed game facts."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from child.clicker_game import GAME_TZ, period_key
from child.engagement import observe

FIELDS = ("title", "description", "starts", "days", "taps", "expeditions", "routes", "reward", "badge")
SEED = {
    "title": "Неделя разведчика",
    "description": "Сделай 25 новых кликов и заверши экспедицию, начатую после вступления. "
    "Забери 40 игрового XP и значок разведчика.",
    "starts": "сегодня",
    "days": 7,
    "taps": 25,
    "expeditions": 1,
    "routes": 0,
    "reward": 40,
    "badge": "🧭 Разведчик недели",
}
GRACE = 7 * 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS clicker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL,
    taps INTEGER NOT NULL,
    expeditions INTEGER NOT NULL,
    routes INTEGER NOT NULL,
    reward INTEGER NOT NULL,
    badge TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    copy_of INTEGER
);
CREATE INDEX IF NOT EXISTS ix_clicker_events_schedule ON clicker_events(status,start_day,end_day);
CREATE UNIQUE INDEX IF NOT EXISTS ux_clicker_event_copy ON clicker_events(copy_of,created_by)
    WHERE copy_of IS NOT NULL AND status='draft';
CREATE TABLE IF NOT EXISTS clicker_event_players (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    join_day TEXT NOT NULL,
    base_day_taps INTEGER NOT NULL,
    min_trip_id INTEGER NOT NULL,
    reward_claimed_at INTEGER,
    reward_xp INTEGER,
    badge_title TEXT,
    PRIMARY KEY(event_id,user_id)
);
CREATE INDEX IF NOT EXISTS ix_clicker_event_claims ON clicker_event_players(event_id,reward_claimed_at,user_id);
CREATE INDEX IF NOT EXISTS ix_clicker_event_user ON clicker_event_players(user_id,reward_claimed_at,event_id);
CREATE TABLE IF NOT EXISTS clicker_event_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(event_id,revision)
);
"""


class EventError(Exception):
    """Safe participant or owner message."""


def date_value(raw):
    return datetime.strptime(raw, "%Y%m%d").date()


class ClickerEvents:
    def __init__(self, db, *, owner_id, owner_check=None, clock=None, game=None, initialize=True):
        self.db, self.game, self.owner_id = db, game, owner_id
        self.owner_check = owner_check or (lambda uid: uid == owner_id)
        self.clock = clock or time.time
        if not initialize:
            return
        with db.lock:
            if db.conn.in_transaction:
                raise RuntimeError("Initialize event schema outside transactions")
            db.conn.executescript(SCHEMA)
        with db.tx():
            if not db.one("SELECT 1 FROM clicker_events LIMIT 1"):
                cid = self._insert(owner_id, self._validated(SEED))
                self._audit(cid, owner_id, "example_draft", 1)

    def _now(self):
        return int(self.clock())

    def _owner(self, actor):
        if not self.owner_check(actor):
            raise EventError("Доступно только текущему владельцу бота.")

    def validate_field(self, field, value):
        if isinstance(value, str):
            value = value.strip()
        bounds = {"days": (1, 30), "taps": (0, 100000), "expeditions": (0, 1000), "routes": (0, 4), "reward": (1, 1000)}
        if field in bounds:
            low, high = bounds[field]
            if isinstance(value, bool) or len(str(value)) > 6 or not str(value).isascii() or not str(value).isdigit():
                raise EventError(f"Введите целое число от {low} до {high}.")
            number = int(value)
            if not low <= number <= high:
                raise EventError(f"Введите целое число от {low} до {high}.")
            return number
        if field == "starts":
            today = datetime.fromtimestamp(self._now(), GAME_TZ).date()
            try:
                if str(value).casefold() == "сегодня":
                    start = today
                elif isinstance(value, str) and len(value) == 8 and value.isascii() and value.isdigit():
                    start = date_value(value)
                else:
                    start = datetime.strptime(str(value), "%Y-%m-%d").date()
            except ValueError:
                raise EventError("Дата начала: ГГГГ-ММ-ДД или «сегодня».") from None
            if not today <= start <= today + timedelta(days=365):
                raise EventError("Начало должно быть сегодня или в течение следующего года.")
            return start.strftime("%Y%m%d")
        maximum = {"title": 80, "description": 900, "badge": 60}.get(field)
        if maximum is None or not isinstance(value, str) or not 1 <= len(value) <= maximum:
            raise EventError(f"Введите текст от 1 до {maximum or 80} символов.")
        return value

    def _validated(self, values):
        data = {field: self.validate_field(field, values.get(field, "")) for field in FIELDS}
        if not any(data[key] for key in ("taps", "expeditions", "routes")):
            raise EventError("Нужна хотя бы одна цель: клики, экспедиции или разные маршруты.")
        return data

    def _event(self, cid):
        row = self.db.one("SELECT * FROM clicker_events WHERE id=?", cid)
        if not row:
            raise EventError("Событие не найдено.")
        return dict(row)

    def _audit(self, cid, actor, kind, revision):
        self.db.exec(
            "INSERT INTO clicker_event_audit(event_id,actor_id,kind,revision,created_at) VALUES(?,?,?,?,?)",
            cid,
            actor,
            kind,
            revision,
            self._now(),
        )

    def _insert(self, actor, data):
        cur = self.db.exec(
            "INSERT INTO clicker_events(title,description,start_day,days,taps,expeditions,routes,reward,badge,"
            "created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            data["title"],
            data["description"],
            data["starts"],
            data["days"],
            data["taps"],
            data["expeditions"],
            data["routes"],
            data["reward"],
            data["badge"],
            actor,
            self._now(),
        )
        return int(cur.lastrowid)

    def owner_event(self, actor, cid):
        self._owner(actor)
        c = self._event(cid)
        c["starts"] = c["start_day"]
        c["participants"] = self.db.scalar("SELECT COUNT(*) FROM clicker_event_players WHERE event_id=?", cid)
        c["claimed"] = self.db.scalar(
            "SELECT COUNT(*) FROM clicker_event_players WHERE event_id=? AND reward_claimed_at IS NOT NULL", cid
        )
        return c

    def create_draft(self, actor, values):
        with self.db.tx():
            self._owner(actor)
            cid = self._insert(actor, self._validated(values))
            self._audit(cid, actor, "draft", 1)
            return cid

    def update_draft(self, actor, cid, revision, values):
        with self.db.tx():
            self._owner(actor)
            c = self._event(cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise EventError("Событие уже изменилось или опубликовано. Удалите устаревшую форму.")
            data = self._validated(values)
            self.db.exec(
                "UPDATE clicker_events SET title=?,description=?,start_day=?,days=?,taps=?,expeditions=?,routes=?,"
                "reward=?,badge=?,"
                "revision=revision+1 WHERE id=?",
                data["title"],
                data["description"],
                data["starts"],
                data["days"],
                data["taps"],
                data["expeditions"],
                data["routes"],
                data["reward"],
                data["badge"],
                cid,
            )
            self._audit(cid, actor, "edit", revision + 1)

    def publish(self, actor, cid, revision):
        with self.db.tx():
            self._owner(actor)
            c = self.owner_event(actor, cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise EventError("Черновик изменился или уже опубликован.")
            data = self._validated(c)
            end = (date_value(data["starts"]) + timedelta(days=data["days"] - 1)).strftime("%Y%m%d")
            overlap = self.db.one(
                "SELECT 1 FROM clicker_events WHERE id<>? AND status IN ('active','paused') AND start_day<=? AND end_day>=?",
                cid,
                end,
                data["starts"],
            )
            if overlap:
                raise EventError("На эти дни уже опубликовано событие. Выберите другой период в черновике.")
            self.db.exec(
                "UPDATE clicker_events SET status='active',end_day=?,published_at=?,revision=revision+1 WHERE id=?",
                end,
                self._now(),
                cid,
            )
            self._audit(cid, actor, "publish", revision + 1)

    def set_status(self, actor, cid, status, revision):
        with self.db.tx():
            self._owner(actor)
            c = self._event(cid)
            if c["status"] == "draft" or c["status"] == status or status not in {"active", "paused"} or c["revision"] != revision:
                raise EventError("Откройте актуальную карточку события.")
            if status == "active" and c["end_day"] < period_key("day", self._now()):
                raise EventError("Событие завершено. Для нового запуска создайте копию.")
            self.db.exec("UPDATE clicker_events SET status=?,revision=revision+1 WHERE id=?", status, cid)
            self._audit(cid, actor, status, revision + 1)

    def copy_draft(self, actor, cid):
        with self.db.tx():
            self._owner(actor)
            old = self.db.one("SELECT id FROM clicker_events WHERE copy_of=? AND created_by=? AND status='draft'", cid, actor)
            if old:
                return old["id"]
            c = self.owner_event(actor, cid)
            c.update(title=("Копия: " + c["title"])[:80], starts="сегодня")
            copied = self.create_draft(actor, c)
            self.db.exec("UPDATE clicker_events SET copy_of=? WHERE id=?", cid, copied)
            return copied

    def list_events(self, actor, page=0, *, owner=False):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise EventError("Откройте список событий заново.")
        if owner:
            self._owner(actor)
        where = "1=1" if owner else "status<>'draft'"
        total = self.db.scalar(f"SELECT COUNT(*) FROM clicker_events WHERE {where}")
        page = min(page, max(0, (total - 1) // 5))
        order, params = "start_day DESC,id DESC", ()
        if not owner:
            today = period_key("day", self._now())
            order = (
                "CASE WHEN start_day<=? AND end_day>=? THEN 0 WHEN start_day>? THEN 1 ELSE 2 END,"
                "CASE WHEN start_day>? THEN start_day END,start_day DESC,id DESC"
            )
            params = (today, today, today, today)
        return {
            "page": page,
            "total": total,
            "rows": [
                dict(r)
                for r in self.db.all(
                    f"SELECT * FROM clicker_events WHERE {where} ORDER BY {order} LIMIT 5 OFFSET ?", *params, page * 5
                )
            ],
        }

    def window(self, c):
        end = datetime.combine(date_value(c["end_day"]) + timedelta(days=1), datetime.min.time(), GAME_TZ)
        return int(end.timestamp()), int(end.timestamp()) + GRACE

    def _progress(self, c, player):
        today = period_key("day", self._now())
        if today < player["join_day"]:
            return {"taps": 0, "expeditions": 0, "routes": 0}
        end, _ = self.window(c)
        cutoff = min(end, self._now() + 1)
        taps = self.db.scalar(
            "SELECT COALESCE(SUM(value),0) FROM clicker_counters WHERE user_id=? AND metric='taps' AND period>=? AND period<=?",
            player["user_id"],
            "day:" + player["join_day"],
            "day:" + min(today, c["end_day"]),
        )
        trips = self.db.one(
            "SELECT COUNT(*) AS trips,COUNT(DISTINCT route_key) AS routes FROM clicker_expeditions "
            "WHERE user_id=? AND id>? AND claimed_at>=? AND claimed_at<?",
            player["user_id"],
            player["min_trip_id"],
            player["joined_at"],
            cutoff,
        )
        return {"taps": max(0, taps - player["base_day_taps"]), "expeditions": trips["trips"], "routes": trips["routes"]}

    def player_event(self, uid, cid):
        if not self.db.user(uid):
            raise EventError("Сначала нажмите /start в личном чате с ботом.")
        c = self._event(cid)
        if c["status"] == "draft":
            raise EventError("Событие ещё не опубликовано.")
        p = self.db.one("SELECT * FROM clicker_event_players WHERE event_id=? AND user_id=?", cid, uid)
        c["player"] = dict(p) if p else None
        c["progress"] = self._progress(c, p) if p else {"taps": 0, "expeditions": 0, "routes": 0}
        c["ready"] = bool(p) and all(c["progress"][key] >= c[key] for key in ("taps", "expeditions", "routes"))
        c["ends_at"], c["claim_until"] = self.window(c)
        return c

    def join(self, uid, cid, revision):
        with self.db.tx():
            c = self.player_event(uid, cid)
            today = period_key("day", self._now())
            if c["revision"] != revision or c["status"] != "active" or not c["start_day"] <= today <= c["end_day"]:
                raise EventError("Событие сейчас не принимает участников. Обновите карточку.")
            if c["player"]:
                return
            if self.game is None:
                raise EventError("Откройте событие в дочернем боте.")
            self.game._ensure(uid, self._now())
            baseline = (
                self.db.scalar(
                    "SELECT value FROM clicker_counters WHERE user_id=? AND period=? AND metric='taps'", uid, "day:" + today
                )
                or 0
            )
            anchor = self.db.scalar("SELECT COALESCE(MAX(id),0) FROM clicker_expeditions WHERE user_id=?", uid)
            self.db.exec(
                "INSERT INTO clicker_event_players(event_id,user_id,joined_at,join_day,base_day_taps,min_trip_id) "
                "VALUES(?,?,?,?,?,?)",
                cid,
                uid,
                self._now(),
                today,
                baseline,
                anchor,
            )
            observe(self.db, uid, "event_join")

    def claim(self, uid, cid):
        with self.db.tx():
            c = self.player_event(uid, cid)
            p = c["player"]
            if not p or p["reward_claimed_at"] is not None:
                raise EventError("Сначала вступите в событие; повторно награда не выдаётся.")
            if self._now() < p["joined_at"] or self._now() >= c["claim_until"] or not c["ready"]:
                raise EventError("Цели ещё не выполнены или срок получения награды закончился.")
            if self.game is None:
                raise EventError("Заберите награду в дочернем боте.")
            at = self._now()
            self.game._ensure(uid, at)
            self.game._award(uid, c["reward"], "mission", f"event:{cid}:{uid}", at)
            self.db.exec(
                "UPDATE clicker_event_players SET reward_claimed_at=?,reward_xp=?,badge_title=? WHERE event_id=? AND user_id=?",
                at,
                c["reward"],
                c["badge"],
                cid,
                uid,
            )
            self.game._activity(uid, at)
            self.game._badges(uid, at)
            observe(self.db, uid, "event_claim")
            return c["reward"]

    def leaderboard(self, cid):
        c = self._event(cid)
        if c["status"] == "draft":
            raise EventError("Событие ещё не опубликовано.")
        return [
            dict(r)
            for r in self.db.all(
                "SELECT u.full_name,p.reward_claimed_at,p.reward_xp FROM clicker_event_players p "
                "JOIN users u ON u.id=p.user_id WHERE p.event_id=? AND p.reward_claimed_at IS NOT NULL "
                "ORDER BY p.reward_claimed_at,p.user_id LIMIT 10",
                cid,
            )
        ]

    def awards(self, uid, page=0):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise EventError("Откройте награды заново.")
        total = self.db.scalar(
            "SELECT COUNT(*) FROM clicker_event_players WHERE user_id=? AND reward_claimed_at IS NOT NULL", uid
        )
        page = min(page, max(0, (total - 1) // 8))
        return {
            "total": total,
            "page": page,
            "rows": [
                dict(r)
                for r in self.db.all(
                    "SELECT p.*,e.title FROM clicker_event_players p JOIN clicker_events e ON e.id=p.event_id "
                    "WHERE p.user_id=? AND p.reward_claimed_at IS NOT NULL "
                    "ORDER BY p.reward_claimed_at DESC,p.event_id DESC LIMIT 8 OFFSET ?",
                    uid,
                    page * 8,
                )
            ],
        }
