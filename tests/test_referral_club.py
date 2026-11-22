"""Referral campaign accounting on synthetic data; existing cash rewards are unchanged."""

import concurrent.futures
import sqlite3
import threading

import pytest

from child.db import ChildDB
from child.referral_club import SEED, WIZARD_FIELDS, ClubError, ReferralClub, validate_campaign

OWNER, A, B, C = 1, 10, 20, 30


@pytest.fixture
def env(tmp_path):
    db = ChildDB(tmp_path / "club.db")
    clock = [1794301200]
    club = ReferralClub(db, owner_id=OWNER, clock=lambda: clock[0])
    club.join(A, None, "Synthetic inviter", "")
    club.confirm_access(A)
    p = club.summary(A)["member"]
    club.choose_interest(A, "learn", p["version"])
    club.finish_onboarding(A, 1, club.summary(A)["member"]["version"])
    yield club, db, clock
    db.conn.close()


def qualify(club, uid):
    club.confirm_access(uid)
    club.choose_interest(uid, "learn", club.summary(uid)["member"]["version"])
    club.finish_onboarding(uid, 1, club.summary(uid)["member"]["version"])
    campaign = club.open_quiz(uid, 1)
    return club.answer(uid, 1, 1, campaign["revision"])


def test_seed_and_reopen_do_not_change_wallet_or_duplicate_campaign(env):
    club, db, clock = env
    db.add_balance(A, 17.5)
    before = tuple(db.user(A))
    ReferralClub(db, owner_id=OWNER, clock=lambda: clock[0])
    assert tuple(db.user(A)) == before
    assert db.scalar("SELECT COUNT(*) FROM ref_campaigns") == 1
    assert club.summary(A)["member"]["points"] == 20
    assert db.user(A)["balance"] == 17.5


def test_qualified_friend_rewards_once_and_preserves_legacy_rule(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    assert len("c_" + token) <= 64
    assert club.join(B, None, "Synthetic friend", "c_" + token)
    assert db.user(B)["referrer_id"] == A
    assert db.reward_referrer_once(B, 5) == A
    assert club.summary(A)["qualified"] == 0
    assert qualify(club, B)["correct"]
    assert club.summary(A)["qualified"] == 1
    assert club.summary(A)["member"]["points"] == 45
    assert club.summary(B)["member"]["points"] == 30
    assert db.user(A)["balance"] == 5 and db.user(B)["balance"] == 0
    with pytest.raises(ClubError):
        club.answer(B, 1, 1, club.open_quiz(B, 1)["revision"])
    club.confirm_access(B)
    assert club.summary(A)["member"]["points"] == 45


def test_existing_user_and_self_link_do_not_create_campaign_referral(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    club.join(A, None, "Synthetic inviter", "c_" + token)
    db.upsert_user(B, None, "Existing", None)
    club.join(B, None, "Existing", "c_" + token)
    assert db.user(B)["referrer_id"] is None
    assert db.scalar("SELECT COUNT(*) FROM ref_club_invites") == 0


def test_first_source_cannot_be_overwritten(env):
    club, db, clock = env
    club.join(C, None, "Other inviter", "")
    qualify(club, C)
    first, second = club.personal_link(A, 1), club.personal_link(C, 1)
    club.join(B, None, "Friend", "c_" + first)
    club.join(B, None, "Friend", "c_" + second)
    qualify(club, B)
    assert club.summary(A)["qualified"] == 1 and club.summary(C)["qualified"] == 0
    assert db.user(B)["referrer_id"] == A


def test_no_campaign_points_before_access_and_both_tasks(env):
    club, db, clock = env
    club.join(B, None, "Friend", "c_" + club.personal_link(A, 1))
    club.choose_interest(B, "events", club.summary(B)["member"]["version"])
    club.finish_onboarding(B, 1, club.summary(B)["member"]["version"])
    campaign = club.open_quiz(B, 1)
    club.answer(B, 1, 1, campaign["revision"])
    assert club.summary(A)["qualified"] == 0
    assert club.summary(B)["member"]["points"] == 30
    club.confirm_access(B)
    assert club.summary(A)["qualified"] == 1


def test_wrong_answer_and_retry_limits_are_persistent(env):
    club, db, clock = env
    campaign = club.open_quiz(A, 1)
    assert not club.answer(A, 1, 0, campaign["revision"])["correct"]
    with pytest.raises(ClubError):
        club.answer(A, 1, 1, campaign["revision"])
    for _ in range(2):
        clock[0] += 30
        assert not club.answer(A, 1, 0, campaign["revision"])["correct"]
    clock[0] += 30
    with pytest.raises(ClubError):
        club.answer(A, 1, 1, campaign["revision"])
    clock[0] += 86400
    assert club.answer(A, 1, 1, campaign["revision"])["correct"]
    assert club.summary(A)["member"]["points"] == 30


@pytest.mark.parametrize("table", ["ref_club_points", "ref_club_badges"])
def test_qualification_failure_rolls_back_reward_and_quota(env, table):
    club, db, clock = env
    club.join(B, None, "Friend", "c_" + club.personal_link(A, 1))
    club.confirm_access(B)
    club.choose_interest(B, "tasks", club.summary(B)["member"]["version"])
    club.finish_onboarding(B, 1, club.summary(B)["member"]["version"])
    campaign = club.open_quiz(B, 1)
    db.exec(f"CREATE TRIGGER fault BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        club.answer(B, 1, 1, campaign["revision"])
    assert club.summary(A)["member"]["points"] == 20
    assert club.summary(A)["qualified"] == 0
    assert db.scalar("SELECT qualified_count FROM ref_campaigns WHERE id=1") == 0
    assert db.scalar("SELECT answered_at FROM ref_campaign_answers WHERE user_id=?", B) is None
    db.exec("DROP TRIGGER fault")
    assert club.answer(B, 1, 1, campaign["revision"])["correct"]


def test_registration_and_attribution_are_atomic(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON ref_club_invites BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        club.join(B, None, "Friend", "c_" + token)
    assert db.user(B) is None


def test_pause_blocks_new_entries_but_allows_existing_participant(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    club.join(B, None, "Friend", "c_" + token)
    campaign = club.owner_campaign(OWNER, 1)
    club.set_status(OWNER, 1, "paused", campaign["revision"])
    club.join(C, None, "Late friend", "c_" + token)
    assert db.user(C)["referrer_id"] is None
    assert qualify(club, B)["correct"]
    assert club.summary(A)["qualified"] == 1


def test_expired_campaign_does_not_qualify_or_award_referral(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    club.join(B, None, "Friend", "c_" + token)
    db.exec("UPDATE ref_campaigns SET ends_at=? WHERE id=1", clock[0] + 1)
    clock[0] += 2
    club.confirm_access(B)
    assert club.summary(A)["qualified"] == 0
    assert club.summary(A)["member"]["points"] == 20
    with pytest.raises(ClubError):
        club.personal_link(A, 1)


def test_campaign_qualification_cap_is_atomic_across_connections(env, tmp_path):
    club, db, clock = env
    token = club.personal_link(A, 1)
    db.exec("UPDATE ref_campaigns SET quota=1 WHERE id=1")
    for uid in (B, C):
        club.join(uid, None, "Friend", "c_" + token)
        club.confirm_access(uid)
        club.choose_interest(uid, "learn", club.summary(uid)["member"]["version"])
        club.finish_onboarding(uid, 1, club.summary(uid)["member"]["version"])
        club.open_quiz(uid, 1)
    second = ChildDB(tmp_path / "club.db")
    other = ReferralClub(second, owner_id=OWNER, clock=lambda: clock[0])
    barrier = threading.Barrier(2)

    def answer(engine, uid):
        barrier.wait(timeout=5)
        return engine.answer(uid, 1, 1, 1)

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(answer, engine, uid) for engine, uid in ((club, B), (other, C))]
            assert all(j.result(timeout=10)["correct"] for j in jobs)
        assert club.summary(A)["qualified"] == 1
        assert club.summary(A)["member"]["points"] == 45
        assert db.scalar("SELECT COUNT(*) FROM ref_club_invites WHERE status='rejected'") == 1
    finally:
        second.conn.close()


def campaign_data(**changes):
    return {**SEED, "days": 7, **changes}


def test_wizard_resume_edit_publish_and_audit(env, tmp_path):
    club, db, clock = env
    club.wizard_begin(OWNER)
    club.wizard_input(OWNER, "Local campaign")
    club.wizard_pause(OWNER)
    second = ChildDB(tmp_path / "club.db")
    try:
        reopened = ReferralClub(second, owner_id=OWNER, clock=lambda: clock[0])
        draft = reopened.wizard_begin(OWNER)
        assert draft["step"] == "description" and draft["data"]["title"] == "Local campaign"
        for field in WIZARD_FIELDS[1:]:
            reopened.wizard_input(OWNER, campaign_data()[field])
        draft = reopened.wizard_get(OWNER)
        with pytest.raises(ClubError):
            reopened.wizard_save(OWNER, draft["version"] - 1)
        reopened.wizard_edit_field(OWNER, "reward", draft["version"])
        edited = reopened.wizard_input(OWNER, "70")
        assert edited["step"] == "confirm"
        cid = reopened.wizard_save(OWNER, edited["version"])
        assert reopened.owner_campaign(OWNER, cid)["status"] == "draft"
        assert reopened.owner_campaign(OWNER, cid)["reward"] == 70
        assert reopened.list_campaigns(A)["total"] == 1
        assert reopened.wizard_get(OWNER) is None
        with pytest.raises(ClubError):
            reopened.wizard_save(OWNER, edited["version"])
        reopened.publish(OWNER, cid, 1)
        c = reopened.owner_campaign(OWNER, cid)
        assert c["ends_at"] == clock[0] + 7 * 86400 and c["revision"] == 2
        assert "correct" not in reopened.campaign(A, cid)
        with pytest.raises(ClubError):
            reopened.publish(OWNER, cid, 1)
        with pytest.raises(ClubError):
            reopened.wizard_edit_campaign(OWNER, cid, 2)
        assert [r["kind"] for r in second.all("SELECT kind FROM ref_campaign_events WHERE campaign_id=? ORDER BY id", cid)] == [
            "draft",
            "publish",
        ]
    finally:
        second.conn.close()


def test_copy_is_idempotent_editable_and_preserves_original(env):
    club, db, clock = env
    cid = club.copy_draft(OWNER, 1)
    assert club.copy_draft(OWNER, 1) == cid
    c = club.owner_campaign(OWNER, cid)
    draft = club.wizard_edit_campaign(OWNER, cid, c["revision"])
    club.wizard_edit_field(OWNER, "title", draft["version"])
    draft = club.wizard_input(OWNER, "Edited copy")
    assert club.wizard_save(OWNER, draft["version"]) == cid
    c = club.owner_campaign(OWNER, cid)
    assert c["title"] == "Edited copy" and c["revision"] == 2
    assert club.owner_campaign(OWNER, 1)["title"] == SEED["title"]
    with pytest.raises(ClubError):
        club.publish(OWNER, cid, 1)
    club.publish(OWNER, cid, 2)
    assert club.copy_draft(OWNER, 1) != cid


def test_stale_edit_cannot_overwrite_published_campaign(env):
    club, db, clock = env
    cid = club.create_draft(OWNER, campaign_data())
    draft = club.wizard_edit_campaign(OWNER, cid, 1)
    club.wizard_edit_field(OWNER, "reward", draft["version"])
    draft = club.wizard_input(OWNER, 99)
    club.publish(OWNER, cid, 1)
    with pytest.raises(ClubError):
        club.wizard_save(OWNER, draft["version"])
    assert club.owner_campaign(OWNER, cid)["reward"] == 25
    assert club.wizard_get(OWNER)


def test_publish_and_edit_failures_keep_draft_and_audit_consistent(env):
    club, db, clock = env
    cid = club.create_draft(OWNER, campaign_data())
    draft = club.wizard_edit_campaign(OWNER, cid, 1)
    club.wizard_edit_field(OWNER, "title", draft["version"])
    draft = club.wizard_input(OWNER, "Changed")
    db.exec("CREATE TRIGGER fault BEFORE INSERT ON ref_campaign_events BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        club.wizard_save(OWNER, draft["version"])
    assert club.owner_campaign(OWNER, cid)["title"] == SEED["title"]
    assert club.wizard_get(OWNER)["data"]["title"] == "Changed"
    with pytest.raises(sqlite3.IntegrityError):
        club.publish(OWNER, cid, 1)
    assert club.owner_campaign(OWNER, cid)["status"] == "draft"
    assert club.owner_campaign(OWNER, cid)["revision"] == 1


def test_owner_permissions_are_rechecked_in_domain(env):
    club, db, clock = env
    current = [OWNER]
    club.owner_check = lambda actor: actor == current[0]
    cid = club.create_draft(OWNER, campaign_data())
    current[0] = A
    for call in (
        lambda: club.owner_campaign(OWNER, cid),
        lambda: club.publish(OWNER, cid, 1),
        lambda: club.copy_draft(OWNER, cid),
        lambda: club.wizard_begin(OWNER),
        lambda: club.stats(OWNER),
        lambda: club.list_campaigns(OWNER, owner=True),
    ):
        with pytest.raises(ClubError):
            call()
    club.publish(A, cid, 1)
    assert club.owner_campaign(A, cid)["status"] == "active"


def test_nested_transaction_can_recover_inner_failure_without_losing_outer_write(env):
    club, db, clock = env
    with db.tx():
        db.add_balance(A, 7)
        try:
            with db.tx():
                db.add_balance(A, 3)
                raise RuntimeError("injected inner failure")
        except RuntimeError:
            pass
        assert db.user(A)["balance"] == 7
        db.add_balance(A, 2)
    assert db.user(A)["balance"] == 9


def test_goal_badges_and_ledger_reconcile_without_cash(env):
    club, db, clock = env
    token = club.personal_link(A, 1)
    for uid in range(100, 110):
        club.join(uid, None, "Synthetic participant", "c_" + token)
        qualify(club, uid)
    s = club.summary(A)
    assert s["qualified"] == 10 and s["member"]["points"] == 270
    assert {r["key"] for r in s["badges"]} == {"welcome", "first", "ambassador", "guide", "goal:1"}
    assert db.scalar("SELECT SUM(amount) FROM ref_club_points WHERE user_id=?", A) == s["member"]["points"]
    assert db.user(A)["balance"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "x" * 81},
        {"description": "x" * 901},
        {"question": "x" * 181},
        {"goal": 0},
        {"goal": 1001},
        {"goal": True},
        {"goal": "9" * 6000},
        {"goal": "١"},
        {"reward": -1},
        {"reward": 1001},
        {"reward": "1.5"},
        {"quota": 2},
        {"quota": 100001},
        {"days": 0},
        {"days": 366},
        {"correct": -1},
        {"correct": 3},
        {"answers": ["a", "a", "b"]},
        {"answers": ["a", "A", "b"]},
        {"answers": ["a", "b"]},
        {"answers": ["a", "b", "x" * 81]},
    ],
)
def test_invalid_campaign_is_rejected_before_publication(env, changes):
    club, db, clock = env
    with pytest.raises(ClubError):
        club.create_draft(OWNER, campaign_data(**changes))
    assert db.scalar("SELECT COUNT(*) FROM ref_campaigns") == 1
    assert validate_campaign(campaign_data())["days"] == 7
