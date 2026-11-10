"""Observed activity, closed-day retention and optional recording failure isolation."""

import concurrent.futures
import sqlite3
import threading
from datetime import datetime

import pytest

from child.db import ChildDB
from child.engagement import TZ, Engagement, day_key
from creator.engagement import read, render

OWNER, A, B, C = 1, 10, 20, 30


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp())


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "activity.db"
    db = ChildDB(path)
    for uid in (OWNER, A, B, C):
        db.upsert_user(uid, "synthetic", "Synthetic participant", None)
    clock = [stamp("2026-11-01T09:00:00")]
    collector = Engagement(db, owner_check=lambda uid: uid == OWNER, clock=lambda: clock[0])
    yield collector, db, clock, path
    db.conn.close()


def counts(db):
    return tuple(
        db.scalar(f"SELECT COUNT(*) FROM {name}") for name in ("engagement_people", "engagement_activity", "engagement_events")
    )


def test_initialization_and_reading_never_backfill_legacy_users(env):
    e, db, clock, path = env
    before = [tuple(r) for r in db.all("SELECT * FROM users ORDER BY id")]
    since = db.scalar("SELECT started_at FROM engagement_meta")
    clock[0] += 86400
    Engagement(db, owner_check=lambda uid: uid == OWNER, clock=lambda: clock[0])
    assert db.scalar("SELECT started_at FROM engagement_meta") == since
    assert counts(db) == (0, 0, 0)
    result = read(path, "clicker", now=clock[0])
    assert result["status"] == "ok" and result["observed"] == 0
    assert [tuple(r) for r in db.all("SELECT * FROM users ORDER BY id")] == before


def test_repeated_events_are_daily_unique_and_registration_flag_survives_races(env):
    e, db, clock, path = env
    assert e.record(A, "interaction")
    assert e.record(A, "start", registered=False)
    assert e.record(A, "start", registered=True)
    for _ in range(100):
        assert e.record(A, "tap")
    assert counts(db) == (1, 1, 3)
    result = read(path, "clicker", now=clock[0])
    assert result["registered"] == result["observed"] == 1
    assert result["active"] == {1: 1, 7: 1, 30: 1}
    assert dict(result["steps"])["Успешный клик"] == 1
    clock[0] += 86400
    assert e.record(A, "tap")
    assert counts(db) == (1, 2, 4)


@pytest.mark.parametrize("uid,kind", [(OWNER, "start"), (999, "tap"), (True, "tap"), (A, "not-an-event"), (A, "123456:SECRET")])
def test_owner_unknown_user_and_unapproved_payloads_are_ignored(env, uid, kind):
    e, db, clock, path = env
    assert not e.record(uid, kind)
    assert counts(db) == (0, 0, 0)


def test_passive_qualified_friend_never_counts_as_inviter_return(env):
    e, db, clock, path = env
    e.record(A, "start", registered=True)
    clock[0] += 86400
    e.record(A, "qualified", active=False)
    e.record(B, "start", registered=True)
    assert db.scalar("SELECT COUNT(*) FROM engagement_activity WHERE user_id=?", A) == 1
    assert db.scalar("SELECT COUNT(*) FROM engagement_events WHERE user_id=? AND kind='qualified'", A) == 1
    clock[0] += 86400
    report = read(path, "refbot", now=clock[0])
    assert report["retention"][1]["eligible"] == 1
    assert report["retention"][1]["returned"] == 0
    assert dict(report["steps"])["Получили подтверждённого друга"] == 1
    # A passive event on an old link does not create an observed cohort either.
    e.record(C, "qualified", active=False)
    assert db.scalar("SELECT COUNT(*) FROM engagement_people WHERE user_id=?", C) == 0


def test_d1_and_d7_only_include_fully_closed_target_days(env):
    e, db, clock, path = env
    start = clock[0]
    e.record(A, "start", registered=True)
    e.record(B, "start")
    clock[0] = start + 86400
    e.record(A, "tap")
    e.record(C, "start")
    clock[0] = start + 2 * 86400
    e.record(C, "tap")  # C's D1 is still open today and excluded even though a return exists.
    report = read(path, "clicker", now=clock[0])
    assert report["retention"][1]["eligible"] == 2
    assert report["retention"][1]["returned"] == 1
    assert report["retention"][7]["eligible"] == 0
    text, _ = render(1, report, section="retention")
    assert "D1: 1/2 · 50,0%" in text
    clock[0] = start + 7 * 86400
    e.record(A, "tap")
    assert read(path, "clicker", now=clock[0])["retention"][7]["eligible"] == 0
    clock[0] = start + 9 * 86400
    report = read(path, "clicker", now=clock[0])
    assert report["retention"][7]["eligible"] == 3
    assert report["retention"][7]["returned"] == 1
    assert report["active"] == {1: 0, 7: 1, 30: 3}


def test_calendar_boundaries_use_moscow_and_cross_years(env):
    e, db, clock, path = env
    clock[0] = stamp("2026-12-31T23:59:59")
    e.record(A, "start")
    clock[0] += 2
    e.record(A, "tap")
    assert [r[0] for r in db.all("SELECT day FROM engagement_activity ORDER BY day")] == ["2026-12-31", "2027-01-01"]
    assert read(path, "clicker", now=clock[0])["active"] == {1: 1, 7: 1, 30: 1}
    assert day_key(clock[0]) == "2027-01-01"


def test_failed_observation_does_not_rollback_wallet_action(env):
    e, db, clock, path = env
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON engagement_events BEGIN SELECT RAISE(ABORT,'injected'); END")
    with db.tx():
        db.add_balance(A, 5)
        assert not e.record(A, "tap")
    assert db.user(A)["balance"] == 5
    assert counts(db) == (0, 0, 0)
    db.exec("DROP TRIGGER fault")
    assert e.record(A, "tap")
    assert counts(db) == (1, 1, 1)


def test_failed_business_operation_rolls_back_observation_with_it(env):
    e, db, clock, path = env
    with pytest.raises(RuntimeError):
        with db.tx():
            db.add_balance(A, 7)
            e.record(A, "tap")
            raise RuntimeError("injected later business failure")
    assert db.user(A)["balance"] == 0
    assert counts(db) == (0, 0, 0)


def test_two_connections_cannot_duplicate_daily_activity(env):
    e, db, clock, path = env
    second = ChildDB(path)
    other = Engagement(second, clock=lambda: clock[0])
    barrier = threading.Barrier(2)

    def record(collector):
        barrier.wait(timeout=5)
        return collector.record(A, "tap")

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(record, collector) for collector in (e, other)]
            assert all(job.result(timeout=10) for job in jobs)
        assert counts(db) == (1, 1, 1)
    finally:
        second.conn.close()


def test_owner_read_can_exclude_a_new_owner_formerly_active_as_a_player(env):
    e, db, clock, path = env
    e.record(A, "start")
    report = read(path, "clicker", now=clock[0], exclude_uid=A)
    assert report["observed"] == 0 and report["active"][1] == 0
    assert all(value == 0 for _, value in report["steps"])


@pytest.mark.parametrize("state", ["missing", "old", "broken", "partial"])
def test_missing_old_or_invalid_storage_never_becomes_fake_zero(tmp_path, state):
    path = tmp_path / "report.db"
    if state == "old":
        ChildDB(path).conn.close()
    elif state == "broken":
        path.write_text("not SQLite")
    elif state == "partial":
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE engagement_people(user_id INTEGER)")
        conn.close()
    report = read(path, "clicker")
    expected = "not_started" if state == "old" else "missing" if state == "missing" else "error"
    assert report["status"] == expected
    assert "active" not in report
    if state == "missing":
        assert not path.exists()
