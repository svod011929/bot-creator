"""Ordered learning tracks compiled from published Star cards; completion awards badges only."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from child.engagement import observe
from child.star_journey import TZ, day_date, period

FIELDS = ("title", "description", "cards", "starts", "days", "badge")
GRACE = 7 * 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS star_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    cards_json TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL,
    badge TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    copy_of INTEGER
);
CREATE INDEX IF NOT EXISTS ix_star_tracks_schedule ON star_tracks(status,start_day,end_day);
CREATE UNIQUE INDEX IF NOT EXISTS ux_star_track_copy ON star_tracks(copy_of,created_by)
    WHERE copy_of IS NOT NULL AND status='draft';
CREATE TABLE IF NOT EXISTS star_track_stages (
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    source_revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    lesson TEXT NOT NULL,
    question TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    correct INTEGER NOT NULL,
    PRIMARY KEY(track_id,position)
);
CREATE TABLE IF NOT EXISTS star_track_players (
    track_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    last_action_at INTEGER NOT NULL,
    completed_at INTEGER,
    badge_claimed_at INTEGER,
    badge_title TEXT,
    PRIMARY KEY(track_id,user_id)
);
CREATE INDEX IF NOT EXISTS ix_star_track_user ON star_track_players(user_id,track_id);
CREATE TABLE IF NOT EXISTS star_track_answers (
    track_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    opened_at INTEGER NOT NULL,
    answered_at INTEGER,
    PRIMARY KEY(track_id,user_id,position)
);
CREATE TABLE IF NOT EXISTS star_track_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(track_id,revision)
);
"""


class TrackError(Exception):
    """Safe owner or participant message."""


class StarTracks:
    def __init__(self, db, *, owner_id, owner_check=None, clock=None, initialize=True):
        self.db, self.owner_id = db, owner_id
        self.owner_check = owner_check or (lambda uid: uid == owner_id)
        self.clock = clock or time.time
        if not initialize:
            return
        with db.lock:
            if db.conn.in_transaction:
                raise RuntimeError("Initialize learning tracks outside transactions")
            db.conn.executescript(SCHEMA)
        with db.tx():
            if not db.one("SELECT 1 FROM star_tracks LIMIT 1"):
                day = period("day", self._now())
                last = (day_date(day) + timedelta(days=13)).strftime("%Y%m%d")
                rows = db.all(
                    "SELECT id FROM star_cards WHERE status='active' AND start_day<=? AND end_day>=? "
                    "ORDER BY category,id LIMIT 3",
                    day,
                    last,
                )
                if len(rows) == 3:
                    tid = self._insert(
                        owner_id,
                        {
                            "title": "Первые открытия",
                            "description": "Три небольших шага: изучи материал, подтверди ответ "
                            "и собери значок исследователя. Этапы открываются по порядку.",
                            "cards": [r[0] for r in rows],
                            "starts": day,
                            "days": 14,
                            "badge": "🧭 Первые открытия",
                        },
                    )
                    self._audit(owner_id, tid, "example_draft", 1)

    def _now(self):
        return int(self.clock())

    def _owner(self, actor):
        if not self.owner_check(actor):
            raise TrackError("Доступно только текущему владельцу бота.")

    def _user(self, uid):
        if not self.db.user(uid):
            raise TrackError("Сначала нажмите /start в личном чате с ботом.")

    def validate_field(self, field, value):
        if field == "cards":
            if isinstance(value, str):
                if len(value) > 400:
                    raise TrackError("Выберите от 3 до 12 карточек.")
                parts = re.split(r"[,\s]+", value.strip())
                if any(not p.isascii() or not p.isdigit() or len(p) > 19 for p in parts):
                    raise TrackError("ID карточек: целые числа через пробел или запятую, в порядке этапов.")
                value = [int(p) for p in parts]
            if (
                not isinstance(value, list)
                or not 3 <= len(value) <= 12
                or any(type(n) is not int or not 1 <= n <= 2**63 - 1 for n in value)
                or len(set(value)) != len(value)
            ):
                raise TrackError("Нужны 3–12 разных карточек в порядке прохождения.")
            return list(value)
        if field == "starts":
            today = day_date(period("day", self._now()))
            try:
                if value == "сегодня":
                    start = today
                elif isinstance(value, str) and len(value) == 8 and value.isascii() and value.isdigit():
                    start = day_date(value)
                else:
                    start = datetime.strptime(str(value), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                raise TrackError("Дата: ГГГГ-ММ-ДД или «сегодня».") from None
            if not today <= start <= today + timedelta(days=365):
                raise TrackError("Начало должно быть сегодня или в течение следующего года.")
            return start.strftime("%Y%m%d")
        if field == "days":
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or len(str(value)) > 3:
                raise TrackError("Срок: от 1 до 365 календарных дней.")
            if not 1 <= int(value) <= 365:
                raise TrackError("Срок: от 1 до 365 календарных дней.")
            return int(value)
        maximum = {"title": 80, "description": 900, "badge": 60}.get(field)
        if not maximum or not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise TrackError(f"Введите текст от 1 до {maximum or 80} символов.")
        return value.strip()

    def _validated(self, values):
        return {field: self.validate_field(field, values.get(field, "")) for field in FIELDS}

    def _track(self, tid):
        row = self.db.one("SELECT * FROM star_tracks WHERE id=?", tid)
        if not row:
            raise TrackError("Маршрут не найден.")
        c = dict(row)
        c["cards"] = json.loads(c.pop("cards_json"))
        c["starts"] = c["start_day"]
        return c

    def _source(self, cid):
        row = self.db.one("SELECT * FROM star_cards WHERE id=?", cid)
        if not row:
            raise TrackError("Одна из карточек недоступна. Исправьте список этапов.")
        return dict(row)

    def _stages(self, track, *, public=False):
        if track["status"] == "draft":
            rows = [{**self._source(cid), "position": pos, "card_id": cid} for pos, cid in enumerate(track["cards"])]
        else:
            rows = [
                dict(r) for r in self.db.all("SELECT * FROM star_track_stages WHERE track_id=? ORDER BY position", track["id"])
            ]
        for r in rows:
            r["answers"] = json.loads(r.pop("answers_json"))
            if public:
                r.pop("correct", None)
        return rows

    def _insert(self, actor, data):
        cur = self.db.exec(
            "INSERT INTO star_tracks(title,description,cards_json,start_day,days,badge,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            data["title"],
            data["description"],
            json.dumps(data["cards"]),
            data["starts"],
            data["days"],
            data["badge"],
            actor,
            self._now(),
        )
        return int(cur.lastrowid)

    def _audit(self, actor, tid, kind, revision):
        self.db.exec(
            "INSERT INTO star_track_audit(track_id,actor_id,kind,revision,created_at) VALUES(?,?,?,?,?)",
            tid,
            actor,
            kind,
            revision,
            self._now(),
        )

    def create_draft(self, actor, values):
        with self.db.tx():
            self._owner(actor)
            tid = self._insert(actor, self._validated(values))
            self._audit(actor, tid, "draft", 1)
            return tid

    def update_draft(self, actor, tid, revision, values):
        with self.db.tx():
            self._owner(actor)
            c = self._track(tid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise TrackError("Черновик уже изменился или опубликован. Откройте каталог заново.")
            d = self._validated(values)
            self.db.exec(
                "UPDATE star_tracks SET title=?,description=?,cards_json=?,start_day=?,days=?,badge=?,"
                "revision=revision+1 WHERE id=?",
                d["title"],
                d["description"],
                json.dumps(d["cards"]),
                d["starts"],
                d["days"],
                d["badge"],
                tid,
            )
            self._audit(actor, tid, "edit", revision + 1)

    def owner_track(self, actor, tid):
        self._owner(actor)
        c = self._track(tid)
        c["stages"] = self._stages(c)
        c["participants"] = self.db.scalar("SELECT COUNT(*) FROM star_track_players WHERE track_id=?", tid)
        c["completed"] = self.db.scalar(
            "SELECT COUNT(*) FROM star_track_players WHERE track_id=? AND completed_at IS NOT NULL", tid
        )
        c["claimed"] = self.db.scalar(
            "SELECT COUNT(*) FROM star_track_players WHERE track_id=? AND badge_claimed_at IS NOT NULL", tid
        )
        passed = {
            r[0]: r[1]
            for r in self.db.all(
                "SELECT position,COUNT(*) FROM star_track_answers WHERE track_id=? AND answered_at IS NOT NULL GROUP BY position",
                tid,
            )
        }
        for stage in c["stages"]:
            stage["passed"] = passed.get(stage["position"], 0)
        return c

    def sources(self, actor, page=0):
        self._owner(actor)
        self._page(page)
        total = self.db.scalar("SELECT COUNT(*) FROM star_cards WHERE status='active'")
        page = min(page, max(0, (total - 1) // 6))
        return {
            "page": page,
            "total": total,
            "rows": [
                dict(r)
                for r in self.db.all(
                    "SELECT id,title,start_day,end_day FROM star_cards WHERE status='active' "
                    "ORDER BY category,id LIMIT 6 OFFSET ?",
                    page * 6,
                )
            ],
        }

    def selected(self, actor, cards):
        self._owner(actor)
        return [{"id": cid, "title": self._source(cid)["title"]} for cid in cards]

    def source(self, actor, cid):
        self._owner(actor)
        row = self._source(cid)
        if row["status"] != "active":
            raise TrackError("Выбирать можно только опубликованные активные карточки.")
        return {"id": row["id"], "title": row["title"]}

    def publish(self, actor, tid, revision):
        with self.db.tx():
            self._owner(actor)
            c = self._track(tid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise TrackError("Черновик изменился или уже опубликован.")
            d = self._validated(c)
            end = (day_date(d["starts"]) + timedelta(days=d["days"] - 1)).strftime("%Y%m%d")
            rows = [self._source(cid) for cid in d["cards"]]
            for pos, row in enumerate(rows):
                if row["status"] != "active" or row["start_day"] > d["starts"] or row["end_day"] < end:
                    raise TrackError("Все карточки должны быть опубликованы и доступны на весь срок маршрута.")
                self.db.exec(
                    "INSERT INTO star_track_stages(track_id,position,card_id,source_revision,title,lesson,question,"
                    "answers_json,correct) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    tid,
                    pos,
                    row["id"],
                    row["revision"],
                    row["title"],
                    row["lesson"],
                    row["question"],
                    row["answers_json"],
                    row["correct"],
                )
            self.db.exec(
                "UPDATE star_tracks SET end_day=?,status='active',published_at=?,revision=revision+1 WHERE id=?",
                end,
                self._now(),
                tid,
            )
            self._audit(actor, tid, "publish", revision + 1)

    def set_status(self, actor, tid, status, revision):
        with self.db.tx():
            self._owner(actor)
            c = self._track(tid)
            if (
                c["status"] == "draft"
                or c["status"] == status
                or status not in {"active", "paused"}
                or c["revision"] != revision
                or c["end_day"] < period("day", self._now())
            ):
                raise TrackError("Откройте актуальный маршрут, который ещё не завершился.")
            self.db.exec("UPDATE star_tracks SET status=?,revision=revision+1 WHERE id=?", status, tid)
            self._audit(actor, tid, status, revision + 1)

    def copy_draft(self, actor, tid):
        with self.db.tx():
            self._owner(actor)
            old = self.db.one("SELECT id FROM star_tracks WHERE copy_of=? AND created_by=? AND status='draft'", tid, actor)
            if old:
                return old[0]
            c = self._track(tid)
            copied = self.create_draft(actor, {**c, "title": ("Копия: " + c["title"])[:80], "starts": "сегодня"})
            self.db.exec("UPDATE star_tracks SET copy_of=? WHERE id=?", tid, copied)
            return copied

    def _page(self, page):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise TrackError("Откройте список маршрутов заново.")

    def list_tracks(self, actor, page=0, *, owner=False, mine=False):
        self._page(page)
        if owner:
            self._owner(actor)
        where, params = ("1=1", ()) if owner else ("status<>'draft'", ())
        if mine:
            where += " AND id IN (SELECT track_id FROM star_track_players WHERE user_id=?)"
            params = (actor,)
        total = self.db.scalar(f"SELECT COUNT(*) FROM star_tracks WHERE {where}", *params)
        page = min(page, max(0, (total - 1) // 5))
        order, order_params = "start_day DESC,id DESC", ()
        if not owner:
            day = period("day", self._now())
            order = (
                "CASE WHEN start_day<=? AND end_day>=? THEN 0 WHEN start_day>? THEN 1 ELSE 2 END,"
                "CASE WHEN start_day>? THEN start_day END,start_day DESC,id DESC"
            )
            order_params = (day, day, day, day)
        rows = self.db.all(
            f"SELECT * FROM star_tracks WHERE {where} ORDER BY {order} LIMIT 5 OFFSET ?", *params, *order_params, page * 5
        )
        result = []
        for row in rows:
            c = dict(row)
            c["cards"] = json.loads(c.pop("cards_json"))
            result.append(c)
        return {"page": page, "total": total, "rows": result}

    def window(self, c):
        end = datetime.combine(day_date(c["end_day"]) + timedelta(days=1), datetime.min.time(), TZ)
        return int(end.timestamp()), int(end.timestamp()) + GRACE

    def player_track(self, uid, tid):
        self._user(uid)
        c = self._track(tid)
        if c["status"] == "draft":
            raise TrackError("Маршрут ещё не опубликован.")
        c["stages"] = [{"title": s["title"], "position": s["position"]} for s in self._stages(c, public=True)]
        p = self.db.one("SELECT * FROM star_track_players WHERE track_id=? AND user_id=?", tid, uid)
        c["player"] = dict(p) if p else None
        c["ends_at"], c["claim_until"] = self.window(c)
        return c

    def join(self, uid, tid, revision):
        with self.db.tx():
            c = self.player_track(uid, tid)
            day = period("day", self._now())
            if c["status"] != "active" or c["revision"] != revision or not c["start_day"] <= day <= c["end_day"]:
                raise TrackError("Маршрут сейчас не принимает участников. Обновите карточку.")
            if c["player"]:
                return
            self.db.exec(
                "INSERT INTO star_track_players(track_id,user_id,joined_at,last_action_at) VALUES(?,?,?,?)",
                tid,
                uid,
                self._now(),
                self._now(),
            )
            observe(self.db, uid, "track_join")

    def _stage(self, uid, tid, position):
        c = self.player_track(uid, tid)
        p = c["player"]
        if not p or type(position) is not int or not 0 <= position < len(c["cards"]) or position > p["current_step"]:
            raise TrackError("Сначала вступите и пройдите предыдущие этапы.")
        if self._now() < p["last_action_at"]:
            raise TrackError("Время изменилось. Откройте маршрут позже.")
        if position == p["current_step"] and self._now() >= c["ends_at"]:
            raise TrackError("Срок прохождения закончился. Можно повторить уже пройденные этапы.")
        return c

    def open_stage(self, uid, tid, position):
        with self.db.tx():
            c = self._stage(uid, tid, position)
            if position == c["player"]["current_step"]:
                self.db.exec(
                    "INSERT INTO star_track_answers(track_id,user_id,position,opened_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                    tid,
                    uid,
                    position,
                    self._now(),
                )
            row = dict(self.db.one("SELECT * FROM star_track_stages WHERE track_id=? AND position=?", tid, position))
            row["answers"] = json.loads(row.pop("answers_json"))
            row.pop("correct", None)
            row["answer"] = dict(
                self.db.one("SELECT * FROM star_track_answers WHERE track_id=? AND user_id=? AND position=?", tid, uid, position)
            )
            row["version"] = c["player"]["version"]
            return row

    def answer(self, uid, tid, position, choice, version):
        with self.db.tx():
            c = self._stage(uid, tid, position)
            if type(choice) is not int or choice not in (0, 1, 2) or c["player"]["version"] != version:
                raise TrackError("Экран изменился. Откройте текущий этап заново.")
            row = self.db.one(
                "SELECT * FROM star_track_answers WHERE track_id=? AND user_id=? AND position=?", tid, uid, position
            )
            if not row or position != c["player"]["current_step"] or row["answered_at"] is not None:
                raise TrackError("Откройте текущий непройденный этап маршрута.")
            at = self._now()
            if at < row["retry_at"]:
                raise TrackError(f"Повторная попытка через {row['retry_at'] - at} сек.")
            correct = self.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=?", tid, position)
            self.db.exec(
                "UPDATE star_track_players SET version=version+1,last_action_at=? WHERE track_id=? AND user_id=?", at, tid, uid
            )
            if choice != correct:
                attempts = (0 if row["attempts"] >= 3 else row["attempts"]) + 1
                self.db.exec(
                    "UPDATE star_track_answers SET attempts=?,retry_at=? WHERE track_id=? AND user_id=? AND position=?",
                    attempts,
                    at + (86400 if attempts == 3 else 30),
                    tid,
                    uid,
                    position,
                )
                return False
            done = position + 1 == len(c["cards"])
            self.db.exec(
                "UPDATE star_track_answers SET answered_at=? WHERE track_id=? AND user_id=? AND position=?",
                at,
                tid,
                uid,
                position,
            )
            self.db.exec(
                "UPDATE star_track_players SET current_step=current_step+1,completed_at=? WHERE track_id=? AND user_id=?",
                at if done else None,
                tid,
                uid,
            )
            observe(self.db, uid, "track_step")
            if done:
                observe(self.db, uid, "track_complete")
            return True

    def claim(self, uid, tid):
        with self.db.tx():
            c = self.player_track(uid, tid)
            p, at = c["player"], self._now()
            if (
                not p
                or p["completed_at"] is None
                or p["badge_claimed_at"] is not None
                or not p["last_action_at"] <= at < c["claim_until"]
            ):
                raise TrackError("Завершите маршрут в срок; значок выдаётся один раз в течение семи суток после окончания.")
            self.db.exec(
                "UPDATE star_track_players SET badge_claimed_at=?,badge_title=?,last_action_at=? WHERE track_id=? AND user_id=?",
                at,
                c["badge"],
                at,
                tid,
                uid,
            )
            observe(self.db, uid, "track_claim")
            return c["badge"]

    def awards(self, uid, page=0):
        self._page(page)
        total = self.db.scalar("SELECT COUNT(*) FROM star_track_players WHERE user_id=? AND badge_claimed_at IS NOT NULL", uid)
        page = min(page, max(0, (total - 1) // 8))
        return {
            "page": page,
            "total": total,
            "rows": [
                dict(r)
                for r in self.db.all(
                    "SELECT p.*,t.title FROM star_track_players p JOIN star_tracks t ON t.id=p.track_id "
                    "WHERE p.user_id=? AND p.badge_claimed_at IS NOT NULL "
                    "ORDER BY p.badge_claimed_at DESC,p.track_id DESC LIMIT 8 OFFSET ?",
                    uid,
                    page * 8,
                )
            ],
        }
