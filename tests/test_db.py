import sqlite3

import pytest

from creator.db import DB, InsufficientFunds, now


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "c.db")
    yield d
    d.close()


def test_users_and_referrer(db):
    assert db.upsert_user(1, "a", "A") is True
    assert db.upsert_user(1, "a2", "A") is False
    assert db.upsert_user(2, "b", "B", referrer_id=1)
    assert db.upsert_user(3, "c", "C", referrer_id=999)  # несуществующий реферер игнорируется
    assert db.user(2)["referrer_id"] == 1
    assert db.user(3)["referrer_id"] is None
    assert db.referrals_count(1) == 1


def test_balance_and_history(db):
    db.upsert_user(1, None, "A")
    assert db.change_balance(1, 100, "topup") == 100
    with pytest.raises(InsufficientFunds):
        db.change_balance(1, -101, "x")
    assert db.change_balance(1, -40, "vip") == 60
    assert [r["amount"] for r in db.history(1)] == [-40, 100]


def test_referral_credit(db):
    db.upsert_user(1, None, "A")
    db.upsert_user(2, None, "B", 1)
    assert db.credit_referrer(2, 155, 10) == (1, 15)
    assert db.user(1)["balance"] == 15 and db.user(1)["ref_earned"] == 15
    assert db.credit_referrer(1, 100, 10) is None


def test_payment_idempotent(db):
    assert db.register_payment("ch1", 1, 10, "topup:10")
    assert not db.register_payment("ch1", 1, 10, "topup:10")


def test_draft_materialized_once(db):
    db.upsert_user(1, None, "A")
    d = db.create_draft(1, "clicker", "1:tok", "b", "B")
    bot_id = db.materialize_draft(d, 21)
    assert bot_id and db.bot(bot_id)["hosting_balance"] == 21
    assert db.materialize_draft(d, 21) is None
    assert db.token_in_use("1:tok")
    db.cleanup_drafts()
    assert db.draft(d)["token"] == ""


def test_hosting_charge(db):
    db.upsert_user(1, None, "A")
    b1 = db.materialize_draft(db.create_draft(1, "clicker", "1:a", "a", "A"), 3)
    b2 = db.materialize_draft(db.create_draft(1, "refbot", "1:b", "b", "B"), 2)
    db.update_bot(b1, next_charge_at=now() - 1)
    db.update_bot(b2, next_charge_at=now() - 1)
    res = {b["id"]: ok for b, ok in db.charge_due_bots(3)}
    assert res == {b1: True, b2: False}
    assert db.bot(b1)["hosting_balance"] == 0 and db.bot(b1)["next_charge_at"] > now()
    assert db.bot(b2)["desired"] == 0 and db.bot(b2)["status"] == "stopped"
    assert db.charge_due_bots(3) == []  # повторно не списывается


def test_resale(db):
    for u in (1, 2):
        db.upsert_user(u, None, str(u))
    b = db.materialize_draft(db.create_draft(1, "clicker", "1:a", "a", "A"), 0)
    db.update_bot(b, sale_price=100)
    assert [r["id"] for r in db.bots_for_sale(exclude_owner=2)] == [b]
    assert db.bots_for_sale(exclude_owner=1) == []
    with pytest.raises(InsufficientFunds):
        db.buy_bot(2, b, 5)
    db.change_balance(2, 150, "topup")
    assert db.buy_bot(2, b, 5) == (1, 100, 95)
    assert db.bot(b)["owner_id"] == 2 and db.bot(b)["sale_price"] is None
    assert db.user(1)["balance"] == 95 and db.user(2)["balance"] == 50
    with pytest.raises(LookupError):
        db.buy_bot(2, b, 5)


def test_stats(db):
    db.upsert_user(1, None, "A")
    s = db.stats()
    assert s["users_total"] == 1 and s["users_today"] == 1 and s["bots_total"] == 0


LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, referrer_id INTEGER,
    balance INTEGER NOT NULL DEFAULT 0, ref_earned INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL);
CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
    kind TEXT NOT NULL, comment TEXT, created_at INTEGER NOT NULL);
CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, template TEXT NOT NULL,
    token TEXT NOT NULL, username TEXT, title TEXT, status TEXT NOT NULL DEFAULT 'stopped',
    desired INTEGER NOT NULL DEFAULT 0, hosting_balance INTEGER NOT NULL DEFAULT 0, next_charge_at INTEGER,
    vip INTEGER NOT NULL DEFAULT 0, sale_price INTEGER, last_error TEXT, created_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
    payload TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE lave_invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT UNIQUE, user_id INTEGER NOT NULL,
    payload TEXT NOT NULL, stars INTEGER NOT NULL, rub INTEGER NOT NULL, usd TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new', pay_url TEXT, created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0, paid_at INTEGER);
CREATE INDEX lave_invoices_status ON lave_invoices(status);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE tgrass_ops (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, tg_user_id INTEGER NOT NULL,
    offer_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, reward_centi INTEGER NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE (bot_id, tg_user_id, offer_id));
CREATE TABLE tgrass_frac (user_id INTEGER PRIMARY KEY, centi INTEGER NOT NULL DEFAULT 0);
INSERT INTO users VALUES (1,'a','A',NULL,10,2,0,1), (2,'b','B',1,0,0,0,1);
INSERT INTO transactions(user_id,amount,kind,comment,created_at) VALUES (1,12,'topup','',1), (1,-2,'vip','',1);
INSERT INTO bots(owner_id,template,token,hosting_balance,sale_price,created_at) VALUES (1,'clicker','t',21,100,1);
INSERT INTO bots(owner_id,template,token,hosting_balance,created_at) VALUES (2,'refbot','t2',3,1);
INSERT INTO payments VALUES ('lave:x',1,100,'topup:100:150',1);
INSERT INTO lave_invoices(invoice_id,user_id,payload,stars,rub,usd,status,created_at) VALUES
    ('inv_a',1,'topup:100:150',100,150,'1.77','paid',1),
    ('inv_b',1,'host:1:90',90,135,'1.59','active',1),
    ('inv_c',1,'create:3',150,225,'2.65','active',1),
    ('inv_d',1,'topup:33:50',33,50,'0.59','active',1);
INSERT INTO kv VALUES ('created_at','1'), ('price:PRICE_CLICKER','0'), ('price:VIP_PRICE','120'),
    ('price:RUB_PRICE_CLICKER','300'), ('price:RUB_PER_100_STARS','150'), ('price:TGRASS_REWARD_CENTI','40'),
    ('price:REF_PERCENT','12');
INSERT INTO tgrass_ops(bot_id,tg_user_id,offer_id,owner_id,reward_centi,created_at) VALUES (1,5,6,1,35,1);
INSERT INTO tgrass_frac VALUES (1,35);
"""


def test_migrate_stars_to_rub(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.close()
    d = DB(path)
    assert d.kv_get("currency") == "rub"
    u1 = d.user(1)
    assert u1["balance"] == 10 * 150 + 53 and u1["ref_earned"] == 300  # +0,35 ⭐ из копилки TGrass = 53 коп.
    assert [r["amount"] for r in d.history(1)] == [53, -300, 1800]
    b1, b2 = d.bot(1), d.bot(2)
    assert b1["hosting_balance"] == 3150 and b1["sale_price"] == 15000 and b2["hosting_balance"] == 450
    assert d.scalar("SELECT amount FROM payments") == 15000
    inv = {r["invoice_id"]: (r["payload"], r["amount"]) for r in d.all("SELECT * FROM lave_invoices")}
    assert inv == {"inv_a": ("topup:100:150", 15000), "inv_b": ("host:1:13500", 13500),
                   "inv_c": ("create:3", 22500), "inv_d": ("topup:5000", 5000)}
    assert "rub" not in d._columns("lave_invoices") and "stars" not in d._columns("lave_invoices")
    assert d.scalar("SELECT reward_kop FROM tgrass_ops") == 53
    assert d.one("SELECT 1 FROM sqlite_master WHERE name='tgrass_frac'") is None
    kv = {r[0]: r[1] for r in d.all("SELECT key, value FROM kv WHERE key LIKE 'price:%'")}
    assert kv == {"price:PRICE_CLICKER": "0", "price:VIP_PRICE": "18000", "price:TGRASS_REWARD": "60",
                  "price:REF_PERCENT": "12"}
    # новые записи работают со схемой после миграции
    assert d.lave_new(1, "topup:5000", 5000, "0.59") > 0
    assert d.tgrass_credit(1, 7, 6, 50) and d.user(1)["balance"] == 1500 + 53 + 50
    d.close()
    # повторное открытие не мигрирует второй раз
    d = DB(path)
    assert d.user(1)["balance"] == 1500 + 53 + 50 and d.bot(1)["hosting_balance"] == 3150
    d.close()


def test_fresh_db_marked_rub(tmp_path):
    d = DB(tmp_path / "new.db")
    assert d.kv_get("currency") == "rub"
    assert "amount" in d._columns("lave_invoices") and "reward_kop" in d._columns("tgrass_ops")
    d.close()
