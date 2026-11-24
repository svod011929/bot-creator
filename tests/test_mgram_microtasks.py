"""Safe MGRAM tasks in the microtasks template: key isolation, confirmed credit only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from child.db import ChildDB
from child.mgram_proxy import check_completion, fetch_pending
from child.templates.microtasks import INFO, _mgram_enabled
from creator.config import Config, load_config
from creator.db import DB
from creator.mgram_tasks import (
    MgramTasksClient,
    completion_of,
    load_hub_key,
    pending_tasks,
    start_proxy,
    stop_proxy,
)
from creator.supervisor import Supervisor

HUB = "synthetic-mgram-hub-key"
PARTNER = "synthetic-mgram-partner-key"
USER = 1001
TASK_ID = 3773
PENDING = {
    "id": TASK_ID,
    "type": "channel",
    "title": "Канал про маркетинг",
    "link": "https://t.me/+AbCdEfGh",
    "button_text": "Подписаться",
    "reward": "0.77",
    "currency": "RUB",
    "status": "pending",
    "payout": "none",
    "earned": "0.00",
}
COMPLETED = {**PENDING, "status": "completed", "payout": "paid", "earned": "0.77"}


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(data_dir=tmp_path, **kw)


def _micro_bot(db: DB, template="microtasks"):
    db.upsert_user(1, None, "A")
    return db.materialize_draft(db.create_draft(1, template, "1:tok", "b", "B"), 10)


def test_setting_is_off_by_default():
    setting = next(s for s in INFO.settings if s.key == "mgram_tasks")
    assert setting.default == 0
    assert INFO.defaults()["mgram_tasks"] == 0

    class App:
        s = INFO.defaults()

    assert _mgram_enabled(App()) is False
    App.s = {"mgram_tasks": 1}
    assert _mgram_enabled(App()) is True
    App.s = {"mgram_tasks": "0"}
    assert _mgram_enabled(App()) is False


def test_hub_key_from_env_and_file_never_uses_partner_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mgram_hub_api_key="", mgram_partner_api_key=PARTNER)
    assert load_hub_key(cfg) == ""
    (tmp_path / "mgram_hub.json").write_text(json.dumps({"api_key": HUB}), encoding="utf-8")
    assert load_hub_key(cfg) == HUB
    cfg.mgram_hub_api_key = "  env-hub  "
    assert load_hub_key(cfg) == "env-hub"
    monkeypatch.setenv("MGRAM_HUB_API_KEY", "  loaded-hub  ")
    monkeypatch.setenv("MGRAM_PARTNER_API_KEY", PARTNER)
    loaded = load_config(tmp_path / "missing.env")
    assert loaded.mgram_hub_api_key == "loaded-hub"
    assert loaded.mgram_partner_api_key == PARTNER
    assert load_hub_key(loaded) == "loaded-hub"


def test_pending_tasks_only_pending_and_sanitized():
    payload = {"ok": True, "data": {"skip": False, "tasks": [PENDING, COMPLETED, {"id": "bad"}]}}
    rows = pending_tasks(payload)
    assert [t["id"] for t in rows] == [TASK_ID]
    assert rows[0]["reward"] == 0.77
    assert rows[0]["link"].startswith("https://t.me/")
    assert pending_tasks({"ok": False, "data": {"tasks": [PENDING]}}) == []
    assert pending_tasks({}) == []
    assert pending_tasks(None) == []


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"ok": True, "data": {"tasks": [COMPLETED]}}, True),
        ({"ok": True, "data": {"tasks": [PENDING]}}, False),
        ({"ok": True, "data": {"tasks": [{**PENDING, "status": "refunded"}]}}, False),
        ({"ok": True, "data": {"tasks": [{**PENDING, "status": "expired"}]}}, False),
        ({"ok": True, "data": {"tasks": [{**PENDING, "status": "unavailable"}]}}, False),
        ({"ok": True, "data": {"skip": True, "status": "completed", "tasks": []}}, False),
        ({"ok": True, "data": {"reason": "all_done", "tasks": []}}, False),
        ({"ok": True, "data": {"reason": "busy", "tasks": []}}, False),
        ({"ok": True, "data": {"reason": "no_list", "tasks": []}}, False),
        ({"ok": True, "data": {"tasks": [PENDING, {**COMPLETED, "id": 9999}]}}, False),
        ({"ok": False, "data": {"tasks": [COMPLETED]}}, False),
        ({}, False),
        (None, False),
        ({"ok": True, "data": {}}, False),
    ],
)
def test_completion_requires_explicit_completed_status(payload, expected):
    result = completion_of(payload, TASK_ID)
    assert result.confirmed is expected
    if expected:
        assert result.reward == 0.77
    else:
        assert result.reward is None


def test_credit_mgram_task_is_atomic_and_idempotent(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    db.upsert_user(USER, "u", "User", None)
    first = db.credit_mgram_task(USER, TASK_ID, 0.77)
    second = db.credit_mgram_task(USER, TASK_ID, 0.77)
    db.credit_mgram_task(USER, 999, 1.5)
    assert first == 0.77
    assert second is None
    assert db.user(USER)["balance"] == pytest.approx(2.27)
    rows = db.all("SELECT task_id, reward FROM micro_mgram_done WHERE user_id=? ORDER BY task_id", USER)
    assert [(r["task_id"], r["reward"]) for r in rows] == [(999, 1.5), (TASK_ID, 0.77)]


def test_credit_rejects_missing_user_and_zero_is_logged_once(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    assert db.credit_mgram_task(USER, TASK_ID, 0.5) is None
    db.upsert_user(USER, "u", "User", None)
    assert db.credit_mgram_task(USER, TASK_ID, 0.0) == 0.0
    assert db.user(USER)["balance"] == 0
    assert db.credit_mgram_task(USER, TASK_ID, 10) is None
    assert db.user(USER)["balance"] == 0


def test_write_files_keeps_hub_and_partner_keys_out_of_child(tmp_path):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB, mgram_partner_api_key=PARTNER)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)
    Supervisor(db, cfg).write_files(bot_id)
    payload = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert HUB not in blob and PARTNER not in blob
    assert "mgram_key" not in payload
    assert "mgram_hub_api_key" not in payload
    assert "mgram_partner_api_key" not in payload
    assert payload["mgram_proxy"].endswith("mgram_tasks.sock")
    assert payload["mgram_proxy_token"]
    assert payload["mgram_proxy_token"] != HUB
    clicker_id = _micro_bot(db, "clicker")
    Supervisor(db, cfg).write_files(clicker_id)
    other = json.loads((cfg.bots_dir / str(clicker_id) / "config.json").read_text(encoding="utf-8"))
    assert "mgram_proxy" not in other and "mgram_proxy_token" not in other
    assert HUB not in json.dumps(other)


@pytest.mark.asyncio
async def test_proxy_confirms_only_via_check_and_child_never_sees_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)
    seen = []

    async def fake_post(self, path, body):
        seen.append((path, body, self.api_key))
        if path == "/tasks":
            return {"ok": True, "data": {"skip": False, "tasks": [PENDING]}}, "ok"
        if path == "/tasks/check":
            if body.get("task_ids") == [TASK_ID]:
                return {"ok": True, "data": {"tasks": [COMPLETED]}}, "ok"
            return {"ok": True, "data": {"tasks": []}}, "ok"
        return None, "error"

    monkeypatch.setattr(MgramTasksClient, "_post", fake_post)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        assert HUB not in json.dumps(child_cfg)
        tasks = await fetch_pending(child_cfg, bot_id, USER)
        assert tasks and tasks[0]["id"] == TASK_ID
        ok = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok.confirmed is True
        assert ok.reward == pytest.approx(0.77)
        assert all(item[2] == HUB for item in seen)
        assert any(path == "/tasks/check" for path, _, _ in seen)
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "error", "empty"])
async def test_proxy_does_not_confirm_on_transport_failures(tmp_path, monkeypatch, reason):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)

    async def fake_post(self, path, body):
        return None, reason

    monkeypatch.setattr(MgramTasksClient, "_post", fake_post)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        result = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert result.confirmed is False
        assert result.reason == reason
        assert await fetch_pending(child_cfg, bot_id, USER) == []
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_task_disappearing_is_not_completion(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)

    async def fake_post(self, path, body):
        return {"ok": True, "data": {"skip": True, "status": "completed", "tasks": []}}, "ok"

    monkeypatch.setattr(MgramTasksClient, "_post", fake_post)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        result = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert result.confirmed is False
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_bad_proxy_token_is_not_found(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)

    async def fake_post(self, path, body):  # pragma: no cover
        raise AssertionError("MGRAM must not be called with a bad token")

    monkeypatch.setattr(MgramTasksClient, "_post", fake_post)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        child_cfg["mgram_proxy_token"] = "wrong-token"
        assert await fetch_pending(child_cfg, bot_id, USER) == []
        result = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert result.confirmed is False
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_child_refuses_config_that_contains_creator_key(tmp_path):
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)
    Supervisor(db, cfg).write_files(bot_id)
    child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    child_cfg["mgram_key"] = HUB
    assert await fetch_pending(child_cfg, bot_id, USER) == []
    result = await check_completion(child_cfg, bot_id, USER, TASK_ID)
    assert result.confirmed is False


def test_credit_uses_confirmed_reward_not_a_guess(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    db.upsert_user(USER, "u", "User", None)
    db.remember_mgram_shown(USER, TASK_ID, 0.77)
    shown = db.one("SELECT reward FROM micro_mgram_shown WHERE user_id=? AND task_id=?", USER, TASK_ID)
    result = completion_of({"ok": True, "data": {"tasks": [COMPLETED]}}, TASK_ID)
    reward = result.reward if result.reward is not None else float(shown["reward"])
    assert result.confirmed
    assert db.credit_mgram_task(USER, TASK_ID, reward) == pytest.approx(0.77)
    assert db.user(USER)["balance"] == pytest.approx(0.77)
