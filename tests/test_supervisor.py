import asyncio
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
