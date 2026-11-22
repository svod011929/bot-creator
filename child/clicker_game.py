"""Clicker adventure v1: deterministic, server-timed gameplay with a separate non-cash XP ledger."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from child.db import ChildDB
from child.engagement import observe

GAME_TZ = ZoneInfo("Europe/Moscow")
ENERGY_MAX = 5
ENERGY_INTERVAL = 20 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS clicker_profiles (
    user_id INTEGER PRIMARY KEY,
    xp_total INTEGER NOT NULL DEFAULT 0 CHECK(xp_total>=0),
    xp_available INTEGER NOT NULL DEFAULT 0 CHECK(xp_available>=0),
    energy INTEGER NOT NULL DEFAULT 5 CHECK(energy>=0 AND energy<=5),
    energy_updated_at INTEGER NOT NULL,
    navigation INTEGER NOT NULL DEFAULT 0,
    equipment INTEGER NOT NULL DEFAULT 0,
    research INTEGER NOT NULL DEFAULT 0,
    taps_total INTEGER NOT NULL DEFAULT 0,
    expeditions_total INTEGER NOT NULL DEFAULT 0,
    skills_total INTEGER NOT NULL DEFAULT 0,
    active_streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    last_active_day TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clicker_counters (
    user_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    metric TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id,period,metric)
);
CREATE INDEX IF NOT EXISTS ix_clicker_period ON clicker_counters(period,metric,value DESC);
CREATE TABLE IF NOT EXISTS clicker_expeditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    route_key TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ready_at INTEGER NOT NULL,
    reward_xp INTEGER NOT NULL,
    energy_cost INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    claimed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_clicker_active ON clicker_expeditions(user_id) WHERE claimed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_clicker_trips ON clicker_expeditions(user_id,claimed_at);
CREATE TABLE IF NOT EXISTS clicker_mission_claims (
    user_id INTEGER NOT NULL,
    mission_key TEXT NOT NULL,
    period TEXT NOT NULL,
    reward_xp INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,mission_key,period)
);
CREATE TABLE IF NOT EXISTS clicker_inventory (
    user_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_received_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,item_key)
);
CREATE TABLE IF NOT EXISTS clicker_achievements (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    awarded_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,key)
);
CREATE TABLE IF NOT EXISTS clicker_xp_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('mission','expedition','skill')),
    source_key TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clicker_xp_user ON clicker_xp_entries(user_id,id);
"""


@dataclass(frozen=True)
class Route:
    key: str
    title: str
    description: str
    duration: int
    energy: int
    xp: int
    min_xp: int
    research: int
    item: str


ROUTES = {
    r.key: r for r in (
        Route("trail", "Тропа разведчика", "Найди первые следы и подготовь карту окрестностей.", 60, 1, 15, 0, 0, "leaf"),
        Route("forest", "Лесная станция", "Восстанови заброшенный пост и отыщи компас.", 300, 2, 45, 40, 0, "compass"),
        Route("ruins", "Старые руины", "Расшифруй знаки исследователей прошлого.", 900, 3, 110, 140, 1, "tablet"),
        Route("summit", "Звёздная обсерватория", "Собери карту неба в горной обсерватории.", 1800, 4, 210, 360, 2, "star_map"),
    )
}
ITEMS = {"leaf": "🍃 Лист следопыта", "compass": "🧭 Старый компас", "tablet": "🪨 Каменная табличка",
         "star_map": "🌌 Карта звёзд"}


@dataclass(frozen=True)
class Skill:
    key: str
    title: str
    description: str
    max_level: int


SKILLS = {s.key: s for s in (
    Skill("navigation", "🧭 Навигация", "Каждый уровень сокращает время новых экспедиций на 10% от базового.", 3),
    Skill("equipment", "🎒 Снаряжение", "Каждый уровень даёт +10% опыта в новых экспедициях от базовой награды.", 3),
    Skill("research", "🔎 Исследование", "Уровень 1 открывает руины, уровень 2 — обсерваторию; нужен и общий опыт.", 2),
)}


@dataclass(frozen=True)
class Mission:
    key: str
    scope: str
    title: str
    metric: str
    target: int
    xp: int


MISSIONS = {m.key: m for m in (
    Mission("first_taps", "story", "Первые шаги: 5 кликов", "taps", 5, 20),
    Mission("first_expedition", "story", "Вернуться из первой экспедиции", "expeditions", 1, 25),
    Mission("first_skill", "story", "Выбрать первый навык", "skills", 1, 25),
    Mission("two_routes", "story", "Найти предметы на двух маршрутах", "routes", 2, 40),
    Mission("three_routes", "story", "Собрать три разных находки", "routes", 3, 60),
    Mission("daily_taps", "day", "Сделать 20 кликов сегодня", "taps", 20, 15),
    Mission("daily_trip", "day", "Завершить одну экспедицию сегодня", "expeditions", 1, 25),
    Mission("daily_two", "day", "Завершить две экспедиции сегодня", "expeditions", 2, 35),
    Mission("weekly_taps", "week", "Сделать 200 кликов за неделю", "taps", 200, 80),
    Mission("weekly_trips", "week", "Завершить пять экспедиций за неделю", "expeditions", 5, 100),
    Mission("weekly_routes", "week", "Исследовать три разных маршрута за неделю", "routes", 3, 120),
)}
ACHIEVEMENTS = {"first_route": "🥾 Первый маршрут", "collector": "🗺 Полная коллекция",
                "craftsman": "🛠 Три улучшения навыков", "veteran": "🏕 Десять экспедиций", "regular": "🔥 Три дня подряд"}


class GameError(Exception):
    """A safe message that may be shown directly to a player."""


def period_key(scope: str, stamp: int) -> str:
    local = datetime.fromtimestamp(stamp, GAME_TZ)
    if scope == "story":
        return "once"
    if scope == "day":
        return local.strftime("%Y%m%d")
    if scope == "week":
        year, week, _ = local.isocalendar()
        return f"{year}W{week:02d}"
    if scope == "month":
        return local.strftime("%Y%m")
    raise GameError("Неизвестный период. Откройте приключение заново.")


def skill_cost(level: int) -> int:
    return 20 * (level + 1) ** 2


class ClickerGame:
    def __init__(self, db: ChildDB, clock: Callable[[], float] | None = None):
        self.db = db
        self.clock = clock or time.time
        with db.lock:
            if db.conn.in_transaction:
                raise RuntimeError("Initialize gameplay outside financial transactions")
            db.conn.executescript(SCHEMA)

    def _now(self) -> int:
        return int(self.clock())

    def _ensure(self, uid: int, at: int) -> dict:
        if not self.db.user(uid):
            raise GameError("Сначала нажмите /start в личном чате с ботом.")
        self.db.exec("INSERT INTO clicker_profiles(user_id,energy_updated_at,created_at) VALUES(?,?,?) "
                     "ON CONFLICT(user_id) DO NOTHING", uid, at, at)
        p = dict(self.db.one("SELECT * FROM clicker_profiles WHERE user_id=?", uid))
        steps = max(0, at - p["energy_updated_at"]) // ENERGY_INTERVAL
        energy = min(ENERGY_MAX, p["energy"] + steps)
        anchor = max(at, p["energy_updated_at"]) if energy == ENERGY_MAX else p["energy_updated_at"] + steps * ENERGY_INTERVAL
        if energy != p["energy"] or anchor != p["energy_updated_at"]:
            self.db.exec("UPDATE clicker_profiles SET energy=?,energy_updated_at=? WHERE user_id=?", energy, anchor, uid)
            p.update(energy=energy, energy_updated_at=anchor)
        return p

    def _increment(self, uid: int, scope: str, metric: str, value: int, at: int) -> None:
        self.db.exec("INSERT INTO clicker_counters(user_id,period,metric,value) VALUES(?,?,?,?) "
                     "ON CONFLICT(user_id,period,metric) DO UPDATE SET value=value+excluded.value",
                     uid, f"{scope}:{period_key(scope, at)}", metric, value)

    def _activity(self, uid: int, at: int) -> None:
        p = self.db.one("SELECT last_active_day,active_streak,best_streak FROM clicker_profiles WHERE user_id=?", uid)
        today = period_key("day", at)
        if p["last_active_day"] == today:
            return
        yesterday = (datetime.fromtimestamp(at, GAME_TZ) - timedelta(days=1)).strftime("%Y%m%d")
        streak = p["active_streak"] + 1 if p["last_active_day"] == yesterday else 1
        self.db.exec("UPDATE clicker_profiles SET last_active_day=?,active_streak=?,best_streak=? WHERE user_id=?",
                     today, streak, max(streak, p["best_streak"]), uid)

    def _record(self, uid: int, metric: str, at: int) -> None:
        observe(self.db, uid, {"taps": "tap", "expeditions": "expedition_complete", "skills": "skill"}[metric])
        column = {"taps": "taps_total", "expeditions": "expeditions_total", "skills": "skills_total"}[metric]
        self.db.exec(f"UPDATE clicker_profiles SET {column}={column}+1 WHERE user_id=?", uid)
        for scope in ("day", "week"):
            self._increment(uid, scope, metric, 1, at)
        self._activity(uid, at)

    def _award(self, uid: int, xp: int, kind: str, key: str, at: int) -> None:
        if kind == "mission":
            observe(self.db, uid, "mission")
        self.db.exec("INSERT INTO clicker_xp_entries(user_id,delta,kind,source_key,created_at) VALUES(?,?,?,?,?)",
                     uid, xp, kind, key, at)
        self.db.exec("UPDATE clicker_profiles SET xp_total=xp_total+?,xp_available=xp_available+?,version=version+1 "
                     "WHERE user_id=?", xp, xp, uid)
        self._increment(uid, "month", "xp", xp, at)

    def _badges(self, uid: int, at: int) -> None:
        p = self.db.one("SELECT * FROM clicker_profiles WHERE user_id=?", uid)
        count = self.db.scalar("SELECT COUNT(*) FROM clicker_inventory WHERE user_id=?", uid)
        checks = {"first_route": p["expeditions_total"] >= 1, "collector": count >= len(ITEMS),
                  "craftsman": p["skills_total"] >= 3, "veteran": p["expeditions_total"] >= 10,
                  "regular": p["best_streak"] >= 3}
        for key, earned in checks.items():
            if earned:
                self.db.exec("INSERT INTO clicker_achievements(user_id,key,awarded_at) VALUES(?,?,?) "
                             "ON CONFLICT(user_id,key) DO NOTHING", uid, key, at)

    def tap(self, uid: int, *, base_reward: int):
        if type(base_reward) is not int or base_reward < 0:
            raise GameError("Настройка силы клика требует внимания владельца.")
        at = self._now()
        with self.db.tx():
            self._ensure(uid, at)
            u = self.db.user(uid)
            self.db.exec("UPDATE users SET balance=balance+?,clicks=clicks+1 WHERE id=?", base_reward * u["level"], uid)
            self._record(uid, "taps", at)
            self._badges(uid, at)
            return self.db.user(uid)

    def upgrade_legacy(self, uid: int, *, level: int, cost: int, max_level: int) -> bool:
        at = self._now()
        with self.db.tx():
            self._ensure(uid, at)
            changed = self.db.try_upgrade(uid, level=level, cost=cost, max_level=max_level)
            if changed:
                self._activity(uid, at)
                self._badges(uid, at)
            return changed

    def overview(self, uid: int) -> dict:
        at = self._now()
        with self.db.tx():
            p = self._ensure(uid, at)
            inventory = [dict(r) for r in self.db.all("SELECT * FROM clicker_inventory WHERE user_id=? "
                                                     "ORDER BY first_received_at,item_key", uid)]
            achievements = [dict(r) for r in self.db.all("SELECT * FROM clicker_achievements WHERE user_id=? "
                                                        "ORDER BY awarded_at,key", uid)]
            trip = self.db.one("SELECT * FROM clicker_expeditions WHERE user_id=? AND claimed_at IS NULL", uid)
            history = [dict(r) for r in self.db.all("SELECT delta,kind,created_at FROM clicker_xp_entries "
                                                  "WHERE user_id=? ORDER BY id DESC LIMIT 5", uid)]
            yesterday = (datetime.fromtimestamp(at, GAME_TZ) - timedelta(days=1)).strftime("%Y%m%d")
            if p["last_active_day"] not in (period_key("day", at), yesterday):
                p["active_streak"] = 0
            return {"profile": p, "inventory": inventory, "achievements": achievements,
                    "expedition": dict(trip) if trip else None, "history": history, "now": at}

    def _progress(self, uid: int, m: Mission, at: int, p: dict) -> int:
        if m.metric == "routes":
            if m.scope == "story":
                return self.db.scalar("SELECT COUNT(*) FROM clicker_inventory WHERE user_id=?", uid)
            local = datetime.fromtimestamp(at, GAME_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            if m.scope == "week":
                local -= timedelta(days=local.weekday())
            end = local + timedelta(days=7 if m.scope == "week" else 1)
            return self.db.scalar("SELECT COUNT(DISTINCT route_key) FROM clicker_expeditions "
                                  "WHERE user_id=? AND claimed_at>=? AND claimed_at<?", uid,
                                  int(local.timestamp()), int(end.timestamp()))
        if m.scope == "story":
            return p[{"taps": "taps_total", "expeditions": "expeditions_total", "skills": "skills_total"}[m.metric]]
        return self.db.scalar("SELECT value FROM clicker_counters WHERE user_id=? AND period=? AND metric=?",
                              uid, f"{m.scope}:{period_key(m.scope, at)}", m.metric) or 0

    def missions(self, uid: int, scope: str) -> list[dict]:
        at = self._now()
        with self.db.tx():
            p = self._ensure(uid, at)
            period = period_key(scope, at)
            rows = []
            for m in MISSIONS.values():
                if m.scope != scope:
                    continue
                claimed = self.db.one("SELECT 1 FROM clicker_mission_claims WHERE user_id=? AND mission_key=? AND period=?",
                                      uid, m.key, period) is not None
                rows.append({"key": m.key, "title": m.title, "scope": scope, "period": period, "xp": m.xp,
                             "target": m.target, "progress": self._progress(uid, m, at, p), "claimed": claimed})
            return rows

    def claim_mission(self, uid: int, key: str, period: str) -> int:
        m = MISSIONS.get(key)
        if m is None:
            raise GameError("Миссия недоступна. Откройте список заново.")
        at = self._now()
        with self.db.tx():
            p = self._ensure(uid, at)
            if period != period_key(m.scope, at):
                raise GameError("Миссия обновилась. Откройте список заново.")
            if self.db.one("SELECT 1 FROM clicker_mission_claims WHERE user_id=? AND mission_key=? AND period=?",
                           uid, key, period):
                raise GameError("Награда за эту миссию уже получена.")
            if self._progress(uid, m, at, p) < m.target:
                raise GameError("Миссия пока не выполнена. Прогресс указан в списке.")
            self.db.exec("INSERT INTO clicker_mission_claims(user_id,mission_key,period,reward_xp,claimed_at) VALUES(?,?,?,?,?)",
                         uid, key, period, m.xp, at)
            self._award(uid, m.xp, "mission", f"mission:{uid}:{key}:{period}", at)
            self._activity(uid, at)
            self._badges(uid, at)
            return m.xp

    @staticmethod
    def _version(p: dict, expected: int) -> None:
        if type(expected) is not int or p["version"] != expected:
            raise GameError("Экран устарел. Откройте раздел заново и повторите действие.")

    def learn_skill(self, uid: int, key: str, version: int) -> int:
        s = SKILLS.get(key)
        if s is None:
            raise GameError("Неизвестный навык.")
        at = self._now()
        with self.db.tx():
            p = self._ensure(uid, at)
            self._version(p, version)
            level, cost = p[key], skill_cost(p[key])
            if level >= s.max_level:
                raise GameError("Этот навык уже развит до максимума.")
            if p["xp_available"] < cost:
                raise GameError(f"Для этого улучшения нужно {cost} XP. Выполняйте миссии и экспедиции.")
            self.db.exec("INSERT INTO clicker_xp_entries(user_id,delta,kind,source_key,created_at) VALUES(?,?,?,?,?)",
                         uid, -cost, "skill", f"skill:{uid}:{key}:{level + 1}", at)
            self.db.exec(f"UPDATE clicker_profiles SET {key}={key}+1,xp_available=xp_available-?,version=version+1 "
                         "WHERE user_id=?",
                         cost, uid)
            self._record(uid, "skills", at)
            self._badges(uid, at)
            return level + 1

    def start_expedition(self, uid: int, key: str, version: int) -> dict:
        route = ROUTES.get(key)
        if route is None:
            raise GameError("Этот маршрут недоступен.")
        at = self._now()
        with self.db.tx():
            p = self._ensure(uid, at)
            self._version(p, version)
            if self.db.one("SELECT 1 FROM clicker_expeditions WHERE user_id=? AND claimed_at IS NULL", uid):
                raise GameError("Сначала завершите текущую экспедицию и заберите находку.")
            if p["xp_total"] < route.min_xp or p["research"] < route.research:
                raise GameError(f"Для маршрута нужны {route.min_xp} общего XP и исследование уровня {route.research}.")
            if p["energy"] < route.energy:
                raise GameError("Не хватает энергии. Одна единица восстанавливается за 20 минут.")
            duration = max(10, route.duration * (100 - 10 * p["navigation"]) // 100)
            xp = route.xp * (100 + 10 * p["equipment"]) // 100
            self.db.exec("UPDATE clicker_profiles SET energy=energy-?,version=version+1 WHERE user_id=?", route.energy, uid)
            cur = self.db.exec("INSERT INTO clicker_expeditions"
                               "(user_id,route_key,started_at,ready_at,reward_xp,energy_cost,item_key) "
                               "VALUES(?,?,?,?,?,?,?)", uid, key, at, at + duration, xp, route.energy, route.item)
            self._activity(uid, at)
            self._badges(uid, at)
            observe(self.db, uid, "expedition_start")
            return dict(self.db.one("SELECT * FROM clicker_expeditions WHERE id=?", cur.lastrowid))

    def claim_expedition(self, uid: int, trip_id: int) -> dict:
        at = self._now()
        with self.db.tx():
            self._ensure(uid, at)
            trip = self.db.one("SELECT * FROM clicker_expeditions WHERE id=? AND user_id=?", trip_id, uid)
            if not trip:
                raise GameError("Экспедиция недоступна. Откройте приключение заново.")
            if trip["claimed_at"] is not None:
                raise GameError("Находка и опыт уже получены.")
            if at < trip["ready_at"]:
                raise GameError(f"Экспедиция ещё идёт: осталось {trip['ready_at'] - at} сек.")
            self.db.exec("UPDATE clicker_expeditions SET claimed_at=? WHERE id=? AND claimed_at IS NULL", at, trip_id)
            self._award(uid, trip["reward_xp"], "expedition", f"expedition:{trip_id}", at)
            self.db.exec("INSERT INTO clicker_inventory(user_id,item_key,count,first_received_at) VALUES(?,?,1,?) "
                         "ON CONFLICT(user_id,item_key) DO UPDATE SET count=count+1", uid, trip["item_key"], at)
            self._record(uid, "expeditions", at)
            self._badges(uid, at)
            return {"xp": trip["reward_xp"], "item": trip["item_key"]}

    def leaderboard(self, scope: str) -> list[dict]:
        if scope == "month":
            rows = self.db.all("SELECT u.id,u.full_name,c.value AS score FROM clicker_counters c JOIN users u ON u.id=c.user_id "
                               "WHERE c.period=? AND c.metric='xp' AND c.value>0 ORDER BY c.value DESC,u.id LIMIT 10",
                               f"month:{period_key('month', self._now())}")
        elif scope == "all":
            rows = self.db.all("SELECT u.id,u.full_name,p.xp_total AS score FROM clicker_profiles p "
                               "JOIN users u ON u.id=p.user_id "
                               "WHERE p.xp_total>0 ORDER BY p.xp_total DESC,u.id LIMIT 10")
        elif scope == "balance":
            rows = self.db.all("SELECT id,full_name,balance AS score FROM users ORDER BY balance DESC,id LIMIT 10")
        else:
            raise GameError("Неизвестный рейтинг.")
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        return {"players": self.db.scalar("SELECT COUNT(*) FROM clicker_profiles"),
                "expeditions": self.db.scalar("SELECT COUNT(*) FROM clicker_expeditions WHERE claimed_at IS NOT NULL"),
                "missions": self.db.scalar("SELECT COUNT(*) FROM clicker_mission_claims"),
                "season_players": self.db.scalar("SELECT COUNT(*) FROM clicker_counters "
                                                 "WHERE period=? AND metric='xp' AND value>0",
                                                 f"month:{period_key('month', self._now())}")}
