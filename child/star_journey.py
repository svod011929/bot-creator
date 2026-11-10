"""Transactional Star bot game. Sparks never read, change or convert the legacy wallet."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from child.db import ChildDB
from child.engagement import observe
from child.star_catalog import BADGES, CATEGORIES, CONSTELLATIONS, MISSIONS, SYMBOLS, starter_cards

TZ = ZoneInfo("Europe/Moscow")
EDITOR_FIELDS = ("category", "title", "lesson", "question", "answers", "correct", "starts", "days")
CARD_REWARD = 15
MEMORY_REWARD = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS star_profiles (
    user_id INTEGER PRIMARY KEY,
    sparks INTEGER NOT NULL DEFAULT 0 CHECK(sparks>=0),
    earned INTEGER NOT NULL DEFAULT 0 CHECK(earned>=0),
    streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    last_day TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS star_metrics (
    user_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    metric TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id,period,metric)
);
CREATE TABLE IF NOT EXISTS star_claims (
    user_id INTEGER NOT NULL,
    mission_key TEXT NOT NULL,
    period TEXT NOT NULL,
    amount INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,mission_key,period)
);
CREATE TABLE IF NOT EXISTS star_memory (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    sequence_json TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    phase TEXT NOT NULL CHECK(phase IN ('preview','answer','failed','done')),
    retry_at INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    completed_at INTEGER,
    PRIMARY KEY(user_id,day)
);
CREATE TABLE IF NOT EXISTS star_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    lesson TEXT NOT NULL,
    question TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    correct INTEGER NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    copy_of INTEGER
);
CREATE INDEX IF NOT EXISTS ix_star_cards_status ON star_cards(status,start_day,end_day);
CREATE UNIQUE INDEX IF NOT EXISTS ux_star_card_copy ON star_cards(copy_of,created_by)
    WHERE copy_of IS NOT NULL AND status='draft';
CREATE TABLE IF NOT EXISTS star_decks (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    card_ids_json TEXT NOT NULL,
    PRIMARY KEY(user_id,day)
);
CREATE TABLE IF NOT EXISTS star_answers (
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    opened_at INTEGER NOT NULL,
    answered_at INTEGER,
    PRIMARY KEY(user_id,card_id)
);
CREATE TABLE IF NOT EXISTS star_constellations (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    cost INTEGER NOT NULL,
    built_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,key)
);
CREATE TABLE IF NOT EXISTS star_sparks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL CHECK(delta<>0),
    kind TEXT NOT NULL CHECK(kind IN ('visit','card','memory','mission','build')),
    source_key TEXT NOT NULL UNIQUE,
    month TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_star_sparks_user ON star_sparks(user_id,id);
CREATE INDEX IF NOT EXISTS ix_star_sparks_month ON star_sparks(month,user_id);
CREATE TABLE IF NOT EXISTS star_editor (
    owner_id INTEGER PRIMARY KEY,
    card_id INTEGER,
    card_revision INTEGER,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    edit_field INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS star_card_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(card_id,revision)
);
"""


class JourneyError(Exception):
    """A bounded, safe message for Telegram."""


def period(scope: str, at: int) -> str:
    local = datetime.fromtimestamp(at, TZ)
    if scope == "story":
        return "once"
    if scope == "day":
        return local.strftime("%Y%m%d")
    if scope == "week":
        year, week, _ = local.isocalendar()
        return f"{year}W{week:02d}"
    if scope == "month":
        return local.strftime("%Y%m")
    raise JourneyError("Откройте маршрут заново: неизвестный период.")


def day_date(value: str):
    return datetime.strptime(value, "%Y%m%d").date()


class StarJourney:
    def __init__(
        self,
        db: ChildDB,
        *,
        owner_id: int,
        owner_check: Callable[[int], bool] | None = None,
        clock: Callable[[], float] | None = None,
        initialize: bool = True,
    ):
        self.db, self.owner_id = db, owner_id
        self.owner_check = owner_check or (lambda uid: uid == owner_id)
        self.clock = clock or time.time
        if not initialize:
            return
        with db.lock:
            if db.conn.in_transaction:
                raise RuntimeError("Initialize Star game outside transactions")
            db.conn.executescript(SCHEMA)
        with db.tx():
            if not db.one("SELECT 1 FROM star_cards LIMIT 1"):
                for card in starter_cards():
                    cid = self._insert_card(owner_id, {**card, "starts": "00010101", "days": 0})
                    db.exec("UPDATE star_cards SET status='active',end_day='99991231',published_at=? WHERE id=?", self.now(), cid)
                    self._event(cid, owner_id, "starter", 1)

    def now(self):
        return int(self.clock())

    def _owner(self, actor):
        if not self.owner_check(actor):
            raise JourneyError("Доступно только текущему владельцу бота.")

    def _ensure(self, uid):
        if not self.db.user(uid):
            raise JourneyError("Сначала нажмите /start в личном чате с ботом.")
        self.db.exec("INSERT INTO star_profiles(user_id,created_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING", uid, self.now())
        return dict(self.db.one("SELECT * FROM star_profiles WHERE user_id=?", uid))

    def _entry(self, uid, delta, kind, source, at):
        self.db.exec(
            "INSERT INTO star_sparks(user_id,delta,kind,source_key,month,created_at) VALUES(?,?,?,?,?,?)",
            uid,
            delta,
            kind,
            source,
            period("month", at),
            at,
        )
        self.db.exec(
            "UPDATE star_profiles SET sparks=sparks+?,earned=earned+?,version=version+1 WHERE user_id=?",
            delta,
            max(0, delta),
            uid,
        )

    def _record(self, uid, metric, at):
        observe(
            self.db,
            uid,
            {"visits": "checkin", "cards": "card_complete", "memory": "memory_complete", "builds": "constellation"}[metric],
        )
        for scope in ("story", "day", "week", "month"):
            self.db.exec(
                "INSERT INTO star_metrics(user_id,period,metric,value) VALUES(?,?,?,1) "
                "ON CONFLICT(user_id,period,metric) DO UPDATE SET value=value+1",
                uid,
                period(scope, at),
                metric,
            )

    def counters(self, uid, key="once"):
        return {
            r["metric"]: r["value"]
            for r in self.db.all("SELECT metric,value FROM star_metrics WHERE user_id=? AND period=?", uid, key)
        }

    def overview(self, uid):
        with self.db.tx():
            p, at = self._ensure(uid), self.now()
            day = period("day", at)
            yesterday = (day_date(day) - timedelta(days=1)).strftime("%Y%m%d")
            effective = p["streak"] if p["last_day"] in {day, yesterday} else 0
            total = self.counters(uid)
            badges = [title for metric, target, title in BADGES.values() if total.get(metric, 0) >= target]
            if p["best_streak"] >= 3:
                badges.append("🔥 Три дня подряд")
            return {
                "profile": p,
                "day": day,
                "streak": effective,
                "checked": p["last_day"] == day,
                "today": self.counters(uid, day),
                "total": total,
                "badges": badges,
                "built": [r["key"] for r in self.db.all("SELECT key FROM star_constellations WHERE user_id=?", uid)],
                "history": [
                    dict(r)
                    for r in self.db.all("SELECT delta,kind FROM star_sparks WHERE user_id=? ORDER BY id DESC LIMIT 5", uid)
                ],
            }

    def checkin(self, uid, expected_day):
        with self.db.tx():
            p, at = self._ensure(uid), self.now()
            day = period("day", at)
            if expected_day != day or p["last_day"] >= day:
                raise JourneyError("Отметка уже получена или день изменился. Откройте маршрут заново.")
            yesterday = (day_date(day) - timedelta(days=1)).strftime("%Y%m%d")
            streak = p["streak"] + 1 if p["last_day"] == yesterday else 1
            amount = 10 + 2 * min(6, streak - 1)
            self.db.exec(
                "UPDATE star_profiles SET last_day=?,streak=?,best_streak=MAX(best_streak,?) WHERE user_id=?",
                day,
                streak,
                streak,
                uid,
            )
            self._entry(uid, amount, "visit", f"visit:{uid}:{day}", at)
            self._record(uid, "visits", at)
            return amount

    def _public_card(self, row):
        data = dict(row)
        data["answers"] = json.loads(data.pop("answers_json"))
        data.pop("correct", None)
        return data

    def _card(self, cid):
        row = self.db.one("SELECT * FROM star_cards WHERE id=?", cid)
        if not row:
            raise JourneyError("Карточка недоступна.")
        return row

    def _available(self, card, day):
        return card["status"] == "active" and card["start_day"] <= day <= card["end_day"]

    def _deck(self, uid, day):
        old = self.db.one("SELECT card_ids_json FROM star_decks WHERE user_id=? AND day=?", uid, day)
        if old:
            return json.loads(old["card_ids_json"])
        available = self.db.all(
            "SELECT id,category FROM star_cards WHERE status='active' AND start_day<=? AND end_day>=? "
            "AND id NOT IN (SELECT card_id FROM star_answers WHERE user_id=? AND answered_at IS NOT NULL)",
            day,
            day,
            uid,
        )
        ordered = sorted(available, key=lambda r: hashlib.sha256(f"{uid}:{day}:{r['id']}".encode()).digest())
        picked, categories = [], set()
        for row in ordered:
            if row["category"] not in categories and len(picked) < 3:
                picked.append(row["id"])
                categories.add(row["category"])
        for row in ordered:
            if len(picked) < 3 and row["id"] not in picked:
                picked.append(row["id"])
        self.db.exec("INSERT INTO star_decks(user_id,day,card_ids_json) VALUES(?,?,?)", uid, day, json.dumps(picked))
        return picked

    def deck(self, uid):
        with self.db.tx():
            self._ensure(uid)
            day = period("day", self.now())
            cards = []
            for cid in self._deck(uid, day):
                c = self._public_card(self._card(cid))
                c["available"] = self._available(c, day)
                answer = self.db.one("SELECT * FROM star_answers WHERE user_id=? AND card_id=?", uid, cid)
                c["answer"] = dict(answer) if answer else None
                cards.append(c)
            return {"day": day, "cards": cards}

    def open_card(self, uid, cid, expected_day):
        with self.db.tx():
            self._ensure(uid)
            day = period("day", self.now())
            c = self._card(cid)
            if expected_day != day or cid not in self._deck(uid, day) or not self._available(c, day):
                raise JourneyError("Карточка не входит в доступный маршрут дня. Обновите список.")
            self.db.exec(
                "INSERT INTO star_answers(user_id,card_id,opened_at) VALUES(?,?,?) ON CONFLICT(user_id,card_id) DO NOTHING",
                uid,
                cid,
                self.now(),
            )
            result = self._public_card(c)
            observe(self.db, uid, "card_open")
            result["answer"] = dict(self.db.one("SELECT * FROM star_answers WHERE user_id=? AND card_id=?", uid, cid))
            return result

    def answer(self, uid, cid, choice, revision, expected_day):
        with self.db.tx():
            at = self.now()
            if not self.db.one("SELECT 1 FROM star_answers WHERE user_id=? AND card_id=?", uid, cid):
                raise JourneyError("Сначала откройте карточку из маршрута дня.")
            card = self.open_card(uid, cid, expected_day)
            row = card["answer"]
            if type(choice) is not int or choice not in (0, 1, 2) or card["revision"] != revision:
                raise JourneyError("Откройте актуальную карточку и выберите ответ кнопкой.")
            if row["answered_at"] is not None:
                raise JourneyError("Эта карточка уже пройдена, искры получены.")
            if at < row["retry_at"]:
                raise JourneyError(f"Повторная попытка через {row['retry_at'] - at} сек.")
            correct = self._card(cid)["correct"]
            if choice != correct:
                attempts = (0 if row["attempts"] >= 3 else row["attempts"]) + 1
                retry = at + (86400 if attempts == 3 else 30)
                self.db.exec(
                    "UPDATE star_answers SET attempts=?,retry_at=? WHERE user_id=? AND card_id=?", attempts, retry, uid, cid
                )
                return {"correct": False, "retry_at": retry}
            self.db.exec("UPDATE star_answers SET answered_at=? WHERE user_id=? AND card_id=?", at, uid, cid)
            self._entry(uid, CARD_REWARD, "card", f"card:{uid}:{cid}", at)
            self._record(uid, "cards", at)
            return {"correct": True, "amount": CARD_REWARD}

    def _memory_public(self, row):
        data = dict(row)
        sequence = json.loads(data.pop("sequence_json"))
        if data["phase"] == "preview":
            data["sequence"] = sequence
        return data

    def memory(self, uid, expected_day):
        with self.db.tx():
            self._ensure(uid)
            at = self.now()
            day = period("day", at)
            if expected_day != day:
                raise JourneyError("День изменился. Откройте маршрут заново.")
            old = self.db.one("SELECT * FROM star_memory WHERE user_id=? AND day=?", uid, day)
            if old and (old["phase"] != "failed" or old["attempts"] >= 3 or at < old["retry_at"]):
                return self._memory_public(old)
            sequence = [secrets.randbelow(len(SYMBOLS)) for _ in range(3)]
            self.db.exec(
                "INSERT INTO star_memory(user_id,day,sequence_json,phase) VALUES(?,?,?,'preview') "
                "ON CONFLICT(user_id,day) DO UPDATE SET sequence_json=excluded.sequence_json,position=0,phase='preview',"
                "attempts=attempts+1,version=version+1,retry_at=0",
                uid,
                day,
                json.dumps(sequence),
            )
            return self._memory_public(self.db.one("SELECT * FROM star_memory WHERE user_id=? AND day=?", uid, day))

    def memory_step(self, uid, expected_day, version, symbol=None):
        with self.db.tx():
            self._ensure(uid)
            at = self.now()
            day = period("day", at)
            row = self.db.one("SELECT * FROM star_memory WHERE user_id=? AND day=?", uid, day)
            if expected_day != day or not row or row["version"] != version:
                raise JourneyError("Экран тренировки устарел. Откройте её заново.")
            if symbol is None and row["phase"] == "preview":
                self.db.exec("UPDATE star_memory SET phase='answer',version=version+1 WHERE user_id=? AND day=?", uid, day)
                return 0
            if row["phase"] != "answer" or type(symbol) is not int or not 0 <= symbol < len(SYMBOLS):
                raise JourneyError("Выберите действие на актуальном экране тренировки.")
            sequence = json.loads(row["sequence_json"])
            if symbol != sequence[row["position"]]:
                self.db.exec(
                    "UPDATE star_memory SET phase='failed',retry_at=?,version=version+1 WHERE user_id=? AND day=?",
                    at + 60,
                    uid,
                    day,
                )
                return -1
            position = row["position"] + 1
            done = position == len(sequence)
            self.db.exec(
                "UPDATE star_memory SET position=?,phase=?,version=version+1,completed_at=? WHERE user_id=? AND day=?",
                position,
                "done" if done else "answer",
                at if done else None,
                uid,
                day,
            )
            if done:
                self._entry(uid, MEMORY_REWARD, "memory", f"memory:{uid}:{day}", at)
                self._record(uid, "memory", at)
            return MEMORY_REWARD if done else 0

    def missions(self, uid, scope):
        if scope not in {"story", "day", "week"}:
            raise JourneyError("Выберите период миссий кнопкой.")
        with self.db.tx():
            self._ensure(uid)
            key = period(scope, self.now())
            values = self.counters(uid, key)
            rows = []
            for name, (kind, title, needed, reward) in MISSIONS.items():
                if kind == scope:
                    claimed = bool(
                        self.db.one("SELECT 1 FROM star_claims WHERE user_id=? AND mission_key=? AND period=?", uid, name, key)
                    )
                    rows.append(
                        {
                            "key": name,
                            "title": title,
                            "needed": needed,
                            "values": values,
                            "reward": reward,
                            "ready": all(values.get(metric, 0) >= amount for metric, amount in needed.items()),
                            "claimed": claimed,
                        }
                    )
            return {"period": key, "rows": rows}

    def claim(self, uid, name, expected_period):
        with self.db.tx():
            self._ensure(uid)
            if name not in MISSIONS:
                raise JourneyError("Миссия не найдена.")
            scope, _, needed, amount = MISSIONS[name]
            at = self.now()
            key = period(scope, at)
            values = self.counters(uid, key)
            if key != expected_period or not all(values.get(metric, 0) >= target for metric, target in needed.items()):
                raise JourneyError("Условия ещё не выполнены или период изменился. Обновите миссии.")
            if self.db.one("SELECT 1 FROM star_claims WHERE user_id=? AND mission_key=? AND period=?", uid, name, key):
                raise JourneyError("Награда за эту миссию уже получена.")
            self.db.exec(
                "INSERT INTO star_claims(user_id,mission_key,period,amount,claimed_at) VALUES(?,?,?,?,?)",
                uid,
                name,
                key,
                amount,
                at,
            )
            self._entry(uid, amount, "mission", f"mission:{uid}:{name}:{key}", at)
            return amount

    def build(self, uid, key, version):
        with self.db.tx():
            p, at = self._ensure(uid), self.now()
            item = CONSTELLATIONS.get(key)
            if not item or p["version"] != version:
                raise JourneyError("Карта изменилась. Откройте созвездия заново.")
            if self.db.one("SELECT 1 FROM star_constellations WHERE user_id=? AND key=?", uid, key):
                raise JourneyError("Это созвездие уже зажжено.")
            values = self.counters(uid)
            if p["sparks"] < item["cost"] or values.get("cards", 0) < item["cards"] or values.get("visits", 0) < item["days"]:
                raise JourneyError("Пока не хватает искр, карточек или дней участия. Условия указаны на карте.")
            self.db.exec("INSERT INTO star_constellations(user_id,key,cost,built_at) VALUES(?,?,?,?)", uid, key, item["cost"], at)
            self._entry(uid, -item["cost"], "build", f"build:{uid}:{key}", at)
            self._record(uid, "builds", at)

    def ranking(self, scope):
        if scope not in {"month", "all"}:
            raise JourneyError("Выберите период рейтинга кнопкой.")
        where, args = ("AND s.month=?", (period("month", self.now()),)) if scope == "month" else ("", ())
        return [
            dict(r)
            for r in self.db.all(
                "SELECT u.id,u.full_name,SUM(s.delta) AS score FROM star_sparks s JOIN users u ON u.id=s.user_id "
                f"WHERE s.delta>0 {where} GROUP BY u.id ORDER BY score DESC,u.id LIMIT 10",
                *args,
            )
        ]

    def _event(self, cid, actor, kind, revision):
        self.db.exec(
            "INSERT INTO star_card_events(card_id,actor_id,kind,revision,created_at) VALUES(?,?,?,?,?)",
            cid,
            actor,
            kind,
            revision,
            self.now(),
        )

    def _insert_card(self, actor, data):
        cur = self.db.exec(
            "INSERT INTO star_cards(category,title,lesson,question,answers_json,correct,start_day,days,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            data["category"],
            data["title"],
            data["lesson"],
            data["question"],
            json.dumps(data["answers"], ensure_ascii=False),
            data["correct"],
            data["starts"],
            data["days"],
            actor,
            self.now(),
        )
        return int(cur.lastrowid)

    def validate_field(self, field, value):
        if field in {"correct", "days"}:
            low, high = (0, 2) if field == "correct" else (1, 365)
            if isinstance(value, bool) or len(str(value)) > 3 or not str(value).isascii() or not str(value).isdigit():
                raise JourneyError(f"Введите целое число от {low} до {high}.")
            number = int(value)
            if not low <= number <= high:
                raise JourneyError(f"Введите целое число от {low} до {high}.")
            return number
        if field == "category":
            if value not in CATEGORIES:
                raise JourneyError("Выберите тему кнопкой.")
            return value
        if field == "answers":
            answers = value if isinstance(value, list) else str(value).splitlines()
            answers = [str(v).strip() for v in answers]
            if len(answers) != 3 or any(not v or len(v) > 80 for v in answers) or len({v.casefold() for v in answers}) != 3:
                raise JourneyError("Пришлите три разных ответа с новой строки, до 80 символов каждый.")
            return answers
        if field == "starts":
            today = day_date(period("day", self.now()))
            try:
                if value == "сегодня":
                    start = today
                elif isinstance(value, str) and len(value) == 8 and value.isascii() and value.isdigit():
                    start = day_date(value)
                else:
                    start = datetime.strptime(str(value), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                raise JourneyError("Дата: ГГГГ-ММ-ДД или «сегодня».") from None
            if not today <= start <= today + timedelta(days=365):
                raise JourneyError("Начало должно быть сегодня или в течение следующего года.")
            return start.strftime("%Y%m%d")
        maximum = {"title": 80, "lesson": 700, "question": 180}.get(field)
        if maximum is None or not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise JourneyError(f"Введите текст от 1 до {maximum or 80} символов.")
        return value.strip()

    def _validated(self, data):
        return {field: self.validate_field(field, data.get(field, "")) for field in EDITOR_FIELDS}

    def owner_card(self, actor, cid):
        self._owner(actor)
        c = dict(self._card(cid))
        c["answers"] = json.loads(c.pop("answers_json"))
        c["starts"] = c["start_day"]
        return c

    def list_cards(self, actor, page=0):
        self._owner(actor)
        if type(page) is not int or not 0 <= page <= 1000000:
            raise JourneyError("Откройте список заново.")
        total = self.db.scalar("SELECT COUNT(*) FROM star_cards")
        page = min(page, max(0, (total - 1) // 6))
        return {
            "page": page,
            "total": total,
            "rows": [
                self._public_card(r) for r in self.db.all("SELECT * FROM star_cards ORDER BY id DESC LIMIT 6 OFFSET ?", page * 6)
            ],
        }

    def create_draft(self, actor, data):
        with self.db.tx():
            self._owner(actor)
            cid = self._insert_card(actor, self._validated(data))
            self._event(cid, actor, "draft", 1)
            return cid

    def update_draft(self, actor, cid, revision, values):
        with self.db.tx():
            self._owner(actor)
            c = self._card(cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise JourneyError("Карточка уже изменилась или опубликована. Удалите устаревшую форму.")
            data = self._validated(values)
            self.db.exec(
                "UPDATE star_cards SET category=?,title=?,lesson=?,question=?,answers_json=?,correct=?,start_day=?,days=?,"
                "revision=revision+1 WHERE id=?",
                data["category"],
                data["title"],
                data["lesson"],
                data["question"],
                json.dumps(data["answers"], ensure_ascii=False),
                data["correct"],
                data["starts"],
                data["days"],
                cid,
            )
            self._event(cid, actor, "edit", revision + 1)

    def publish(self, actor, cid, revision):
        with self.db.tx():
            self._owner(actor)
            c = self.owner_card(actor, cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise JourneyError("Черновик изменился или уже опубликован. Откройте карточку заново.")
            data = self._validated(c)
            end = (day_date(data["starts"]) + timedelta(days=data["days"] - 1)).strftime("%Y%m%d")
            self.db.exec(
                "UPDATE star_cards SET status='active',end_day=?,published_at=?,revision=revision+1 WHERE id=?",
                end,
                self.now(),
                cid,
            )
            self._event(cid, actor, "publish", revision + 1)

    def set_status(self, actor, cid, status, revision):
        with self.db.tx():
            self._owner(actor)
            c = self._card(cid)
            if c["status"] == "draft" or c["status"] == status or status not in {"active", "paused"} or c["revision"] != revision:
                raise JourneyError("Статус изменился. Откройте карточку заново.")
            if status == "active" and c["end_day"] < period("day", self.now()):
                raise JourneyError("Срок истёк. Создайте копию на новый период.")
            self.db.exec("UPDATE star_cards SET status=?,revision=revision+1 WHERE id=?", status, cid)
            self._event(cid, actor, status, revision + 1)

    def copy_card(self, actor, cid):
        with self.db.tx():
            self._owner(actor)
            old = self.db.one("SELECT id FROM star_cards WHERE copy_of=? AND created_by=? AND status='draft'", cid, actor)
            if old:
                return old["id"]
            c = self.owner_card(actor, cid)
            c.update(title=("Копия: " + c["title"])[:80], starts=period("day", self.now()), days=min(c["days"] or 7, 365))
            copied = self.create_draft(actor, c)
            self.db.exec("UPDATE star_cards SET copy_of=? WHERE id=?", cid, copied)
            return copied

    def editor_get(self, actor):
        self._owner(actor)
        row = self.db.one("SELECT * FROM star_editor WHERE owner_id=?", actor)
        if not row:
            return None
        data = dict(row)
        data["data"] = json.loads(data.pop("payload_json"))
        return data

    def editor_begin(self, actor, cid=None, revision=None):
        with self.db.tx():
            self._owner(actor)
            old = self.editor_get(actor)
            if old:
                if cid is not None and old["card_id"] != cid:
                    raise JourneyError("Сначала завершите или удалите сохранённый мастер через «Продолжить».")
                self.db.exec("UPDATE star_editor SET active=1 WHERE owner_id=?", actor)
                return self.editor_get(actor)
            data, step = {}, "category"
            if cid is not None:
                c = self.owner_card(actor, cid)
                if c["status"] != "draft" or c["revision"] != revision:
                    raise JourneyError("Редактировать можно только актуальный неопубликованный черновик.")
                data, step = {field: c[field] for field in EDITOR_FIELDS}, "confirm"
            self.db.exec(
                "INSERT INTO star_editor(owner_id,card_id,card_revision,step,payload_json,updated_at) VALUES(?,?,?,?,?,?)",
                actor,
                cid,
                revision,
                step,
                json.dumps(data, ensure_ascii=False),
                self.now(),
            )
            return self.editor_get(actor)

    def editor_field(self, actor, field, version):
        with self.db.tx():
            self._owner(actor)
            draft = self.editor_get(actor)
            if field not in EDITOR_FIELDS or not draft or draft["step"] != "confirm" or draft["version"] != version:
                raise JourneyError("Откройте предпросмотр мастера заново.")
            self.db.exec("UPDATE star_editor SET step=?,edit_field=1,active=1,version=version+1 WHERE owner_id=?", field, actor)

    def editor_input(self, actor, value, version=None):
        with self.db.tx():
            self._owner(actor)
            draft = self.editor_get(actor)
            if (
                not draft
                or not draft["active"]
                or draft["step"] == "confirm"
                or (version is not None and version != draft["version"])
            ):
                raise JourneyError("Экран мастера устарел. Откройте сохранённую форму заново.")
            data, field = draft["data"], draft["step"]
            data[field] = self.validate_field(field, value)
            index = EDITOR_FIELDS.index(field) + 1
            step = EDITOR_FIELDS[index] if index < len(EDITOR_FIELDS) and not draft["edit_field"] else "confirm"
            self.db.exec(
                "UPDATE star_editor SET step=?,payload_json=?,edit_field=0,version=version+1,updated_at=? WHERE owner_id=?",
                step,
                json.dumps(data, ensure_ascii=False),
                self.now(),
                actor,
            )
            return self.editor_get(actor)

    def editor_pause(self, actor):
        with self.db.tx():
            self._owner(actor)
            self.db.exec("UPDATE star_editor SET active=0 WHERE owner_id=?", actor)

    def editor_discard(self, actor):
        with self.db.tx():
            self._owner(actor)
            self.db.exec("DELETE FROM star_editor WHERE owner_id=?", actor)

    def editor_save(self, actor, version):
        with self.db.tx():
            self._owner(actor)
            draft = self.editor_get(actor)
            if not draft or draft["step"] != "confirm" or draft["version"] != version:
                raise JourneyError("Откройте предпросмотр мастера заново.")
            data = self._validated(draft["data"])
            cid = draft["card_id"]
            if cid is None:
                cid = self.create_draft(actor, data)
            else:
                self.update_draft(actor, cid, draft["card_revision"], data)
            self.db.exec("DELETE FROM star_editor WHERE owner_id=?", actor)
            return cid

    def stats(self, actor):
        self._owner(actor)
        day = period("day", self.now())
        return {
            "players": self.db.scalar("SELECT COUNT(*) FROM star_profiles"),
            "cards": self.db.scalar(
                "SELECT COUNT(*) FROM star_cards WHERE status='active' AND start_day<=? AND end_day>=?", day, day
            ),
            "answers": self.db.scalar("SELECT COUNT(*) FROM star_answers WHERE answered_at IS NOT NULL"),
            "builds": self.db.scalar("SELECT COUNT(*) FROM star_constellations"),
        }
