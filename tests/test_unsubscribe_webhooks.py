"""Unsubscribe-penalty webhooks for TGrass and PiarFlow (approved rules 2026-11-10)."""
from __future__ import annotations

import json
import logging
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

from creator import withdrawals as wd
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, SLEEPER, TOKEN, env  # noqa: F401

USER = 555
LINK = "https://t.me/cryptonews"
TRACK = "https://t.me/PiarFlowBot?startapp=abc123&bot=example_ref_bot"
REJECT_REASON = "автоотклонена: штраф за отписку"


def user_row(db: DB, uid=OWNER):
    if db.user(uid) is None:
        db.conn.execute("INSERT INTO users(id,username,full_name,created_at) VALUES(?,?,?,0)",
                        (uid, f"u{uid}", "U"))
    return uid


def add_bot(db: DB, owner=OWNER, token=TOKEN, username="my_clicker_bot"):
    user_row(db, owner)
    cur = db.conn.execute(
        "INSERT INTO bots(owner_id,template,token,username,title,status,desired,hosting_balance,created_at) "
        "VALUES(?,?,?,?,?,'running',1,0,0)",
        (owner, "starbot", token, username, "Bot"))
    return int(cur.lastrowid)


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "c.db")
    yield d
    d.close()


@pytest.fixture
def svc(tmp_path, db):
    from creator.unsubscribe_webhooks import UnsubscribeService
    cfg = Config(data_dir=tmp_path, admin_ids={ADMIN}, webhook_public_base="https://bnbb.tech")
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup)
    owner_notes, child_notes = [], []

    async def notify_owner(uid, text):
        owner_notes.append((uid, text))

    async def notify_child(bot_id, uid, text):
        child_notes.append((bot_id, uid, text))

    service = UnsubscribeService(ctx, notify_owner=notify_owner, notify_child=notify_child)
    service.owner_notes, service.child_notes = owner_notes, child_notes
    return service


def credit_tgrass(db, bot_id, uid=USER, offer_id=42, reward=50, link=LINK, owner=OWNER):
    assert db.tgrass_credit(bot_id, uid, offer_id, reward, offer_link=link) is True
    return reward


def credit_pf(db, bot_id, uid=USER, link=LINK, reward=50):
    assert db.pf_credit(bot_id, uid, link, reward) is True
    return reward


def seed_child(svc, bot_id, provider, uid=USER, offer_id=42, link=LINK):
    path = svc.ctx.sup.bot_dir(bot_id)
    path.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(path / "bot.db")
    if provider == "tgrass":
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS tgrass_shown("
            "user_id INTEGER NOT NULL, offer_id INTEGER NOT NULL, shown_at INTEGER NOT NULL, "
            "offer_link TEXT, PRIMARY KEY(user_id,offer_id));"
            "CREATE TABLE IF NOT EXISTS tgrass_done("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, offer_id INTEGER NOT NULL, "
            "fake INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, offer_link TEXT, "
            "UNIQUE(user_id,offer_id));")
        conn.execute("INSERT INTO tgrass_shown(user_id,offer_id,shown_at,offer_link) VALUES(?,?,1,?)",
                     (uid, offer_id, link))
        conn.execute("INSERT INTO tgrass_done(user_id,offer_id,fake,created_at,offer_link) VALUES(?,?,0,1,?)",
                     (uid, offer_id, link))
    else:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS pf_shown("
            "user_id INTEGER NOT NULL, link TEXT NOT NULL, shown_at INTEGER NOT NULL, "
            "PRIMARY KEY(user_id,link));"
            "CREATE TABLE IF NOT EXISTS pf_done("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, link TEXT NOT NULL, "
            "counted INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, UNIQUE(user_id,link));")
        conn.execute("INSERT INTO pf_shown(user_id,link,shown_at) VALUES(?,?,1)", (uid, link))
        conn.execute("INSERT INTO pf_done(user_id,link,counted,created_at) VALUES(?,?,1,1)", (uid, link))
    conn.commit()
    conn.close()


# ---------- formula ----------
def test_formula_subtracts_penalty_and_floors_negative_balance(db):
    uid = user_row(db)
    db.change_balance(uid, 5000, "tgrass")
    db.change_balance(uid, -5000, "tgrass_penalty", allow_negative=True)
    db.change_balance(uid, -2000, "tgrass_penalty", allow_negative=True)
    assert db.user(uid)["balance"] == -2000
    a = wd.available(db, uid)
    assert a.available == 0
    # 50 earned minus 70 penalties; available floors at 0 even with negative balance
    assert a.earned - a.penalties - a.spent - a.withdrawn == -2000

    uid2 = user_row(db, 2)
    db.change_balance(uid2, 10000, "piarflow")
    db.change_balance(uid2, 8000, "topup")
    db.change_balance(uid2, -3000, "piarflow_penalty", allow_negative=True)
    assert wd.available(db, uid2).available == 7000


# ---------- mapping / normalize ----------
def test_normalize_piarflow_tracking_and_tgrass_link():
    from creator.unsubscribe_webhooks import links_match, normalize_offer_link
    assert normalize_offer_link("https://t.me/CryptoNews/") == normalize_offer_link("https://t.me/cryptonews")
    assert links_match(TRACK, TRACK)
    assert links_match(LINK, LINK)
    assert links_match("https://t.me/CryptoNews/", "https://t.me/cryptonews")
    # tracking URL that embeds the same target as the stored display link
    assert links_match("https://piarflow.com/track?offer=https://t.me/cryptonews", LINK)


# ---------- core apply ----------
@pytest.mark.asyncio
async def test_penalty_exact_once_and_idempotent(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=50)
    seed_child(svc, bot_id, "tgrass")
    payload = {"tg_user_id": USER, "offer_link": LINK, "status": "unsubscribed", "reason": "unsubscribed"}
    first = await svc.apply("tgrass", bot_id, payload)
    assert first.applied and first.amount == 50
    assert db.user(OWNER)["balance"] == 0
    kinds = [r["kind"] for r in db.history(OWNER, 10)]
    assert "tgrass_penalty" in kinds
    assert db.scalar("SELECT amount FROM transactions WHERE kind='tgrass_penalty'") == -50
    second = await svc.apply("tgrass", bot_id, payload)
    assert not second.applied and second.reason == "already"
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    # all reasons, only if credited
    for reason in ("wrong_lang", "from_other_link", "fake_subscriber"):
        r = await svc.apply("tgrass", bot_id, {**payload, "reason": reason})
        assert not r.applied
    none = await svc.apply("tgrass", bot_id, {**payload, "tg_user_id": 999001, "offer_link": "https://t.me/other"})
    assert not none.applied and none.reason == "no_op"
    assert db.user(OWNER)["balance"] == 0


@pytest.mark.asyncio
async def test_negative_balance_allowed(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=80)
    db.change_balance(OWNER, -50, "create")  # spent part of the reward
    seed_child(svc, bot_id, "tgrass")
    await svc.apply("tgrass", bot_id, {"tg_user_id": USER, "offer_link": LINK, "status": "unsubscribed",
                                      "reason": "unsubscribed"})
    assert db.user(OWNER)["balance"] == -50
    assert wd.available(db, OWNER).available == 0


@pytest.mark.asyncio
async def test_auto_reject_open_withdrawal_then_penalize(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=20000)
    seed_child(svc, bot_id, "tgrass")
    req = wd.create_request(db, OWNER, 15000, "card", "4111111111111111", fee_percent=5, minimum=10000)
    assert db.user(OWNER)["balance"] == 5000
    notes_before = len(svc.owner_notes)
    result = await svc.apply("tgrass", bot_id, {"tg_user_id": USER, "offer_link": LINK,
                                               "status": "unsubscribed", "reason": "unsubscribed"})
    assert result.applied
    row = db.one("SELECT * FROM withdrawal_requests WHERE id=?", req["id"])
    assert row["status"] == "rejected" and row["reason"] == REJECT_REASON
    assert db.user(OWNER)["balance"] == 0  # 5000 refunded to 20000, then -20000
    assert any(REJECT_REASON in t or "отклонен" in t for _, t in svc.owner_notes[notes_before:])


@pytest.mark.asyncio
async def test_paid_withdrawal_is_not_touched(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=20000)
    seed_child(svc, bot_id, "tgrass")
    req = wd.create_request(db, OWNER, 10000, "card", "4111111111111111", fee_percent=5, minimum=10000)
    assert wd.resolve(db, req["id"], approve=True, by=ADMIN)["status"] == "paid"
    before = db.one("SELECT * FROM withdrawal_requests WHERE id=?", req["id"])
    await svc.apply("tgrass", bot_id, {"tg_user_id": USER, "offer_link": LINK,
                                      "status": "unsubscribed", "reason": "unsubscribed"})
    after = db.one("SELECT * FROM withdrawal_requests WHERE id=?", req["id"])
    assert after["status"] == "paid" and after["resolved_by"] == before["resolved_by"]
    assert db.user(OWNER)["balance"] == -10000


@pytest.mark.asyncio
async def test_penalty_vs_approver_race(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=20000)
    seed_child(svc, bot_id, "tgrass")
    req = wd.create_request(db, OWNER, 10000, "card", "4111111111111111", fee_percent=5, minimum=10000)
    outcomes = []

    def approve():
        outcomes.append(("approve", wd.resolve(db, req["id"], approve=True, by=ADMIN)))

    def punish():
        import asyncio
        outcomes.append(("penalty", asyncio.run(svc.apply(
            "tgrass", bot_id, {"tg_user_id": USER, "offer_link": LINK,
                               "status": "unsubscribed", "reason": "unsubscribed"}))))

    t1, t2 = threading.Thread(target=approve), threading.Thread(target=punish)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    row = db.one("SELECT * FROM withdrawal_requests WHERE id=?", req["id"])
    assert row["status"] in ("paid", "rejected")
    if row["status"] == "rejected":
        assert row["reason"] == REJECT_REASON
        assert sum(o is not None and name == "approve" for name, o in outcomes if name == "approve") == 0 or \
            all(o is None for name, o in outcomes if name == "approve")
    else:
        assert row["reason"] != REJECT_REASON
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    # balance: paid leaves 10000 then -20000 = -10000; rejected refunds then -20000 = 0
    assert db.user(OWNER)["balance"] in (0, -10000)
    # no double refund
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='withdraw_refund'") in (0, 1)


@pytest.mark.asyncio
async def test_gate_revert_no_repeat_credit(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=50)
    seed_child(svc, bot_id, "tgrass")
    await svc.apply("tgrass", bot_id, {"tg_user_id": USER, "offer_link": LINK,
                                      "status": "unsubscribed", "reason": "unsubscribed"})
    from child.tgrass import completion_state
    st = completion_state(svc.ctx.sup.bot_dir(bot_id), USER, 42)
    assert st["done"] is False
    # UNIQUE still blocks a second credit for the same op
    assert db.tgrass_credit(bot_id, USER, 42, 50, offer_link=LINK) is False
    assert db.scalar("SELECT COUNT(*) FROM tgrass_ops WHERE bot_id=? AND tg_user_id=? AND offer_id=?",
                     bot_id, USER, 42) == 1


@pytest.mark.asyncio
async def test_notifies_owner_and_user_once(svc, db):
    bot_id = add_bot(db)
    credit_tgrass(db, bot_id, reward=50)
    seed_child(svc, bot_id, "tgrass")
    payload = {"tg_user_id": USER, "offer_link": LINK, "status": "unsubscribed", "reason": "unsubscribed"}
    await svc.apply("tgrass", bot_id, payload)
    await svc.apply("tgrass", bot_id, payload)
    # owner notes may also include withdrawal; count penalty notices
    penalty_owner = [t for uid, t in svc.owner_notes if uid == OWNER and "отпис" in t]
    assert len(penalty_owner) == 1
    assert str(USER) not in penalty_owner[0]  # do not leak the child's Telegram id
    assert "0,50" in penalty_owner[0] or "0.50" in penalty_owner[0]
    child = [t for b, uid, t in svc.child_notes if b == bot_id and uid == USER]
    assert len(child) == 1 and "подпис" in child[0].lower()


@pytest.mark.asyncio
async def test_piarflow_bot_id_and_tracking_link(svc, db):
    token = "987654321:AAEexampleexampleexampleexample12345"
    bot_id = add_bot(db, token=token, username="example_ref_bot")
    db.pf_set_row = db.pf_set
    db.conn.execute(
        "INSERT INTO pf_bots(bot_id,applicant_id,status,mode,api_key,pf_bot_id,users_at_apply,applied_at) "
        "VALUES(?,?, 'approved','factory','k',987654321,0,0)", (bot_id, OWNER))
    credit_pf(db, bot_id, link=LINK, reward=70)
    seed_child(svc, bot_id, "piarflow", link=LINK)
    payload = {"tg_user_id": USER, "offer_link": LINK, "status": "unsubscribed",
               "chat_id": USER, "bot_id": 987654321}
    r = await svc.apply("piarflow", bot_id, payload)
    assert r.applied and r.amount == 70
    assert db.scalar("SELECT amount FROM transactions WHERE kind='piarflow_penalty'") == -70


# ---------- HTTP ----------
@pytest.mark.asyncio
async def test_http_auth_test_event_and_idempotent_post(svc, db):
    bot_id = add_bot(db)
    secret = svc.ensure_secret(bot_id, "tgrass")
    credit_tgrass(db, bot_id, reward=50)
    seed_child(svc, bot_id, "tgrass")
    url = f"/webhooks/tgrass/{bot_id}/{secret}"
    bad = f"/webhooks/tgrass/{bot_id}/not-the-secret"
    payload = {"tg_user_id": USER, "offer_link": LINK, "status": "unsubscribed", "reason": "unsubscribed"}
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("unsubscribe")
    h = H()
    log.addHandler(h)
    try:
        async with TestClient(TestServer(svc.app())) as client:
            r = await client.post(bad, data=json.dumps(payload))
            assert r.status == 404
            assert db.user(OWNER)["balance"] == 50
            r = await client.post(f"/webhooks/tgrass/99999/{secret}", data=json.dumps(payload))
            assert r.status == 404
            r = await client.post(url, data=json.dumps({"test": True, "tg_user_id": USER}))
            assert r.status in (200, 204, 201, 202) and 200 <= r.status < 300
            assert db.user(OWNER)["balance"] == 50
            for _ in range(2):
                r = await client.post(url, data=json.dumps(payload))
                assert 200 <= r.status < 300
            assert db.user(OWNER)["balance"] == 0
            assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='tgrass_penalty'") == 1
    finally:
        log.removeHandler(h)
    joined = "\n".join(records)
    assert secret not in joined


# ---------- UI (owner + admin only) ----------
@pytest.mark.asyncio
async def test_owner_and_admin_can_open_webhook_urls(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.webhook_public_base = "https://bnbb.tech"
    from creator.unsubscribe_webhooks import attach
    attach(ctx)
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(ADMIN, "/start"))
    ctx.db.change_balance(OWNER, ctx.template_price("starbot") or ctx.template_price("clicker") or 15000, "topup")
    # create via helper if clicker price exists
    from tests.test_creator_flow import create_paid_bot
    bot_id = await create_paid_bot(ctx, feed, session, OWNER)
    ctx.db.conn.execute(
        "INSERT INTO tgrass_bots(bot_id,applicant_id,status,mode,api_key,users_at_apply,applied_at) "
        "VALUES(?,?, 'approved','factory','k',0,0)", (bot_id, OWNER))
    await feed(cb_update(OWNER, f"b:{bot_id}:open") if False else cb_update(OWNER, f"b:{bot_id}:wh"))
    # button should exist on the card
    await feed(msg_update(OWNER, "🤖 Мои боты"))
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    texts = [(b.text, b.callback_data) for m in session.requests
             for kb in [getattr(m, "reply_markup", None)]
             for row in (getattr(kb, "inline_keyboard", None) or [])
             for b in row]
    assert any(data == f"b:{bot_id}:wh" or "Вебхук" in (text or "") for text, data in texts)
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:wh"))
    shown = "\n".join(session.texts())
    secret = ctx.unsub.ensure_secret(bot_id, "tgrass")
    assert f"https://bnbb.tech/webhooks/tgrass/{bot_id}/{secret}" in shown
    assert "отписк" in shown.lower()
    # stranger
    session.clear()
    await feed(cb_update(200, f"b:{bot_id}:wh"))
    shown = "\n".join(session.texts())
    assert secret not in shown
    # admin
    session.clear()
    await feed(cb_update(ADMIN, f"a:wh:{bot_id}"))
    shown = "\n".join(session.texts())
    assert secret in shown and f"/webhooks/tgrass/{bot_id}/" in shown
