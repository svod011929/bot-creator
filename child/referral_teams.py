"""Weekly cooperative club teams. Participation scores never credit any wallet or points ledger."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from child.engagement import observe

TZ = ZoneInfo("Europe/Moscow")
CAPACITY, GOAL, PERSONAL_GOAL, GRACE = 5, 15, 3, 7 * 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week TEXT NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','paused')),
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_ref_teams_week ON ref_teams(week,id);
CREATE TABLE IF NOT EXISTS ref_team_members (
    user_id INTEGER NOT NULL,
    week TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    quiz_baseline INTEGER NOT NULL,
    min_invite_id INTEGER NOT NULL,
    PRIMARY KEY(user_id,week)
);
CREATE INDEX IF NOT EXISTS ix_ref_team_members ON ref_team_members(team_id,user_id);
CREATE TABLE IF NOT EXISTS ref_team_checkins (
    user_id INTEGER NOT NULL,
    week TEXT NOT NULL,
    day TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,week,day)
);
CREATE TABLE IF NOT EXISTS ref_team_claims (
    user_id INTEGER NOT NULL,
    week TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    team_score INTEGER NOT NULL,
    personal_score INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,week)
);
CREATE INDEX IF NOT EXISTS ix_ref_team_claims ON ref_team_claims(team_id,user_id);
CREATE TABLE IF NOT EXISTS ref_team_forms (
    user_id INTEGER PRIMARY KEY,
    week TEXT NOT NULL,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_team_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(team_id,revision)
);
"""


class TeamError(Exception):
    """Safe message for a participant or the current bot owner."""


def week_window(at, offset=0):
    if type(offset) is not int or not 0 <= offset <= 52:
        raise TeamError("Выберите неделю заново.")
    date = datetime.fromtimestamp(at, TZ).date()
    monday = date - timedelta(days=date.weekday() + offset * 7)
    start = datetime.combine(monday, datetime.min.time(), TZ)
    year, week, _ = monday.isocalendar()
    return f"{year}W{week:02d}", int(start.timestamp()), int((start + timedelta(days=7)).timestamp())


class ReferralTeams:
    def __init__(self, db, *, owner_id, owner_check=None, clock=None, initialize=True):
        self.db, self.owner_id = db, owner_id
        self.owner_check = owner_check or (lambda uid: uid == owner_id)
        self.clock = clock or time.time
        if initialize:
            with db.lock:
                if db.conn.in_transaction:
                    raise RuntimeError("Initialize teams outside transactions")
                db.conn.executescript(SCHEMA)

    def _now(self):
        return int(self.clock())

    def _owner(self, uid):
        if not self.owner_check(uid):
            raise TeamError("Доступно только текущему владельцу бота.")

    def _eligible(self, uid):
        p = self.db.one("SELECT * FROM ref_club_members WHERE user_id=?", uid)
        if not self.db.user(uid) or not p or not p["onboarding_complete"] or not p["access_confirmed_at"]:
            raise TeamError("Сначала нажмите /start, пройдите знакомство и подтвердите доступ в «🚀 Клуб».")

    def _team(self, tid):
        row = self.db.one("SELECT * FROM ref_teams WHERE id=?", tid)
        if not row:
            raise TeamError("Команда не найдена.")
        return dict(row)

    def _current(self, team):
        return team["start_at"] <= self._now() < team["end_at"]

    def membership(self, uid, week=None):
        week = week or week_window(self._now())[0]
        row = self.db.one("SELECT * FROM ref_team_members WHERE user_id=? AND week=?", uid, week)
        return dict(row) if row else None

    def form_get(self, uid):
        row = self.db.one("SELECT * FROM ref_team_forms WHERE user_id=?", uid)
        return dict(row) if row else None

    def form_begin(self, uid):
        with self.db.tx():
            self._eligible(uid)
            week = week_window(self._now())[0]
            if self.membership(uid, week):
                raise TeamError("Ты уже в команде этой недели. Следующий состав можно выбрать в понедельник.")
            old = self.form_get(uid)
            if old and old["week"] == week:
                self.db.exec("UPDATE ref_team_forms SET active=1 WHERE user_id=?", uid)
                return self.form_get(uid)
            self.db.exec("DELETE FROM ref_team_forms WHERE user_id=?", uid)
            self.db.exec(
                "INSERT INTO ref_team_forms(user_id,week,version,updated_at) VALUES(?,?,?,?)",
                uid,
                week,
                secrets.randbelow(2**52) + 1,
                self._now(),
            )
            return self.form_get(uid)

    def _form(self, uid, version):
        self._eligible(uid)
        form = self.form_get(uid)
        if not form or form["version"] != version or form["week"] != week_window(self._now())[0]:
            raise TeamError("Форма изменилась или неделя закончилась. Откройте создание команды заново.")
        if self.membership(uid, form["week"]):
            raise TeamError("Ты уже выбрал команду на эту неделю.")
        return form

    def form_name(self, uid, name, version):
        with self.db.tx():
            form = self._form(uid, version)
            if form["name"] is not None:
                raise TeamError("Для нового названия нажмите «Изменить название».")
            if not isinstance(name, str):
                raise TeamError("Введите название команды.")
            name = name.strip()
            if not 1 <= len(name) <= 48 or any(ord(c) < 32 for c in name):
                raise TeamError("Название: от 1 до 48 символов, одной строкой.")
            if re.search(r"\d{5,}:[A-Za-z0-9_-]{20,}", name):
                raise TeamError("Удалите ключ доступа из названия.")
            self.db.exec(
                "UPDATE ref_team_forms SET name=?,version=version+1,updated_at=? WHERE user_id=?",
                name,
                self._now(),
                uid,
            )
            return self.form_get(uid)

    def form_edit(self, uid, version):
        with self.db.tx():
            self._form(uid, version)
            self.db.exec(
                "UPDATE ref_team_forms SET name=NULL,active=1,version=version+1,updated_at=? WHERE user_id=?",
                self._now(),
                uid,
            )

    def form_pause(self, uid):
        with self.db.tx():
            self.db.exec("UPDATE ref_team_forms SET active=0 WHERE user_id=? AND active=1", uid)

    def form_discard(self, uid, version):
        with self.db.tx():
            form = self.form_get(uid)
            if not form or form["version"] != version:
                raise TeamError("Форма уже изменилась. Откройте команды заново.")
            self.db.exec("DELETE FROM ref_team_forms WHERE user_id=?", uid)

    def _add_member(self, uid, team):
        if self.membership(uid, team["week"]):
            raise TeamError("На этой неделе состав уже выбран; смена команды откроется в понедельник.")
        baseline = self.db.scalar(
            "SELECT COUNT(*) FROM ref_campaign_answers WHERE user_id=? AND answered_at>=? AND answered_at<=?",
            uid,
            team["start_at"],
            self._now(),
        )
        anchor = self.db.scalar("SELECT COALESCE(MAX(id),0) FROM ref_club_invites WHERE inviter_id=?", uid)
        self.db.exec(
            "INSERT INTO ref_team_members(user_id,week,team_id,joined_at,quiz_baseline,min_invite_id) VALUES(?,?,?,?,?,?)",
            uid,
            team["week"],
            team["id"],
            self._now(),
            baseline,
            anchor,
        )
        self.db.exec("DELETE FROM ref_team_forms WHERE user_id=?", uid)
        observe(self.db, uid, "team_join")

    def create(self, uid, version):
        with self.db.tx():
            form = self._form(uid, version)
            if form["name"] is None:
                raise TeamError("Сначала введите название.")
            week, start, end = week_window(self._now())
            cur = self.db.exec(
                "INSERT INTO ref_teams(week,start_at,end_at,name,created_by,created_at) VALUES(?,?,?,?,?,?)",
                week,
                start,
                end,
                form["name"],
                uid,
                self._now(),
            )
            tid = int(cur.lastrowid)
            self._add_member(uid, self._team(tid))
            self._audit(uid, tid, "open", 1)
            return tid

    def join(self, uid, tid, revision):
        with self.db.tx():
            self._eligible(uid)
            team = self._team(tid)
            if not self._current(team) or team["status"] != "open" or team["revision"] != revision:
                raise TeamError("Вступление закрыто или карточка изменилась. Обновите список.")
            own = self.membership(uid, team["week"])
            if own and own["team_id"] == tid:
                return
            if self.db.scalar("SELECT COUNT(*) FROM ref_team_members WHERE team_id=?", tid) >= CAPACITY:
                raise TeamError("Все пять мест заняты. Выберите другую команду.")
            self._add_member(uid, team)

    def _metrics(self, team):
        cutoff = min(team["end_at"], self._now() + 1)
        return (
            """WITH members AS (
            SELECT m.*,
              (SELECT COUNT(*) FROM ref_team_checkins c WHERE c.user_id=m.user_id AND c.week=m.week
                 AND c.created_at>=m.joined_at AND c.created_at<?) AS visits,
              MAX(0,(SELECT COUNT(*) FROM ref_campaign_answers a WHERE a.user_id=m.user_id
                 AND a.answered_at>=? AND a.answered_at<?)-m.quiz_baseline) AS quizzes,
              (SELECT COUNT(*) FROM ref_club_invites i WHERE i.inviter_id=m.user_id AND i.id>m.min_invite_id
                 AND i.status='qualified' AND i.resolved_at>=m.joined_at AND i.resolved_at<?) AS friends
            FROM ref_team_members m WHERE m.week=?
        ), scores AS (
            SELECT *,visits+2*quizzes+3*friends AS score FROM members
        ), totals AS (
            SELECT team_id,COUNT(*) AS members,SUM(score) AS score FROM scores GROUP BY team_id
        ) """,
            (cutoff, team["start_at"], cutoff, cutoff, team["week"]),
        )

    def team(self, uid, tid):
        team = self._team(tid)
        sql, params = self._metrics(team)
        team["roster"] = [
            dict(r)
            for r in self.db.all(
                sql + "SELECT s.*,u.full_name FROM scores s JOIN users u ON u.id=s.user_id WHERE s.team_id=? "
                "ORDER BY s.score DESC,s.joined_at,s.user_id",
                *params,
                tid,
            )
        ]
        team["members"] = len(team["roster"])
        team["score"] = sum(m["score"] for m in team["roster"])
        team["mine"] = next((p for p in team["roster"] if p["user_id"] == uid), None)
        team["receipt"] = self.db.one("SELECT * FROM ref_team_claims WHERE user_id=? AND week=?", uid, team["week"])
        if team["receipt"]:
            team["receipt"] = dict(team["receipt"])
        day = datetime.fromtimestamp(self._now(), TZ).strftime("%Y%m%d")
        team["checked_today"] = bool(
            self.db.one(
                "SELECT 1 FROM ref_team_checkins WHERE user_id=? AND week=? AND day=?",
                uid,
                team["week"],
                day,
            )
        )
        return team

    def _page(self, week, start, end, page, *, ranking=False):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise TeamError("Откройте список команд заново.")
        total = self.db.scalar("SELECT COUNT(*) FROM ref_teams WHERE week=?", week)
        page = min(page, max(0, (total - 1) // 5))
        sql, params = self._metrics({"week": week, "start_at": start, "end_at": end})
        order = "score DESC,t.created_at,t.id" if ranking else "CASE t.status WHEN 'open' THEN 0 ELSE 1 END,t.id DESC"
        rows = self.db.all(
            sql + "SELECT t.*,COALESCE(p.members,0) AS members,COALESCE(p.score,0) AS score,"
            "(SELECT COUNT(*) FROM ref_team_claims c WHERE c.team_id=t.id) AS claims "
            f"FROM ref_teams t LEFT JOIN totals p ON p.team_id=t.id WHERE t.week=? ORDER BY {order} LIMIT 5 OFFSET ?",
            *params,
            week,
            page * 5,
        )
        return {"week": week, "start_at": start, "end_at": end, "page": page, "total": total, "rows": [dict(r) for r in rows]}

    def list_teams(self, uid, page=0, *, ranking=False):
        return self._page(*week_window(self._now()), page, ranking=ranking)

    def checkin(self, uid, tid):
        with self.db.tx():
            self._eligible(uid)
            team = self._team(tid)
            p = self.membership(uid, team["week"])
            if not self._current(team) or not p or p["team_id"] != tid or self._now() < p["joined_at"]:
                raise TeamError("Сегодняшний вклад доступен участнику команды в течение её недели.")
            day = datetime.fromtimestamp(self._now(), TZ).strftime("%Y%m%d")
            cur = self.db.exec(
                "INSERT INTO ref_team_checkins(user_id,week,day,created_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                uid,
                team["week"],
                day,
                self._now(),
            )
            if cur.rowcount:
                observe(self.db, uid, "team_checkin")
            return cur.rowcount == 1

    def claim(self, uid, tid):
        with self.db.tx():
            self._eligible(uid)
            team = self.team(uid, tid)
            mine = team["mine"]
            if not mine or team["receipt"] or self._now() < mine["joined_at"] or self._now() >= team["end_at"] + GRACE:
                raise TeamError("Значок уже получен, недоступен этой команде или срок получения закончился.")
            if team["members"] < 2 or team["score"] < GOAL or mine["score"] < PERSONAL_GOAL:
                raise TeamError("Нужно 15 командных очков, минимум два участника и твой личный вклад от 3 очков.")
            title = f"🤝 {team['name']} · {team['week']}"
            self.db.exec(
                "INSERT INTO ref_team_claims(user_id,week,team_id,title,team_score,personal_score,claimed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                uid,
                team["week"],
                tid,
                title,
                team["score"],
                mine["score"],
                self._now(),
            )
            observe(self.db, uid, "team_claim")
            return title

    def awards(self, uid, page=0):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise TeamError("Откройте значки заново.")
        total = self.db.scalar("SELECT COUNT(*) FROM ref_team_claims WHERE user_id=?", uid)
        page = min(page, max(0, (total - 1) // 8))
        return {
            "page": page,
            "total": total,
            "rows": [
                dict(r)
                for r in self.db.all(
                    "SELECT * FROM ref_team_claims WHERE user_id=? ORDER BY claimed_at DESC,week DESC LIMIT 8 OFFSET ?",
                    uid,
                    page * 8,
                )
            ],
        }

    def history(self, uid, page=0):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise TeamError("Откройте историю недель заново.")
        total = self.db.scalar("SELECT COUNT(*) FROM ref_team_members WHERE user_id=?", uid)
        page = min(page, max(0, (total - 1) // 8))
        return {
            "page": page,
            "total": total,
            "rows": [
                dict(r)
                for r in self.db.all(
                    "SELECT t.*,c.claimed_at FROM ref_team_members m JOIN ref_teams t ON t.id=m.team_id "
                    "LEFT JOIN ref_team_claims c ON c.user_id=m.user_id AND c.week=m.week "
                    "WHERE m.user_id=? ORDER BY t.start_at DESC,t.id DESC LIMIT 8 OFFSET ?",
                    uid,
                    page * 8,
                )
            ],
        }

    def owner_list(self, uid, offset=0, page=0):
        self._owner(uid)
        return {**self._page(*week_window(self._now(), offset), page, ranking=True), "offset": offset}

    def owner_team(self, uid, tid):
        self._owner(uid)
        return self.team(uid, tid)

    def _audit(self, actor, tid, status, revision):
        self.db.exec(
            "INSERT INTO ref_team_audit(team_id,actor_id,status,revision,created_at) VALUES(?,?,?,?,?)",
            tid,
            actor,
            status,
            revision,
            self._now(),
        )

    def set_status(self, uid, tid, status, revision):
        with self.db.tx():
            self._owner(uid)
            team = self._team(tid)
            if (
                status not in {"open", "paused"}
                or team["status"] == status
                or team["revision"] != revision
                or not self._current(team)
            ):
                raise TeamError("Откройте актуальную карточку команды текущей недели.")
            self.db.exec("UPDATE ref_teams SET status=?,revision=revision+1 WHERE id=?", status, tid)
            self._audit(uid, tid, status, revision + 1)
