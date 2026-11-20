"""Owner-shop USDT TRC-20 payouts: encrypted keys, approval quote, no double pay."""
from __future__ import annotations

import json
import time

import pytest
from cryptography.fernet import Fernet

from child.db import ChildDB
from child.files import read_config, write_config, write_setting
from child.payouts import normalize_trc20, parse_idempotency_key, payouts_ready
from creator.child_payouts import ChildPayoutService
from creator.config import Config, load_config
from creator.db import DB
from creator.lavepay import LavePayError
from creator.owner_payouts import (
    child_flags,
    disconnect_shop,
    look_key,
    save_shop,
    set_rate,
    shop_plain_key,
)
from creator.secretbox import box_for, decrypt
from creator.services import Ctx
from creator.supervisor import CHILD_ENV_KEYS, Supervisor
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, make

pytest_plugins = ("tests.test_creator_flow",)

FERNET = Fernet.generate_key().decode()
OWNER_KEY = "LAVE-99-k8Vn3pQxxxxxxxxxxxxxxxxxxxx"
TRC20 = "T" + "1" * 33


class FakeOwnerLave:
    def __init__(self):
        self.calls = []
        self.chain = {}
        self.fail = None
        self.me_data = {
            "id": 99, "name": "OwnerShop", "status": "active", "payouts_enabled": True,
            "balance": "20.00", "balances": {"USD": "20.00"},
        }
        self.rate = {"available": True, "rate": "80"}
        self.quote = {"amount": "1.25", "fee": "4", "debit": "5.25", "enough": True, "balance": "20.00",
                      "currency": "USDT"}

    async def me(self):
        self.calls.append(("me",))
        if self.fail and self.fail.code == "unauthorized":
            raise self.fail
        return dict(self.me_data)

    async def card_rate(self, amount_rub=None):
        self.calls.append(("card_rate", amount_rub))
        return dict(self.rate)

    async def sbp_rate(self, amount_rub=None):
        return dict(self.rate)

    async def withdrawals_quote(self, asset, network, amount):
        self.calls.append(("quote", asset, network, amount))
        q = dict(self.quote)
        q["amount"] = amount
        try:
            from decimal import Decimal
            q["debit"] = str(Decimal(amount) + Decimal(q.get("fee") or "0"))
        except Exception:
            pass
        return q

    async def create_chain_withdrawal(self, asset, network, address, amount, idempotency_key, max_debit=None):
        self.calls.append(("chain", asset, network, amount, idempotency_key, address[-4:], max_debit))
        if self.fail:
            raise self.fail
        existing = next((v for v in self.chain.values() if v.get("idempotency_key") == idempotency_key), None)
        if existing:
            return {**existing, "replayed": True}
        pid = f"wd_{len(self.chain) + 1}"
        row = {"id": pid, "status": "processing", "amount": amount, "debit": max_debit or "5.25",
               "fee": "4", "idempotency_key": idempotency_key, "replayed": False, "asset": asset,
               "network": network, "address": address}
        self.chain[pid] = row
        self.chain[idempotency_key] = row
        return dict(row)

    async def get_chain_withdrawal(self, ident):
        if ident not in self.chain:
            raise LavePayError("not_found", "nope", 404)
        return dict(self.chain[ident])

    async def close(self):
        self.calls.append(("close",))


def _cfg(tmp_path, **kw):
    opts = dict(data_dir=tmp_path, admin_ids={777}, child_lavepay_payouts=True,
                child_lavepay_payouts_auto=False, owner_payout_key=FERNET)
    opts.update(kw)
    return Config(**opts)


def _ctx(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    db = DB(cfg.db_path)
    ctx = Ctx(db=db, cfg=cfg, sup=Supervisor(db, cfg, cmd=["true"]))
    notes = []

    async def notify(uid, text):
        notes.append((uid, text))

    payouts = ChildPayoutService(ctx, None, notify=notify)
    ctx.payouts = payouts
    return ctx, payouts, notes


def _insert_bot(db, template="refbot", bot_id=7, owner=OWNER):
    db.conn.execute(
        "INSERT INTO bots(id,owner_id,token,username,title,template,status,desired,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (bot_id, owner, "42:TEST", "kid_bot", "Kid", template, "running", 1, int(time.time())),
    )
    db.conn.commit()


def _bot_dir(tmp_path, db, template="refbot", bot_id=7, owner=OWNER):
    _insert_bot(db, template, bot_id, owner)
    path = tmp_path / "bots" / str(bot_id)
    path.mkdir(parents=True)
    write_config(path, {"token": "42:TEST", "template": template, "owner_id": owner, "username": "kid_bot",
                        "bot_id": bot_id, "vip": False, "creator_username": "creator_bot"})
    if template == "refbot":
        write_setting(path, "currency", "₽")
    return path


def _reserve(db, uid=10, amount=100, bot_id=7, status="awaiting_approval"):
    db.upsert_user(uid, None, "u", None)
    db.add_balance(uid, amount)
    dest = json.dumps({"address": TRC20})
    return db.create_withdrawal(
        uid, amount, "USDT TRC-20 T1…1111", currency="₽", method="usdt_trc20",
        payout={"bot_id": bot_id, "method": "usdt_trc20", "status": status,
                "destination": dest, "amount_rub": int(amount)},
    )


async def _connect(ctx, fake, bot_id=7, owner=OWNER):
    async def inspect(api_key, base_url):
        assert api_key == OWNER_KEY
        return await fake.me()

    from creator import owner_payouts as mod
    orig = mod.inspect_key
    mod.inspect_key = inspect
    try:
        return await save_shop(ctx.cfg, ctx.db, bot_id, owner, OWNER_KEY)
    finally:
        mod.inspect_key = orig


def test_flags_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CHILD_LAVEPAY_PAYOUTS", raising=False)
    monkeypatch.delenv("CHILD_LAVEPAY_PAYOUTS_AUTO", raising=False)
    monkeypatch.delenv("OWNER_PAYOUT_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.child_lavepay_payouts is False
    assert cfg.child_lavepay_payouts_auto is False
    assert cfg.owner_payout_key == ""


def test_child_env_has_no_lave_secrets():
    assert "LAVEPAY_API_KEY" not in CHILD_ENV_KEYS
    assert "OWNER_PAYOUT_KEY" not in CHILD_ENV_KEYS
    assert "LAVEPAY_WEBHOOK_SECRET" not in CHILD_ENV_KEYS
    assert "MGRAM_PARTNER_API_KEY" not in CHILD_ENV_KEYS


def test_look_key_shape():
    assert look_key("  LAVE-99-k8Vn3pQxxxxxxxxxxxxxxxxxxxx  ") == OWNER_KEY
    with pytest.raises(ValueError):
        look_key("not-a-key")
    assert parse_idempotency_key("cwd-7-3") == (7, 3)
    assert normalize_trc20(TRC20) == TRC20


@pytest.mark.asyncio
async def test_key_encrypted_and_absent_from_child_config(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path)
    _bot_dir(tmp_path, ctx.db)
    fake = FakeOwnerLave()
    await _connect(ctx, fake)
    row = ctx.db.owner_lavepay_shop(7)
    assert row is not None
    raw = bytes(row["key_blob"])
    assert OWNER_KEY.encode() not in raw
    assert OWNER_KEY not in json.dumps(ctx.db.owner_lavepay_public(7))
    assert shop_plain_key(ctx.cfg, ctx.db, 7) == OWNER_KEY
    fernet = box_for(ctx.cfg.data_dir, FERNET)
    assert decrypt(fernet, raw) == OWNER_KEY
    flags = child_flags(ctx.cfg, ctx.db, 7)
    assert flags["lavepay_payouts_enabled"] is True
    apply = ctx.sup.write_files(7)
    data = read_config(apply)
    dumped = json.dumps(data)
    assert data["lavepay_payouts_enabled"] is True
    assert OWNER_KEY not in dumped
    assert "LAVE-99" not in dumped
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_transfer_and_delete_drop_the_key(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path)
    _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    assert ctx.db.owner_lavepay_shop(7) is not None
    ctx.db.conn.execute("INSERT INTO users(id,created_at) VALUES(200,0)")
    ctx.db.conn.commit()
    ctx.db.transfer_bot(7, 200)
    assert ctx.db.owner_lavepay_shop(7) is None
    _insert_bot(ctx.db, bot_id=8)
    path = tmp_path / "bots" / "8"
    path.mkdir()
    write_config(path, {"token": "x", "template": "refbot", "owner_id": OWNER, "bot_id": 8})
    await _connect(ctx, FakeOwnerLave(), bot_id=8)
    ctx.db.delete_bot(8)
    assert ctx.db.owner_lavepay_shop(8) is None
    payouts.close()
    ctx.db.close()


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
    assert "payout_jobs" in {r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
    db.upsert_user(10, None, "u", None)
    db.add_balance(10, 20)
    wid = db.create_withdrawal(10, 5, "card 1111", currency="₽")
    assert db.resolve_withdrawal(wid, True)
    assert db.withdrawal(wid)["status"] == "paid"
    assert db.payout_job(wid) is None
    db.conn.close()


@pytest.mark.asyncio
async def test_clicker_stays_off_until_rate_is_set(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path)
    path = _bot_dir(tmp_path, ctx.db, template="clicker")
    write_setting(path, "currency", "🪙")
    await _connect(ctx, FakeOwnerLave())
    flags = child_flags(ctx.cfg, ctx.db, 7)
    assert flags["lavepay_payouts_enabled"] is False
    assert set_rate(ctx.cfg, ctx.db, 7, OWNER, "usdt", "0.001")
    flags = child_flags(ctx.cfg, ctx.db, 7)
    assert flags["lavepay_payouts_enabled"] is True
    assert flags["lavepay_unit_kind"] == "usdt"
    disconnect_shop(ctx.cfg, ctx.db, 7)
    assert child_flags(ctx.cfg, ctx.db, 7)["lavepay_payouts_enabled"] is False
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_shop_without_payouts_switch_does_not_enable(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path)
    _bot_dir(tmp_path, ctx.db)
    fake = FakeOwnerLave()
    fake.me_data["payouts_enabled"] = False
    public = await _connect(ctx, fake)
    assert public["payouts_enabled"] is False
    assert child_flags(ctx.cfg, ctx.db, 7)["lavepay_payouts_enabled"] is False
    payouts.close()
    ctx.db.close()


def test_global_flag_off_disables_child_path(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path, child_lavepay_payouts=False)
    _bot_dir(tmp_path, ctx.db)
    from creator.secretbox import encrypt
    blob = encrypt(box_for(ctx.cfg.data_dir, FERNET), OWNER_KEY)
    ctx.db.upsert_owner_lavepay_shop(
        7, OWNER, blob, merchant_id=99, merchant_name="S", status="active",
        payouts_enabled=True, usdt_balance="10",
    )
    ctx.db.set_owner_lavepay_rate(7, OWNER, "rub", "1")
    assert child_flags(ctx.cfg, ctx.db, 7)["lavepay_payouts_enabled"] is False
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_quote_and_send_from_owner_shop(tmp_path, monkeypatch):
    ctx, payouts, notes = _ctx(tmp_path)
    path = _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    child = ChildDB(path / "bot.db")
    wid = _reserve(child)
    child.conn.close()

    fake = FakeOwnerLave()

    async def fake_client(cfg, db, bot_id):
        assert shop_plain_key(cfg, db, bot_id) == OWNER_KEY
        return fake

    monkeypatch.setattr("creator.child_payouts.owner_client", fake_client)
    quote = await payouts.quote_withdrawal(7, wid)
    assert quote["amount"] == "1.25"  # 100 / 80
    assert Decimal_like(quote["debit"])
    status = await payouts.confirm_and_queue(7, wid, quote)
    assert status == "submitted"
    job = payouts._db(7).payout_job(wid)
    assert job["idempotency_key"] == f"cwd-7-{wid}"
    assert any(c[0] == "chain" and c[4] == job["idempotency_key"] for c in fake.calls)
    fake.chain[job["idempotency_key"]]["status"] = "completed"
    await payouts.poll_once()
    assert payouts._db(7).withdrawal(wid)["status"] == "paid"
    assert payouts._db(7).user(10)["balance"] == 0
    payouts.close()
    ctx.db.close()


def Decimal_like(value):
    from decimal import Decimal
    Decimal(str(value))
    return True


@pytest.mark.asyncio
async def test_insufficient_funds_stays_pending(tmp_path, monkeypatch):
    ctx, payouts, notes = _ctx(tmp_path)
    path = _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    child = ChildDB(path / "bot.db")
    wid = _reserve(child)
    child.conn.close()
    fake = FakeOwnerLave()
    fake.fail = LavePayError("insufficient_funds", "Недостаточно USDT", 409)

    async def fake_client(cfg, db, bot_id):
        return fake

    monkeypatch.setattr("creator.child_payouts.owner_client", fake_client)
    quote = dict(fake.quote)
    quote["amount"] = "1.25"
    status = await payouts.confirm_and_queue(7, wid, quote)
    assert status in {"queued", "pending"}
    job = payouts._db(7).payout_job(wid)
    assert job["status"] == "awaiting_approval"
    assert payouts._db(7).withdrawal(wid)["status"] == "pending"
    assert payouts._db(7).user(10)["balance"] == 0
    assert any("не хватает USDT" in t for _, t in notes)
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_validation_error_refunds_once(tmp_path, monkeypatch):
    ctx, payouts, notes = _ctx(tmp_path)
    path = _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    child = ChildDB(path / "bot.db")
    wid = _reserve(child)
    child.conn.close()
    fake = FakeOwnerLave()
    fake.fail = LavePayError("validation_error", "bad address", 422)

    async def fake_client(cfg, db, bot_id):
        return fake

    monkeypatch.setattr("creator.child_payouts.owner_client", fake_client)
    quote = {"amount": "1.25", "fee": "4", "debit": "5.25", "enough": True}
    await payouts.confirm_and_queue(7, wid, quote)
    assert payouts._db(7).withdrawal(wid)["status"] == "failed"
    assert payouts._db(7).user(10)["balance"] == 100
    await payouts.confirm_and_queue(7, wid, quote) if False else None
    assert payouts._db(7).apply_payout_outcome(wid, ok=False, error="again") in {None, "failed"}
    assert payouts._db(7).user(10)["balance"] == 100
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_retry_reuses_idempotency_key(tmp_path, monkeypatch):
    ctx, payouts, notes = _ctx(tmp_path)
    path = _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    child = ChildDB(path / "bot.db")
    wid = _reserve(child)
    child.conn.close()
    fake = FakeOwnerLave()

    async def fake_client(cfg, db, bot_id):
        return fake

    monkeypatch.setattr("creator.child_payouts.owner_client", fake_client)
    quote = await payouts.quote_withdrawal(7, wid)
    await payouts.confirm_and_queue(7, wid, quote)
    payouts._db(7).exec(
        "UPDATE payout_jobs SET status='queued',provider_id=NULL WHERE withdrawal_id=?", wid
    )
    await payouts.poll_once()
    chains = [c for c in fake.calls if c[0] == "chain"]
    assert len(chains) == 2
    assert chains[0][4] == chains[1][4] == f"cwd-7-{wid}"
    ids = {v["id"] for v in fake.chain.values() if "id" in v}
    assert ids == {"wd_1"}
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_child_flow_collects_trc20_and_waits_for_constructor(tmp_path, bot, session):
    write_config(tmp_path, {"token": "42:TEST", "template": "refbot", "owner_id": OWNER, "username": "kid_bot",
                            "vip": False, "creator_username": "creator_bot", "bot_id": 7,
                            "lavepay_payouts_enabled": True, "lavepay_unit_kind": "rub", "lavepay_unit_rate": "1"})
    from child.__main__ import build_dispatcher
    from child.core import ChildApp
    from child.payout_flow import constructor_link, owner_resolve
    app = ChildApp(tmp_path)
    dp = build_dispatcher(app)

    async def feed(update):
        await dp.feed_update(bot, update)

    assert payouts_ready(app)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 1000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "100"))
    last = session.texts()[-1]
    assert "TRC-20" in last and "карту" not in last.lower()
    await feed(msg_update(A, TRC20))
    rows = app.db.all("SELECT * FROM withdrawals")
    assert len(rows) == 1 and rows[0]["method"] == "usdt_trc20"
    job = app.db.payout_job(rows[0]["id"])
    assert job["status"] == "awaiting_approval"
    action, w = owner_resolve(app, rows[0]["id"], True)
    assert action == "confirm_in_creator"
    assert constructor_link(app, w["id"]).endswith(f"lpayout_7_{w['id']}")
    action, _ = owner_resolve(app, rows[0]["id"], False)
    assert action == "manual_rejected"
    assert app.db.user(A)["balance"] == 1000


@pytest.mark.asyncio
async def test_clicker_without_flag_stays_manual(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    write_config(tmp_path, {**read_config(tmp_path), "bot_id": 7, "lavepay_payouts_enabled": False})
    app.cfg = read_config(tmp_path)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 20000)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "10000"))
    last = session.texts()[-1]
    assert "Укажите" in last or "реквизит" in last.lower()
    assert "LavePay" not in last


def test_starbot_has_no_lavepay_settings_or_card_button():
    from child import templates
    from creator.keyboards import bot_card_kb
    keys = {s.key for s in templates.info("starbot").settings}
    assert "lavepay_payouts" not in keys
    kb = bot_card_kb({"id": 1, "template": "starbot", "desired": 1, "status": "running", "sale_price": None}, False)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "💳 LavePay выплаты" not in texts
    kb2 = bot_card_kb({"id": 2, "template": "refbot", "desired": 1, "status": "running", "sale_price": None}, False)
    texts2 = [b.text for row in kb2.inline_keyboard for b in row]
    assert "💳 LavePay выплаты" in texts2
    kb3 = bot_card_kb({"id": 3, "template": "microtasks", "desired": 1, "status": "running", "sale_price": None}, False)
    texts3 = [b.text for row in kb3.inline_keyboard for b in row]
    assert "💳 LavePay выплаты" in texts3


@pytest.mark.asyncio
async def test_constructor_connects_key_and_deletes_message(env, monkeypatch):
    from aiogram.methods import DeleteMessage, SendMessage

    from tests.test_creator_flow import OWNER, create_paid_bot

    ctx, feed, session = env
    ctx.cfg.child_lavepay_payouts = True
    ctx.cfg.owner_payout_key = FERNET
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.requests.clear()

    async def inspect(api_key, base_url):
        assert api_key == OWNER_KEY
        return {
            "id": 99, "name": "OwnerShop", "status": "active", "payouts_enabled": True,
            "balance": "20.00", "balances": {"USD": "20.00"},
        }

    monkeypatch.setattr("creator.owner_payouts.inspect_key", inspect)
    await feed(cb_update(OWNER, f"olp:{bot_id}"))
    await feed(cb_update(OWNER, f"olp:{bot_id}:key"))
    await feed(msg_update(OWNER, f"  {OWNER_KEY}  "))
    assert any(isinstance(m, DeleteMessage) for m in session.requests)
    replies = "\n".join(m.text or "" for m in session.of(SendMessage))
    assert OWNER_KEY not in replies
    assert "OwnerShop" in replies
    public = ctx.db.owner_lavepay_public(bot_id)
    assert public["merchant_name"] == "OwnerShop"
    assert shop_plain_key(ctx.cfg, ctx.db, bot_id) == OWNER_KEY
    session.requests.clear()
    await feed(cb_update(OWNER, f"olp:{bot_id}:rate"))
    await feed(msg_update(OWNER, "usdt 0.001"))
    assert ctx.db.owner_lavepay_public(bot_id)["unit_kind"] == "usdt"
    assert child_flags(ctx.cfg, ctx.db, bot_id)["lavepay_payouts_enabled"] is True
    dumped = "\n".join(m.text or "" for m in session.of(SendMessage))
    assert OWNER_KEY not in dumped


@pytest.mark.asyncio
async def test_buy_bot_drops_previous_owner_shop(tmp_path):
    ctx, payouts, notes = _ctx(tmp_path)
    _bot_dir(tmp_path, ctx.db)
    await _connect(ctx, FakeOwnerLave())
    assert ctx.db.owner_lavepay_shop(7) is not None
    ctx.db.conn.execute("INSERT INTO users(id,created_at,balance) VALUES(1,0,0)")
    ctx.db.conn.execute("INSERT INTO users(id,created_at,balance) VALUES(200,0,5000)")
    ctx.db.conn.execute("UPDATE bots SET sale_price=100 WHERE id=7")
    ctx.db.conn.commit()
    ctx.db.buy_bot(200, 7, 0)
    assert ctx.db.owner_lavepay_shop(7) is None
    assert ctx.db.bot(7)["owner_id"] == 200
    payouts.close()
    ctx.db.close()


@pytest.mark.asyncio
async def test_owner_payout_instructions_hide_signup_link(env):
    from aiogram.methods import EditMessageText, SendMessage

    from creator.lavepay_signup import LAVEPAY_SIGNUP_URL
    from tests.test_creator_flow import OWNER, create_paid_bot

    def hidden(text: str, *labels: str) -> None:
        assert text
        for label in labels:
            assert f'<a href="{LAVEPAY_SIGNUP_URL}">{label}</a>' in text
        assert LAVEPAY_SIGNUP_URL not in text.replace(f'href="{LAVEPAY_SIGNUP_URL}"', "")

    ctx, feed, session = env
    ctx.cfg.child_lavepay_payouts = True
    ctx.cfg.owner_payout_key = FERNET
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.requests.clear()
    await feed(cb_update(OWNER, f"olp:{bot_id}"))
    card = [m for m in session.requests if isinstance(m, (SendMessage, EditMessageText))
            and "LavePay выплаты" in (m.text or "")][-1]
    hidden(card.text, "LavePay", "Laveshka")
    assert card.disable_web_page_preview is True
    session.requests.clear()
    await feed(cb_update(OWNER, f"olp:{bot_id}:key"))
    prompt = session.of(SendMessage)[-1]
    hidden(prompt.text, "Laveshka", "зарегистрируйтесь")
    assert prompt.disable_web_page_preview is True


def test_bots_default_parse_mode_is_html():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("creator/__main__.py", "child/__main__.py", "child/worker.py"):
        text = (root / rel).read_text()
        assert "DefaultBotProperties(parse_mode=ParseMode.HTML)" in text
