"""Adventure progression uses its own XP; only legacy tap/upgrade touch the existing wallet."""
import concurrent.futures
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from child.clicker_game import ClickerGame, GameError, period_key
from child.db import ChildDB

UID = 10
TZ = ZoneInfo("Europe/Moscow")


@pytest.fixture
def game_env(tmp_path):
    db = ChildDB(tmp_path / "game.db")
    db.upsert_user(UID, None, "Synthetic player", None)
    clock = [int(datetime(2026, 11, 10, 12, tzinfo=TZ).timestamp())]
    game = ClickerGame(db, clock=lambda: clock[0])
    yield game, db, clock
    db.conn.close()


def first_mission(game):
    for _ in range(5):
        game.tap(UID, base_reward=1)
    mission = next(m for m in game.missions(UID, "story") if m["key"] == "first_taps")
    return game.claim_mission(UID, "first_taps", mission["period"])


def test_schema_and_profile_leave_existing_wallet_untouched(game_env):
    game, db, clock = game_env
    db.add_balance(UID, 123.5)
    db.exec("UPDATE users SET level=4,clicks=99 WHERE id=?", UID)
    before = tuple(db.user(UID))
    profile = game.overview(UID)["profile"]
    assert profile["xp_total"] == profile["xp_available"] == 0
    assert profile["energy"] == 5 and profile["version"] == 0
    assert tuple(db.user(UID)) == before
    ClickerGame(db, clock=lambda: clock[0]).overview(UID)
    assert tuple(db.user(UID)) == before


def test_taps_and_first_mission_credit_xp_once(game_env):
    game, db, clock = game_env
    assert first_mission(game) == 20
    assert db.user(UID)["balance"] == db.user(UID)["clicks"] == 5
    assert game.overview(UID)["profile"]["xp_total"] == 20
    with pytest.raises(GameError):
        game.claim_mission(UID, "first_taps", "once")
    assert db.scalar("SELECT COUNT(*) FROM clicker_xp_entries") == 1
    assert db.user(UID)["balance"] == 5


def test_incomplete_and_forged_missions_cannot_award(game_env):
    game, db, clock = game_env
    for key, period in [("first_taps", "once"), ("unknown", "once"), ("daily_taps", "20990101")]:
        with pytest.raises(GameError):
            game.claim_mission(UID, key, period)
    assert game.overview(UID)["profile"]["xp_total"] == 0


def test_first_session_has_strategy_and_a_completed_route(game_env):
    game, db, clock = game_env
    first_mission(game)
    version = game.overview(UID)["profile"]["version"]
    assert game.learn_skill(UID, "navigation", version) == 1
    p = game.overview(UID)["profile"]
    assert p["xp_available"] == 0 and p["xp_total"] == 20
    trip = game.start_expedition(UID, "trail", p["version"])
    assert trip["ready_at"] - trip["started_at"] == 54
    assert trip["reward_xp"] == 15
    with pytest.raises(GameError):
        game.claim_expedition(UID, trip["id"])
    clock[0] = trip["ready_at"]
    assert game.claim_expedition(UID, trip["id"])["xp"] == 15
    assert db.user(UID)["balance"] == 5
    with pytest.raises(GameError):
        game.claim_expedition(UID, trip["id"])
    state = game.overview(UID)
    assert state["profile"]["expeditions_total"] == 1
    assert state["profile"]["xp_available"] == 15 and state["profile"]["xp_total"] == 35
    assert len(state["inventory"]) == 1
    assert any(a["key"] == "first_route" for a in state["achievements"])


def test_stale_start_and_skill_buttons_are_rejected(game_env):
    game, db, clock = game_env
    first_mission(game)
    version = game.overview(UID)["profile"]["version"]
    game.learn_skill(UID, "equipment", version)
    with pytest.raises(GameError):
        game.learn_skill(UID, "navigation", version)
    with pytest.raises(GameError):
        game.start_expedition(UID, "trail", version)
    p = game.overview(UID)["profile"]
    assert p["equipment"] == 1 and p["navigation"] == 0 and p["energy"] == 5


def test_only_one_route_and_its_reward_snapshot_survives_restart(game_env):
    game, db, clock = game_env
    first_mission(game)
    p = game.overview(UID)["profile"]
    game.learn_skill(UID, "equipment", p["version"])
    p = game.overview(UID)["profile"]
    trip = game.start_expedition(UID, "trail", p["version"])
    assert trip["reward_xp"] == 16
    with pytest.raises(GameError):
        game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    clock[0] = trip["ready_at"]
    reopened = ClickerGame(db, clock=lambda: clock[0])
    assert reopened.overview(UID)["expedition"]["id"] == trip["id"]
    assert reopened.claim_expedition(UID, trip["id"])["xp"] == 16


def test_energy_is_server_timed_and_does_not_bank_at_capacity(game_env):
    game, db, clock = game_env
    clock[0] += 86400
    p = game.overview(UID)["profile"]
    assert p["energy"] == 5
    trip = game.start_expedition(UID, "trail", p["version"])
    assert game.overview(UID)["profile"]["energy"] == 4
    clock[0] += 1199
    assert game.overview(UID)["profile"]["energy"] == 4
    clock[0] += 1
    assert game.overview(UID)["profile"]["energy"] == 5
    clock[0] = trip["started_at"] - 100
    assert game.overview(UID)["profile"]["energy"] == 5


def test_routes_require_real_progress_and_research(game_env):
    game, db, clock = game_env
    p = game.overview(UID)["profile"]
    with pytest.raises(GameError):
        game.start_expedition(UID, "summit", p["version"])
    assert game.overview(UID)["profile"]["energy"] == 5
    assert db.scalar("SELECT COUNT(*) FROM clicker_expeditions") == 0


def test_daily_and_weekly_windows_and_streak(game_env):
    game, db, clock = game_env
    clock[0] = int(datetime(2026, 11, 15, 23, 59, 50, tzinfo=TZ).timestamp())
    for _ in range(20):
        game.tap(UID, base_reward=1)
    day = next(m for m in game.missions(UID, "day") if m["key"] == "daily_taps")
    week = game.missions(UID, "week")[0]["period"]
    assert game.claim_mission(UID, day["key"], day["period"]) == 15
    clock[0] += 20  # Monday, next day and week in Moscow; the host timezone is irrelevant.
    missions = game.missions(UID, "day")
    assert all(not m["claimed"] and m["progress"] == 0 for m in missions)
    assert game.missions(UID, "week")[0]["period"] != week
    with pytest.raises(GameError):
        game.claim_mission(UID, day["key"], day["period"])
    game.tap(UID, base_reward=1)
    assert game.overview(UID)["profile"]["active_streak"] == 2
    clock[0] += 2 * 86400
    game.tap(UID, base_reward=1)
    assert game.overview(UID)["profile"]["active_streak"] == 1
    assert game.overview(UID)["profile"]["best_streak"] == 2


def test_spending_xp_never_lowers_rank_and_new_month_resets_only_season(game_env):
    game, db, clock = game_env
    first_mission(game)
    before = game.leaderboard("month")[0]["score"]
    game.learn_skill(UID, "navigation", game.overview(UID)["profile"]["version"])
    assert game.leaderboard("month")[0]["score"] == before == 20
    clock[0] = int(datetime(2026, 12, 1, 0, 1, tzinfo=TZ).timestamp())
    assert game.leaderboard("month") == []
    assert game.leaderboard("all")[0]["score"] == 20
    assert game.overview(UID)["profile"]["navigation"] == 1


@pytest.mark.parametrize("target", ["clicker_xp_entries", "clicker_inventory", "clicker_achievements"])
def test_expedition_failure_rolls_back_every_reward(game_env, target):
    game, db, clock = game_env
    trip = game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    clock[0] = trip["ready_at"]
    db.exec(f"CREATE TRIGGER fault BEFORE INSERT ON {target} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.claim_expedition(UID, trip["id"])
    assert game.overview(UID)["profile"]["xp_total"] == 0
    assert game.overview(UID)["expedition"]["id"] == trip["id"]
    assert db.scalar("SELECT COUNT(*) FROM clicker_inventory") == 0
    db.exec("DROP TRIGGER fault")
    assert game.claim_expedition(UID, trip["id"])["xp"] == 15


def test_tap_progress_failure_also_rolls_back_legacy_wallet(game_env):
    game, db, clock = game_env
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON clicker_counters BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.tap(UID, base_reward=3)
    assert db.user(UID)["balance"] == db.user(UID)["clicks"] == 0


def test_competing_mission_claims_credit_once(game_env, tmp_path):
    game, db, clock = game_env
    for _ in range(5):
        game.tap(UID, base_reward=1)
    second = ChildDB(tmp_path / "game.db")
    other = ClickerGame(second, clock=lambda: clock[0])
    barrier = threading.Barrier(2)

    def claim(engine):
        barrier.wait(timeout=5)
        try:
            return engine.claim_mission(UID, "first_taps", "once")
        except GameError:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(claim, g) for g in (game, other)]
            assert [j.result(timeout=10) for j in jobs].count(20) == 1
        assert game.overview(UID)["profile"]["xp_total"] == 20
    finally:
        second.conn.close()


def test_user_cannot_claim_another_players_expedition(game_env):
    game, db, clock = game_env
    db.upsert_user(20, None, "Synthetic other", None)
    trip = game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    clock[0] = trip["ready_at"]
    with pytest.raises(GameError):
        game.claim_expedition(20, trip["id"])
    assert game.overview(UID)["profile"]["xp_total"] == 0
    assert game.overview(20)["profile"]["xp_total"] == 0


def test_mission_and_skill_failures_preserve_available_xp(game_env):
    game, db, clock = game_env
    for _ in range(5):
        game.tap(UID, base_reward=1)
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON clicker_xp_entries BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.claim_mission(UID, "first_taps", "once")
    assert db.scalar("SELECT COUNT(*) FROM clicker_mission_claims") == 0
    db.exec("DROP TRIGGER fault")
    game.claim_mission(UID, "first_taps", "once")
    p = game.overview(UID)["profile"]
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON clicker_counters BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.learn_skill(UID, "equipment", p["version"])
    assert game.overview(UID)["profile"]["xp_available"] == 20
    assert game.overview(UID)["profile"]["equipment"] == 0
    assert db.scalar("SELECT SUM(delta) FROM clicker_xp_entries") == 20


def test_start_failure_does_not_consume_energy_or_version(game_env):
    game, db, clock = game_env
    p = game.overview(UID)["profile"]
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON clicker_expeditions BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.start_expedition(UID, "trail", p["version"])
    after = game.overview(UID)["profile"]
    assert after["energy"] == 5 and after["version"] == p["version"]


@pytest.mark.parametrize("operation", ["start", "claim"])
def test_competing_expedition_requests_apply_once(game_env, tmp_path, operation):
    game, db, clock = game_env
    p = game.overview(UID)["profile"]
    trip = None
    if operation == "claim":
        trip = game.start_expedition(UID, "trail", p["version"])
        clock[0] = trip["ready_at"]
    second = ChildDB(tmp_path / "game.db")
    other = ClickerGame(second, clock=lambda: clock[0])
    barrier = threading.Barrier(2)

    def act(engine):
        barrier.wait(timeout=5)
        try:
            if operation == "start":
                return engine.start_expedition(UID, "trail", p["version"])
            return engine.claim_expedition(UID, trip["id"])
        except GameError:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(act, g) for g in (game, other)]
            assert sum(j.result(timeout=10) is not None for j in jobs) == 1
        assert game.overview(UID)["profile"]["energy"] == 4
        assert db.scalar("SELECT COUNT(*) FROM clicker_expeditions") == 1
        if operation == "claim":
            assert game.overview(UID)["profile"]["xp_total"] == 15
            assert db.scalar("SELECT SUM(count) FROM clicker_inventory") == 1
    finally:
        second.conn.close()


def test_fully_reopened_database_preserves_trip_and_skill(game_env, tmp_path):
    game, db, clock = game_env
    first_mission(game)
    game.learn_skill(UID, "navigation", game.overview(UID)["profile"]["version"])
    trip = game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    db.conn.close()
    fresh = ChildDB(tmp_path / "game.db")
    try:
        engine = ClickerGame(fresh, clock=lambda: clock[0])
        assert engine.overview(UID)["profile"]["navigation"] == 1
        assert engine.overview(UID)["expedition"]["ready_at"] == trip["ready_at"]
        clock[0] = trip["ready_at"]
        engine.claim_expedition(UID, trip["id"])
        p = engine.overview(UID)["profile"]
        assert p["xp_total"] == fresh.scalar("SELECT SUM(delta) FROM clicker_xp_entries WHERE delta>0")
        assert p["xp_available"] == fresh.scalar("SELECT SUM(delta) FROM clicker_xp_entries")
        assert fresh.user(UID)["balance"] == 5
    finally:
        fresh.conn.close()


def test_exhausted_energy_requires_real_recharge(game_env):
    game, db, clock = game_env
    for _ in range(5):
        trip = game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
        clock[0] = trip["ready_at"]
        game.claim_expedition(UID, trip["id"])
    assert game.overview(UID)["profile"]["energy"] == 0
    with pytest.raises(GameError):
        game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    clock[0] += 1200
    game.start_expedition(UID, "trail", game.overview(UID)["profile"]["version"])
    assert db.user(UID)["balance"] == 0


@pytest.mark.parametrize("date,expected", [("2026-12-31", "2026W53"), ("2027-01-01", "2026W53"),
                                          ("2027-01-04", "2027W01")])
def test_week_uses_iso_year_and_moscow_time(date, expected):
    stamp = int(datetime.fromisoformat(date).replace(tzinfo=TZ).timestamp())
    assert period_key("week", stamp) == expected
