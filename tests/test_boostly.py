"""Safe Boostly relay: key stays in the constructor, credit only on status=confirmed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiogram.methods import EditMessageText
from aiogram.types import User

from child.boostly import BTN_TASKS, BoostlyGate, check_completion, fetch_pending
from creator.boostly import BoostlyService, load_hub_key
from creator.boostly_tasks import (
    BoostlyTasksClient,
    completion_of,
    is_rewardable,
    parse_event,
    parse_event_list,
    pending_tasks,
    poll_events_once,
    process_events,
    start_proxy,
    stop_proxy,
)
from creator.config import PRICE_DEFAULTS, Config
from creator.db import DB
from creator.integration_keys import resolve, revision, save, spec, value
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, create_paid_bot, env  # noqa: F401

HUB = "synthetic-boostly-hub-key-aaaa"
TASK_ID = "task-42"
USER = 1001
PENDING = {
    "id": TASK_ID,
    "title": "Подписаться на канал",
    "url": "https://t.me/boostly_demo",
    "status": "pending",
}
CONFIRMED = {**PENDING, "status": "confirmed"}


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(data_dir=tmp_path, admin_ids={ADMIN}, **kw)


def _bot(db: DB, owner=OWNER, template="clicker"):
    db.upsert_user(owner, "alice", "Alice")
    return db.materialize_draft(db.create_draft(owner, template, "1:tok", "b", "B"), 10)


def test_default_prices_keep_tgrass_and_set_boostly():
    assert PRICE_DEFAULTS["BOOSTLY_REWARD"] == 50
    assert PRICE_DEFAULTS["BOOSTLY_MIN_USERS"] == 0
    assert PRICE_DEFAULTS["TGRASS_MIN_USERS"] == 50
    assert PRICE_DEFAULTS["PIARFLOW_MIN_USERS"] == 50
    assert PRICE_DEFAULTS["TGRASS_REWARD"] == 50
    assert PRICE_DEFAULTS["PIARFLOW_REWARD"] == 50


def test_completion_requires_explicit_confirmed_status():
    assert completion_of({"status": "confirmed"}, TASK_ID).confirmed is True
    assert completion_of({"data": {"status": "confirmed", "id": TASK_ID}}, TASK_ID).confirmed is True
    assert completion_of({"ok": True, "tasks": [CONFIRMED]}, TASK_ID).confirmed is True
    assert completion_of({"ok": True, "tasks": [PENDING]}, TASK_ID).confirmed is False
    assert completion_of({"ok": True, "tasks": [{**PENDING, "status": "completed"}]}, TASK_ID).confirmed is False
    assert completion_of({"ok": True, "tasks": [], "status": "completed"}, TASK_ID).confirmed is False
    assert completion_of({"ok": True, "data": {"skip": True, "status": "confirmed", "tasks": []}}, "other").confirmed is False
    assert completion_of(None, TASK_ID).confirmed is False
    assert completion_of({"ok": True}, TASK_ID).confirmed is False
    assert completion_of({"ok": False, "status": "confirmed"}, TASK_ID).confirmed is False


def test_pending_tasks_drop_confirmed_and_sanitize():
    rows = pending_tasks({"ok": True, "tasks": [PENDING, CONFIRMED, {"title": "no id"}, {"id": TASK_ID, "url": "ftp://x"}]})
    assert [t["id"] for t in rows] == [TASK_ID]
    assert "status" not in rows[0]
    assert rows[0]["url"].startswith("https://")


def test_credit_is_atomic_idempotent_and_integer(tmp_path):
    db = DB(tmp_path / "creator.db")
    bot_id = _bot(db)
    assert db.user(OWNER)["balance"] == 10 or db.user(OWNER)["balance"] >= 0
    before = int(db.user(OWNER)["balance"])
    assert db.boostly_credit(bot_id, USER, TASK_ID, 50) is True
    assert int(db.user(OWNER)["balance"]) == before + 50
    assert db.boostly_credit(bot_id, USER, TASK_ID, 50) is False
    assert int(db.user(OWNER)["balance"]) == before + 50
    assert db.boostly_already_credited(bot_id, USER, TASK_ID)
    # A different task credits again; a missing bot does not.
    assert db.boostly_credit(bot_id, USER, "task-other", 50) is True
    assert int(db.user(OWNER)["balance"]) == before + 100
    assert db.boostly_credit(999, USER, TASK_ID, 50) is False
    count, kop = db.boostly_stats(bot_id)
    assert count == 2 and kop == 100


def test_boostly_set_cannot_store_a_hub_key(tmp_path):
    db = DB(tmp_path / "creator.db")
    bot_id = _bot(db)
    assert db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved", api_key=HUB, error=None)
    row = dict(db.boostly(bot_id))
    assert row["status"] == "approved"
    assert "api_key" not in row.keys() or row.get("api_key") in (None, "")
    blob = " ".join(str(v) for v in row.values())
    assert HUB not in blob


def test_hub_key_env_admin_legacy_priority(tmp_path):
    cfg = _cfg(tmp_path, boostly_hub_api_key="")
    assert load_hub_key(cfg) == ""
    legacy = tmp_path / "boostly_hub.json"
    legacy.write_text(json.dumps({"api_key": "legacy-boostly-hub-key-zzzz"}), encoding="utf-8")
    st = resolve(cfg, "boostly_hub")
    assert st.origin == "file" and st.key.endswith("zzzz")
    save(cfg, "boostly_hub", "admin-boostly-hub-key-yyyy", revision(cfg, spec("boostly_hub")))
    st = resolve(cfg, "boostly_hub")
    assert st.origin == "admin" and st.key.endswith("yyyy")
    cfg.boostly_hub_api_key = HUB
    assert resolve(cfg, "boostly_hub").origin == "env"
    assert load_hub_key(cfg) == HUB
    assert value(cfg, "boostly_hub") == HUB


async def test_owner_can_apply_with_zero_users_and_admin_approves(env, tmp_path):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.boostly_hub_api_key = HUB
    ctx.boostly = BoostlyService(ctx)
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    labels = [(b.text, b.callback_data) for r in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in r]
    assert ("💰 Заработок", f"b:{bot_id}:g:earn") in labels
    await feed(cb_update(OWNER, f"b:{bot_id}:g:earn"))
    labels = [(b.text, b.callback_data) for r in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in r]
    assert ("🚀 Boostly", f"b:{bot_id}:bl") in labels
    await feed(cb_update(OWNER, f"b:{bot_id}:bl"))
    text = session.texts()[-1]
    assert "Boostly" in text and "0,50 ₽" in text
    await feed(cb_update(OWNER, f"b:{bot_id}:blapply"))
    row = ctx.db.boostly(bot_id)
    assert row is not None and row["status"] == "pending"
    assert int(row["users_at_apply"] or 0) == 0
    # Admin notification, then approve
    await feed(cb_update(ADMIN, f"a:bl:ok:{bot_id}"))
    assert ctx.db.boostly_approved(bot_id)
    payload = json.loads((ctx.cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert HUB not in blob
    assert "boostly_key" not in payload
    assert "boostly_hub_api_key" not in payload
    assert payload["boostly_proxy"].endswith("boostly_tasks.sock")
    assert payload["boostly_proxy_token"] and payload["boostly_proxy_token"] != HUB


def test_write_files_keeps_hub_key_out_and_omits_proxy_until_approved(tmp_path):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB, mgram_hub_api_key="mgram-hub-not-for-child")
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    Supervisor(db, cfg).write_files(bot_id)
    payload = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert HUB not in blob
    assert "mgram-hub-not-for-child" not in blob
    assert "boostly_proxy" not in payload
    assert "boostly_key" not in payload
    assert db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    Supervisor(db, cfg).write_files(bot_id)
    payload = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert HUB not in blob and "boostly_key" not in payload
    assert payload["boostly_proxy"].endswith("boostly_tasks.sock")
    assert payload["boostly_proxy_token"] != HUB


@pytest.mark.asyncio
async def test_proxy_credits_only_on_confirmed_and_is_idempotent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])
    seen = []

    async def fake_request(self, method, path, *, params=None, body=None):
        seen.append((method, path, self.api_key, params, body))
        if method == "GET" and path == "/v1/tasks":
            return {"ok": True, "tasks": [PENDING]}, "ok"
        if method == "POST" and path.endswith("/check"):
            return {"status": "confirmed", "id": TASK_ID}, "ok"
        return None, "error"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        assert HUB not in json.dumps(child_cfg)
        tasks = await fetch_pending(child_cfg, bot_id, USER)
        assert tasks and tasks[0]["id"] == TASK_ID
        ok, reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is True and reason == "confirmed"
        assert int(db.user(OWNER)["balance"]) == before + 50
        # Second confirmation does not pay twice.
        ok, reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is True
        assert int(db.user(OWNER)["balance"]) == before + 50
        assert all(item[2] == HUB for item in seen)
        assert any(path.endswith("/check") for _, path, *_ in seen)
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "error", "empty"])
async def test_proxy_does_not_credit_on_transport_failures(tmp_path, monkeypatch, reason):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])

    async def fake_request(self, method, path, *, params=None, body=None):
        return None, reason

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        ok, got = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is False and got == reason
        assert int(db.user(OWNER)["balance"]) == before
        assert await fetch_pending(child_cfg, bot_id, USER) == []
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_disappeared_task_is_not_completion(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])

    async def fake_request(self, method, path, *, params=None, body=None):
        return {"ok": True, "tasks": []}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        ok, _reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is False
        assert int(db.user(OWNER)["balance"]) == before
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_child_refuses_config_that_contains_creator_key(tmp_path):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    Supervisor(db, cfg).write_files(bot_id)
    child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    child_cfg["boostly_key"] = HUB
    assert await fetch_pending(child_cfg, bot_id, USER) == []
    ok, reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
    assert ok is False and reason == "refused"


@pytest.mark.asyncio
async def test_bad_proxy_token_is_not_found(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")

    async def fake_request(self, method, path, *, params=None, body=None):  # pragma: no cover
        raise AssertionError("Boostly API must not be called with a bad token")

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        child_cfg["boostly_proxy_token"] = "wrong-token"
        assert await fetch_pending(child_cfg, bot_id, USER) == []
        ok, _reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is False
    finally:
        await stop_proxy(runner, cfg)


async def test_admin_keys_and_tools_show_boostly(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/start"))
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:g:tools"))
    tools = [(b.text, b.callback_data) for r in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in r]
    assert ("🚀 Заявки Boostly", "a:bl:list") in tools
    await feed(cb_update(ADMIN, "a:g:settings"))
    settings = [(b.text, b.callback_data) for r in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in r]
    assert ("🔑 Ключи интеграций", "ik:home") in settings
    await feed(cb_update(ADMIN, "ik:home"))
    text = session.texts()[-1]
    assert "Ключи интеграций" in text
    assert "Boostly хаб" in text



def test_paid_false_and_op_are_not_rewardable():
    assert is_rewardable(status="confirmed") is True
    assert is_rewardable(status="confirmed", paid=True, op=False) is True
    assert is_rewardable(status="confirmed", paid=False) is False
    assert is_rewardable(status="confirmed", op=True) is False
    assert is_rewardable(status="incomplete") is False
    result = completion_of({"id": TASK_ID, "status": "confirmed", "paid": False}, TASK_ID)
    assert result.confirmed is True and result.rewardable is False
    result = completion_of({"id": TASK_ID, "status": "confirmed", "op": True}, TASK_ID)
    assert result.confirmed is True and result.rewardable is False


def test_parse_events_list_and_wrapper():
    rows = parse_event_list([
        {
            "sequence": 41,
            "id": "task:task-42",
            "payload": {
                "event": "task.confirmed",
                "task_id": TASK_ID,
                "telegram_user": USER,
                "paid": True,
            },
        }
    ])
    assert len(rows) == 1
    ev = parse_event(rows[0])
    assert ev["rewardable"] is True and ev["tg_user_id"] == USER and ev["task_id"] == TASK_ID
    wrapped = parse_event_list({"events": rows})
    assert len(wrapped) == 1
    skipped = parse_event({
        "sequence": 42,
        "id": "task:task-42:op",
        "payload": {"event": "task.confirmed", "task_id": TASK_ID, "telegram_user": USER, "op": True},
    })
    assert skipped["rewardable"] is False


@pytest.mark.asyncio
async def test_proxy_does_not_credit_paid_false_onboarding(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])

    async def fake_request(self, method, path, *, params=None, body=None):
        return {"id": TASK_ID, "status": "confirmed", "paid": False}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        ok, reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is True and reason == "confirmed"
        assert int(db.user(OWNER)["balance"]) == before
        assert db.boostly_already_credited(bot_id, USER, TASK_ID) is False
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_events_credit_once_and_dedup_with_check(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])
    db.boostly_remember_shown(bot_id, USER, [TASK_ID])

    event = {
        "sequence": 7,
        "id": "task:task-42",
        "payload": {"event": "task.confirmed", "task_id": TASK_ID, "telegram_user": USER},
    }
    first = process_events(db, cfg, [event])
    assert first["credited"] == 1
    assert int(db.user(OWNER)["balance"]) == before + 50
    # Repeat event does not pay again.
    again = process_events(db, cfg, [event])
    assert again["credited"] == 0
    assert int(db.user(OWNER)["balance"]) == before + 50

    async def fake_request(self, method, path, *, params=None, body=None):
        return {"id": TASK_ID, "status": "confirmed"}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        ok, _reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is True
        assert int(db.user(OWNER)["balance"]) == before + 50
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_check_then_event_does_not_double_credit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    before = int(db.user(OWNER)["balance"])

    async def fake_request(self, method, path, *, params=None, body=None):
        return {"id": TASK_ID, "status": "confirmed"}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        ok, _reason = await check_completion(child_cfg, bot_id, USER, TASK_ID)
        assert ok is True
        assert int(db.user(OWNER)["balance"]) == before + 50
    finally:
        await stop_proxy(runner, cfg)

    event = {
        "sequence": 9,
        "id": "task:task-42-b",
        "payload": {"event": "task.confirmed", "task_id": TASK_ID, "telegram_user": USER},
    }
    result = process_events(db, cfg, [event])
    assert result["credited"] == 0
    assert int(db.user(OWNER)["balance"]) == before + 50


@pytest.mark.asyncio
async def test_events_poll_parses_list_response_and_advances_cursor(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    db.boostly_remember_shown(bot_id, USER, [TASK_ID])
    before = int(db.user(OWNER)["balance"])

    async def fake_request(self, method, path, *, params=None, body=None):
        assert method == "GET" and path == "/v1/events"
        assert params == {"after_sequence": 0}
        return [{
            "sequence": 11,
            "id": "task:task-42",
            "payload": {"event": "task.confirmed", "task_id": TASK_ID, "telegram_user": USER},
        }], "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    result = await poll_events_once(cfg, db)
    assert result["ok"] is True and result["credited"] == 1 and result["cursor"] == 11
    assert db.boostly_event_cursor() == 11
    assert int(db.user(OWNER)["balance"]) == before + 50


@pytest.mark.asyncio
async def test_fetch_pending_sends_profile_and_remembers_shown(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")
    seen = []

    async def fake_request(self, method, path, *, params=None, body=None):
        seen.append((method, path, params))
        return {"tasks": [PENDING], "onboarding_required": False}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        tasks = await fetch_pending(
            child_cfg, bot_id, USER,
            {"language_code": "ru", "first_name": "Ann", "username": "ann", "is_premium": True},
        )
        assert tasks and tasks[0]["id"] == TASK_ID
        assert seen and seen[0][1] == "/v1/tasks"
        params = seen[0][2]
        assert params["telegram_user_id"] == USER
        assert params["language_code"] == "ru"
        assert params["is_premium"] is True
        assert db.boostly_shown_bots(USER, TASK_ID) == [bot_id]
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_owner_can_open_boostly_task_section(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, boostly_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _bot(db)
    db.boostly_apply(bot_id, OWNER, 0)
    db.boostly_set(bot_id, status="approved")

    async def fake_request(self, method, path, *, params=None, body=None):
        return {"tasks": [PENDING]}, "ok"

    monkeypatch.setattr(BoostlyTasksClient, "_request", fake_request)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
        from child.db import ChildDB
        child_db = ChildDB(tmp_path / "child.db")
        gate = BoostlyGate(child_cfg, child_db, bot_id)
        user = User(id=OWNER, is_bot=False, first_name="Owner", username="alice", language_code="ru")
        text, kb = await gate.render_list(user)
        assert "Boostly" in text
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any(b.startswith("📢 ") for b in labels)
        assert "✅ Проверить" in labels and "🔄 Обновить" in labels
        assert BTN_TASKS == "🚀 Boostly"
    finally:
        await stop_proxy(runner, cfg)
