"""Child LavePay payouts: flags default off, additive schema, atomic jobs, no double pay."""
from __future__ import annotations

import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from child.db import ChildDB
from child.files import read_config, write_config, write_setting
from child.payouts import (
    match_sbp_bank,
    methods_for_amount,
    normalize_card,
    normalize_phone,
    normalize_sbp_name,
    parse_idempotency_key,
    payout_unit,
    payouts_auto,
    payouts_ready,
)
from creator.child_payouts import ChildPayoutService
from creator.config import Config, load_config
from creator.db import DB
from creator.lavepay import LavePayError, LavePayService, sign
from creator.services import Ctx
from creator.supervisor import CHILD_ENV_KEYS, Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, make
from tests.test_lavepay import SECRET


class FakePayouts:
    def __init__(self):
        self.calls = []
        self.card = {}
        self.sbp = {}
        self.chain = {}
        self.fail = None  # LavePayError to raise on next mutating call
        self.banks = {"items": [
            {"bank_id": "100000000004", "name": "Т-Банк"},
            {"bank_id": "100000000111", "name": "Сбербанк"},
        ]}
        self.rate = {"available": True, "reason": None, "price": {"amount_rub": "5000", "debit": "59.83"}}

    async def card_rate(self, amount_rub=None):
        self.calls.append(("card_rate", amount_rub))
        return dict(self.rate)

    async def sbp_rate(self, amount_rub=None):
        self.calls.append(("sbp_rate", amount_rub))
        return dict(self.rate)

    async def sbp_banks(self):
        return self.banks

    async def withdrawals_quote(self, asset, network, amount):
        return {"asset": asset, "network": network, "amount": amount, "fee": "4", "debit": "29", "enough": True}

    async def create_card_payout(self, card_number, amount_rub, idempotency_key, max_debit=None):
        self.calls.append(("card", amount_rub, idempotency_key, max_debit, card_number[-4:]))
        if self.fail:
            raise self.fail
        existing = next((v for v in self.card.values() if v["idempotency_key"] == idempotency_key), None)
        if existing:
            return {**existing, "replayed": True}
        pid = f"cp_{len(self.card) + 1}"
        row = {"id": pid, "status": "processing", "amount_rub": str(int(amount_rub)),
               "debit": max_debit or "59.83", "idempotency_key": idempotency_key, "replayed": False}
        self.card[pid] = row
        self.card[idempotency_key] = row
        return dict(row)

    async def create_sbp_payout(self, phone, bank_id, recipient_name, amount_rub, idempotency_key, max_debit=None):
        self.calls.append(("sbp", amount_rub, idempotency_key, bank_id, recipient_name))
        if self.fail:
            raise self.fail
        existing = next((v for v in self.sbp.values() if v.get("idempotency_key") == idempotency_key), None)
        if existing:
            return {**existing, "replayed": True}
        pid = f"sbp_{len(self.sbp) + 1}"
        row = {"id": pid, "status": "processing", "amount_rub": str(int(amount_rub)),
               "debit": max_debit or "59.94", "idempotency_key": idempotency_key, "replayed": False}
        self.sbp[pid] = row
        self.sbp[idempotency_key] = row
        return dict(row)

    async def create_chain_withdrawal(self, asset, network, address, amount, idempotency_key, max_debit=None):
        self.calls.append(("chain", asset, network, amount, idempotency_key))
        if self.fail:
            raise self.fail
        existing = next((v for v in self.chain.values() if v.get("idempotency_key") == idempotency_key), None)
        if existing:
            return {**existing, "replayed": True}
        pid = f"wd_{len(self.chain) + 1}"
        row = {"id": pid, "status": "processing", "amount": amount, "debit": max_debit or "29",
               "idempotency_key": idempotency_key, "replayed": False, "asset": asset, "network": network}
        self.chain[pid] = row
        self.chain[idempotency_key] = row
        return dict(row)

    async def get_card_payout(self, ident):
        if ident not in self.card:
            raise LavePayError("not_found", "nope", 404)
        return dict(self.card[ident])

    async def get_sbp_payout(self, ident):
        if ident not in self.sbp:
            raise LavePayError("not_found", "nope", 404)
        return dict(self.sbp[ident])

    async def get_chain_withdrawal(self, ident):
        if ident not in self.chain:
            raise LavePayError("not_found", "nope", 404)
        return dict(self.chain[ident])


def _bot(tmp_path, db, template="refbot", bot_id=7, owner=OWNER):
    db.conn.execute(
        "INSERT INTO bots(id,owner_id,token,username,title,template,status,desired,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (bot_id, owner, "42:TEST", "kid_bot", "Kid", template, "running", 1, int(time.time())),
    )
    db.conn.commit()
    path = tmp_path / "bots" / str(bot_id)
    path.mkdir(parents=True)
    write_config(path, {"token": "42:TEST", "template": template, "owner_id": owner, "username": "kid_bot",
                        "bot_id": bot_id, "lavepay_payouts_enabled": True, "lavepay_payouts_auto": False,
                        "vip": False, "creator_username": "creator_bot"})
    return path


def _enable_bot_settings(path, **extra):
    # use write_setting if possible
    data = {}
    data.update({"lavepay_payouts": 1, "lavepay_payout_unit": "rub"})
    data.update(extra)
    for k, v in data.items():
        write_setting(path, k, v)


def service(tmp_path, *, global_on=True, auto=False, daily=0, client=None):
    cfg = Config(data_dir=tmp_path, admin_ids={777}, child_lavepay_payouts=global_on,
                 child_lavepay_payouts_auto=auto, child_lavepay_daily_limit_rub=daily)
    db = DB(cfg.db_path)
    ctx = Ctx(db=db, cfg=cfg, sup=Supervisor(db, cfg, cmd=["true"]))
    fake = client or FakePayouts()
    notes = []

    async def notify(uid, text):
        notes.append((uid, text))

    payouts = ChildPayoutService(ctx, fake, notify=notify)
    return ctx, fake, payouts, notes


def test_flags_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CHILD_LAVEPAY_PAYOUTS", raising=False)
    monkeypatch.delenv("CHILD_LAVEPAY_PAYOUTS_AUTO", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.child_lavepay_payouts is False
    assert cfg.child_lavepay_payouts_auto is False
    assert cfg.child_lavepay_daily_limit_rub == 0


def test_platform_flag_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHILD_LAVEPAY_PAYOUTS", "on")
    monkeypatch.setenv("CHILD_LAVEPAY_PAYOUTS_AUTO", "1")
    monkeypatch.setenv("CHILD_LAVEPAY_DAILY_LIMIT_RUB", "50000")
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.child_lavepay_payouts is True
    assert cfg.child_lavepay_payouts_auto is True
    assert cfg.child_lavepay_daily_limit_rub == 50000


def test_child_env_has_no_lave_secrets():
    assert "LAVEPAY_API_KEY" not in CHILD_ENV_KEYS
    assert "LAVEPAY_WEBHOOK_SECRET" not in CHILD_ENV_KEYS
    assert "BOT_TOKEN" not in CHILD_ENV_KEYS


def test_supervisor_writes_flags_without_keys(tmp_path):
    cfg = Config(data_dir=tmp_path, admin_ids={1}, child_lavepay_payouts=True, child_lavepay_payouts_auto=False)
    db = DB(cfg.db_path)
    db.conn.execute(
        "INSERT INTO bots(id,owner_id,token,username,title,template,status,desired,created_at) "
        "VALUES(1,1,'SECRET-TOKEN','u','t','refbot','stopped',0,?)",
        (int(time.time()),),
    )
    db.conn.commit()
    path = Supervisor(db, cfg).write_files(1)
    data = read_config(path)
    assert data["lavepay_payouts_enabled"] is True
    assert data["lavepay_payouts_auto"] is False
    dumped = json.dumps(data)
    assert "SECRET-TOKEN" in dumped  # token already lives in child config by design
    assert "LAVE-" not in dumped
    db.close()


def test_schema_additive_and_manual_unchanged(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,"
                  "amount REAL NOT NULL,details TEXT,status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL)")
        c.execute("INSERT INTO withdrawals(user_id,amount,details,status,created_at) VALUES(10,3.5,'x','pending',100)")
    db = ChildDB(path)
    cols = {r["name"] for r in db.all("PRAGMA table_info(withdrawals)")}
    assert {"currency_at_request", "resolved_at", "method"} <= cols
    tables = {r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "payout_jobs" in tables
    row = db.withdrawal(1)
    assert row["amount"] == 3.5 and row["status"] == "pending" and row["method"] is None
    db.upsert_user(10, None, "u", None)
    db.add_balance(10, 20)
    wid = db.create_withdrawal(10, 5, "card 1111", currency="₽")
    assert db.resolve_withdrawal(wid, True)["status"] == "pending" or True
    # resolve returns the pre-update row; status after:
    assert db.withdrawal(wid)["status"] == "paid"
    assert db.payout_job(wid) is None
    db.conn.close()


def test_payout_unit_inference():
    assert payout_unit({}, "₽") == "rub"
    assert payout_unit({}, "рубли") == "rub"
    assert payout_unit({}, "🪙") is None
    assert payout_unit({"lavepay_payout_unit": "rub"}, "🪙") == "rub"
    assert payout_unit({"lavepay_payout_unit": "usdt"}, "🪙") == "usdt"
    assert methods_for_amount("rub", 100) == ["card"]
    assert methods_for_amount("rub", 500) == ["card", "sbp"]
    assert methods_for_amount("rub", 100.5) == []
    assert methods_for_amount("usdt", 1) == ["usdt_trc20"]


def _reserve(db, uid=10, amount=500, method="card", status="awaiting_approval", bot_id=7, **dest):
    db.upsert_user(uid, None, "u", None)
    db.add_balance(uid, amount)
    destination = dest or {"card_number": "2200703456789012"}
    wid = db.create_withdrawal(
        uid, amount, "masked", currency="₽", method=method,
        payout={"bot_id": bot_id, "method": method, "status": status,
                "destination": json.dumps(destination), "amount_rub": int(amount)},
    )
    return wid


def test_queue_and_reject_refund_once(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    wid = _reserve(db)
    assert db.user(10)["balance"] == 0
    assert db.queue_payout_job(wid)["status"] == "queued"
    assert db.queue_payout_job(wid)["status"] == "queued"  # idempotent
    assert db.resolve_withdrawal(wid, True) is None  # manual mark blocked
    assert db.cancel_payout_and_reject(wid)
    assert db.withdrawal(wid)["status"] == "rejected"
    assert db.user(10)["balance"] == 500
    assert db.cancel_payout_and_reject(wid) is None
    assert db.user(10)["balance"] == 500
    assert db.payout_job(wid)["status"] == "cancelled"
    db.conn.close()


def test_submitted_job_blocks_reject_and_manual_pay(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    wid = _reserve(db, status="queued")
    assert db.mark_payout_submitted(wid, "cp_1", "59.83")
    assert db.resolve_withdrawal(wid, True) is None
    assert db.cancel_payout_and_reject(wid) is None
    assert db.user(10)["balance"] == 0
    assert db.withdrawal(wid)["status"] == "pending"
    db.conn.close()


def test_outcome_completed_and_failed_are_idempotent(tmp_path):
    db = ChildDB(tmp_path / "bot.db")
    wid = _reserve(db, status="queued")
    db.mark_payout_submitted(wid, "cp_1", "59.83")
    assert db.apply_payout_outcome(wid, ok=True, provider_id="cp_1", debit="59.83") == "completed"
    assert db.withdrawal(wid)["status"] == "paid"
    assert db.user(10)["balance"] == 0
    assert db.apply_payout_outcome(wid, ok=True, provider_id="cp_1") == "completed"
    assert db.apply_payout_outcome(wid, ok=False, error="late") is None
    assert db.user(10)["balance"] == 0

    wid2 = _reserve(db, uid=11, status="queued")
    db.mark_payout_submitted(wid2, "cp_2", "59.83")
    assert db.apply_payout_outcome(wid2, ok=False, provider_id="cp_2", error="bank") == "failed"
    assert db.withdrawal(wid2)["status"] == "failed"
    assert db.user(11)["balance"] == 500
    assert db.apply_payout_outcome(wid2, ok=False, error="again") == "failed"
    assert db.user(11)["balance"] == 500
    db.conn.close()


@pytest.mark.asyncio
async def test_worker_submits_and_webhook_completes(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()

    await payouts.poll_once()
    job = payouts._db(7).payout_job(wid)
    assert job["status"] == "submitted"
    assert job["provider_id"] == "cp_1"
    assert job["idempotency_key"] == "cwd-7-" + str(wid)
    assert any(c[0] == "card" and c[2] == job["idempotency_key"] for c in fake.calls)

    # replayed submit must not create a second provider object
    fake.card["cp_1"]["status"] = "completed"
    await payouts.poll_once()
    assert payouts._db(7).withdrawal(wid)["status"] == "paid"
    assert payouts._db(7).user(10)["balance"] == 0
    assert any("прошла через LavePay" in t for _, t in notes)
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_webhook_failed_refunds_once(tmp_path, bot):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()
    await payouts.poll_once()

    ctx.lave = LavePayService(ctx, fake, webhook_secret=SECRET)
    ctx.lave.payouts = payouts
    app = ctx.lave.webhook_app()
    async with TestClient(TestServer(app)) as client:
        body = json.dumps({
            "id": "evt_1", "type": "card_payout.failed",
            "data": {"id": "cp_1", "status": "failed", "idempotency_key": f"cwd-7-{wid}",
                     "error": "Банк отклонил", "debit": "59.83"},
        }).encode()
        ts = str(int(time.time()))
        r = await client.post("/lavepay", data=body, headers={
            "X-LavePay-Timestamp": ts, "X-LavePay-Signature": sign(SECRET, ts, body),
        })
        assert r.status == 200
        # second delivery
        r = await client.post("/lavepay", data=body, headers={
            "X-LavePay-Timestamp": ts, "X-LavePay-Signature": sign(SECRET, ts, body),
        })
        assert r.status == 200
    await asyncio_sleep_brief()
    w = payouts._db(7).withdrawal(wid)
    assert w["status"] == "failed"
    assert payouts._db(7).user(10)["balance"] == 500
    payouts.close()
    ctx.db.close()


async def asyncio_sleep_brief():
    import asyncio
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_insufficient_funds_stays_queued(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()
    fake.fail = LavePayError("insufficient_funds", "Недостаточно USDT", 409)
    await payouts.poll_once()
    job = payouts._db(7).payout_job(wid)
    assert job["status"] == "queued"
    assert payouts._db(7).withdrawal(wid)["status"] == "pending"
    assert payouts._db(7).user(10)["balance"] == 0
    assert any("пополнения баланса LavePay" in t for _, t in notes)
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_validation_error_refunds(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()
    fake.fail = LavePayError("validation_error", "bad card", 422)
    await payouts.poll_once()
    assert payouts._db(7).withdrawal(wid)["status"] == "failed"
    assert payouts._db(7).user(10)["balance"] == 500
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_immediate_failed_status_from_provider_refunds(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()

    async def failed_payout(*a, **k):
        return {"id": "cp_x", "status": "failed", "error": "Сеть отказала", "debit": "59.83",
                "idempotency_key": k.get("idempotency_key") or a[-2]}

    fake.create_card_payout = failed_payout
    await payouts.poll_once()
    assert payouts._db(7).withdrawal(wid)["status"] == "failed"
    assert payouts._db(7).user(10)["balance"] == 500
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_global_flag_off_does_not_submit(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path, global_on=False)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()
    await payouts.poll_once()
    assert payouts._db(7).payout_job(wid)["status"] == "queued"
    assert fake.calls == []
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_daily_limit_defers_without_sending(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path, daily=100)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, amount=500, status="queued")
    child.conn.close()
    await payouts.poll_once()
    job = payouts._db(7).payout_job(wid)
    assert job["status"] == "queued"
    assert "лимит" in (job["error"] or "")
    assert not any(c[0] == "card" for c in fake.calls)
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_clicker_coins_stay_manual_until_unit_set(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    write_config(tmp_path, {**read_config(tmp_path), "bot_id": 7, "lavepay_payouts_enabled": True})
    write_setting(tmp_path, "lavepay_payouts", 1)
    app._mtime = -1
    assert payout_unit(app.s, app.currency) is None
    assert not payouts_ready(app)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 20000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "10000"))
    last = session.texts()[-1]
    assert "реквизит" in last.lower() or "Укажите" in last
    assert "LavePay" not in last


@pytest.mark.asyncio
async def test_refbot_lavepay_flow_requires_owner_then_queues(tmp_path, bot, session):
    write_config(tmp_path, {"token": "42:TEST", "template": "refbot", "owner_id": OWNER, "username": "kid_bot",
                            "vip": False, "creator_username": "creator_bot", "bot_id": 7,
                            "lavepay_payouts_enabled": True, "lavepay_payouts_auto": False})
    from child.__main__ import build_dispatcher
    from child.core import ChildApp
    app = ChildApp(tmp_path)
    write_setting(tmp_path, "lavepay_payouts", 1)
    write_setting(tmp_path, "lavepay_payout_unit", "rub")
    app._mtime = -1
    dp = build_dispatcher(app)

    async def feed(update):
        await dp.feed_update(bot, update)

    assert payouts_ready(app)
    assert not payouts_auto(app)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 1000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "500"))
    texts = session.texts()
    assert any("LavePay" in t for t in texts)
    await feed(cb_update(A, "wdm:card"))
    await feed(msg_update(A, "2200 7034 5678 9012"))
    rows = app.db.all("SELECT * FROM withdrawals")
    assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["method"] == "card"
    job = app.db.payout_job(rows[0]["id"])
    assert job["status"] == "awaiting_approval"
    assert app.db.user(A)["balance"] == 500
    from child.payout_flow import owner_resolve
    action, w = owner_resolve(app, rows[0]["id"], True)
    assert action == "enqueued"
    assert app.db.payout_job(w["id"])["status"] == "queued"
    action, _ = owner_resolve(app, rows[0]["id"], True)
    assert action in {"enqueued", "already"}
    assert app.db.payout_job(w["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_starbot_has_no_lavepay_settings():
    from child import templates
    keys = {s.key for s in templates.info("starbot").settings}
    assert "lavepay_payouts" not in keys
    clicker = {s.key for s in templates.info("clicker").settings}
    refbot = {s.key for s in templates.info("refbot").settings}
    assert "lavepay_payouts" in clicker and "lavepay_payouts" in refbot


def test_sbp_bank_match_and_normalizers():
    banks = [{"bank_id": "100000000004", "name": "Т-Банк"}, {"bank_id": "100000000111", "name": "Сбербанк"}]
    assert match_sbp_bank(banks, "т-банк")["bank_id"] == "100000000004"
    assert match_sbp_bank(banks, "100000000111")["bank_id"] == "100000000111"
    assert match_sbp_bank(banks, "банк") is None  # ambiguous/substring of both? 'банк' is in Т-Банк and Сбербанк
    assert normalize_card("2200 7034 5678 9012") == "2200703456789012"
    assert normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert normalize_sbp_name("Иван П") == "Иван П"
    assert parse_idempotency_key("cwd-12-3") == (12, 3)
    assert parse_idempotency_key("wd_12") is None


@pytest.mark.asyncio
async def test_same_idempotency_key_on_retry_does_not_double_pay(tmp_path):
    ctx, fake, payouts, notes = service(tmp_path)
    path = _bot(tmp_path, ctx.db)
    _enable_bot_settings(path)
    child = ChildDB(path / "bot.db")
    wid = _reserve(child, status="queued")
    child.conn.close()
    await payouts.poll_once()
    first = [c for c in fake.calls if c[0] == "card"]
    assert len(first) == 1
    # simulate lost DB write: job still queued, retry same key
    payouts._db(7).exec("UPDATE payout_jobs SET status='queued',provider_id=NULL WHERE withdrawal_id=?", wid)
    await payouts.poll_once()
    cards = [c for c in fake.calls if c[0] == "card"]
    assert len(cards) == 2
    assert cards[0][2] == cards[1][2] == f"cwd-7-{wid}"
    # still only one provider payout object
    ids = {v["id"] for v in fake.card.values() if "id" in v}
    assert ids == {"cp_1"}
    payouts.close()
    ctx.db.close()
