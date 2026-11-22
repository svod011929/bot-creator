"""Creator withdrawals of earned money: approved rules, money edge cases, concurrency, privacy."""
import threading

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from creator import withdrawals as wd
from creator.config import Config
from creator.db import DB
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, env  # noqa: F401

CARD = "4111 1111 1111 1111"
TRC = "T" + "X" * 29 + "RTv8"  # synthetic, format-valid sample


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "c.db")
    yield d
    d.close()


def user(d, uid=1):
    if d.user(uid) is None:
        d.conn.execute("INSERT INTO users(id,username,full_name,created_at) VALUES(?,?,?,0)", (uid, "u", "U"))
    return uid


# ---------- formula ----------
def test_formula_earned_only_spending_first(db):
    uid = user(db)
    db.change_balance(uid, 50000, "topup")          # deposit: never withdrawable
    db.change_balance(uid, 30000, "tgrass")         # earned
    db.change_balance(uid, 10000, "piarflow")       # earned
    assert wd.available(db, uid).available == 40000
    db.change_balance(uid, -15000, "create")        # spending eats earned first
    assert wd.available(db, uid).available == 25000
    db.change_balance(uid, 20000, "admin")          # positive admin: not earned
    assert wd.available(db, uid).available == 25000
    db.change_balance(uid, -5000, "admin", allow_negative=True)  # negative admin: spending
    assert wd.available(db, uid).available == 20000
    db.change_balance(uid, 99999, "refund")         # refunds ignored
    assert wd.available(db, uid).available == 20000
    a = wd.available(db, uid)
    assert (a.earned, a.spent, a.withdrawn) == (40000, 20000, 0)


def test_formula_floor_and_cap(db):
    uid = user(db)
    db.change_balance(uid, 10000, "ref")
    db.change_balance(uid, -30000, "hosting", allow_negative=True)
    assert wd.available(db, uid).available == 0          # never negative
    uid2 = user(db, 2)
    db.change_balance(uid2, 50000, "resale_sell")
    db.conn.execute("UPDATE users SET balance=12000 WHERE id=2")  # balance lower than formula -> capped
    assert wd.available(db, uid2).available == 12000


def test_fee_and_validation():
    assert wd.fee_split(10000, 5) == (500, 9500)
    assert wd.fee_split(10001, 5) == (501, 9500)        # fee rounded up to a kopeck
    assert wd.fee_split(10000, 0) == (0, 10000)
    assert wd.parse_amount("150,50") == 15050 and wd.parse_amount("200") == 20000
    for bad in ("", "abc", "-5", "0", "1e3", "inf", "10.555"):
        assert wd.parse_amount(bad) is None
    assert wd.validate("card", CARD) == "4111111111111111"
    assert wd.validate("card", "4111 1111 1111 1112") is None
    assert wd.validate("sbp", "+7 (999) 123-45-67 Т-Банк") == "+79991234567 Т-Банк"
    assert wd.validate("sbp", "89991234567 Сбер") == "+79991234567 Сбер"
    assert wd.validate("sbp", "+79991234567") is None
    assert wd.validate("usdt_trc20", TRC) == TRC
    assert wd.validate("usdt_trc20", "T0" + "a" * 32) is None
    assert wd.mask("4111111111111111") == "••••1111" and wd.mask("+79991234567 Т-Банк") == "••••4567"
    assert wd.mask(TRC) == "••••RTv8"


# ---------- requests ----------
def make(db, uid=1, earned=50000):
    user(db, uid)
    db.change_balance(uid, earned, "tgrass")
    return uid


def test_create_and_resolve(db):
    uid = make(db)
    r = wd.create_request(db, uid, 20000, "card", "4111111111111111", fee_percent=5, minimum=10000)
    assert (r["amount"], r["fee"], r["net"], r["status"]) == (20000, 1000, 19000, "pending")
    assert db.user(uid)["balance"] == 30000
    assert db.scalar("SELECT amount FROM transactions WHERE kind='withdraw'") == -20000
    with pytest.raises(wd.WithdrawError, match="pending"):
        wd.create_request(db, uid, 10000, "card", "4111111111111111", fee_percent=5, minimum=10000)
    paid = wd.resolve(db, r["id"], approve=True, by=ADMIN)
    assert paid["status"] == "paid" and paid["details"] == "••••1111"
    assert wd.resolve(db, r["id"], approve=False, by=ADMIN) is None   # idempotent
    assert db.user(uid)["balance"] == 30000
    assert wd.available(db, uid).available == 30000
    r2 = wd.create_request(db, uid, 30000, "usdt_trc20", TRC, fee_percent=5, minimum=10000)
    rej = wd.resolve(db, r2["id"], approve=False, by=ADMIN, reason="Неверный адрес")
    assert rej["status"] == "rejected" and rej["reason"] == "Неверный адрес" and rej["details"] == "••••RTv8"
    assert db.user(uid)["balance"] == 30000
    assert db.scalar("SELECT amount FROM transactions WHERE kind='withdraw_refund'") == 30000
    assert wd.available(db, uid).available == 30000


def test_limits(db):
    uid = make(db, earned=15000)
    with pytest.raises(wd.WithdrawError, match="minimum"):
        wd.create_request(db, uid, 9999, "card", "4111111111111111", fee_percent=5, minimum=10000)
    with pytest.raises(wd.WithdrawError, match="available"):
        wd.create_request(db, uid, 15001, "card", "4111111111111111", fee_percent=5, minimum=10000)
    with pytest.raises(wd.WithdrawError, match="fee"):
        wd.create_request(db, uid, 10000, "card", "4111111111111111", fee_percent=100, minimum=10000)
    topup_only = user(db, 3)
    db.change_balance(topup_only, 100000, "topup")
    with pytest.raises(wd.WithdrawError, match="available"):
        wd.create_request(db, topup_only, 10000, "card", "4111111111111111", fee_percent=5, minimum=10000)


def test_concurrent_create_and_resolve(db):
    uid = make(db, earned=100000)
    results = []

    def attempt():
        try:
            results.append(wd.create_request(db, uid, 20000, "card", "4111111111111111", fee_percent=5, minimum=10000))
        except wd.WithdrawError as e:
            results.append(e)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    created = [r for r in results if not isinstance(r, Exception)]
    assert len(created) == 1 and db.user(uid)["balance"] == 80000
    outcomes = []
    threads = [threading.Thread(target=lambda a=a: outcomes.append(wd.resolve(db, created[0]["id"], approve=a, by=ADMIN)))
               for a in (True, False) * 4]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(o is not None for o in outcomes) == 1
    assert db.user(uid)["balance"] in (80000, 100000)
    assert db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='withdraw_refund'") in (0, 1)


def test_approver_resolution():
    assert Config(admin_ids={5}).approver == 5
    assert Config(admin_ids={5, 6}).approver is None
    assert Config(admin_ids={5, 6}, withdraw_approver_id=6).approver == 6


# ---------- UI ----------
def kb(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


def to(session, uid):
    return [m for m in session.of(SendMessage) if int(m.chat_id) == uid]


async def wizard(ctx, feed, session, amount="200"):
    await feed(cb_update(OWNER, "cab:wd"))
    await feed(cb_update(OWNER, "cab:wdm:card"))
    await feed(msg_update(OWNER, CARD))
    await feed(msg_update(OWNER, amount))


async def test_full_flow_paid(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(ADMIN, "/start"))
    ctx.db.change_balance(OWNER, 50000, "tgrass")
    ctx.db.change_balance(OWNER, 70000, "topup")
    await feed(msg_update(OWNER, "💼 Мой кабинет"))
    assert ("💸 Вывести заработанное", "cab:wd") in kb(session.of(SendMessage)[-1])
    await feed(cb_update(OWNER, "cab:wd"))
    t = session.of(EditMessageText)[-1].text
    assert "Доступно к выводу: <b>500 ₽</b>" in t and "Комиссия 5%" in t and "от 100 ₽" in t
    await feed(cb_update(OWNER, "cab:wdm:card"))
    await feed(msg_update(OWNER, "4111 1111 1111 1112"))
    assert "Luhn" in to(session, OWNER)[-1].text or "номер карты" in to(session, OWNER)[-1].text
    await feed(msg_update(OWNER, CARD))
    await feed(msg_update(OWNER, "50"))
    assert "от 100 ₽ до 500 ₽" in to(session, OWNER)[-1].text
    await feed(msg_update(OWNER, "200"))
    p = to(session, OWNER)[-1]
    assert "Сумма: 200 ₽" in p.text and "Комиссия (5%): 10 ₽" in p.text and "К получению: <b>190 ₽</b>" in p.text
    assert ctx.db.scalar("SELECT COUNT(*) FROM withdrawal_requests") == 0
    await feed(cb_update(OWNER, "cab:wdok"))
    assert ctx.db.user(OWNER)["balance"] == 100000
    note = to(session, ADMIN)[-1]
    assert "4111111111111111" in note.text and "К выплате: <b>190 ₽</b>" in note.text
    assert ("✅ Отметить выплаченной", "wdr:ok:1") in kb(note) and ("❌ Отклонить", "wdr:no:1") in kb(note)
    await feed(cb_update(OWNER, "cab:wdok"))  # stale confirm
    assert ctx.db.scalar("SELECT COUNT(*) FROM withdrawal_requests") == 1
    await feed(cb_update(OWNER, "cab:wd"))
    assert "уже есть заявка №1" in session.of(EditMessageText)[-1].text
    await feed(cb_update(OWNER, "cab:wdl:0"))
    t = session.of(EditMessageText)[-1].text
    assert "••••1111" in t and "4111111111111111" not in t
    n = ctx.db.scalar("SELECT status FROM withdrawal_requests")
    await feed(cb_update(OWNER, "wdr:ok:1"))  # requester is not the approver
    assert ctx.db.scalar("SELECT status FROM withdrawal_requests") == n == "pending"
    await feed(cb_update(ADMIN, "wdr:ok:1"))
    assert ctx.db.scalar("SELECT status FROM withdrawal_requests") == "paid"
    msg = to(session, OWNER)[-1].text
    assert "отмечена выплаченной" in msg and "190 ₽" in msg
    await feed(cb_update(ADMIN, "wdr:ok:1"))
    assert "уже обработана" in session.of(AnswerCallbackQuery)[-1].text
    assert ctx.db.scalar("SELECT details FROM withdrawal_requests") == "••••1111"
    await feed(cb_update(OWNER, "cab:h:withdraw:0"))
    assert "Вывод" in session.of(EditMessageText)[-1].text


async def test_reject_with_reason_and_admin_list(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(ADMIN, "/start"))
    ctx.db.change_balance(OWNER, 50000, "ref")
    await wizard(ctx, feed, session)
    await feed(cb_update(OWNER, "cab:wdok"))
    await feed(msg_update(ADMIN, "/admin"))
    assert ("💸 Заявки на вывод", "a:wd:0") in kb(session.of(SendMessage)[-1])
    await feed(cb_update(ADMIN, "a:wd:0"))
    lst = session.of(EditMessageText)[-1]
    assert "••••1111" in lst.text and "4111111111111111" not in lst.text
    await feed(cb_update(ADMIN, "wdr:d:1"))
    assert "4111111111111111" in session.of(EditMessageText)[-1].text  # approver sees full pending details
    await feed(cb_update(ADMIN, "wdr:no:1"))
    assert ("Без причины", "wdr:nr:1") in kb(to(session, ADMIN)[-1])
    await feed(msg_update(ADMIN, "Неверные <реквизиты>"))
    r = ctx.db.one("SELECT * FROM withdrawal_requests")
    assert r["status"] == "rejected" and r["reason"] == "Неверные <реквизиты>" and r["details"] == "••••1111"
    assert ctx.db.user(OWNER)["balance"] == 50000
    msg = to(session, OWNER)[-1].text
    assert "отклонена" in msg and "200 ₽ возвращены на баланс" in msg and "&lt;реквизиты&gt;" in msg
    await feed(cb_update(ADMIN, "wdr:nr:1"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM transactions WHERE kind='withdraw_refund'") == 1


async def test_reject_without_reason(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    ctx.db.change_balance(OWNER, 50000, "piarflow")
    await wizard(ctx, feed, session)
    await feed(cb_update(OWNER, "cab:wdok"))
    await feed(cb_update(ADMIN, "wdr:no:1"))
    await feed(cb_update(ADMIN, "wdr:nr:1"))
    assert ctx.db.scalar("SELECT status FROM withdrawal_requests") == "rejected"
    assert ctx.db.user(OWNER)["balance"] == 50000


async def test_nothing_to_withdraw_and_cancel(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    ctx.db.change_balance(OWNER, 50000, "topup")
    await feed(cb_update(OWNER, "cab:wd"))
    t = session.of(EditMessageText)[-1].text
    methods = [d for _, d in kb(session.of(EditMessageText)[-1]) if d.startswith("cab:wdm:")]
    assert "Доступно к выводу: <b>0 ₽</b>" in t and not methods
    ctx.db.change_balance(OWNER, 20000, "tgrass")
    await wizard(ctx, feed, session)
    await feed(cb_update(OWNER, "cab:wdno"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM withdrawal_requests") == 0
    assert ctx.db.user(OWNER)["balance"] == 70000


async def test_unconfigured_approver(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.admin_ids = {ADMIN, 778}
    await feed(msg_update(OWNER, "/start"))
    ctx.db.change_balance(OWNER, 50000, "tgrass")
    await wizard(ctx, feed, session)
    await feed(cb_update(OWNER, "cab:wdok"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM withdrawal_requests") == 1
    assert not to(session, ADMIN) and not to(session, 778)
    await feed(cb_update(ADMIN, "wdr:ok:1"))
    assert ctx.db.scalar("SELECT status FROM withdrawal_requests") == "pending"
