"""Durable receipt, atomic fulfilment and startup recovery using synthetic invoices."""
import asyncio
import json
import sqlite3
import subprocess
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from creator.config import Config
from creator.db import DB
from creator.lavepay import LavePayService, sign
from creator.services import Ctx


class Runtime:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def start(self, bid, **kwargs):
        if self.fail:
            raise RuntimeError("injected")
        self.calls.append(("start", bid))

    async def restart(self, bid, **kwargs):
        if self.fail:
            raise RuntimeError("injected")
        self.calls.append(("restart", bid))

    def write_files(self, bid):
        if self.fail:
            raise RuntimeError("injected")
        self.calls.append(("write", bid))


@pytest.fixture
def payment_env(tmp_path):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    db.upsert_user(1, None, "Synthetic referrer")
    db.upsert_user(2, None, "Synthetic payer", 1)
    ctx = Ctx(db=db, cfg=cfg, sup=Runtime())
    service = LavePayService(ctx, None)
    yield ctx, service
    db.close()


def invoice(ctx, payload="topup:10000"):
    local_id = ctx.db.lave_new(2, payload, 10000, "1.00")
    external_id = f"synthetic-{local_id}"
    ctx.db.lave_set_created(local_id, external_id, "https://example.invalid", 1)
    event = {"id": external_id, "order_id": f"bc-{local_id}", "status": "paid", "face_amount": "1.00", "currency": "USD"}
    return ctx.db.lave_get(local_id), event


async def test_failed_credit_stays_recoverable_and_credits_referrer_once(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE UPDATE OF balance ON users "
                        "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await service.apply(row, event)
    assert ctx.db.lave_get(row["id"])["status"] == "received"
    assert ctx.db.user(2)["balance"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM payments") == 0
    ctx.db.conn.execute("DROP TRIGGER fault")
    resumed = LavePayService(ctx, None)
    await resumed.poll_once()
    await resumed.poll_once()
    assert ctx.db.lave_get(row["id"])["status"] == "paid"
    assert ctx.db.user(2)["balance"] == 10000
    assert ctx.db.user(1)["balance"] == 1000
    assert ctx.db.scalar("SELECT COUNT(*) FROM payments") == 1


async def test_partner_failure_also_rolls_back_payer_credit(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE UPDATE OF ref_earned ON users "
                        "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await service.apply(row, event)
    assert ctx.db.user(2)["balance"] == ctx.db.user(1)["balance"] == 0
    ctx.db.conn.execute("DROP TRIGGER fault")
    await service.apply(row, event)
    assert ctx.db.user(2)["balance"] == 10000 and ctx.db.user(1)["balance"] == 1000


async def test_duplicate_callbacks_cannot_credit_twice(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    other = LavePayService(ctx, None)
    await asyncio.gather(*(s.apply(row, event) for s in (service, other, service)))
    assert ctx.db.user(2)["balance"] == 10000 and ctx.db.user(1)["balance"] == 1000


async def test_legacy_paid_without_recovery_record_is_not_replayed(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.lave_mark_paid(row["id"])
    ctx.db.register_payment(f"lave:{row['invoice_id']}", 2, 10000, row["payload"])
    assert await service.apply(row, event) is None
    await service.poll_once()
    assert ctx.db.user(2)["balance"] == ctx.db.user(1)["balance"] == 0


async def test_balance_creation_failure_does_not_charge_or_consume_draft(payment_env):
    ctx, service = payment_env
    ctx.db.change_balance(2, 50000, "topup")
    draft = ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic")
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE INSERT ON bots BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await ctx.create_bot_from_balance(2, draft)
    assert ctx.db.user(2)["balance"] == 50000 and ctx.db.draft(draft)["used"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='create'") == 0
    ctx.db.conn.execute("DROP TRIGGER fault")
    await ctx.create_bot_from_balance(2, draft)
    await ctx.create_bot_from_balance(2, draft)
    assert ctx.db.user(2)["balance"] == 50000 - ctx.template_price("clicker")


async def test_runtime_failure_survives_restart_without_double_payment(payment_env):
    ctx, service = payment_env
    draft = ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic")
    row, event = invoice(ctx, f"create:{draft}")
    ctx.sup.fail = True
    await service.apply(row, event)
    assert ctx.db.lave_get(row["id"])["status"] == "paid"
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    assert ctx.db.user(1)["balance"] == 1000
    ctx.sup.fail = False
    await LavePayService(ctx, None).poll_once()
    assert ctx.sup.calls == [("start", 1)]
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    assert ctx.db.user(1)["balance"] == 1000


async def test_recovery_after_database_reopen(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE UPDATE OF balance ON users "
                        "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await service.apply(row, event)
    ctx.db.conn.execute("DROP TRIGGER fault")
    ctx.db.close()
    db = DB(ctx.cfg.db_path)
    try:
        fresh = Ctx(db=db, cfg=ctx.cfg, sup=Runtime())
        await LavePayService(fresh, None).poll_once()
        assert db.user(2)["balance"] == 10000 and db.user(1)["balance"] == 1000
        assert db.lave_get(row["id"])["status"] == "paid"
    finally:
        db.close()


@pytest.mark.parametrize("trigger", [
    "BEFORE UPDATE OF status ON lave_invoices WHEN NEW.status='paid'",
    "BEFORE UPDATE OF status ON payment_jobs WHEN NEW.status='applied'",
])
async def test_completion_marker_failure_rolls_back_all_credits(payment_env, trigger):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.conn.execute(f"CREATE TRIGGER fault {trigger} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await service.apply(row, event)
    assert ctx.db.user(2)["balance"] == ctx.db.user(1)["balance"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM payments") == 0
    assert ctx.db.lave_get(row["id"])["status"] == "received"
    ctx.db.conn.execute("DROP TRIGGER fault")
    await service.poll_once()
    assert ctx.db.user(2)["balance"] == 10000 and ctx.db.user(1)["balance"] == 1000


async def test_webhook_acknowledges_only_after_durable_receipt(payment_env, monkeypatch):
    ctx, service = payment_env
    service.webhook_secret = "synthetic-webhook-key"
    row, event = invoice(ctx)

    async def simulate_lost_background_work(*args):
        pass

    monkeypatch.setattr(service, "_resume_safe", simulate_lost_background_work)
    body = json.dumps({"id": "synthetic-event", "type": "invoice.paid", "data": event}).encode()
    ts = str(int(time.time()))
    async with TestClient(TestServer(service.webhook_app())) as client:
        response = await client.post("/lavepay", data=body, headers={"X-LavePay-Timestamp": ts,
                                      "X-LavePay-Signature": sign(service.webhook_secret, ts, body)})
        assert response.status == 200
    assert ctx.db.lave_get(row["id"])["status"] == "received"
    assert ctx.db.user(2)["balance"] == 0
    await LavePayService(ctx, None).poll_once()
    assert ctx.db.user(2)["balance"] == 10000


async def test_failed_receipt_persistence_is_not_acknowledged(payment_env):
    ctx, service = payment_env
    service.webhook_secret = "synthetic-webhook-key"
    row, event = invoice(ctx)
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE INSERT ON payment_jobs BEGIN SELECT RAISE(ABORT, 'injected'); END")
    body = json.dumps({"type": "invoice.paid", "data": event}).encode()
    ts = str(int(time.time()))
    async with TestClient(TestServer(service.webhook_app())) as client:
        response = await client.post("/lavepay", data=body, headers={"X-LavePay-Timestamp": ts,
                                      "X-LavePay-Signature": sign(service.webhook_secret, ts, body)})
        assert response.status >= 500
    assert ctx.db.lave_get(row["id"])["status"] == "active"
    assert ctx.db.user(2)["balance"] == 0


async def test_vip_purchase_rollback_and_duplicate(payment_env):
    ctx, service = payment_env
    ctx.db.change_balance(2, 50000, "topup")
    bid = ctx.db.materialize_draft(ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic"), 3150)
    ctx.db.conn.execute("CREATE TRIGGER fault BEFORE UPDATE OF vip ON bots BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await ctx.buy_vip_from_balance(2, bid)
    assert ctx.db.user(2)["balance"] == 50000 and ctx.db.bot(bid)["vip"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='vip'") == 0
    ctx.db.conn.execute("DROP TRIGGER fault")
    assert await ctx.buy_vip_from_balance(2, bid)
    assert not await ctx.buy_vip_from_balance(2, bid)
    assert ctx.db.user(2)["balance"] == 50000 - ctx.price("VIP_PRICE")


async def test_stopped_bot_is_not_started_by_old_job(payment_env):
    ctx, service = payment_env
    bid = ctx.db.materialize_draft(ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic"), 3150)
    ctx.db.enqueue_runtime("synthetic-start", bid, "start")
    ctx.db.update_bot(bid, desired=0, status="stopped")
    await ctx.run_runtime_jobs()
    assert ctx.sup.calls == []
    assert ctx.db.runtime_pending() == []


async def test_real_process_exit_after_receipt_is_recovered(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    code = '''
import asyncio, json, os, sys
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.lavepay import LavePayService
db = DB(sys.argv[1])
db.conn.create_function("injected_exit", 0, lambda: os._exit(99))
db.conn.execute("CREATE TEMP TRIGGER fault AFTER INSERT ON payments BEGIN SELECT injected_exit(); END")
ctx = Ctx(db=db, cfg=Config(), sup=None)
asyncio.run(LavePayService(ctx, None).apply(db.lave_get(1), json.loads(sys.argv[2])))
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(ctx.cfg.db_path), json.dumps(event)],
                                     capture_output=True, timeout=15)
    assert result.returncode == 99, result.stderr.decode()
    assert ctx.db.lave_get(row["id"])["status"] == "received"
    assert ctx.db.user(2)["balance"] == 0 and ctx.db.scalar("SELECT COUNT(*) FROM payments") == 0
    await service.poll_once()
    assert ctx.db.user(2)["balance"] == 10000 and ctx.db.user(1)["balance"] == 1000


def test_additive_schema_preserves_existing_financial_rows(payment_env):
    ctx, service = payment_env
    row, event = invoice(ctx)
    ctx.db.lave_mark_paid(row["id"])
    ctx.db.change_balance(2, 10000, "topup")
    ctx.db.register_payment(f"lave:{row['invoice_id']}", 2, 10000, row["payload"])
    tables = ("users", "transactions", "payments", "lave_invoices", "kv")
    before = {t: [tuple(r) for r in ctx.db.all(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    ctx.db.conn.execute("DROP TABLE payment_jobs")
    ctx.db.conn.execute("DROP TABLE bot_runtime_jobs")
    ctx.db.close()
    reopened = DB(ctx.cfg.db_path)
    try:
        assert {t: [tuple(r) for r in reopened.all(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables} == before
        assert reopened.lave_received() == reopened.runtime_pending() == []
    finally:
        reopened.close()


def test_nested_helper_failure_is_part_of_outer_transaction(payment_env):
    ctx, service = payment_env
    with pytest.raises(RuntimeError), ctx.db.tx():
        ctx.db.change_balance(2, 10000, "topup")
        ctx.db.credit_referrer(2, 10000, 10)
        raise RuntimeError("injected after helpers")
    assert ctx.db.user(2)["balance"] == ctx.db.user(1)["balance"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions") == 0


@pytest.mark.parametrize("kind", ["host", "vip"])
async def test_late_payment_does_not_change_transferred_bot(payment_env, kind):
    ctx, service = payment_env
    ctx.db.upsert_user(3, None, "Synthetic buyer")
    bid = ctx.db.materialize_draft(ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic"), 3150)
    payload = f"host:{bid}:10000" if kind == "host" else f"vip:{bid}"
    row, event = invoice(ctx, payload)
    ctx.db.transfer_bot(bid, 3)
    await service.apply(row, event)
    await service.apply(row, event)
    assert ctx.db.bot(bid)["hosting_balance"] == 3150 and ctx.db.bot(bid)["vip"] == 0
    assert ctx.db.user(2)["balance"] == 10000
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='refund'") == 1


async def test_balance_creation_rolls_back_when_materialize_returns_none(payment_env, monkeypatch):
    ctx, _service = payment_env
    ctx.db.change_balance(2, 50000, "topup")
    draft = ctx.db.create_draft(2, "clicker", "42:SYNTHETIC", "synthetic_bot", "Synthetic")
    start = ctx.db.user(2)["balance"]

    def already_used(draft_id, start_hosting):
        return None

    monkeypatch.setattr(ctx.db, "materialize_draft", already_used)
    bot_id = await ctx.create_bot_from_balance(2, draft)
    assert bot_id is None
    assert ctx.db.user(2)["balance"] == start
    assert ctx.db.draft(draft)["used"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots WHERE deleted=0") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='create'") == 0
    assert ctx.db.runtime_pending() == []
