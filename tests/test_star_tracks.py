"""Track ordering, immutable lesson snapshots and badge transactions on synthetic databases."""

import concurrent.futures
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from child.db import ChildDB
from child.star_journey import TZ, StarJourney, period
from child.star_tracks import GRACE, StarTracks, TrackError
from tests.test_clicker_events import snapshot

OWNER, A, B = 1, 10, 20
VALUES = {
    "title": "Маленькие открытия",
    "description": "Изучи три урока и подтверди ответы.",
    "cards": [1, 2, 3],
    "starts": "сегодня",
    "days": 7,
    "badge": "🧭 Исследователь",
}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "star.db"
    db = ChildDB(path)
    for uid in (OWNER, A, B):
        db.upsert_user(uid, None, f"Synthetic {uid}", None)
    clock = [int(datetime(2026, 11, 11, 12, tzinfo=TZ).timestamp())]
    sky = StarJourney(db, owner_id=OWNER, clock=lambda: clock[0])
    tracks = StarTracks(db, owner_id=OWNER, clock=lambda: clock[0])
    yield SimpleNamespace(path=path, db=db, sky=sky, tracks=tracks, clock=clock)
    db.conn.close()


def publish(w, **values):
    tid = w.tracks.create_draft(OWNER, {**VALUES, **values})
    w.tracks.publish(OWNER, tid, 1)
    return tid


def join(w, tid, uid=A):
    w.tracks.join(uid, tid, w.tracks.owner_track(OWNER, tid)["revision"])


def answer(w, tid, pos, uid=A):
    stage = w.tracks.open_stage(uid, tid, pos)
    choice = w.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=?", tid, pos)
    return w.tracks.answer(uid, tid, pos, choice, stage["version"])


def finish(w, tid, uid=A):
    for pos in range(len(w.tracks.owner_track(OWNER, tid)["cards"])):
        assert answer(w, tid, pos, uid)


def test_seed_is_hidden_draft_and_initialization_never_backfills_players(env):
    w = env
    before = snapshot(w.db)
    StarTracks(w.db, owner_id=OWNER, clock=lambda: w.clock[0])
    assert snapshot(w.db) == before
    assert w.tracks.owner_track(OWNER, 1)["status"] == "draft"
    assert not w.tracks.list_tracks(A)["rows"]
    assert w.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0
    with pytest.raises(TrackError):
        w.tracks.player_track(A, 1)


def test_track_and_badge_never_credit_cash_sparks_or_daily_metrics(env):
    w = env
    w.db.add_balance(A, 17.5)
    before = {
        name: rows for name, rows in snapshot(w.db).items() if not name.startswith("star_track") and name != "sqlite_sequence"
    }
    tid = publish(w)
    join(w, tid)
    finish(w, tid)
    assert w.tracks.claim(A, tid) == VALUES["badge"]
    after = snapshot(w.db)
    assert all(after[name] == rows for name, rows in before.items())
    assert w.db.scalar("SELECT COUNT(*) FROM star_profiles") == 0
    assert w.tracks.awards(A)["rows"][0]["badge_title"] == VALUES["badge"]
    with pytest.raises(TrackError):
        w.tracks.claim(A, tid)


def test_only_current_stage_can_be_answered_and_previous_can_be_reviewed(env):
    w = env
    tid = publish(w)
    with pytest.raises(TrackError):
        w.tracks.open_stage(A, tid, 0)
    join(w, tid)
    with pytest.raises(TrackError):
        w.tracks.open_stage(A, tid, 1)
    with pytest.raises(TrackError):
        w.tracks.answer(A, tid, 0, 0, 1)
    stage = w.tracks.open_stage(A, tid, 0)
    assert "correct" not in stage and "answers_json" not in stage
    assert all("correct" not in s and "lesson" not in s for s in w.tracks.player_track(A, tid)["stages"])
    assert answer(w, tid, 0)
    before = snapshot(w.db)
    assert w.tracks.open_stage(A, tid, 0)["answer"]["answered_at"] is not None
    assert snapshot(w.db) == before
    with pytest.raises(TrackError):
        w.tracks.answer(A, tid, 0, 0, 2)
    assert w.tracks.player_track(A, tid)["player"]["current_step"] == 1


def test_daily_card_completion_does_not_skip_track_or_award_sparks_twice(env):
    w = env
    deck = w.sky.deck(A)
    cards = [c["id"] for c in deck["cards"]]
    first = cards[0]
    c = w.sky.open_card(A, first, deck["day"])
    choice = w.db.scalar("SELECT correct FROM star_cards WHERE id=?", first)
    w.sky.answer(A, first, choice, c["revision"], deck["day"])
    tid = publish(w, cards=cards)
    join(w, tid)
    assert w.tracks.player_track(A, tid)["player"]["current_step"] == 0
    before = snapshot(w.db)
    finish(w, tid)
    w.tracks.claim(A, tid)
    after = snapshot(w.db)
    for name in ("star_sparks", "star_metrics", "star_answers", "star_decks", "star_profiles", "users"):
        assert after[name] == before[name]


def test_published_snapshot_survives_source_pause_and_changed_source_content(env):
    w = env
    tid = publish(w)
    original = w.tracks.owner_track(OWNER, tid)["stages"]
    w.sky.set_status(OWNER, 1, "paused", 1)
    w.db.exec("UPDATE star_cards SET lesson='Changed outside published track' WHERE id=2")
    assert w.tracks.owner_track(OWNER, tid)["stages"] == original
    join(w, tid)
    finish(w, tid)
    assert w.tracks.player_track(A, tid)["player"]["completed_at"] == w.clock[0]


def test_wrong_answers_cooldown_and_versions_survive_restart(env):
    w = env
    tid = publish(w)
    join(w, tid)
    correct = w.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=0", tid)
    for _ in range(3):
        stage = w.tracks.open_stage(A, tid, 0)
        assert not w.tracks.answer(A, tid, 0, (correct + 1) % 3, stage["version"])
        with pytest.raises(TrackError):
            w.tracks.answer(A, tid, 0, correct, stage["version"])
        w.clock[0] += 30
    other = StarTracks(w.db, owner_id=OWNER, clock=lambda: w.clock[0], initialize=False)
    stage = other.open_stage(A, tid, 0)
    assert stage["answer"]["attempts"] == 3
    with pytest.raises(TrackError):
        other.answer(A, tid, 0, correct, stage["version"])
    w.clock[0] = stage["answer"]["retry_at"]
    assert other.answer(A, tid, 0, correct, other.open_stage(A, tid, 0)["version"])


def test_pause_blocks_new_starts_but_participants_continue_and_claim(env):
    w = env
    tid = publish(w)
    join(w, tid)
    w.tracks.set_status(OWNER, tid, "paused", 2)
    with pytest.raises(TrackError):
        w.tracks.join(B, tid, 3)
    finish(w, tid)
    assert w.tracks.claim(A, tid)
    w.tracks.set_status(OWNER, tid, "active", 3)
    with pytest.raises(TrackError):
        w.tracks.join(B, tid, 2)
    join(w, tid, B)


def test_schedule_opens_and_closes_exactly_at_moscow_midnight(env):
    w = env
    tid = publish(w, starts="2026-11-12", days=1)
    with pytest.raises(TrackError):
        join(w, tid)
    w.clock[0] = int(datetime(2026, 11, 12, tzinfo=TZ).timestamp())
    join(w, tid)
    answer(w, tid, 0)
    w.clock[0] = w.tracks.player_track(A, tid)["ends_at"]
    with pytest.raises(TrackError):
        w.tracks.open_stage(A, tid, 1)
    with pytest.raises(TrackError):
        join(w, tid, B)
    assert w.tracks.open_stage(A, tid, 0)["answer"]["answered_at"] is not None
    assert w.tracks.list_tracks(A, mine=True)["rows"][0]["id"] == tid


def test_grace_last_second_then_expiry_and_completed_materials_still_readable(env):
    w = env
    tid = publish(w, days=1)
    for uid in (A, B):
        join(w, tid, uid)
        finish(w, tid, uid)
    end = w.tracks.player_track(A, tid)["ends_at"]
    w.clock[0] = end + GRACE - 1
    assert w.tracks.claim(A, tid)
    w.clock[0] += 1
    with pytest.raises(TrackError):
        w.tracks.claim(B, tid)
    assert w.tracks.open_stage(B, tid, 2)["answer"]["answered_at"] is not None


def test_clock_rollback_cannot_advance_or_claim(env):
    w = env
    tid = publish(w)
    join(w, tid)
    w.clock[0] += 60
    answer(w, tid, 0)
    w.clock[0] -= 1
    with pytest.raises(TrackError):
        w.tracks.open_stage(A, tid, 1)
    w.clock[0] += 1
    answer(w, tid, 1)
    answer(w, tid, 2)
    w.clock[0] -= 1
    with pytest.raises(TrackError):
        w.tracks.claim(A, tid)


@pytest.mark.parametrize("problem", ["missing", "draft", "paused", "late_start", "early_end"])
def test_source_must_be_published_for_entire_route_and_publish_rolls_back(env, problem):
    w = env
    tid = w.tracks.create_draft(OWNER, VALUES)
    if problem == "missing":
        w.db.exec("DELETE FROM star_cards WHERE id=3")
    elif problem in {"draft", "paused"}:
        w.db.exec("UPDATE star_cards SET status=? WHERE id=3", problem)
    else:
        key, value = ("start_day", "20261112") if problem == "late_start" else ("end_day", "20261111")
        w.db.exec(f"UPDATE star_cards SET {key}=? WHERE id=3", value)
    before = snapshot(w.db)
    with pytest.raises(TrackError):
        w.tracks.publish(OWNER, tid, 1)
    assert snapshot(w.db) == before


def test_published_conditions_are_immutable_and_copy_is_editable_and_idempotent(env):
    w = env
    tid = publish(w)
    with pytest.raises(TrackError):
        w.tracks.update_draft(OWNER, tid, 2, VALUES)
    copy = w.tracks.copy_draft(OWNER, tid)
    assert copy == w.tracks.copy_draft(OWNER, tid)
    w.tracks.update_draft(OWNER, copy, 1, {**VALUES, "title": "Revised", "cards": [3, 2, 1]})
    assert w.tracks.owner_track(OWNER, copy)["cards"] == [3, 2, 1]
    with pytest.raises(TrackError):
        w.tracks.publish(OWNER, copy, 1)
    w.tracks.publish(OWNER, copy, 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cards", [1, 1, 2]),
        ("cards", [1, 2]),
        ("cards", list(range(1, 14))),
        ("cards", [True, 2, 3]),
        ("cards", "1,2,٣"),
        ("cards", "1,x,3"),
        ("cards", [1, 2, 2**63]),
        ("title", "a" * 81),
        ("description", "d" * 901),
        ("badge", "b" * 61),
        ("days", 0),
        ("days", True),
        ("days", 366),
        ("starts", "2026-11-10"),
        ("starts", "wrong"),
        ("starts", "2027-11-12"),
    ],
)
def test_invalid_conditions_cannot_create_track(env, field, value):
    with pytest.raises(TrackError):
        env.tracks.create_draft(OWNER, {**VALUES, field: value})
    assert env.db.scalar("SELECT COUNT(*) FROM star_tracks") == 1


@pytest.mark.parametrize("operation", ["create", "edit", "publish", "pause", "copy", "owner", "sources", "list"])
def test_owner_permission_is_current_for_each_owner_action(env, operation):
    w = env
    actions = {
        "create": lambda: w.tracks.create_draft(A, VALUES),
        "edit": lambda: w.tracks.update_draft(A, 1, 1, VALUES),
        "publish": lambda: w.tracks.publish(A, 1, 1),
        "pause": lambda: w.tracks.set_status(A, 1, "paused", 1),
        "copy": lambda: w.tracks.copy_draft(A, 1),
        "owner": lambda: w.tracks.owner_track(A, 1),
        "sources": lambda: w.tracks.sources(A),
        "list": lambda: w.tracks.list_tracks(A, owner=True),
    }
    before = snapshot(w.db)
    with pytest.raises(TrackError):
        actions[operation]()
    assert snapshot(w.db) == before


@pytest.mark.parametrize("failure", ["publish", "answer", "claim"])
def test_database_fault_rolls_back_stage_snapshot_progress_and_badge(env, failure):
    w = env
    tid = w.tracks.create_draft(OWNER, VALUES)
    if failure == "publish":
        clause = "BEFORE INSERT ON star_track_audit"

        def operation():
            return w.tracks.publish(OWNER, tid, 1)
    else:
        w.tracks.publish(OWNER, tid, 1)
        join(w, tid)
        if failure == "answer":
            c = w.tracks.open_stage(A, tid, 0)
            choice = w.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=0", tid)
            clause = "BEFORE UPDATE OF current_step ON star_track_players"

            def operation():
                return w.tracks.answer(A, tid, 0, choice, c["version"])
        else:
            finish(w, tid)
            clause = "BEFORE UPDATE OF badge_claimed_at ON star_track_players"

            def operation():
                return w.tracks.claim(A, tid)

    w.db.exec(f"CREATE TRIGGER fail {clause} BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        operation()
    assert snapshot(w.db) == before
    w.db.exec("DROP TRIGGER fail")
    operation()


@pytest.mark.parametrize("race", ["publish", "answer", "claim"])
def test_two_connections_cannot_publish_answer_or_claim_twice(env, race):
    w = env
    tid = w.tracks.create_draft(OWNER, VALUES)
    if race != "publish":
        w.tracks.publish(OWNER, tid, 1)
        join(w, tid)
        if race == "claim":
            finish(w, tid)
        else:
            c = w.tracks.open_stage(A, tid, 0)
            choice = w.db.scalar("SELECT correct FROM star_track_stages WHERE track_id=? AND position=0", tid)
    other_db = ChildDB(w.path)
    other = StarTracks(other_db, owner_id=OWNER, clock=lambda: w.clock[0], initialize=False)
    barrier = threading.Barrier(2)

    def action(engine):
        barrier.wait(timeout=5)
        try:
            if race == "publish":
                engine.publish(OWNER, tid, 1)
            elif race == "answer":
                engine.answer(A, tid, 0, choice, c["version"])
            else:
                engine.claim(A, tid)
            return True
        except TrackError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(action, engine) for engine in (w.tracks, other)]
            assert sum(job.result(timeout=10) for job in jobs) == 1
        if race == "publish":
            assert w.db.scalar("SELECT COUNT(*) FROM star_track_stages WHERE track_id=?", tid) == 3
        elif race == "answer":
            assert w.tracks.player_track(A, tid)["player"]["current_step"] == 1
        else:
            assert w.tracks.awards(A)["total"] == 1
    finally:
        other_db.conn.close()


def test_read_views_do_not_enroll_or_create_old_game_profiles(env):
    w = env
    tid = publish(w)
    before = snapshot(w.db)
    w.tracks.player_track(A, tid)
    w.tracks.owner_track(OWNER, tid)
    w.tracks.sources(OWNER)
    w.tracks.list_tracks(A, mine=True)
    w.tracks.awards(A)
    assert snapshot(w.db) == before
    with pytest.raises(TrackError):
        w.tracks.player_track(999, tid)


def test_cross_year_calendar_and_completed_source_dates(env):
    w = env
    w.clock[0] = int(datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ).timestamp())
    tid = publish(w, days=1)
    join(w, tid)
    finish(w, tid)
    w.clock[0] += 1
    assert period("day", w.clock[0]) == "20270101"
    assert w.tracks.claim(A, tid)
