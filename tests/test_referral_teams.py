"""Weekly membership, goal snapshots and atomic receipts on synthetic SQLite databases."""

import concurrent.futures
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from child.db import ChildDB
from child.referral_club import SEED, ReferralClub
from child.referral_teams import GRACE, TZ, ReferralTeams, TeamError, week_window
from tests.test_clicker_events import snapshot

OWNER, A, B, C, D, E, F, G = 1, 10, 20, 30, 40, 50, 60, 70


def onboard(club, uid, payload=""):
    club.join(uid, None, f"Synthetic {uid}", payload)
    club.confirm_access(uid)
    club.choose_interest(uid, "learn", club.summary(uid)["member"]["version"])
    club.finish_onboarding(uid, 1, club.summary(uid)["member"]["version"])


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "club.db"
    db = ChildDB(path)
    clock = [int(datetime(2026, 11, 9, 12, tzinfo=TZ).timestamp())]
    club = ReferralClub(db, owner_id=OWNER, clock=lambda: clock[0])
    teams = ReferralTeams(db, owner_id=OWNER, clock=lambda: clock[0])
    for uid in (A, B, C, D, E, F, G):
        onboard(club, uid)
    yield SimpleNamespace(db=db, club=club, teams=teams, clock=clock, path=path)
    db.conn.close()


def create(w, uid=A, name="Исследователи"):
    form = w.teams.form_begin(uid)
    form = w.teams.form_name(uid, name, form["version"])
    return w.teams.create(uid, form["version"])


def join(w, uid, tid):
    w.teams.join(uid, tid, w.teams.owner_team(OWNER, tid)["revision"])


def answer(w, uid, cid=1):
    card = w.club.open_quiz(uid, cid)
    return w.club.answer(uid, cid, 1, card["revision"])


def campaign(w):
    cid = w.club.create_draft(OWNER, {**SEED, "days": 7})
    w.club.publish(OWNER, cid, 1)
    return cid


def win_with_checkins_only(w):
    tid = create(w)
    for uid in (B, C, D, E):
        join(w, uid, tid)
    for _ in range(3):
        for uid in (A, B, C, D, E):
            w.teams.checkin(uid, tid)
        w.clock[0] += 86400
    return tid


def test_empty_schema_and_reopen_never_backfill_members_or_change_wallets(env):
    w = env
    before = snapshot(w.db)
    ReferralTeams(w.db, owner_id=OWNER)
    assert snapshot(w.db) == before
    assert w.teams.list_teams(A)["total"] == 0
    assert not w.teams.membership(A)
    assert not w.teams.form_get(A)
    assert not w.teams.awards(A)["rows"]


def test_team_can_finish_without_inviting_and_badge_does_not_credit_points_or_cash(env):
    w = env
    before = {uid: tuple(w.db.user(uid)) for uid in (A, B, C, D, E)}
    points = [tuple(r) for r in w.db.all("SELECT * FROM ref_club_points")]
    members = [tuple(r) for r in w.db.all("SELECT * FROM ref_club_members")]
    tid = win_with_checkins_only(w)
    t = w.teams.team(A, tid)
    assert t["score"] == 15 and t["mine"]["score"] == 3
    assert not w.db.scalar("SELECT COUNT(*) FROM ref_club_invites")
    title = w.teams.claim(A, tid)
    assert "Исследователи" in title and t["week"] in title
    with pytest.raises(TeamError):
        w.teams.claim(A, tid)
    assert w.teams.awards(A)["rows"][0]["personal_score"] == 3
    assert {uid: tuple(w.db.user(uid)) for uid in before} == before
    assert [tuple(r) for r in w.db.all("SELECT * FROM ref_club_points")] == points
    assert [tuple(r) for r in w.db.all("SELECT * FROM ref_club_members")] == members


def test_existing_quiz_even_same_second_is_excluded_and_new_answer_counts(env):
    w = env
    answer(w, A)
    cid = campaign(w)
    tid = create(w)
    assert w.teams.team(A, tid)["score"] == 0
    answer(w, A, cid)
    t = w.teams.team(A, tid)
    assert t["mine"]["quizzes"] == 1 and t["score"] == 2
    assert w.teams.checkin(A, tid)
    assert not w.teams.checkin(A, tid)
    assert w.teams.team(A, tid)["score"] == 3


def test_old_pending_invite_is_excluded_and_new_qualified_friend_counts_once(env):
    w = env
    token = w.club.personal_link(A, 1)
    w.club.join(100, None, "Old pending", "c_" + token)
    tid = create(w)
    # Finish the existing invitation after team entry at the same clock second.
    w.club.confirm_access(100)
    w.club.choose_interest(100, "learn", w.club.summary(100)["member"]["version"])
    w.club.finish_onboarding(100, 1, w.club.summary(100)["member"]["version"])
    answer(w, 100)
    assert w.teams.team(A, tid)["score"] == 0
    onboard(w.club, 200, "c_" + token)
    answer(w, 200)
    assert w.teams.team(A, tid)["mine"]["friends"] == 1
    assert w.teams.team(A, tid)["score"] == 3
    w.club.confirm_access(200)
    assert w.teams.team(A, tid)["score"] == 3


def test_full_team_and_weekly_membership_lock_are_enforced(env):
    w = env
    first, second = create(w), create(w, F, "Другая команда")
    for uid in (B, C, D, E):
        join(w, uid, first)
    with pytest.raises(TeamError):
        join(w, G, first)
    with pytest.raises(TeamError):
        join(w, B, second)
    with pytest.raises(TeamError):
        w.teams.form_begin(B)
    join(w, A, first)  # Idempotent duplicate, including after all places were taken.
    assert w.teams.team(A, first)["members"] == 5


def test_pause_only_blocks_new_entries_and_current_owner_revision_is_required(env):
    w = env
    tid = create(w)
    w.teams.set_status(OWNER, tid, "paused", 1)
    with pytest.raises(TeamError):
        w.teams.join(B, tid, 1)
    with pytest.raises(TeamError):
        w.teams.join(B, tid, 2)
    assert w.teams.checkin(A, tid)
    w.teams.set_status(OWNER, tid, "open", 2)
    with pytest.raises(TeamError):
        w.teams.join(B, tid, 1)
    join(w, B, tid)
    with pytest.raises(TeamError):
        w.teams.set_status(OWNER, tid, "paused", 2)


@pytest.mark.parametrize("operation", ["list", "detail", "status"])
def test_owner_transfer_rechecks_authority_on_each_operation(env, operation):
    w = env
    tid = create(w)
    w.teams.owner_check = lambda uid: uid == B
    actions = {
        "list": lambda: w.teams.owner_list(OWNER),
        "detail": lambda: w.teams.owner_team(OWNER, tid),
        "status": lambda: w.teams.set_status(OWNER, tid, "paused", 1),
    }
    with pytest.raises(TeamError):
        actions[operation]()
    assert w.teams.owner_team(B, tid)["status"] == "open"


def test_incomplete_onboarding_or_access_cannot_join_create_checkin_or_claim(env):
    w = env
    tid = create(w)
    w.club.join(100, None, "Not onboarded", "")
    for action in (
        lambda: w.teams.form_begin(100),
        lambda: w.teams.join(100, tid, 1),
        lambda: w.teams.checkin(100, tid),
        lambda: w.teams.claim(100, tid),
    ):
        with pytest.raises(TeamError):
            action()
    w.club.clear_access(A)
    with pytest.raises(TeamError):
        w.teams.checkin(A, tid)


@pytest.mark.parametrize("name", ["", "a" * 49, "two\nlines", "123456:FAKE_CONTENT_TOKEN_NOT_REAL_0123456789"])
def test_invalid_team_name_keeps_original_form(env, name):
    w = env
    form = w.teams.form_begin(A)
    with pytest.raises(TeamError):
        w.teams.form_name(A, name, form["version"])
    assert w.teams.form_get(A) == form


def test_persistent_form_versions_do_not_overwrite_new_or_completed_forms(env):
    w = env
    first = w.teams.form_begin(A)
    w.teams.form_discard(A, first["version"])
    second = w.teams.form_begin(A)
    with pytest.raises(TeamError):
        w.teams.form_discard(A, first["version"])
    with pytest.raises(TeamError):
        w.teams.form_name(A, "stale", first["version"])
    named = w.teams.form_name(A, "Persisted", second["version"])
    w.teams.form_pause(A)
    other = ReferralTeams(w.db, owner_id=OWNER, clock=lambda: w.clock[0], initialize=False)
    assert other.form_get(A)["name"] == "Persisted"
    assert other.form_begin(A)["active"] == 1
    tid = other.create(A, named["version"])
    assert not other.form_get(A)
    with pytest.raises(TeamError):
        other.create(A, named["version"])
    assert other.team(A, tid)["members"] == 1


def test_form_from_last_week_cannot_create_team_and_new_week_allows_new_membership(env):
    w = env
    form = w.teams.form_begin(F)
    tid = create(w)
    end = w.teams.team(A, tid)["end_at"]
    w.clock[0] = end
    with pytest.raises(TeamError):
        w.teams.create(F, form["version"])
    assert w.teams.form_begin(F)["version"] != form["version"]
    next_id = create(w, A, "Следующая неделя")
    assert w.teams.membership(A)["team_id"] == next_id
    assert w.teams.history(A)["total"] == 2
    with pytest.raises(TeamError):
        w.teams.join(B, tid, 1)
    with pytest.raises(TeamError):
        w.teams.checkin(A, tid)
    with pytest.raises(TeamError):
        w.teams.set_status(OWNER, tid, "paused", 1)


def test_points_freeze_at_end_and_badge_grace_expires_at_exact_boundary(env):
    w = env
    tid = win_with_checkins_only(w)
    original = w.teams.team(A, tid)
    cid = campaign(w)
    token = w.club.personal_link(A, 1)
    w.club.join(100, None, "Pending during team week", "c_" + token)
    w.clock[0] = original["end_at"]
    answer(w, A, cid)
    w.club.confirm_access(100)
    w.club.choose_interest(100, "learn", w.club.summary(100)["member"]["version"])
    w.club.finish_onboarding(100, 1, w.club.summary(100)["member"]["version"])
    answer(w, 100)
    assert w.teams.team(A, tid)["score"] == 15
    w.clock[0] = original["end_at"] + GRACE - 1
    assert w.teams.claim(A, tid)
    w.clock[0] += 1
    with pytest.raises(TeamError):
        w.teams.claim(B, tid)
    assert not w.teams.awards(B)["rows"]


def test_no_badge_for_spectator_or_non_contributor_or_solo_team(env):
    w = env
    tid = create(w)
    for _ in range(8):
        cid = campaign(w)
        answer(w, A, cid)
    assert w.teams.team(A, tid)["score"] == 16
    with pytest.raises(TeamError):
        w.teams.claim(A, tid)
    join(w, B, tid)
    with pytest.raises(TeamError):
        w.teams.claim(B, tid)
    with pytest.raises(TeamError):
        w.teams.claim(C, tid)
    assert w.teams.claim(A, tid)


def test_moscow_midnight_clock_rollback_and_iso_week_year(env):
    w = env
    tid = create(w)
    assert w.teams.checkin(A, tid)
    w.clock[0] -= 1
    with pytest.raises(TeamError):
        w.teams.checkin(A, tid)
    w.clock[0] = int(datetime(2026, 11, 10, tzinfo=TZ).timestamp())
    assert w.teams.checkin(A, tid)
    assert w.teams.team(A, tid)["score"] == 2
    at = int(datetime(2027, 1, 1, tzinfo=TZ).timestamp())
    week, start, end = week_window(at)
    assert week == "2026W53"
    assert datetime.fromtimestamp(start, TZ).strftime("%Y-%m-%d") == "2026-12-28"
    assert datetime.fromtimestamp(end, TZ).strftime("%Y-%m-%d") == "2027-01-04"


@pytest.mark.parametrize(
    "table,operation", [("ref_team_members", "INSERT"), ("ref_team_audit", "INSERT"), ("ref_team_forms", "DELETE")]
)
def test_team_creation_form_removal_membership_and_audit_rollback_together(env, table, operation):
    w = env
    f = w.teams.form_begin(A)
    f = w.teams.form_name(A, "Atomic", f["version"])
    w.db.exec(f"CREATE TRIGGER fail BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        w.teams.create(A, f["version"])
    assert snapshot(w.db) == before


def test_owner_pause_rolls_back_if_audit_fails(env):
    w = env
    tid = create(w)
    w.db.exec("CREATE TRIGGER fail BEFORE INSERT ON ref_team_audit BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        w.teams.set_status(OWNER, tid, "paused", 1)
    assert snapshot(w.db) == before


def test_failed_receipt_can_be_retried_without_touching_other_data(env):
    w = env
    tid = win_with_checkins_only(w)
    w.db.exec("CREATE TRIGGER fail BEFORE INSERT ON ref_team_claims BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before = snapshot(w.db)
    with pytest.raises(sqlite3.IntegrityError):
        w.teams.claim(A, tid)
    assert snapshot(w.db) == before
    w.db.exec("DROP TRIGGER fail")
    assert w.teams.claim(A, tid)


@pytest.mark.parametrize("race", ["last_place", "create", "checkin", "claim", "two_teams"])
def test_two_connections_cannot_overfill_choose_twice_or_duplicate_actions(env, race):
    w = env
    if race == "claim":
        tid = win_with_checkins_only(w)
        args = [("claim", A, tid), ("claim", A, tid)]
    elif race == "create":
        f = w.teams.form_begin(A)
        f = w.teams.form_name(A, "Race", f["version"])
        args = [("create", A, f["version"])] * 2
    else:
        tid = create(w)
        if race == "last_place":
            for uid in (B, C, D):
                join(w, uid, tid)
            args = [("join", E, tid, 1), ("join", F, tid, 1)]
        elif race == "two_teams":
            other_tid = create(w, B, "Other")
            args = [("join", C, tid, 1), ("join", C, other_tid, 1)]
        else:
            args = [("checkin", A, tid)] * 2
    other_db = ChildDB(w.path)
    other = ReferralTeams(other_db, owner_id=OWNER, clock=lambda: w.clock[0], initialize=False)
    barrier = threading.Barrier(2)

    def action(engine, parameters):
        barrier.wait(timeout=5)
        try:
            result = getattr(engine, parameters[0])(*parameters[1:])
            return result if race == "checkin" else True
        except TeamError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(action, engine, parameters) for engine, parameters in zip((w.teams, other), args, strict=True)]
            assert sum(bool(job.result(timeout=10)) for job in jobs) == 1
        if race == "last_place":
            assert w.teams.team(A, tid)["members"] == 5
        if race == "claim":
            assert w.db.scalar("SELECT COUNT(*) FROM ref_team_claims") == 1
    finally:
        other_db.conn.close()


def test_read_only_views_never_create_membership_profiles_or_claims(env):
    w = env
    tid = create(w)
    before = snapshot(w.db)
    for _ in range(2):
        w.teams.team(B, tid)
        w.teams.list_teams(B, ranking=True)
        w.teams.owner_list(OWNER)
        w.teams.owner_team(OWNER, tid)
        w.teams.awards(B)
        w.teams.history(B)
    assert snapshot(w.db) == before


@pytest.mark.parametrize("page", [-1, 1000001, True])
def test_invalid_pages_and_week_scopes_are_rejected(env, page):
    with pytest.raises(TeamError):
        env.teams.list_teams(A, page)
    with pytest.raises(TeamError):
        env.teams.awards(A, page)
    with pytest.raises(TeamError):
        env.teams.history(A, page)
    with pytest.raises(TeamError):
        env.teams.owner_list(OWNER, offset=page)
