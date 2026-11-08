import subprocess
import sys
from pathlib import Path

from creator.billing import charge_once
from creator.config import Config
from creator.db import DB, now
from creator.services import Ctx
from creator.supervisor import Supervisor

ROOT = Path(__file__).resolve().parent.parent


async def test_billing_stops_unpaid_bot(tmp_path):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    db.upsert_user(1, None, "A")
    sup = Supervisor(db, cfg, cmd=[sys.executable, "-c", "import time; time.sleep(30)"])
    ctx = Ctx(db=db, cfg=cfg, sup=sup)
    poor = db.materialize_draft(db.create_draft(1, "clicker", "1:a", "a", "A"), 449)
    rich = db.materialize_draft(db.create_draft(1, "refbot", "1:b", "b", "B"), 1300)
    await sup.start(poor)
    await sup.start(rich)
    db.update_bot(poor, next_charge_at=now() - 5)
    db.update_bot(rich, next_charge_at=now() - 5)
    notes = []

    async def notify(uid, text):
        notes.append(text)

    await charge_once(ctx, notify)
    assert not sup.is_running(poor) and db.bot(poor)["desired"] == 0
    assert sup.is_running(rich) and db.bot(rich)["hosting_balance"] == 850
    assert any("остановлен" in n for n in notes) and any("осталось 8,50 ₽" in n for n in notes)
    await sup.shutdown()


def test_dry_run_cli():
    env = {"PATH": "/usr/bin:/bin", "BOT_TOKEN": "", "HOME": "/tmp"}
    res = subprocess.run([sys.executable, "-m", "creator", "--dry-run"], cwd=ROOT, capture_output=True, text=True,
                         env=env, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("[ok] шаблон") == 3
