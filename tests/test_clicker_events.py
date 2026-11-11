"""Events derive goals from committed game facts and award a single separate XP receipt."""

import concurrent.futures
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from child.clicker_events import GRACE, SEED, ClickerEvents, EventError
from child.clicker_game import GAME_TZ, ClickerGame, period_key
from child.db import ChildDB

OWNER, A, B = 1, 10, 20


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "clicker.db"
    db = ChildDB(path)
    for uid in (OWNER, A, B):
        db.upsert_user(uid, None, f"Synthetic {uid}", None)
    clock = [int(datetime(2026, 11, 11, 12, tzinfo=GAME_TZ).timestamp())]
    game = ClickerGame(db, clock=lambda: clock[0])
    events = ClickerEvents(db, owner_id=OWNER, game=game, clock=lambda: clock[0])
    yield SimpleNamespace(db=db, clock=clock, game=game, events=events, path=path)
    db.conn.close()


def publish(w, **values):
    cid = w.events.create_draft(OWNER, {**SEED, **values})
    w.events.publish(OWNER, cid, 1)
    return cid


def join(w, cid, uid=A):
    w.events.join(uid, cid, w.events.owner_event(OWNER, cid)["revision"])


def trip(w, uid=A, route="trail"):
    p = w.game.overview(uid)["profile"]
    t = w.game.start_expedition(uid, route, p["version"])
    w.clock[0] = t["ready_at"]
    w.game.claim_expedition(uid, t["id"])
    return t


def snapshot(db):
    return {
        r[0]: [tuple(v) for v in db.all(f'SELECT * FROM "{r[0]}" ORDER BY rowid')]
        for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_example_is_private_draft_and_restart_preserves_existing_users(env):
    w = env
    w.db.add_balance(A, 123.5)
    before = snapshot(w.db)
    ClickerEvents(w.db, owner_id=OWNER, game=w.game, clock=lambda: w.clock[0])
    assert snapshot(w.db) == before
    assert w.events.list_events(A)["rows"] == []
    assert w.events.owner_event(OWNER, 1)["status"] == "draft"
    with pytest.raises(EventError):
        w.events.player_event(A, 1)
    assert w.db.scalar("SELECT COUNT(*) FROM clicker_profiles") == 0
    assert w.db.user(A)["balance"] == 123.5


def test_goals_count_only_new_actions_and_inflight_trip_is_excluded_even_same_second(env):
    w = env
    for _ in range(7):
        w.game.tap(A, base_reward=1)
    p = w.game.overview(A)["profile"]
    old = w.game.start_expedition(A, "trail", p["version"])
    cid = publish(w, taps=3, expeditions=1)
    join(w, cid)
    w.clock[0] = old["ready_at"]
    w.game.claim_expedition(A, old["id"])
    assert w.events.player_event(A, cid)["progress"] == {"taps": 0, "expeditions": 0, "routes": 0}
    for _ in range(3):
        w.game.tap(A, base_reward=1)
    assert not w.events.player_event(A, cid)["ready"]
    trip(w)
    c = w.events.player_event(A, cid)
    assert c["ready"] and c["progress"] == {"taps": 3, "expeditions": 1, "routes": 1}


def test_reward_receipt_badge_and_monthly_ranking_leave_wallet_untouched(env):
    w = env
    cid = publish(w, taps=1, expeditions=0, reward=80)
    join(w, cid)
    w.game.tap(A, base_reward=3)
    before = tuple(w.db.user(A))
    assert w.events.claim(A, cid) == 80
    assert tuple(w.db.user(A)) == before
    p = w.game.overview(A)["profile"]
    assert p["xp_total"] == p["xp_available"] == 80
    assert w.game.leaderboard("month")[0]["score"] == 80
    assert w.events.awards(A)["rows"][0]["badge_title"] == SEED["badge"]
    entry = w.db.one("SELECT * FROM clicker_xp_entries WHERE source_key=?", f"event:{cid}:{A}")
    assert entry["delta"] == 80
    with pytest.raises(EventError):
        w.events.claim(A, cid)
    assert w.db.scalar("SELECT COUNT(*) FROM clicker_xp_entries") == 1
    assert w.game.stats()["missions"] == 0


def test_distinct_routes_are_required_and_repeats_count_as_expeditions_only(env):
    w = env
    cid = publish(w, taps=0, expeditions=2, routes=2)
    join(w, cid)
    trip(w)
    trip(w)
    c = w.events.player_event(A, cid)
    assert c["progress"]["expeditions"] == 2 and c["progress"]["routes"] == 1 and not c["ready"]
    with w.db.tx():
        w.game._award(A, 100, "mission", "synthetic_route_unlock", w.clock[0])
    trip(w, route="forest")
    assert w.events.player_event(A, cid)["ready"]


def test_join_is_idempotent_and_does_not_reset_progress(env):
    w = env
    cid = publish(w, taps=2, expeditions=0)
    join(w, cid)
    w.game.tap(A, base_reward=1)
    before = snapshot(w.db)
    join(w, cid)
    assert snapshot(w.db) == before
    assert w.events.player_event(A, cid)["progress"]["taps"] == 1


def test_pause_blocks_entries_but_existing_player_can_finish(env):
    w = env
    cid = publish(w, taps=1, expeditions=0)
    join(w, cid)
    w.events.set_status(OWNER, cid, "paused", 2)
    with pytest.raises(EventError):
        w.events.join(B, cid, 3)
    w.game.tap(A, base_reward=1)
    assert w.events.claim(A, cid) == SEED["reward"]
    w.events.set_status(OWNER, cid, "active", 3)
    join(w, cid, B)


def test_scheduled_event_opens_at_moscow_midnight_and_old_revision_cannot_join(env):
    w = env
    cid = publish(w, starts="2026-11-12", taps=1, expeditions=0)
    with pytest.raises(EventError):
        join(w, cid)
    w.clock[0] = int(datetime(2026, 11, 12, tzinfo=GAME_TZ).timestamp())
    with pytest.raises(EventError):
        w.events.join(A, cid, 1)
    join(w, cid)
    assert w.events.player_event(A, cid)["player"]["join_day"] == "20261112"


def test_goals_freeze_at_end_and_late_taps_and_trip_cannot_complete_event(env):
    w = env
    cid = publish(w, days=1, taps=2, expeditions=1)
    join(w, cid)
    w.game.tap(A, base_reward=1)
    p = w.game.overview(A)["profile"]
    t = w.game.start_expedition(A, "trail", p["version"])
    end = w.events.player_event(A, cid)["ends_at"]
    w.clock[0] = end
    w.game.tap(A, base_reward=1)
    w.game.claim_expedition(A, t["id"])
    c = w.events.player_event(A, cid)
    assert c["progress"] == {"taps": 1, "expeditions": 0, "routes": 0}
    with pytest.raises(EventError):
        w.events.claim(A, cid)
    with pytest.raises(EventError):
        join(w, cid, B)
    with pytest.raises(EventError):
        w.events.set_status(OWNER, cid, "paused", 1)


def test_seven_day_grace_claims_on_last_second_and_expires_exactly(env):
    w = env
    cid = publish(w, days=1, taps=1, expeditions=0)
    for uid in (A, B):
        join(w, cid, uid)
        w.game.tap(uid, base_reward=1)
    end = w.events.player_event(A, cid)["ends_at"]
    w.clock[0] = end + GRACE - 1
    assert w.events.claim(A, cid) == SEED["reward"]
    w.clock[0] += 1
    with pytest.raises(EventError):
        w.events.claim(B, cid)
    assert not w.events.awards(B)["rows"]


def test_claimed_xp_counts_in_month_of_claim_and_calendar_crosses_year(env):
    w = env
    w.clock[0] = int(datetime(2026, 12, 31, 12, tzinfo=GAME_TZ).timestamp())
    cid = publish(w, days=1, taps=1, expeditions=0)
    join(w, cid)
    w.game.tap(A, base_reward=1)
    w.clock[0] += 86400
    w.events.claim(A, cid)
    assert w.db.scalar("SELECT value FROM clicker_counters WHERE user_id=? AND period='month:202701' AND metric='xp'", A) == 40
    assert period_key("day", w.clock[0]) == "20270101"


def test_clock_rollback_cannot_complete_or_claim(env):
    w = env
    cid = publish(w, taps=1, expeditions=0)
    join(w, cid)
    w.game.tap(A, base_reward=1)
    w.clock[0] -= 1
    with pytest.raises(EventError):
        w.events.claim(A, cid)


def test_owner_changes_are_revision_guarded_and_published_conditions_immutable(env):
    w = env
    cid = w.events.create_draft(OWNER, SEED)
    w.events.update_draft(OWNER, cid, 1, {**SEED, "reward": 90})
    with pytest.raises(EventError):
        w.events.publish(OWNER, cid, 1)
    w.events.publish(OWNER, cid, 2)
    with pytest.raises(EventError):
        w.events.update_draft(OWNER, cid, 3, SEED)
    copied = w.events.copy_draft(OWNER, cid)
    assert copied == w.events.copy_draft(OWNER, cid)
    assert w.events.owner_event(OWNER, copied)["status"] == "draft"
    w.events.update_draft(OWNER, copied, 1, {**SEED, "title": "Edited copy"})
    assert w.events.owner_event(OWNER, cid)["reward"] == 90


@pytest.mark.parametrize("operation", ["create", "read", "edit", "publish", "pause", "copy", "list"])
def test_current_owner_is_required_for_every_owner_operation(env, operation):
    w = env
    actions = {
        "create": lambda: w.events.create_draft(A, SEED),
        "read": lambda: w.events.owner_event(A, 1),
        "edit": lambda: w.events.update_draft(A, 1, 1, SEED),
        "publish": lambda: w.events.publish(A, 1, 1),
        "pause": lambda: w.events.set_status(A, 1, "paused", 1),
        "copy": lambda: w.events.copy_draft(A, 1),
        "list": lambda: w.events.list_events(A, owner=True),
    }
    before = snapshot(w.db)
    with pytest.raises(EventError):
        actions[operation]()
    assert snapshot(w.db) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", ""),
        ("title", "a" * 81),
        ("description", "x" * 901),
        ("badge", "b" * 61),
        ("days", 0),
        ("days", 31),
        ("days", True),
        ("taps", -1),
        ("taps", 100001),
        ("routes", 5),
        ("expeditions", 1001),
        ("reward", 0),
        ("reward", 1001),
        ("reward", "١"),
        ("reward", "1.5"),
        ("starts", "2026-11-10"),
        ("starts", "2027-11-13"),
        ("starts", "2026-02-30"),
        ("starts", "x"),
    ],
)
def test_invalid_conditions_cannot_create_a_draft(env, field, value):
    with pytest.raises(EventError):
        env.events.create_draft(OWNER, {**SEED, field: value})
    assert env.db.scalar("SELECT COUNT(*) FROM clicker_events") == 1


def test_at_least_one_goal_required_and_stale_past_draft_cannot_publish(env):
    w = env
    with pytest.raises(EventError):
        w.events.create_draft(OWNER, {**SEED, "taps": 0, "expeditions": 0, "routes": 0})
    w.clock[0] += 86400
    with pytest.raises(EventError):
        w.events.publish(OWNER, 1, 1)
    w.events.update_draft(OWNER, 1, 1, SEED)
    w.events.publish(OWNER, 1, 2)


def test_paused_events_reserve_dates_and_adjacent_schedule_is_allowed(env):
    w = env
    cid = publish(w, days=1)
    w.events.set_status(OWNER, cid, "paused", 2)
    overlap = w.events.create_draft(OWNER, SEED)
    with pytest.raises(EventError):
        w.events.publish(OWNER, overlap, 1)
    next_id = publish(w, starts="2026-11-12", days=1)
    assert next_id != cid


@pytest.mark.parametrize("fault", ["xp", "receipt"])
def test_reward_and_receipt_rollback_together_on_database_failure(env, fault):
    w = env
    cid = publish(w, taps=1, expeditions=0)
    join(w, cid)
    w.game.tap(A, base_reward=1)
    trigger = "BEFORE INSERT ON clicker_xp_entries" if fault == "xp" else "BEFORE UPDATE ON clicker_event_players"
    w.db.exec(f"CREATE TRIGGER fail {trigger} BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        w.events.claim(A, cid)
    assert snapshot(w.db) == before
    w.db.exec("DROP TRIGGER fail")
    assert w.events.claim(A, cid) == 40


def test_publish_and_audit_are_atomic(env):
    w = env
    w.db.exec("CREATE TRIGGER fail BEFORE INSERT ON clicker_event_audit BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        w.events.publish(OWNER, 1, 1)
    assert snapshot(w.db) == before


def test_failed_entry_does_not_leave_a_game_profile_or_membership(env):
    w = env
    cid = publish(w)
    w.db.exec("CREATE TRIGGER fail BEFORE INSERT ON clicker_event_players BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        join(w, cid)
    assert snapshot(w.db) == before
    w.db.exec("DROP TRIGGER fail")
    join(w, cid)
    assert w.events.player_event(A, cid)["player"]


@pytest.mark.parametrize("action", ["claim", "publish"])
def test_two_database_connections_cannot_double_claim_or_publish_overlapping_events(env, action):
    w = env
    if action == "claim":
        first = second = publish(w, taps=1, expeditions=0)
        join(w, first)
        w.game.tap(A, base_reward=1)
    else:
        first = w.events.create_draft(OWNER, SEED)
        second = w.events.create_draft(OWNER, SEED)
    other_db = ChildDB(w.path)
    other_game = ClickerGame(other_db, clock=lambda: w.clock[0])
    other = ClickerEvents(other_db, owner_id=OWNER, game=other_game, clock=lambda: w.clock[0], initialize=False)
    barrier = threading.Barrier(2)

    def perform(engine, cid):
        barrier.wait(timeout=5)
        try:
            engine.claim(A, cid) if action == "claim" else engine.publish(OWNER, cid, 1)
            return True
        except EventError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(perform, engine, cid) for engine, cid in ((w.events, first), (other, second))]
            assert sum(job.result(timeout=10) for job in jobs) == 1
        if action == "claim":
            assert w.db.scalar("SELECT COUNT(*) FROM clicker_xp_entries WHERE source_key LIKE 'event:%'") == 1
            assert w.game.overview(A)["profile"]["xp_total"] == 40
        else:
            assert w.db.scalar("SELECT COUNT(*) FROM clicker_events WHERE status='active'") == 1
    finally:
        other_db.conn.close()


def test_read_only_screens_never_create_profile_join_or_credit(env):
    w = env
    cid = publish(w)
    before = snapshot(w.db)
    w.events.player_event(A, cid)
    w.events.leaderboard(cid)
    w.events.awards(A)
    w.events.list_events(A)
    assert snapshot(w.db) == before
    with pytest.raises(EventError):
        w.events.claim(A, cid)
    with pytest.raises(EventError):
        w.events.player_event(999, cid)


def test_first_claim_ranking_uses_receipts_not_partial_progress(env):
    w = env
    cid = publish(w, taps=1, expeditions=0)
    for uid in (A, B):
        join(w, cid, uid)
        w.game.tap(uid, base_reward=1)
    assert w.events.leaderboard(cid) == []
    w.events.claim(B, cid)
    w.clock[0] += 1
    w.events.claim(A, cid)
    assert [r["full_name"] for r in w.events.leaderboard(cid)] == [f"Synthetic {B}", f"Synthetic {A}"]


def test_public_catalog_prioritizes_current_then_nearest_future_then_recent_past(env):
    w = env
    old = publish(w, days=1)
    w.clock[0] += 86400
    current = publish(w, days=1)
    far = publish(w, starts="2026-11-20", days=1)
    near = publish(w, starts="2026-11-13", days=1)
    assert [row["id"] for row in w.events.list_events(A)["rows"]] == [current, near, far, old]
