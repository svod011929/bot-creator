"""Game state on synthetic users and clocks; none of these actions changes cash rules."""

import concurrent.futures
import json
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from child.db import ChildDB
from child.star_catalog import CONSTELLATIONS, starter_cards
from child.star_journey import EDITOR_FIELDS, JourneyError, StarJourney, period

OWNER, A, B = 1, 10, 20
TZ = ZoneInfo("Europe/Moscow")


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp())


@pytest.fixture
def env(tmp_path):
    db = ChildDB(tmp_path / "star.db")
    for uid in (OWNER, A, B):
        db.upsert_user(uid, None, "Synthetic participant", None)
    clock = [stamp("2026-11-10T09:00:00")]
    game = StarJourney(db, owner_id=OWNER, clock=lambda: clock[0])
    yield game, db, clock
    db.conn.close()


def solve_card(game, uid=A, index=0):
    deck = game.deck(uid)
    card = deck["cards"][index]
    game.open_card(uid, card["id"], deck["day"])
    correct = game.db.scalar("SELECT correct FROM star_cards WHERE id=?", card["id"])
    return game.answer(uid, card["id"], correct, card["revision"], deck["day"])


def memory_prefix(game, uid=A, count=3):
    day = period("day", game.now())
    m = game.memory(uid, day)
    sequence = m["sequence"]
    game.memory_step(uid, day, m["version"])
    for symbol in sequence[:count]:
        m = game.memory(uid, day)
        game.memory_step(uid, day, m["version"], symbol)
    return sequence


def prepare_build(game):
    game.checkin(A, period("day", game.now()))
    solve_card(game)
    memory_prefix(game)
    game.claim(A, "first_card", "once")


def card_data(game, **changes):
    return {**starter_cards()[0], "starts": period("day", game.now()), "days": 7, **changes}


def test_initialization_is_idempotent_and_never_backfills_wallet(env, tmp_path):
    game, db, clock = env
    db.add_balance(A, 17.5)
    before = tuple(db.user(A))
    reopened = StarJourney(db, owner_id=OWNER, clock=lambda: clock[0])
    assert db.scalar("SELECT COUNT(*) FROM star_cards") == 24
    assert db.scalar("SELECT COUNT(*) FROM star_card_events") == 24
    assert tuple(db.user(A)) == before
    assert reopened.overview(A)["profile"]["sparks"] == 0
    assert not db.one("SELECT name FROM sqlite_master WHERE name='clicker_profiles'")
    assert not db.one("SELECT name FROM sqlite_master WHERE name='ref_club_members'")


def test_complete_first_session_builds_first_constellation_without_friends(env):
    game, db, clock = env
    day = period("day", clock[0])
    assert game.checkin(A, day) == 10
    for i in range(3):
        assert solve_card(game, index=i)["amount"] == 15
    memory_prefix(game)
    for key in ("first_card", "first_memory", "daily_route", "daily_cards"):
        scope = "once" if key.startswith("first") else day
        game.claim(A, key, scope)
    before = game.overview(A)["profile"]
    assert before["sparks"] == before["earned"] == 130
    assert all(m["claimed"] for m in game.missions(A, "day")["rows"])
    game.build(A, "compass", before["version"])
    after = game.overview(A)
    assert after["profile"]["sparks"] == 80 and after["profile"]["earned"] == 130
    assert after["built"] == ["compass"]
    assert db.scalar("SELECT SUM(delta) FROM star_sparks WHERE user_id=?", A) == 80
    assert game.ranking("month")[0]["score"] == 130
    assert db.user(A)["balance"] == 0 and db.referrals(A) == 0


def test_streak_cap_gap_and_calendar_boundaries(env):
    game, db, clock = env
    awards = []
    for _ in range(8):
        awards.append(game.checkin(A, period("day", clock[0])))
        with pytest.raises(JourneyError):
            game.checkin(A, period("day", clock[0]))
        clock[0] += 86400
    assert awards == [10, 12, 14, 16, 18, 20, 22, 22]
    clock[0] += 86400
    assert game.overview(A)["streak"] == 0
    assert game.checkin(A, period("day", clock[0])) == 10
    assert game.overview(A)["profile"]["best_streak"] == 8
    assert game.overview(A)["total"]["visits"] == 9
    # An old button cannot claim a new calendar day's reward.
    previous = period("day", clock[0])
    clock[0] += 86400
    with pytest.raises(JourneyError):
        game.checkin(A, previous)


def test_deck_is_frozen_balanced_and_never_exceeds_three(env):
    game, db, clock = env
    deck = game.deck(A)
    ids = [c["id"] for c in deck["cards"]]
    assert len(ids) == 3 and len({c["category"] for c in deck["cards"]}) == 3
    assert all("correct" not in c for c in deck["cards"])
    cid = game.create_draft(OWNER, card_data(game, title="Published later"))
    game.publish(OWNER, cid, 1)
    assert [c["id"] for c in game.deck(A)["cards"]] == ids
    with pytest.raises(JourneyError):
        game.open_card(A, cid, deck["day"])
    for i in range(3):
        solve_card(game, index=i)
    with pytest.raises(JourneyError):
        solve_card(game)
    clock[0] += 86400
    assert not set(ids) & {c["id"] for c in game.deck(A)["cards"]}
    assert game.overview(A)["profile"]["sparks"] == 45


def test_wrong_quiz_attempts_commit_and_survive_reopening(env, tmp_path):
    game, db, clock = env
    deck = game.deck(A)
    c = deck["cards"][0]
    correct = db.scalar("SELECT correct FROM star_cards WHERE id=?", c["id"])
    with pytest.raises(JourneyError):
        game.answer(A, c["id"], correct, c["revision"], deck["day"])
    game.open_card(A, c["id"], deck["day"])
    assert not game.answer(A, c["id"], (correct + 1) % 3, c["revision"], deck["day"])["correct"]
    second = ChildDB(tmp_path / "star.db")
    try:
        other = StarJourney(second, owner_id=OWNER, clock=lambda: clock[0])
        with pytest.raises(JourneyError):
            other.answer(A, c["id"], correct, c["revision"], deck["day"])
        for _ in range(2):
            clock[0] += 30
            assert not other.answer(A, c["id"], (correct + 1) % 3, c["revision"], deck["day"])["correct"]
        row = second.one("SELECT * FROM star_answers WHERE user_id=? AND card_id=?", A, c["id"])
        assert row["attempts"] == 3 and row["retry_at"] == clock[0] + 86400
        assert other.overview(A)["profile"]["sparks"] == 0
    finally:
        second.conn.close()


def test_memory_hides_sequence_and_awards_only_once(env):
    game, db, clock = env
    day = period("day", clock[0])
    m = game.memory(A, day)
    old = m["version"]
    game.memory_step(A, day, old)
    assert "sequence" not in game.memory(A, day)
    with pytest.raises(JourneyError):
        game.memory_step(A, day, old)
    for symbol in m["sequence"]:
        current = game.memory(A, day)
        game.memory_step(A, day, current["version"], symbol)
    assert game.memory(A, day)["phase"] == "done"
    assert game.overview(A)["profile"]["sparks"] == 20
    with pytest.raises(JourneyError):
        game.memory_step(A, day, game.memory(A, day)["version"], 0)
    assert game.overview(A)["profile"]["sparks"] == 20


def test_memory_failures_cooldown_three_attempt_limit_and_new_day(env):
    game, db, clock = env
    day = period("day", clock[0])
    for attempt in range(1, 4):
        m = game.memory(A, day)
        assert m["phase"] == "preview" and m["attempts"] == attempt
        game.memory_step(A, day, m["version"])
        game.memory_step(A, day, game.memory(A, day)["version"], (m["sequence"][0] + 1) % 6)
        assert game.memory(A, day)["phase"] == "failed"
        clock[0] += 60
    assert game.memory(A, day)["phase"] == "failed"
    assert game.overview(A)["profile"]["sparks"] == 0
    clock[0] += 86400
    with pytest.raises(JourneyError):
        game.memory_step(A, day, 0, 0)
    assert game.memory(A, period("day", clock[0]))["attempts"] == 1


def test_missions_require_progress_and_current_period_and_claim_once(env):
    game, db, clock = env
    with pytest.raises(JourneyError):
        game.claim(A, "first_card", "once")
    solve_card(game)
    assert game.claim(A, "first_card", "once") == 10
    with pytest.raises(JourneyError):
        game.claim(A, "first_card", "once")
    for _ in range(3):
        game.checkin(A, period("day", clock[0]))
        clock[0] += 86400
    week = period("week", clock[0])
    assert game.claim(A, "week_visits", week) == 35
    clock[0] += 7 * 86400
    with pytest.raises(JourneyError):
        game.claim(A, "week_visits", week)


def test_month_ranking_uses_earned_sparks_and_iso_week_year(env):
    game, db, clock = env
    assert period("week", stamp("2021-01-01T09:00:00")) == "2020W53"
    clock[0] = stamp("2026-12-31T23:59:59")
    old_day = period("day", clock[0])
    game.checkin(A, old_day)
    clock[0] += 2
    assert not game.ranking("month")
    assert game.checkin(A, period("day", clock[0])) == 12
    assert game.ranking("month")[0]["score"] == 12
    assert game.ranking("all")[0]["score"] == 22
    clock[0] -= 86400
    with pytest.raises(JourneyError):
        game.checkin(A, old_day)


@pytest.mark.parametrize("operation", ["visit", "card", "memory", "mission", "build"])
@pytest.mark.parametrize("table", ["star_sparks", "star_metrics"])
def test_partial_failure_rolls_back_entire_operation(env, operation, table):
    game, db, clock = env
    day = period("day", clock[0])
    if operation == "visit":

        def call():
            return game.checkin(A, day)
    elif operation == "card":
        c = game.deck(A)["cards"][0]
        game.open_card(A, c["id"], day)

        def call():
            return game.answer(A, c["id"], db.scalar("SELECT correct FROM star_cards WHERE id=?", c["id"]), c["revision"], day)
    elif operation == "memory":
        sequence = memory_prefix(game, count=2)
        m = game.memory(A, day)

        def call():
            return game.memory_step(A, day, m["version"], sequence[-1])
    elif operation == "mission":
        solve_card(game)

        def call():
            return game.claim(A, "first_card", "once")
    else:
        prepare_build(game)
        version = game.overview(A)["profile"]["version"]

        def call():
            return game.build(A, "compass", version)

    # Mission claims do not change counters; fault its claim record instead.
    if operation == "mission" and table == "star_metrics":
        table = "star_claims"
    db.exec(f"CREATE TRIGGER fault BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")

    def snapshot():
        names = [r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'star_%'")]
        return {name: [tuple(r) for r in db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}

    before = snapshot()
    with pytest.raises(sqlite3.IntegrityError):
        call()
    assert snapshot() == before
    assert db.user(A)["balance"] == 0


@pytest.mark.parametrize("operation", ["visit", "card", "build", "memory", "mission"])
def test_concurrent_connections_award_or_spend_only_once(env, tmp_path, operation):
    game, db, clock = env
    day = period("day", clock[0])
    if operation == "card":
        c = game.deck(A)["cards"][0]
        game.open_card(A, c["id"], day)
    elif operation == "build":
        prepare_build(game)
    elif operation == "memory":
        sequence = memory_prefix(game, count=2)
        memory = game.memory(A, day)
    elif operation == "mission":
        solve_card(game)
    p = game.overview(A)["profile"]
    second = ChildDB(tmp_path / "star.db")
    other = StarJourney(second, owner_id=OWNER, clock=lambda: clock[0])
    barrier = threading.Barrier(2)

    def run(engine):
        barrier.wait(timeout=5)
        try:
            if operation == "visit":
                engine.checkin(A, day)
            elif operation == "card":
                correct = db.scalar("SELECT correct FROM star_cards WHERE id=?", c["id"])
                engine.answer(A, c["id"], correct, c["revision"], day)
            elif operation == "build":
                engine.build(A, "compass", p["version"])
            elif operation == "memory":
                engine.memory_step(A, day, memory["version"], sequence[-1])
            else:
                engine.claim(A, "first_card", "once")
            return True
        except JourneyError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(run, engine) for engine in (game, other)]
            assert sum(job.result(timeout=10) for job in jobs) == 1
        assert db.scalar("SELECT COUNT(*) FROM star_cards") == 24
    finally:
        second.conn.close()


def test_owner_editor_persistence_copy_schedule_and_stale_versions(env, tmp_path):
    game, db, clock = env
    draft = game.editor_begin(OWNER)
    game.editor_input(OWNER, "logic", draft["version"])
    game.editor_input(OWNER, "Saved title")
    game.editor_pause(OWNER)
    second = ChildDB(tmp_path / "star.db")
    try:
        other = StarJourney(second, owner_id=OWNER, clock=lambda: clock[0])
        assert other.editor_begin(OWNER)["step"] == "lesson"
        for field in EDITOR_FIELDS[2:]:
            other.editor_input(OWNER, card_data(other)[field])
        d = other.editor_get(OWNER)
        other.editor_field(OWNER, "title", d["version"])
        d = other.editor_input(OWNER, "Revised title")
        cid = other.editor_save(OWNER, d["version"])
        assert other.owner_card(OWNER, cid)["status"] == "draft"
        with pytest.raises(JourneyError):
            other.editor_save(OWNER, d["version"])
        other.publish(OWNER, cid, 1)
        assert other.owner_card(OWNER, cid)["title"] == "Revised title"
        with pytest.raises(JourneyError):
            other.editor_begin(OWNER, cid, 2)
        copy = other.copy_card(OWNER, cid)
        assert other.copy_card(OWNER, cid) == copy
        d = other.editor_begin(OWNER, copy, 1)
        other.editor_field(OWNER, "days", d["version"])
        d = other.editor_input(OWNER, "3")
        assert other.editor_save(OWNER, d["version"]) == copy
        assert other.owner_card(OWNER, copy)["revision"] == 2
        with pytest.raises(JourneyError):
            other.publish(OWNER, copy, 1)
    finally:
        second.conn.close()


def test_scheduled_card_and_paused_or_expired_answers(env):
    game, db, clock = env
    db.exec("UPDATE star_cards SET status='paused'")
    tomorrow = datetime.fromtimestamp(clock[0], TZ).date().toordinal() + 1
    starts = datetime.fromordinal(tomorrow).strftime("%Y-%m-%d")
    cid = game.create_draft(OWNER, card_data(game, starts=starts, days=1))
    game.publish(OWNER, cid, 1)
    assert not game.deck(A)["cards"]
    clock[0] += 86400
    c = game.deck(A)["cards"][0]
    game.open_card(A, cid, period("day", clock[0]))
    game.set_status(OWNER, cid, "paused", c["revision"])
    with pytest.raises(JourneyError):
        solve_card(game)
    game.set_status(OWNER, cid, "active", c["revision"] + 1)
    clock[0] += 86400
    assert not game.deck(A)["cards"]
    with pytest.raises(JourneyError):
        game.set_status(OWNER, cid, "paused", c["revision"] + 2)
        game.set_status(OWNER, cid, "active", c["revision"] + 3)


def test_role_transfer_and_failed_edit_publication_do_not_leak_or_overwrite(env):
    game, db, clock = env
    cid = game.create_draft(OWNER, card_data(game))
    draft = game.editor_begin(OWNER, cid, 1)
    game.editor_field(OWNER, "title", draft["version"])
    draft = game.editor_input(OWNER, "Must not overwrite")
    game.publish(OWNER, cid, 1)
    with pytest.raises(JourneyError):
        game.editor_save(OWNER, draft["version"])
    assert game.owner_card(OWNER, cid)["title"] != "Must not overwrite"
    game.owner_check = lambda uid: uid == B
    for call in (
        lambda: game.owner_card(OWNER, cid),
        lambda: game.list_cards(OWNER),
        lambda: game.editor_get(OWNER),
        lambda: game.create_draft(OWNER, card_data(game)),
        lambda: game.stats(OWNER),
    ):
        with pytest.raises(JourneyError):
            call()
    assert game.owner_card(B, cid)["status"] == "active"


def test_owner_audit_failure_rolls_back_edits_and_publication(env):
    game, db, clock = env
    cid = game.create_draft(OWNER, card_data(game))
    d = game.editor_begin(OWNER, cid, 1)
    game.editor_field(OWNER, "title", d["version"])
    d = game.editor_input(OWNER, "Retain this form")
    before = game.owner_card(OWNER, cid)
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON star_card_events BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        game.editor_save(OWNER, d["version"])
    assert game.owner_card(OWNER, cid) == before
    assert game.editor_get(OWNER)["data"]["title"] == "Retain this form"
    with pytest.raises(sqlite3.IntegrityError):
        game.publish(OWNER, cid, 1)
    assert game.owner_card(OWNER, cid) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "unknown"},
        {"title": ""},
        {"title": "🌟" * 81},
        {"lesson": "x" * 701},
        {"question": "x" * 181},
        {"answers": ["a", "A", "b"]},
        {"answers": ["a", "b"]},
        {"answers": ["a", "b", "c" * 81]},
        {"correct": True},
        {"correct": 3},
        {"correct": -1},
        {"days": 0},
        {"days": 366},
        {"days": "9" * 6000},
        {"days": "١"},
        {"starts": "2026-02-30"},
        {"starts": "2020-01-01"},
        {"starts": "2040-01-01"},
    ],
)
def test_invalid_content_never_creates_campaign(env, changes):
    game, db, clock = env
    with pytest.raises(JourneyError):
        game.create_draft(OWNER, card_data(game, **changes))
    assert db.scalar("SELECT COUNT(*) FROM star_cards") == 24


def test_collection_requires_gameplay_and_rejects_repeats_and_old_versions(env):
    game, db, clock = env
    p = game.overview(A)["profile"]
    for key in CONSTELLATIONS:
        with pytest.raises(JourneyError):
            game.build(A, key, p["version"])
    prepare_build(game)
    with pytest.raises(JourneyError):
        game.build(A, "compass", p["version"])
    game.build(A, "compass", game.overview(A)["profile"]["version"])
    with pytest.raises(JourneyError):
        game.build(A, "compass", game.overview(A)["profile"]["version"])
    assert len(game.overview(A)["built"]) == 1
    assert json.loads(db.scalar("SELECT sequence_json FROM star_memory WHERE user_id=?", A))
