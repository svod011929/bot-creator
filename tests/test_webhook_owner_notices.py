"""Owner webhook-URL notices and creator-hub unsubscribe webhooks."""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, SLEEPER, TOKEN, env  # noqa: F401
from tests.test_unsubscribe_webhooks import (
    LINK,
    USER,
    add_bot,
    credit_pf,
    credit_tgrass,
    seed_child,
    user_row,
)

OWNER2 = 101
OTHER = "https://t.me/otherchannel"


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "c.db")
    yield d
    d.close()


@pytest.fixture
def svc(tmp_path, db):
    from creator.unsubscribe_webhooks import UnsubscribeService
    cfg = Config(data_dir=tmp_path, admin_ids={ADMIN}, webhook_public_base="https://bnbb.tech")
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="DDDCReatorBot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup)
    owner_notes, child_notes = [], []

    async def notify_owner(uid, text):
        owner_notes.append((uid, text))

    async def notify_child(bot_id, uid, text):
        child_notes.append((bot_id, uid, text))

    service = UnsubscribeService(ctx, notify_owner=notify_owner, notify_child=notify_child)
    service.owner_notes, service.child_notes = owner_notes, child_notes
    return service


def seed_network(db, bot_id, provider, mode="factory", status="approved", applicant=OWNER):
    if provider == "tgrass":
        db.conn.execute(
            "INSERT INTO tgrass_bots(bot_id,applicant_id,status,mode,api_key,users_at_apply,applied_at) "
            "VALUES(?,?,?,?, 'k',0,0)", (bot_id, applicant, status, mode))
    else:
        db.conn.execute(
            "INSERT INTO pf_bots(bot_id,applicant_id,status,mode,api_key,users_at_apply,applied_at) "
            "VALUES(?,?,?,?, 'k',0,0)", (bot_id, applicant, status, mode))
    db.conn.commit()


def payload(uid=USER, link=LINK, **extra):
    body = {"tg_user_id": uid, "offer_link": link, "status": "unsubscribed", "reason": "unsubscribed"}
    body.update(extra)
    return body


# ---------- owner URL notices ----------
@pytest.mark.asyncio
async def test_notify_owner_urls_sends_once_per_secret(svc, db):
    bot_id = add_bot(db, username="dhdvxcbot")
    seed_network(db, bot_id, "tgrass")
    seed_network(db, bot_id, "piarflow")
    sent = await svc.notify_owner_urls(bot_id)
    assert sent == 2
    assert len(svc.owner_notes) == 1
    uid, text = svc.owner_notes[0]
    assert uid == OWNER
    tg_url = svc.url(bot_id, "tgrass")
    pf_url = svc.url(bot_id, "piarflow")
    assert tg_url in text and pf_url in text
    assert "Настройки бота → Webhook → отписка" in text
    assert "кабинет @PiarFlowBot → Webhook / API" in text
    assert "награде" in text.lower() or "наград" in text.lower()
    assert "не пересылайте" in text.lower() or "не делитесь" in text.lower()
    assert "🔗 Вебхуки отписок" in text
    # same secret — no resend
    assert await svc.notify_owner_urls(bot_id) == 0
    assert len(svc.owner_notes) == 1
    # only one provider pending after a new secret
    db.conn.execute("UPDATE webhook_secrets SET secret='newsecret012345678901234' "
                    "WHERE bot_id=? AND provider='tgrass'", (bot_id,))
    db.conn.commit()
    assert await svc.notify_owner_urls(bot_id, providers=("tgrass", "piarflow")) == 1
    assert len(svc.owner_notes) == 2
    assert svc.url(bot_id, "tgrass") in svc.owner_notes[1][1]
    assert svc.url(bot_id, "piarflow") not in svc.owner_notes[1][1]


@pytest.mark.asyncio
async def test_notify_owner_urls_failure_is_not_marked(svc, db):
    bot_id = add_bot(db)
    seed_network(db, bot_id, "tgrass")

    async def boom(uid, text):
        raise RuntimeError("bot blocked")

    svc.notify_owner = boom
    svc.notify_owner_strict = boom
    assert await svc.notify_owner_urls(bot_id) == 0
    assert db.scalar("SELECT COUNT(*) FROM webhook_url_notices") == 0
    # later success still sends
    notes = []

    async def ok(uid, text):
        notes.append((uid, text))

    svc.notify_owner = ok
    svc.notify_owner_strict = ok
    assert await svc.notify_owner_urls(bot_id) == 1
    assert len(notes) == 1
    assert db.scalar("SELECT COUNT(*) FROM webhook_url_notices") == 1


@pytest.mark.asyncio
async def test_backfill_one_combined_message_per_bot(svc, db):
    a = add_bot(db, username="dhdvxcbot")
    b = add_bot(db, owner=OWNER2, token="2:AA", username="DDDFreeStarsBot")
    user_row(db, OWNER2)
    seed_network(db, a, "tgrass")
    seed_network(db, a, "piarflow")
    seed_network(db, b, "tgrass")
    seed_network(db, b, "piarflow")
    pending = add_bot(db, owner=OWNER2, token="3:AA", username="not_connected")
    seed_network(db, pending, "tgrass", status="pending")
    result = await svc.backfill_owner_url_notices()
    assert result["sent"] == 2
    assert result["failed"] == 0
    owners = {uid for uid, _ in svc.owner_notes}
    assert owners == {OWNER, OWNER2}
    assert len(svc.owner_notes) == 2
    # idempotent with the same marker
    assert (await svc.backfill_owner_url_notices())["sent"] == 0
    assert len(svc.owner_notes) == 2


@pytest.mark.asyncio
async def test_approve_hooks_notify_and_skip_when_unsub_missing(tmp_path, db):
    from creator.tgrass import TGrassService
    from creator.unsubscribe_webhooks import attach
    from tests.test_tgrass import KEY, FakeClient

    cfg = Config(data_dir=tmp_path, admin_ids={ADMIN}, webhook_public_base="https://bnbb.tech",
                 tgrass_factory_token="factory", tgrass_owner_id=ADMIN,
                 tgrass_owner_username="sample_owner")
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup)
    # no ctx.unsub: approve must still succeed

    async def apply_changes(bot_id):
        return None

    ctx.apply_changes = apply_changes
    client = FakeClient()
    ctx.tgrass = TGrassService(ctx, client)
    bot_id = add_bot(db, username="hookbot")
    db.conn.execute(
        "INSERT INTO tgrass_bots(bot_id,applicant_id,status,mode,users_at_apply,applied_at) "
        "VALUES(?,?,'pending','factory',0,0)", (bot_id, OWNER))
    db.conn.commit()
    await ctx.tgrass.approve(bot_id, ADMIN)
    assert db.tgrass(bot_id)["status"] == "approved"
    assert db.scalar("SELECT COUNT(*) FROM webhook_url_notices") == 0

    # reconnect path with unsub attached
    notes = []

    async def notify_owner(uid, text):
        notes.append((uid, text))

    attach(ctx, notify_owner=notify_owner)
    db.tgrass_set(bot_id, status="disabled")
    await ctx.tgrass.approve(bot_id, ADMIN)
    assert notes and notes[0][0] == OWNER
    assert KEY not in notes[0][1]
    assert f"/webhooks/tgrass/{bot_id}/" in notes[0][1]
    # already approved — no second send
    n = len(notes)
    await ctx.tgrass.approve(bot_id, ADMIN)
    assert len(notes) == n


# ---------- creator hub / relay webhooks ----------
def test_hub_url_uses_hub_path_not_zero(svc):
    url = svc.hub_url("tgrass")
    assert url.startswith("https://bnbb.tech/webhooks/tgrass/hub/")
    assert "/webhooks/tgrass/0/" not in url
    assert "/hub/" in svc.hub_url("piarflow")
    # generating the secret does not require a bots row
    assert svc.ensure_secret(0, "tgrass")
    assert db_has_hub_secret(svc)


def db_has_hub_secret(svc):
    return svc.ctx.db.one("SELECT secret FROM webhook_secrets WHERE bot_id=0 AND provider='tgrass'") is not None


@pytest.mark.asyncio
async def test_hub_unique_relay_op_uses_approved_penalty_rules(svc, db):
    bot_id = add_bot(db)
    seed_network(db, bot_id, "tgrass", mode="relay")
    credit_tgrass(db, bot_id, reward=50)
    seed_child(svc, bot_id, "tgrass")
    secret = svc.ensure_secret(0, "tgrass")
    app = svc.app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/webhooks/tgrass/hub/wrongsecret",
                              data=json.dumps(payload()))
        assert r.status == 404
        r = await client.post(f"/webhooks/tgrass/hub/{secret}",
                              data=json.dumps(payload()))
        assert 200 <= r.status < 300
    assert db.user(OWNER)["balance"] == 0
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    assert db.scalar("SELECT amount FROM transactions WHERE kind='tgrass_penalty'") == -50
    assert svc.owner_notes and svc.owner_notes[0][0] == OWNER
    assert svc.child_notes and svc.child_notes[0][0] == bot_id and svc.child_notes[0][1] == USER
    from child.tgrass import completion_state
    assert completion_state(svc.ctx.sup.bot_dir(bot_id), USER, 42)["done"] is False
    assert db.tgrass_credit(bot_id, USER, 42, 50, offer_link=LINK) is False


@pytest.mark.asyncio
async def test_hub_ignores_factory_ops_and_penalizes_latest_relay(svc, db):
    factory = add_bot(db, username="factorybot")
    relay_a = add_bot(db, token="2:AA", username="relaya")
    relay_b = add_bot(db, owner=OWNER2, token="3:AA", username="relayb")
    user_row(db, OWNER2)
    seed_network(db, factory, "tgrass", mode="factory")
    seed_network(db, relay_a, "tgrass", mode="relay")
    seed_network(db, relay_b, "tgrass", mode="relay")
    assert db.tgrass_credit(factory, USER, 42, 70, ts=50, offer_link=LINK) is True
    assert db.tgrass_credit(relay_a, USER, 42, 50, ts=100, offer_link=LINK) is True
    assert db.tgrass_credit(relay_b, USER, 42, 40, ts=200, offer_link=LINK) is True
    seed_child(svc, factory, "tgrass")
    seed_child(svc, relay_a, "tgrass")
    seed_child(svc, relay_b, "tgrass")
    secret = svc.ensure_secret(0, "tgrass")
    app = svc.app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhooks/tgrass/hub/{secret}", data=json.dumps(payload()))
        assert 200 <= r.status < 300
        # repeat: still only the latest op, no second debit
        r = await client.post(f"/webhooks/tgrass/hub/{secret}", data=json.dumps(payload()))
        assert 200 <= r.status < 300
    assert db.user(OWNER)["balance"] == 120  # 70 factory + 50 earlier relay, untouched
    assert db.user(OWNER2)["balance"] == 0   # latest relay (40) penalized
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    assert db.scalar("SELECT amount FROM transactions WHERE kind='tgrass_penalty'") == -40
    assert db.one("SELECT bot_id, owner_id FROM unsubscribe_penalties")["bot_id"] == relay_b
    from child.tgrass import completion_state
    assert completion_state(svc.ctx.sup.bot_dir(relay_b), USER, 42)["done"] is False
    assert completion_state(svc.ctx.sup.bot_dir(relay_a), USER, 42)["done"] is True
    assert completion_state(svc.ctx.sup.bot_dir(factory), USER, 42)["done"] is True
    # factory-only offer is still ignored on the hub URL
    assert db.tgrass_credit(factory, USER, 99, 30, ts=300, offer_link=OTHER) is True
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhooks/tgrass/hub/{secret}",
                              data=json.dumps(payload(link=OTHER)))
        assert 200 <= r.status < 300
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    assert db.user(OWNER)["balance"] == 150


@pytest.mark.asyncio
async def test_hub_latest_relay_tie_breaks_by_higher_op_id(svc, db):
    older = add_bot(db, username="relaya")
    newer = add_bot(db, owner=OWNER2, token="2:AA", username="relayb")
    user_row(db, OWNER2)
    seed_network(db, older, "tgrass", mode="relay")
    seed_network(db, newer, "tgrass", mode="relay")
    assert db.tgrass_credit(older, USER, 42, 50, ts=100, offer_link=LINK) is True
    assert db.tgrass_credit(newer, USER, 42, 80, ts=100, offer_link=LINK) is True
    seed_child(svc, older, "tgrass")
    seed_child(svc, newer, "tgrass")
    secret = svc.ensure_secret(0, "tgrass")
    app = svc.app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhooks/tgrass/hub/{secret}", data=json.dumps(payload()))
        assert 200 <= r.status < 300
    assert db.user(OWNER)["balance"] == 50
    assert db.user(OWNER2)["balance"] == 0
    assert db.scalar("SELECT amount FROM transactions WHERE kind='tgrass_penalty'") == -80
    assert db.one("SELECT bot_id FROM unsubscribe_penalties")["bot_id"] == newer


@pytest.mark.asyncio
async def test_hub_piarflow_applies_when_unique_even_if_payload_bot_id_is_creator(svc, db):
    bot_id = add_bot(db)
    seed_network(db, bot_id, "piarflow", mode="relay")
    credit_pf(db, bot_id, reward=50)
    seed_child(svc, bot_id, "piarflow")
    secret = svc.ensure_secret(0, "piarflow")
    app = svc.app()
    # payload bot_id is the creator/hub Telegram id, not the child's
    body = payload(bot_id=999001)
    async with TestClient(TestServer(app)) as client:
        r = await client.post(f"/webhooks/piarflow/hub/{secret}", data=json.dumps(body))
        assert 200 <= r.status < 300
    assert db.user(OWNER)["balance"] == 0
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='piarflow_penalty'") == 1
    assert svc.child_notes and svc.child_notes[0][1] == USER


@pytest.mark.asyncio
async def test_admin_hub_screen_is_not_shown_to_owners(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.webhook_public_base = "https://bnbb.tech"
    from creator.unsubscribe_webhooks import attach
    attach(ctx)
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(ADMIN, "/start"))
    from tests.test_creator_flow import create_paid_bot
    bot_id = await create_paid_bot(ctx, feed, session, OWNER)
    ctx.db.conn.execute(
        "INSERT INTO tgrass_bots(bot_id,applicant_id,status,mode,api_key,users_at_apply,applied_at) "
        "VALUES(?,?, 'approved','relay','k',0,0)", (bot_id, OWNER))
    ctx.db.conn.commit()
    hub = ctx.unsub.hub_url("tgrass")
    # owner card must not contain the hub URL
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:wh"))
    shown = "\n".join(session.texts())
    assert "/webhooks/tgrass/hub/" not in shown
    assert hub not in shown
    # admin panel has the relay/hub button
    session.clear()
    await feed(msg_update(ADMIN, "🛠 Админ-панель"))
    texts = [(b.text, b.callback_data) for m in session.requests
             for kb in [getattr(m, "reply_markup", None)]
             for row in (getattr(kb, "inline_keyboard", None) or [])
             for b in row]
    assert any(data == "a:whhub" or "трансляц" in (text or "").lower() for text, data in texts)
    session.clear()
    await feed(cb_update(ADMIN, "a:whhub"))
    shown = "\n".join(session.texts())
    assert hub in shown
    assert "Настройки бота → Webhook → отписка" in shown
    assert "кабинет @PiarFlowBot → Webhook / API" in shown
    # owner cannot open the admin hub screen
    session.clear()
    await feed(cb_update(OWNER, "a:whhub"))
    shown = "\n".join(session.texts())
    assert hub not in shown
