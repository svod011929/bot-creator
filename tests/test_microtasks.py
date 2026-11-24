"""Базовые проверки логики шаблона microtasks (без Telegram)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from child.db import ChildDB, now
from child.templates.microtasks import _can_do_task, _proof_types_label, _validate_proof


class FakeApp:
    def __init__(self, db: ChildDB):
        self.db = db
        self.s = {"max_pending": 3, "currency": "₽", "auto_reject_hours": 48}
        self.owner_id = 1


@pytest.fixture
def db(tmp_path: Path):
    return ChildDB(tmp_path / "bot.db")


def test_once_mode_blocks_second_completion(db):
    db.exec(
        "INSERT INTO micro_tasks(title, description, reward, proof_types, approval_mode, "
        "repeat_mode, cooldown_hours, is_active, created_at) "
        "VALUES ('t', '', 10, ?, 'manual', 'once', 0, 1, ?)",
        json.dumps(["text"]), now()
    )
    task = db.one("SELECT * FROM micro_tasks WHERE id=1")
    app = FakeApp(db)
    ok, _ = _can_do_task(app, 100, task)
    assert ok
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, 1, 'x', 'approved', ?)",
        now()
    )
    ok, reason = _can_do_task(app, 100, task)
    assert not ok
    assert "один раз" in reason


def test_cooldown_blocks_early_repeat(db):
    db.exec(
        "INSERT INTO micro_tasks(title, description, reward, proof_types, approval_mode, "
        "repeat_mode, cooldown_hours, is_active, created_at) "
        "VALUES ('t', '', 10, ?, 'auto', 'repeatable', 2, 1, ?)",
        json.dumps(["text"]), now()
    )
    task = db.one("SELECT * FROM micro_tasks WHERE id=1")
    app = FakeApp(db)
    past = now() - 3600  # 1 hour ago
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, 1, 'x', 'approved', ?)",
        past
    )
    ok, reason = _can_do_task(app, 100, task)
    assert not ok
    assert "Кулдаун" in reason


def test_pending_limit(db):
    db.exec(
        "INSERT INTO micro_tasks(title, description, reward, proof_types, approval_mode, "
        "repeat_mode, cooldown_hours, is_active, created_at) "
        "VALUES ('t', '', 10, ?, 'manual', 'repeatable', 0, 1, ?)",
        json.dumps(["text"]), now()
    )
    task = db.one("SELECT * FROM micro_tasks WHERE id=1")
    app = FakeApp(db)
    for _ in range(3):
        db.exec(
            "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, 1, 'x', 'pending', ?)",
            now()
        )
    ok, reason = _can_do_task(app, 100, task)
    assert not ok
    assert "активных заявок" in reason


def test_validate_proof_types():
    class Msg:
        def __init__(self, text=None, photo=None):
            self.text = text
            self.photo = photo

    assert _validate_proof(Msg("hello"), ["text"]) == "hello"
    assert _validate_proof(Msg("https://t.me/x"), ["link"]) == "https://t.me/x"
    assert _validate_proof(Msg("выполнил"), ["button"]) == "button"
    assert _validate_proof(Msg("random"), ["link"]) is None
    assert _validate_proof(Msg(photo=[type("P", (), {"file_id": "abc"})()]), ["photo"]) == "abc"



def test_proof_types_label():
    assert _proof_types_label(["text", "photo"]) == "текст, фото"
    assert _proof_types_label([]) == "—"


def _task(db, *, mode="manual", repeat="once", reward=10, proof="[\"text\"]"):
    db.exec(
        "INSERT INTO micro_tasks(title, description, reward, proof_types, approval_mode, "
        "repeat_mode, cooldown_hours, is_active, created_at) "
        "VALUES ('t', '', ?, ?, ?, ?, 0, 1, ?)",
        reward, proof, mode, repeat, now()
    )
    return db.one("SELECT * FROM micro_tasks ORDER BY id DESC LIMIT 1")


def test_approve_is_atomic_and_credits_once(db):
    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="manual", reward=10)
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'x', 'pending', ?)",
        task["id"], now()
    )
    sid = db.scalar("SELECT id FROM micro_submissions")
    first = db.approve_micro_submission(sid)
    second = db.approve_micro_submission(sid)
    assert first == 10
    assert second is None
    assert db.user(100)["balance"] == 10
    assert db.one("SELECT status FROM micro_submissions WHERE id=?", sid)["status"] == "approved"


def test_approve_rejects_negative_reward_without_crediting(db):
    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="manual", reward=-5)
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'x', 'pending', ?)",
        task["id"], now()
    )
    sid = db.scalar("SELECT id FROM micro_submissions")
    assert db.approve_micro_submission(sid) is None
    assert db.user(100)["balance"] == 0
    assert db.one("SELECT status FROM micro_submissions WHERE id=?", sid)["status"] == "pending"


def test_reject_only_pending_and_is_once(db):
    db.upsert_user(100, "u", "User", None)
    task = _task(db)
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'x', 'pending', ?)",
        task["id"], now()
    )
    sid = db.scalar("SELECT id FROM micro_submissions")
    assert db.reject_micro_submission(sid, "позже") is True
    assert db.reject_micro_submission(sid, "ещё раз") is False
    row = db.one("SELECT status, comment FROM micro_submissions WHERE id=?", sid)
    assert row["status"] == "rejected"
    assert row["comment"] == "позже"
    assert db.approve_micro_submission(sid) is None
    assert db.user(100)["balance"] == 0


def test_submit_proof_once_auto_credits_atomically(db):
    from child.templates.microtasks import _submit_proof

    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="auto", repeat="once", reward=7)
    app = FakeApp(db)
    first = _submit_proof(app, 100, task["id"], "done")
    second = _submit_proof(app, 100, task["id"], "done")
    assert first == ("approved", "", 7)
    assert second[0] is None and "один раз" in second[1]
    assert db.user(100)["balance"] == 7
    assert db.scalar("SELECT COUNT(*) FROM micro_submissions WHERE user_id=100") == 1


def test_submit_proof_manual_does_not_credit(db):
    from child.templates.microtasks import _submit_proof

    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="manual", reward=15)
    app = FakeApp(db)
    status, reason, reward = _submit_proof(app, 100, task["id"], "proof")
    assert status == "pending" and reason == "" and reward is None
    assert db.user(100)["balance"] == 0


def test_submit_and_approve_are_safe_across_two_connections(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "race.db"
    db = ChildDB(path)
    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="manual", reward=10)
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'x', 'pending', ?)",
        task["id"], now()
    )
    sid = db.scalar("SELECT id FROM micro_submissions")
    other = ChildDB.open_existing(path, writable=True)

    def approve(target: ChildDB):
        return target.approve_micro_submission(sid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, [db, other]))
    credited = [r for r in results if r is not None]
    assert credited == [10]
    assert db.user(100)["balance"] == 10
    assert db.one("SELECT status FROM micro_submissions WHERE id=?", sid)["status"] == "approved"


def test_proof_types_tolerate_broken_json():
    from child.templates.microtasks import _callback_int, _proof_types

    assert _proof_types({"proof_types": "{broken"}) == []
    assert _proof_types({"proof_types": "null"}) == []
    assert _proof_types({"proof_types": '["text", 1, "photo"]'}) == ["text", "photo"]
    assert _callback_int("mt:ok:12", prefix="mt:ok:") == 12
    assert _callback_int("mt:ok:-1", prefix="mt:ok:") is None
    assert _callback_int("mt:ok:abc", prefix="mt:ok:") is None
    assert _callback_int("mt:admin:queue:0", prefix="mt:admin:queue:") == 0


@pytest.mark.asyncio
async def test_non_owner_cannot_approve_or_toggle(tmp_path, bot, session):
    from tests.conftest import cb_update, msg_update
    from tests.test_child_bots import OWNER, A, make

    app, feed = make(tmp_path, "microtasks", bot)
    await feed(msg_update(A, "/start"))
    task = _task(app.db, mode="manual", reward=10)
    app.db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (?, ?, 'x', 'pending', ?)",
        A, task["id"], now()
    )
    sid = app.db.scalar("SELECT id FROM micro_submissions")
    await feed(cb_update(A, f"mt:ok:{sid}"))
    await feed(cb_update(A, f"mt:toggle:{task['id']}"))
    row = app.db.one("SELECT status FROM micro_submissions WHERE id=?", sid)
    assert row["status"] == "pending"
    assert app.db.user(A)["balance"] == 0
    assert app.db.one("SELECT is_active FROM micro_tasks WHERE id=?", task["id"])["is_active"] == 1
    await feed(cb_update(OWNER, f"mt:ok:{sid}"))
    assert app.db.one("SELECT status FROM micro_submissions WHERE id=?", sid)["status"] == "approved"
    assert app.db.user(A)["balance"] == 10
    await feed(cb_update(OWNER, f"mt:ok:{sid}"))
    assert app.db.user(A)["balance"] == 10


@pytest.mark.asyncio
async def test_broken_proof_types_do_not_crash_task_list(tmp_path, bot, session):
    from aiogram.methods import SendMessage

    from tests.conftest import msg_update
    from tests.test_child_bots import A, make

    app, feed = make(tmp_path, "microtasks", bot)
    await feed(msg_update(A, "/start"))
    _task(app.db, proof="{broken")
    _task(app.db, proof='["text"]', reward=3)
    await feed(msg_update(A, "📋 Задания"))
    texts = [m.text for m in session.of(SendMessage)]
    assert any("Доступные задания" in (t or "") or "t" in (t or "") for t in texts)


def test_reject_stale_uses_same_rule_as_queue(db):
    db.upsert_user(100, "u", "User", None)
    task = _task(db, mode="manual", reward=10)
    old = now() - 49 * 3600
    fresh = now() - 10
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'old', 'pending', ?)",
        task["id"], old,
    )
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'new', 'pending', ?)",
        task["id"], fresh,
    )
    db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'done', 'approved', ?)",
        task["id"], old,
    )
    assert db.reject_stale_micro_submissions(48) == 1
    rows = {r["proof"]: r["status"] for r in db.all("SELECT proof, status, comment FROM micro_submissions")}
    assert rows["old"] == "rejected"
    assert rows["new"] == "pending"
    assert rows["done"] == "approved"
    stale = db.one("SELECT comment FROM micro_submissions WHERE proof='old'")
    assert stale["comment"] == "Авто-отклонение по сроку"
    assert db.reject_stale_micro_submissions(48) == 0
    assert db.reject_stale_micro_submissions(0) == 0


@pytest.mark.asyncio
async def test_stale_claims_loop_rejects_and_can_be_stopped(tmp_path, bot):
    from child.core import start_maintenance_tasks, stop_maintenance_tasks
    from tests.test_child_bots import make

    app, _feed = make(tmp_path, "microtasks", bot)
    app.s["auto_reject_hours"] = 48
    task = _task(app.db, mode="manual")
    app.db.upsert_user(100, "u", "User", None)
    app.db.exec(
        "INSERT INTO micro_submissions(user_id, task_id, proof, status, created_at) VALUES (100, ?, 'old', 'pending', ?)",
        task["id"], now() - 49 * 3600,
    )
    tasks = start_maintenance_tasks(app, every=0.05)
    assert tasks
    import asyncio
    await asyncio.sleep(0.2)
    assert app.db.one("SELECT status FROM micro_submissions")["status"] == "rejected"
    await stop_maintenance_tasks(app)
    assert app._maintenance_tasks == []
