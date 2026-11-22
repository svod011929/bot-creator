"""Referral campaigns, onboarding and a separate non-cash club points ledger."""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Callable

from child.db import ChildDB

INTERESTS = {"learn": "📚 Узнавать новое", "events": "🎉 События", "tasks": "🎯 Задания"}
ONBOARD_OPTIONS = ("Гарантированный денежный доход", "Прогресс участия, отдельно от денежного баланса", "Баланс Telegram Stars")
ONBOARD_ANSWER = 1
WIZARD_FIELDS = ("title", "description", "goal", "reward", "quota", "days", "question", "answers", "correct")
SEED = {
    "title": "Первые амбассадоры",
    "description": "Познакомься с клубом, выполни задание и пригласи новых участников. "
    "Цель — три подтверждённых приглашения и значок кампании. Клубные баллы не являются деньгами.",
    "goal": 3,
    "reward": 25,
    "quota": 1000,
    "days": 0,
    "question": "Какой участник засчитывается в кампании?",
    "answers": ["Любой переход по ссылке", "Новый участник, прошедший знакомство и задание", "Повторный запуск одним аккаунтом"],
    "correct": 1,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_club_members (
    user_id INTEGER PRIMARY KEY,
    points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0),
    interest TEXT,
    onboarding_complete INTEGER NOT NULL DEFAULT 0,
    access_confirmed_at INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    goal INTEGER NOT NULL,
    reward INTEGER NOT NULL,
    quota INTEGER NOT NULL,
    days INTEGER NOT NULL,
    question TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    correct INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused')),
    revision INTEGER NOT NULL DEFAULT 1,
    qualified_count INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    ends_at INTEGER NOT NULL DEFAULT 0,
    copy_of INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ref_campaign_status ON ref_campaigns(status,id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_campaign_copy ON ref_campaigns(copy_of,created_by)
    WHERE copy_of IS NOT NULL AND status='draft';
CREATE TABLE IF NOT EXISTS ref_club_links (
    token TEXT PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(campaign_id,inviter_id)
);
CREATE TABLE IF NOT EXISTS ref_club_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    invitee_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'joined' CHECK(status IN ('joined','qualified','rejected')),
    reason TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ref_club_inviter ON ref_club_invites(inviter_id,id);
CREATE INDEX IF NOT EXISTS ix_ref_club_campaign ON ref_club_invites(campaign_id,status);
CREATE TABLE IF NOT EXISTS ref_campaign_answers (
    user_id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER NOT NULL,
    answered_at INTEGER,
    PRIMARY KEY(user_id,campaign_id)
);
CREATE TABLE IF NOT EXISTS ref_club_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount>0),
    kind TEXT NOT NULL CHECK(kind IN ('onboarding','quiz','referral')),
    source_key TEXT NOT NULL UNIQUE,
    campaign_id INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ref_club_points_user ON ref_club_points(user_id,id);
CREATE TABLE IF NOT EXISTS ref_club_badges (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    awarded_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,key)
);
CREATE TABLE IF NOT EXISTS ref_club_drafts (
    owner_id INTEGER PRIMARY KEY,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    campaign_id INTEGER,
    campaign_revision INTEGER,
    edit_field INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_campaign_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(campaign_id,revision)
);
"""


class ClubError(Exception):
    """Safe public message, without identifiers or secret values."""


def validate_field(field: str, value):
    if field in {"goal", "reward", "quota", "days", "correct"}:
        limits = {"goal": (1, 1000), "reward": (1, 1000), "quota": (1, 100000), "days": (1, 365), "correct": (0, 2)}
        low, high = limits[field]
        if isinstance(value, bool) or len(str(value)) > 6 or not str(value).isascii() or not str(value).isdigit():
            raise ClubError(f"Введите целое число от {low} до {high}.")
        number = int(value)
        if not low <= number <= high:
            raise ClubError(f"Введите целое число от {low} до {high}.")
        return number
    if field == "answers":
        values = value if isinstance(value, list) else str(value).splitlines()
        values = [str(v).strip() for v in values]
        if len(values) != 3 or any(not v or len(v) > 80 for v in values) or len({v.casefold() for v in values}) != 3:
            raise ClubError("Нужны три разных ответа, каждый с новой строки, до 80 символов.")
        return values
    maximum = {"title": 80, "description": 900, "question": 180}.get(field)
    if maximum is None or not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ClubError(f"Введите текст от 1 до {maximum or 80} символов.")
    return value.strip()


def validate_campaign(data: dict) -> dict:
    result = {field: validate_field(field, data.get(field, "")) for field in WIZARD_FIELDS}
    if result["quota"] < result["goal"]:
        raise ClubError("Лимит подтверждений должен быть не меньше цели кампании.")
    return result


class ReferralClub:
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
                raise RuntimeError("Initialize club outside transactions")
            db.conn.executescript(SCHEMA)
        with db.tx():
            if not db.one("SELECT 1 FROM ref_campaigns LIMIT 1"):
                cid = self._insert_campaign(owner_id, SEED)
                db.exec("UPDATE ref_campaigns SET status='active',published_at=? WHERE id=?", self._now(), cid)
                self._event(cid, owner_id, "starter", 1)

    def _now(self) -> int:
        return int(self.clock())

    def _owner(self, actor: int) -> None:
        if not self.owner_check(actor):
            raise ClubError("Доступно только текущему владельцу бота.")

    def _ensure(self, uid: int) -> dict:
        if not self.db.user(uid):
            raise ClubError("Сначала нажмите /start в личном чате с ботом.")
        self.db.exec(
            "INSERT INTO ref_club_members(user_id,joined_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING", uid, self._now()
        )
        return dict(self.db.one("SELECT * FROM ref_club_members WHERE user_id=?", uid))

    def _points(self, uid: int, amount: int, kind: str, source: str, campaign: int | None = None) -> None:
        self._ensure(uid)
        self.db.exec(
            "INSERT INTO ref_club_points(user_id,amount,kind,source_key,campaign_id,created_at) VALUES(?,?,?,?,?,?)",
            uid,
            amount,
            kind,
            source,
            campaign,
            self._now(),
        )
        self.db.exec("UPDATE ref_club_members SET points=points+? WHERE user_id=?", amount, uid)

    def _badge(self, uid: int, key: str, title: str) -> None:
        self.db.exec(
            "INSERT INTO ref_club_badges(user_id,key,title,awarded_at) VALUES(?,?,?,?) ON CONFLICT(user_id,key) DO NOTHING",
            uid,
            key,
            title,
            self._now(),
        )

    def _event(self, cid: int, actor: int, kind: str, revision: int) -> None:
        self.db.exec(
            "INSERT INTO ref_campaign_events(campaign_id,actor_id,kind,revision,created_at) VALUES(?,?,?,?,?)",
            cid,
            actor,
            kind,
            revision,
            self._now(),
        )

    @staticmethod
    def _public(row) -> dict:
        result = dict(row)
        result["answers"] = json.loads(result.pop("answers_json"))
        result.pop("correct", None)
        return result

    def _campaign(self, cid: int):
        row = self.db.one("SELECT * FROM ref_campaigns WHERE id=?", cid)
        if not row:
            raise ClubError("Кампания не найдена.")
        return row

    def _open(self, row) -> bool:
        return row["status"] == "active" and (not row["ends_at"] or self._now() < row["ends_at"])

    def join(self, uid: int, username: str | None, name: str, payload: str) -> bool:
        """First-touch legacy registration and new campaign attribution share one SQLite transaction."""
        with self.db.tx():
            link, referrer = None, None
            if re.fullmatch(r"c_[A-Za-z0-9_-]{16}", payload):
                link = self.db.one(
                    "SELECT l.*,c.status,c.ends_at,c.qualified_count,c.quota FROM ref_club_links l "
                    "JOIN ref_campaigns c ON c.id=l.campaign_id WHERE l.token=?",
                    payload[2:],
                )
                if link and self._open(link) and link["qualified_count"] < link["quota"] and link["inviter_id"] != uid:
                    referrer = link["inviter_id"]
                else:
                    link = None
            elif re.fullmatch(r"r[0-9]{1,18}", payload):
                referrer = int(payload[1:])
            fresh = self.db.upsert_user(uid, username, name, referrer)
            self._ensure(uid)
            if fresh and link and self.db.user(uid)["referrer_id"] == referrer:
                self.db.exec(
                    "INSERT INTO ref_club_invites(campaign_id,inviter_id,invitee_id,created_at) VALUES(?,?,?,?)",
                    link["campaign_id"],
                    referrer,
                    uid,
                    self._now(),
                )
            return fresh

    def confirm_access(self, uid: int) -> bool:
        with self.db.tx():
            self._ensure(uid)
            self.db.exec("UPDATE ref_club_members SET access_confirmed_at=? WHERE user_id=?", self._now(), uid)
            return self._qualify(uid)

    def clear_access(self, uid: int) -> None:
        with self.db.tx():
            self._ensure(uid)
            self.db.exec("UPDATE ref_club_members SET access_confirmed_at=0 WHERE user_id=?", uid)

    def choose_interest(self, uid: int, key: str, version: int) -> None:
        if key not in INTERESTS:
            raise ClubError("Выберите интерес кнопкой в знакомстве.")
        with self.db.tx():
            p = self._ensure(uid)
            if p["onboarding_complete"] or p["version"] != version:
                raise ClubError("Экран устарел или знакомство уже завершено. Откройте клуб заново.")
            self.db.exec("UPDATE ref_club_members SET interest=?,version=version+1 WHERE user_id=?", key, uid)

    def finish_onboarding(self, uid: int, choice: int, version: int) -> None:
        with self.db.tx():
            p = self._ensure(uid)
            if p["onboarding_complete"] or p["version"] != version or not p["interest"]:
                raise ClubError("Откройте знакомство заново.")
            if type(choice) is not int or choice != ONBOARD_ANSWER:
                raise ClubError("Баллы отражают участие в клубе. Прочитайте условия и выберите ответ ещё раз.")
            self.db.exec("UPDATE ref_club_members SET onboarding_complete=1,version=version+1 WHERE user_id=?", uid)
            self._points(uid, 20, "onboarding", f"onboarding:{uid}")
            self._badge(uid, "welcome", "👋 Участник клуба")
            self._qualify(uid)

    def _qualify(self, uid: int) -> bool:
        invite = self.db.one("SELECT * FROM ref_club_invites WHERE invitee_id=? AND status='joined'", uid)
        if not invite:
            return False
        c = self._campaign(invite["campaign_id"])
        reason = None
        if c["ends_at"] and self._now() >= c["ends_at"]:
            reason = "Срок кампании истёк"
        elif self.db.user(uid)["referrer_id"] != invite["inviter_id"]:
            reason = "Пригласивший не совпал с первым источником входа"
        if reason:
            self.db.exec(
                "UPDATE ref_club_invites SET status='rejected',reason=?,resolved_at=? WHERE id=?",
                reason,
                self._now(),
                invite["id"],
            )
            return False
        p = self._ensure(uid)
        answer = self.db.one("SELECT answered_at FROM ref_campaign_answers WHERE user_id=? AND campaign_id=?", uid, c["id"])
        if not p["onboarding_complete"] or not p["access_confirmed_at"] or not answer or answer["answered_at"] is None:
            return False
        if c["status"] not in {"active", "paused"}:
            return False
        quota = self.db.exec(
            "UPDATE ref_campaigns SET qualified_count=qualified_count+1 WHERE id=? AND qualified_count<quota", c["id"]
        )
        if not quota.rowcount:
            self.db.exec(
                "UPDATE ref_club_invites SET status='rejected',reason=?,resolved_at=? WHERE id=?",
                "Лимит подтверждений исчерпан",
                self._now(),
                invite["id"],
            )
            return False
        self.db.exec("UPDATE ref_club_invites SET status='qualified',resolved_at=? WHERE id=?", self._now(), invite["id"])
        self._points(invite["inviter_id"], c["reward"], "referral", f"referral:{invite['id']}", c["id"])
        total = self.db.scalar(
            "SELECT COUNT(*) FROM ref_club_invites WHERE inviter_id=? AND status='qualified'", invite["inviter_id"]
        )
        for target, key, title in [
            (1, "first", "🌱 Первое подтверждение"),
            (3, "ambassador", "🚀 Амбассадор"),
            (10, "guide", "🧭 Проводник"),
        ]:
            if total >= target:
                self._badge(invite["inviter_id"], key, title)
        campaign_count = self.db.scalar(
            "SELECT COUNT(*) FROM ref_club_invites WHERE inviter_id=? AND campaign_id=? AND status='qualified'",
            invite["inviter_id"],
            c["id"],
        )
        if campaign_count >= c["goal"]:
            self._badge(invite["inviter_id"], f"goal:{c['id']}", f"🏅 Цель: {c['title']}")
        return True

    def summary(self, uid: int) -> dict:
        with self.db.tx():
            member = self._ensure(uid)
            qualified = self.db.scalar("SELECT COUNT(*) FROM ref_club_invites WHERE inviter_id=? AND status='qualified'", uid)
            joined = self.db.scalar(
                "SELECT COUNT(*) FROM ref_club_invites i JOIN ref_campaigns c ON c.id=i.campaign_id "
                "WHERE inviter_id=? AND i.status='joined' AND (c.ends_at=0 OR c.ends_at>?)",
                uid,
                self._now(),
            )
            invitation = self.db.one(
                "SELECT i.*,c.title,c.ends_at FROM ref_club_invites i JOIN ref_campaigns c ON c.id=i.campaign_id "
                "WHERE invitee_id=?",
                uid,
            )
            badges = [
                dict(r) for r in self.db.all("SELECT key,title FROM ref_club_badges WHERE user_id=? ORDER BY awarded_at,key", uid)
            ]
            history = [
                dict(r)
                for r in self.db.all("SELECT amount,kind FROM ref_club_points WHERE user_id=? ORDER BY id DESC LIMIT 5", uid)
            ]
            return {
                "member": member,
                "qualified": qualified,
                "pending": joined,
                "badges": badges,
                "history": history,
                "invitation": dict(invitation) if invitation else None,
            }

    def personal_link(self, uid: int, cid: int) -> str:
        with self.db.tx():
            p, c = self._ensure(uid), self._campaign(cid)
            if not p["onboarding_complete"]:
                raise ClubError("Сначала завершите знакомство с клубом.")
            if not self._open(c) or c["qualified_count"] >= c["quota"]:
                raise ClubError("Кампания сейчас не принимает новых участников.")
            old = self.db.one("SELECT token FROM ref_club_links WHERE campaign_id=? AND inviter_id=?", cid, uid)
            if old:
                return old["token"]
            token = secrets.token_urlsafe(12)
            self.db.exec(
                "INSERT INTO ref_club_links(token,campaign_id,inviter_id,created_at) VALUES(?,?,?,?)",
                token,
                cid,
                uid,
                self._now(),
            )
            return token

    def campaign(self, uid: int, cid: int) -> dict:
        c = self._campaign(cid)
        if c["status"] == "draft":
            raise ClubError("Кампания пока не опубликована.")
        result = self._public(c)
        result["my_qualified"] = self.db.scalar(
            "SELECT COUNT(*) FROM ref_club_invites WHERE inviter_id=? AND campaign_id=? AND status='qualified'", uid, cid
        )
        answer = self.db.one("SELECT * FROM ref_campaign_answers WHERE user_id=? AND campaign_id=?", uid, cid)
        result["my_answer"] = dict(answer) if answer else None
        return result

    def list_campaigns(self, uid: int, *, owner: bool = False, page: int = 0) -> dict:
        if type(page) is not int or not 0 <= page <= 1000000:
            raise ClubError("Откройте список кампаний заново.")
        if owner:
            self._owner(uid)
        where = "1=1" if owner else "status<>'draft'"
        total = self.db.scalar(f"SELECT COUNT(*) FROM ref_campaigns WHERE {where}")
        page = min(page, max(0, (total - 1) // 5))
        rows = self.db.all(f"SELECT * FROM ref_campaigns WHERE {where} ORDER BY id DESC LIMIT 5 OFFSET ?", page * 5)
        return {"rows": [self._public(r) for r in rows], "total": total, "page": page}

    def open_quiz(self, uid: int, cid: int) -> dict:
        with self.db.tx():
            p, c = self._ensure(uid), self._campaign(cid)
            if not p["onboarding_complete"]:
                raise ClubError("Сначала завершите знакомство с клубом.")
            existing = self.db.one("SELECT 1 FROM ref_campaign_answers WHERE user_id=? AND campaign_id=?", uid, cid)
            invited = self.db.one("SELECT 1 FROM ref_club_invites WHERE invitee_id=? AND campaign_id=?", uid, cid)
            allowed = self._open(c) or (c["status"] == "paused" and (existing or invited))
            if not allowed or (c["ends_at"] and self._now() >= c["ends_at"]):
                raise ClubError("Задание сейчас недоступно: проверьте статус и срок кампании.")
            self.db.exec(
                "INSERT INTO ref_campaign_answers(user_id,campaign_id,started_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id,campaign_id) DO NOTHING",
                uid,
                cid,
                self._now(),
            )
            return self._public(c)

    def answer(self, uid: int, cid: int, choice: int, revision: int) -> dict:
        with self.db.tx():
            p, c = self._ensure(uid), self._campaign(cid)
            answer = self.db.one("SELECT * FROM ref_campaign_answers WHERE user_id=? AND campaign_id=?", uid, cid)
            if not answer or not p["onboarding_complete"] or c["revision"] != revision:
                raise ClubError("Откройте задание кампании заново.")
            if c["status"] not in {"active", "paused"} or (c["ends_at"] and self._now() >= c["ends_at"]):
                raise ClubError("Срок задания истёк или кампания недоступна.")
            if answer["answered_at"] is not None:
                raise ClubError("Задание уже выполнено, баллы получены.")
            if type(choice) is not int or not 0 <= choice <= 2:
                raise ClubError("Выберите один из ответов кнопкой.")
            if self._now() < answer["retry_at"]:
                raise ClubError(f"Следующая попытка через {answer['retry_at'] - self._now()} сек.")
            attempts = 0 if answer["attempts"] >= 3 else answer["attempts"]
            if choice != c["correct"]:
                attempts += 1
                retry = self._now() + (86400 if attempts == 3 else 30)
                self.db.exec(
                    "UPDATE ref_campaign_answers SET attempts=?,retry_at=? WHERE user_id=? AND campaign_id=?",
                    attempts,
                    retry,
                    uid,
                    cid,
                )
                return {"correct": False, "retry_at": retry}
            self.db.exec("UPDATE ref_campaign_answers SET answered_at=? WHERE user_id=? AND campaign_id=?", self._now(), uid, cid)
            self._points(uid, 10, "quiz", f"quiz:{cid}:{uid}", cid)
            return {"correct": True, "qualified": self._qualify(uid)}

    def invites(self, uid: int, page: int = 0) -> dict:
        if type(page) is not int or not 0 <= page <= 1000000:
            raise ClubError("Откройте список приглашений заново.")
        total = self.db.scalar("SELECT COUNT(*) FROM ref_club_invites WHERE inviter_id=?", uid)
        page = min(page, max(0, (total - 1) // 8))
        rows = self.db.all(
            "SELECT i.*,u.full_name,c.title,c.ends_at,m.onboarding_complete,a.answered_at FROM ref_club_invites i "
            "JOIN users u ON u.id=i.invitee_id JOIN ref_campaigns c ON c.id=i.campaign_id "
            "LEFT JOIN ref_club_members m ON m.user_id=i.invitee_id "
            "LEFT JOIN ref_campaign_answers a ON a.user_id=i.invitee_id AND a.campaign_id=i.campaign_id "
            "WHERE i.inviter_id=? ORDER BY i.id DESC LIMIT 8 OFFSET ?",
            uid,
            page * 8,
        )
        return {"rows": [dict(r) for r in rows], "total": total, "page": page}

    def ranking(self) -> list[dict]:
        return [
            dict(r)
            for r in self.db.all(
                "SELECT m.user_id,u.full_name,m.points,COUNT(i.id) AS qualified "
                "FROM ref_club_members m JOIN users u ON u.id=m.user_id "
                "LEFT JOIN ref_club_invites i ON i.inviter_id=m.user_id AND i.status='qualified' "
                "GROUP BY m.user_id HAVING m.points>0 ORDER BY qualified DESC,m.points DESC,m.user_id LIMIT 10"
            )
        ]

    def _insert_campaign(self, actor: int, data: dict) -> int:
        cur = self.db.exec(
            "INSERT INTO ref_campaigns(title,description,goal,reward,quota,days,question,answers_json,correct,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            data["title"],
            data["description"],
            data["goal"],
            data["reward"],
            data["quota"],
            data["days"],
            data["question"],
            json.dumps(data["answers"], ensure_ascii=False),
            data["correct"],
            actor,
            self._now(),
        )
        return int(cur.lastrowid)

    def owner_campaign(self, actor: int, cid: int) -> dict:
        self._owner(actor)
        c = dict(self._campaign(cid))
        c["answers"] = json.loads(c.pop("answers_json"))
        return c

    def create_draft(self, actor: int, data: dict) -> int:
        validated = validate_campaign(data)
        with self.db.tx():
            self._owner(actor)
            cid = self._insert_campaign(actor, validated)
            self._event(cid, actor, "draft", 1)
            return cid

    def update_draft(self, actor: int, cid: int, revision: int, values: dict) -> None:
        with self.db.tx():
            self._owner(actor)
            c = self._campaign(cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise ClubError("Кампания уже изменилась или опубликована. Удалите устаревший мастер.")
            data = validate_campaign(values)
            self.db.exec(
                "UPDATE ref_campaigns SET title=?,description=?,goal=?,reward=?,quota=?,days=?,question=?,"
                "answers_json=?,correct=?,revision=revision+1 WHERE id=?",
                data["title"],
                data["description"],
                data["goal"],
                data["reward"],
                data["quota"],
                data["days"],
                data["question"],
                json.dumps(data["answers"], ensure_ascii=False),
                data["correct"],
                cid,
            )
            self._event(cid, actor, "edit", revision + 1)

    def publish(self, actor: int, cid: int, revision: int) -> None:
        with self.db.tx():
            self._owner(actor)
            c = self._campaign(cid)
            if c["status"] != "draft" or c["revision"] != revision:
                raise ClubError("Черновик изменился или уже опубликован. Откройте карточку заново.")
            ends = self._now() + c["days"] * 86400
            self.db.exec(
                "UPDATE ref_campaigns SET status='active',published_at=?,ends_at=?,revision=revision+1 WHERE id=?",
                self._now(),
                ends,
                cid,
            )
            self._event(cid, actor, "publish", revision + 1)

    def set_status(self, actor: int, cid: int, status: str, revision: int) -> None:
        with self.db.tx():
            self._owner(actor)
            c = self._campaign(cid)
            if status not in {"active", "paused"} or c["status"] == "draft" or c["revision"] != revision:
                raise ClubError("Откройте карточку кампании заново.")
            if c["status"] == status:
                raise ClubError("Этот статус уже установлен.")
            if status == "active" and c["ends_at"] and self._now() >= c["ends_at"]:
                raise ClubError("Срок истёк. Скопируйте кампанию в новый черновик.")
            self.db.exec("UPDATE ref_campaigns SET status=?,revision=revision+1 WHERE id=?", status, cid)
            self._event(cid, actor, status, revision + 1)

    def copy_draft(self, actor: int, cid: int) -> int:
        with self.db.tx():
            self._owner(actor)
            existing = self.db.one("SELECT id FROM ref_campaigns WHERE copy_of=? AND created_by=? AND status='draft'", cid, actor)
            if existing:
                return existing["id"]
            data = self.owner_campaign(actor, cid)
            data["title"] = ("Копия: " + data["title"])[:80]
            data["days"] = data["days"] or 7
            copied = self.create_draft(actor, data)
            self.db.exec("UPDATE ref_campaigns SET copy_of=? WHERE id=?", cid, copied)
            return copied

    def wizard_get(self, actor: int):
        self._owner(actor)
        row = self.db.one("SELECT * FROM ref_club_drafts WHERE owner_id=?", actor)
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("payload_json"))
        return result

    def wizard_begin(self, actor: int) -> dict:
        with self.db.tx():
            self._owner(actor)
            self.db.exec(
                "INSERT INTO ref_club_drafts(owner_id,step,payload_json,updated_at) VALUES(?,'title','{}',?) "
                "ON CONFLICT(owner_id) DO UPDATE SET active=1",
                actor,
                self._now(),
            )
            return self.wizard_get(actor)

    def wizard_edit_campaign(self, actor: int, cid: int, revision: int) -> dict:
        with self.db.tx():
            self._owner(actor)
            campaign = self.owner_campaign(actor, cid)
            if campaign["status"] != "draft" or campaign["revision"] != revision:
                raise ClubError("Редактировать можно только актуальный неопубликованный черновик.")
            existing = self.wizard_get(actor)
            if existing:
                if existing["campaign_id"] != cid:
                    raise ClubError("Сначала завершите или удалите сохранённый мастер через «Продолжить мастер».")
                self.db.exec("UPDATE ref_club_drafts SET active=1 WHERE owner_id=?", actor)
                return self.wizard_get(actor)
            data = {field: campaign[field] for field in WIZARD_FIELDS}
            self.db.exec(
                "INSERT INTO ref_club_drafts(owner_id,step,payload_json,campaign_id,campaign_revision,updated_at) "
                "VALUES(?,'confirm',?,?,?,?)",
                actor,
                json.dumps(data, ensure_ascii=False),
                cid,
                revision,
                self._now(),
            )
            return self.wizard_get(actor)

    def wizard_edit_field(self, actor: int, field: str, version: int) -> dict:
        with self.db.tx():
            self._owner(actor)
            draft = self.wizard_get(actor)
            if field not in WIZARD_FIELDS or not draft or draft["step"] != "confirm" or draft["version"] != version:
                raise ClubError("Откройте предпросмотр мастера заново.")
            self.db.exec(
                "UPDATE ref_club_drafts SET step=?,edit_field=1,active=1,version=version+1,updated_at=? WHERE owner_id=?",
                field,
                self._now(),
                actor,
            )
            return self.wizard_get(actor)

    def wizard_input(self, actor: int, value, *, expected_version: int | None = None) -> dict:
        with self.db.tx():
            self._owner(actor)
            draft = self.wizard_get(actor)
            if not draft or not draft["active"] or draft["step"] == "confirm":
                raise ClubError("Откройте мастер создания кампании заново.")
            if expected_version is not None and expected_version != draft["version"]:
                raise ClubError("Черновик изменился. Откройте мастер заново.")
            field = draft["step"]
            data = draft["data"]
            data[field] = validate_field(field, value)
            if "goal" in data and "quota" in data and data["quota"] < data["goal"]:
                raise ClubError("Лимит должен быть не меньше цели кампании.")
            position = WIZARD_FIELDS.index(field) + 1
            step = WIZARD_FIELDS[position] if position < len(WIZARD_FIELDS) else "confirm"
            if draft["edit_field"]:
                step = "confirm"
            self.db.exec(
                "UPDATE ref_club_drafts SET step=?,payload_json=?,edit_field=0,version=version+1,updated_at=? WHERE owner_id=?",
                step,
                json.dumps(data, ensure_ascii=False),
                self._now(),
                actor,
            )
            return self.wizard_get(actor)

    def wizard_pause(self, actor: int) -> None:
        with self.db.tx():
            self._owner(actor)
            self.db.exec("UPDATE ref_club_drafts SET active=0 WHERE owner_id=?", actor)

    def wizard_discard(self, actor: int) -> None:
        with self.db.tx():
            self._owner(actor)
            self.db.exec("DELETE FROM ref_club_drafts WHERE owner_id=?", actor)

    def wizard_save(self, actor: int, version: int) -> int:
        with self.db.tx():
            self._owner(actor)
            draft = self.wizard_get(actor)
            if not draft or draft["version"] != version or draft["step"] != "confirm":
                raise ClubError("Откройте предпросмотр черновика заново.")
            if draft["campaign_id"] is None:
                cid = self.create_draft(actor, draft["data"])
            else:
                cid = draft["campaign_id"]
                self.update_draft(actor, cid, draft["campaign_revision"], draft["data"])
            self.db.exec("DELETE FROM ref_club_drafts WHERE owner_id=?", actor)
            return cid

    def stats(self, actor: int) -> dict:
        self._owner(actor)
        return {
            "members": self.db.scalar("SELECT COUNT(*) FROM ref_club_members"),
            "active": self.db.scalar(
                "SELECT COUNT(*) FROM ref_campaigns WHERE status='active' AND (ends_at=0 OR ends_at>?)", self._now()
            ),
            "drafts": self.db.scalar("SELECT COUNT(*) FROM ref_campaigns WHERE status='draft'"),
            "qualified": self.db.scalar("SELECT COUNT(*) FROM ref_club_invites WHERE status='qualified'"),
            "answers": self.db.scalar("SELECT COUNT(*) FROM ref_campaign_answers WHERE answered_at IS NOT NULL"),
        }
