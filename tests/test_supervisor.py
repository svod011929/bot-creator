import asyncio
import json
import sys

import pytest

from creator.config import Config
from creator.db import DB
from creator.supervisor import Supervisor

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def env(tmp_path):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    db.upsert_user(1, None, "A")
    bot_id = db.materialize_draft(db.create_draft(1, "clicker", "1:tok", "b", "B"), 10)
    return cfg, db, bot_id


async def wait_for(cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_start_stop_restart_on_crash(env, monkeypatch):
    cfg, db, bot_id = env
    monkeypatch.setattr("creator.supervisor.MAX_CRASHES", 10)
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator")
    await sup.start(bot_id)
    assert sup.is_running(bot_id) and db.bot(bot_id)["status"] == "running"
    cfg_json = (cfg.bots_dir / str(bot_id) / "config.json").read_text()
    assert '"owner_id": 1' in cfg_json and '"creator_username": "creator"' in cfg_json
    pid = sup.children[bot_id].proc.pid
    sup.children[bot_id].proc.kill()  # «падение»
    assert await wait_for(lambda: sup.is_running(bot_id) and sup.children[bot_id].proc.pid != pid)
    await sup.stop(bot_id)
    assert not sup.is_running(bot_id)
    assert db.bot(bot_id)["desired"] == 0 and db.bot(bot_id)["status"] == "stopped"


async def test_bad_token_marks_error(env):
    cfg, db, bot_id = env
    notes = []

    async def notify(uid, text):
        notes.append((uid, text))

    sup = Supervisor(db, cfg, notifier=notify, cmd=[sys.executable, "-c", "import sys; sys.exit(3)"])
    await sup.start(bot_id)
    assert await wait_for(lambda: db.bot(bot_id)["status"] == "error")
    assert db.bot(bot_id)["desired"] == 0
    assert notes and notes[0][0] == 1 and "токен" in notes[0][1]


async def test_too_many_crashes(env, monkeypatch):
    cfg, db, bot_id = env
    monkeypatch.setattr("creator.supervisor.MAX_CRASHES", 2)
    sup = Supervisor(db, cfg, cmd=[sys.executable, "-c", "import sys; sys.exit(1)"])
    await sup.start(bot_id)
    assert await wait_for(lambda: db.bot(bot_id)["status"] == "error", timeout=15)


async def test_shutdown_keeps_desired_and_start_all(env):
    cfg, db, bot_id = env
    sup = Supervisor(db, cfg, cmd=SLEEPER)
    await sup.start(bot_id)
    await sup.shutdown()
    assert not sup.is_running(bot_id)
    assert db.bot(bot_id)["desired"] == 1  # после рестарта сервиса поднимется снова
    sup2 = Supervisor(db, cfg, cmd=SLEEPER)
    assert await sup2.start_all() == 1
    assert sup2.is_running(bot_id)
    await sup2.shutdown()


async def test_child_env_has_no_main_token(env, monkeypatch, tmp_path):
    cfg, db, bot_id = env
    monkeypatch.setenv("BOT_TOKEN", "999:MAIN-SECRET")
    out = tmp_path / "env.txt"
    cmd = [sys.executable, "-c", f"import os; open(r'{out}','w').write(os.environ.get('BOT_TOKEN','none'))"]
    sup = Supervisor(db, cfg, cmd=cmd)
    await sup.start(bot_id)
    assert await wait_for(lambda: out.exists() and out.read_text() != "")
    assert out.read_text() == "none"
    await sup.stop(bot_id)


async def test_child_env_excludes_all_parent_secrets(env, monkeypatch, tmp_path):
    cfg, db, bot_id = env
    names = ("BOT_TOKEN", "ADMIN_IDS", "LAVEPAY_API_KEY", "LAVEPAY_WEBHOOK_SECRET", "TGRASS_FACTORY_TOKEN",
             "PIARFLOW_TOKEN", "CUSTOM_PARENT_SECRET")
    for name in names:
        monkeypatch.setenv(name, "synthetic-parent-value")
    out = tmp_path / "env-presence.txt"
    code = f"import os; open({str(out)!r},'w').write(str([k for k in {names!r} if k in os.environ]))"
    sup = Supervisor(db, cfg, cmd=[sys.executable, "-c", code])
    try:
        await sup.start(bot_id)
        assert await wait_for(lambda: out.exists() and out.read_text() != "")
        assert out.read_text() == "[]"
    finally:
        await sup.shutdown()


async def test_competing_starts_spawn_one_process(env, monkeypatch):
    cfg, db, bot_id = env
    sup = Supervisor(db, cfg, cmd=SLEEPER)
    spawned = []
    original = sup._spawn

    async def slow_spawn(bid):
        await asyncio.sleep(0)
        await original(bid)
        spawned.append(sup.children[bid].proc)

    monkeypatch.setattr(sup, "_spawn", slow_spawn)
    try:
        await asyncio.gather(*(sup.start(bot_id) for _ in range(3)))
        assert len(spawned) == 1 and sup.is_running(bot_id)
    finally:
        await sup.shutdown()
    assert all(p.returncode is not None for p in spawned)


async def test_shutdown_during_spawn_leaves_no_process(env, monkeypatch):
    cfg, db, bot_id = env
    sup = Supervisor(db, cfg, cmd=SLEEPER)
    entered, release = asyncio.Event(), asyncio.Event()
    original = sup._spawn
    spawned = []

    async def slow_spawn(bid):
        entered.set()
        await release.wait()
        await original(bid)
        spawned.append(sup.children[bid].proc)

    monkeypatch.setattr(sup, "_spawn", slow_spawn)
    starting = asyncio.create_task(sup.start(bot_id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    shutdown = asyncio.create_task(sup.shutdown())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(starting, shutdown)
    assert len(spawned) == 1 and spawned[0].returncode is not None
    assert not sup.is_running(bot_id) and db.bot(bot_id)["desired"] == 1


async def test_recovery_start_does_not_override_stop(env):
    cfg, db, bot_id = env
    sup = Supervisor(db, cfg, cmd=SLEEPER)
    await sup.stop(bot_id)
    await sup.start(bot_id, require_desired=True)
    await sup.restart(bot_id, require_desired=True)
    assert not sup.is_running(bot_id) and db.bot(bot_id)["desired"] == 0


async def test_child_env_forwards_runtime_proxy_and_excludes_secrets(env, monkeypatch, tmp_path):
    cfg, db, bot_id = env
    forwarded = {
        "TZ": "Europe/Helsinki",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "localhost,127.0.0.1",
        "ALL_PROXY": "http://127.0.0.1:9",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    }
    secrets = {
        "BOT_TOKEN": "999:MAIN-SECRET",
        "ADMIN_IDS": "1",
        "LAVEPAY_API_KEY": "synthetic-lave-key",
        "LAVEPAY_WEBHOOK_SECRET": "synthetic-webhook",
        "TGRASS_FACTORY_TOKEN": "synthetic-tgrass",
        "TGRASS_HUB_API_KEY": "synthetic-tgrass-hub",
        "PIARFLOW_TOKEN": "synthetic-piarflow",
        "PIARFLOW_HUB_API_KEY": "synthetic-piarflow-hub",
        "CUSTOM_PARENT_SECRET": "synthetic-parent-value",
    }
    for name, value in {**forwarded, **secrets}.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "child-env.json"
    code = (
        "import json,os; "
        f"open({str(out)!r},'w').write(json.dumps(dict(os.environ)))"
    )
    sup = Supervisor(db, cfg, cmd=[sys.executable, "-c", code])
    try:
        await sup.start(bot_id)
        assert await wait_for(lambda: out.exists() and out.stat().st_size > 2)
        env = json.loads(out.read_text())
        for name, value in forwarded.items():
            assert env.get(name) == value, name
        assert env.get("PYTHONUNBUFFERED") == "1"
        assert env.get("PATH")
        assert env.get("HOME")
        for name in secrets:
            assert name not in env
    finally:
        await sup.shutdown()
