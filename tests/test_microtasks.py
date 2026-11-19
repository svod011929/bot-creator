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
