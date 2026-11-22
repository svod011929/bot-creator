"""Shared worker: isolation, lifecycle, restricted environment. Process mode stays default."""
import asyncio
from pathlib import Path

from child.core import ChildApp
from child.files import write_config
from child.piarflow import PiarFlowGate
from child.worker import FORBIDDEN_ENV, BotHost
from creator.config import Config
from creator.supervisor import CHILD_ENV_KEYS, Supervisor


def _write_bot(d: Path, bot_id: int, owner: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    write_config(d, {
        "token": f"{bot_id}:TEST", "template": "clicker", "owner_id": owner,
        "username": f"b{bot_id}", "bot_id": bot_id,
    })


def test_two_apps_isolated(tmp_path):
    d1, d2 = tmp_path / "1", tmp_path / "2"
    _write_bot(d1, 1, 10)
    _write_bot(d2, 2, 20)
    a, b = ChildApp(d1), ChildApp(d2)
    a.set("welcome", "only-a")
    assert a.s.get("welcome") == "only-a"
    assert b.s.get("welcome") != "only-a"
    assert a.owner_id != b.owner_id
    assert a.dir != b.dir


async def test_start_stop_one_leaves_other(tmp_path, monkeypatch):
    d1, d2 = tmp_path / "1", tmp_path / "2"
    _write_bot(d1, 1, 10)
    _write_bot(d2, 2, 20)
    host = BotHost()

    async def fake_run(self, bot_id, app, bot):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(BotHost, "_run", fake_run)
    await host.start(d1)
    await host.start(d2)
    await asyncio.sleep(0.05)
    assert host.is_running(1) and host.is_running(2)
    await host.stop(1)
    assert not host.is_running(1)
    assert host.is_running(2)
    await host.shutdown()


async def test_one_crash_does_not_stop_other(tmp_path, monkeypatch):
    d1, d2 = tmp_path / "1", tmp_path / "2"
    _write_bot(d1, 1, 10)
    _write_bot(d2, 2, 20)
    host = BotHost()

    async def fake_run(self, bot_id, app, bot):
        if bot_id == 1:
            raise RuntimeError("boom")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(BotHost, "_run", fake_run)
    await host.start(d1)
    await host.start(d2)
    await asyncio.sleep(0.05)
    assert host.is_running(2)
    await host.shutdown()


def test_worker_env_has_no_creator_secrets(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "secret")
    monkeypatch.setenv("OWNER_PAYOUT_KEY", "secret")
    monkeypatch.setenv("LAVEPAY_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    cfg = Config(data_dir=Path("/tmp"), child_runtime="shared")
    from creator.db import DB
    db = DB(":memory:")
    sup = Supervisor(db, cfg)
    env = sup.child_env()
    for key in ("BOT_TOKEN", "OWNER_PAYOUT_KEY", "LAVEPAY_API_KEY", "LAVEPAY_WEBHOOK_SECRET"):
        assert key not in env
    assert set(env) <= CHILD_ENV_KEYS | {"PYTHONUNBUFFERED"}
    assert FORBIDDEN_ENV.isdisjoint(env)
    db.close()


def test_process_mode_is_default():
    cfg = Config()
    assert cfg.child_runtime == "process"


def test_remember_pending_writes_cache_not_recurse():
    class Dummy:
        lock = None
        conn = None

    # Build a gate without hitting the DB schema by skipping __init__ body.
    gate = PiarFlowGate.__new__(PiarFlowGate)
    gate._pending = {}
    gate._pass_until = {}
    gate._pass_limit = 5000
    gate._remember_pending(7, ["https://t.me/a"])
    assert gate._pending[7] == ["https://t.me/a"]


async def test_stop_does_not_restart_after_cancel(tmp_path, monkeypatch):
    d1 = tmp_path / "1"
    _write_bot(d1, 1, 10)
    host = BotHost()

    async def fake_run(self, bot_id, app, bot):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(BotHost, "_run", fake_run)
    await host.start(d1)
    await asyncio.sleep(0.05)
    assert host.is_running(1)
    await host.stop(1)
    await asyncio.sleep(0.2)
    assert not host.is_running(1)
    assert host.running_ids() == []


async def test_crash_is_reported_in_status_and_other_bot_stays(tmp_path, monkeypatch):
    from child.worker import _handle
    d1, d2 = tmp_path / "1", tmp_path / "2"
    _write_bot(d1, 1, 10)
    _write_bot(d2, 2, 20)
    host = BotHost()

    async def fake_run(self, bot_id, app, bot):
        if bot_id == 1:
            # Same bookkeeping as BotHost._run finally after a crash.
            slot = self.slots.get(bot_id)
            if slot is not None and slot.task is asyncio.current_task():
                self.exits.append({"id": bot_id, "code": 1})
                self.slots.pop(bot_id, None)
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(BotHost, "_run", fake_run)
    await host.start(d1)
    await host.start(d2)
    await asyncio.sleep(0.1)
    assert host.is_running(2)
    assert not host.is_running(1)
    reply = await _handle(host, '{"cmd":"status"}')
    assert reply["ok"] and 2 in reply["running"]
    assert any(item["id"] == 1 for item in reply["exits"])
    await host.shutdown()
