"""LavePay: счета, проверка, опрос, вебхук с подписью — на замоканном API."""
import json
import time
from decimal import Decimal

import pytest
from aiogram.methods import SendMessage
from aiohttp.test_utils import TestClient, TestServer

from creator.lavepay import LavePayError, LavePayService, rub_to_usd, sign, verify_signature
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import OWNER, REFERRER, TOKEN, env  # noqa: F401

SECRET = "whsec_test"


class FakeLave:
    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail = False

    async def create_invoice(self, amount, *, description, order_id, payload="", ttl=3600):
        self.calls.append(("create", amount, order_id))
        if self.fail:
            raise LavePayError("provider_not_configured", "off", 503)
        assert isinstance(amount, str) and Decimal(amount) >= Decimal("0.10")
        inv_id = f"inv_{len(self.invoices) + 1}"
        est = str(Decimal(amount) + Decimal("0.01"))  # как у реального API: amount — оценка, face_amount — названная
        self.invoices[inv_id] = {"id": inv_id, "status": "active", "amount": est, "face_amount": amount,
                                 "order_id": order_id,
                                 "payload": payload, "description": description, "currency": "USD",
                                 "pay_url": f"https://t.me/laveshka?startapp=lpay_{inv_id}",
                                 "expires_at": "2099-01-01T00:00:00+00:00"}
        return dict(self.invoices[inv_id])

    async def get_invoice(self, inv_id):
        self.calls.append(("get", inv_id))
        if inv_id not in self.invoices:
            raise LavePayError("not_found", "nope", 404)
        return dict(self.invoices[inv_id])

    def pay(self, inv_id, **over):
        self.invoices[inv_id].update({"status": "paid", "paid_via": "balance"}, **over)


@pytest.fixture
async def lenv(env):  # noqa: F811
    ctx, feed, session = env
    fake = FakeLave()
    sent: list[tuple[int, str]] = []

    async def notify(uid, text):
        sent.append((uid, text))

    ctx.lave = LavePayService(ctx, fake, notify=notify, webhook_secret=SECRET)
    await feed(msg_update(REFERRER, "/start"))
    await feed(msg_update(OWNER, f"/start ref{REFERRER}"))
    return ctx, feed, session, fake, sent


def last_invoice_msg(session):
    for m in reversed(session.of(SendMessage)):
        if "Счёт LavePay" in (m.text or ""):
            return m
    raise AssertionError("нет счёта")


def test_helpers():
    assert rub_to_usd(22500, 85) == "2.65"  # 2.647 -> вверх до цента
    assert rub_to_usd(100, 85) == "0.10"    # минимум счёта
    assert rub_to_usd(450, 85) == "0.10"
    assert rub_to_usd(3200, 85) == "0.38"
    body = b'{"a":1}'
    ts = str(int(time.time()))
    assert verify_signature(SECRET, ts, body, sign(SECRET, ts, body))
    assert not verify_signature(SECRET, ts, body + b" ", sign(SECRET, ts, body))
    assert not verify_signature("other", ts, body, sign(SECRET, ts, body))
    old = str(int(time.time()) - 301)
    assert not verify_signature(SECRET, old, body, sign(SECRET, old, body))


async def test_topup_check_idempotent_and_referral(lenv):
    ctx, feed, session, fake, sent = lenv
    await feed(cb_update(OWNER, "cab:topup"))
    from aiogram.methods import EditMessageText
    topup_kb = session.of(EditMessageText)[-1].reply_markup.inline_keyboard
    assert any(b.callback_data == "lp:t:100" for row in topup_kb for b in row)
    assert not any("⭐" in b.text for row in topup_kb for b in row)
    await feed(cb_update(OWNER, "lp:t:150"))
    msg = last_invoice_msg(session)
    row = ctx.db.lave_get(1)
    assert row["payload"] == "topup:15000" and row["amount"] == 15000 and row["usd"] == "1.77"
    assert "150 ₽" in msg.text and "⭐" not in msg.text
    assert msg.reply_markup.inline_keyboard[0][0].url.startswith("https://t.me/laveshka")
    assert msg.reply_markup.inline_keyboard[1][0].callback_data == "lpc:1"

    await feed(cb_update(OWNER, "lpc:1"))  # ещё не оплачен
    assert ctx.db.user(OWNER)["balance"] == 0
    fake.pay("inv_1")
    await feed(cb_update(OWNER, "lpc:1"))
    assert ctx.db.user(OWNER)["balance"] == 15000
    assert ctx.db.user(REFERRER)["balance"] == 1500  # 10% партнёру
    # повторная проверка, опрос и вебхук не зачисляют второй раз
    await feed(cb_update(OWNER, "lpc:1"))
    await ctx.lave.poll_once()
    await ctx.lave.apply(ctx.db.lave_get(1), fake.invoices["inv_1"])
    assert ctx.db.user(OWNER)["balance"] == 15000 and ctx.db.user(REFERRER)["balance"] == 1500
    assert ctx.db.scalar("SELECT amount FROM payments WHERE charge_id='lave:inv_1'") == 15000
    assert any("Баланс пополнен на 150 ₽" in t for _, t in sent)
    assert any("+15 ₽" in t for uid, t in sent if uid == REFERRER)


async def test_forged_topup_rejected(lenv):
    ctx, feed, session, fake, sent = lenv
    # подделанный payload: неверный формат / сумма вне лимитов
    assert ctx.lave.quote(OWNER, "topup:100000:50") is None
    assert ctx.lave.quote(OWNER, "topup:100") is None
    assert ctx.lave.quote(OWNER, "topup:5000") == 5000
    await feed(cb_update(OWNER, "lp:t:10"))  # меньше минимума
    await feed(cb_update(OWNER, "lp:h:999:7"))  # чужой/несуществующий бот
    assert not fake.calls


async def test_create_bot_via_lavepay_by_polling(lenv):
    ctx, feed, session, fake, sent = lenv
    await feed(cb_update(OWNER, "tpl:clicker"))
    await feed(msg_update(OWNER, TOKEN))
    from aiogram.methods import EditMessageText
    draft = ctx.db.scalar("SELECT MAX(id) FROM drafts")
    kb = session.of(EditMessageText)[-1].reply_markup.inline_keyboard
    assert any(b.callback_data == f"lp:c:{draft}" and "225 ₽" in b.text for row in kb for b in row)
    await feed(cb_update(OWNER, f"lp:c:{draft}"))
    row = ctx.db.lave_get(1)
    assert row["amount"] == 22500 and row["usd"] == "2.65"
    await ctx.lave.poll_once()
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 0
    fake.pay("inv_1")
    await ctx.lave.poll_once()
    await ctx.lave.poll_once()
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    assert ctx.db.user(REFERRER)["balance"] == 2250


async def test_amount_mismatch_not_credited(lenv):
    ctx, feed, session, fake, sent = lenv
    await feed(cb_update(OWNER, "lp:t:500"))
    fake.pay("inv_1", face_amount="0.10")
    await ctx.lave.poll_once()
    assert ctx.db.user(OWNER)["balance"] == 0
    assert ctx.db.lave_get(1)["status"] == "mismatch"


async def test_expired_and_api_error(lenv):
    ctx, feed, session, fake, sent = lenv
    await feed(cb_update(OWNER, "lp:t:100"))
    fake.invoices["inv_1"]["status"] = "expired"
    await ctx.lave.poll_once()
    assert ctx.db.lave_get(1)["status"] == "expired"
    assert not ctx.db.lave_active()
    fake.fail = True
    session.clear()
    await feed(cb_update(OWNER, "lp:t:100"))
    assert not any("Счёт LavePay" in (m.text or "") for m in session.of(SendMessage))


async def test_hosting_and_vip_via_lavepay(lenv):
    ctx, feed, session, fake, sent = lenv
    ctx.db.change_balance(OWNER, 22500, "topup")
    await feed(cb_update(OWNER, "tpl:clicker"))
    await feed(msg_update(OWNER, TOKEN))
    draft = ctx.db.scalar("SELECT MAX(id) FROM drafts")
    await feed(cb_update(OWNER, f"bal:create:{draft}"))
    bot_id = ctx.db.scalar("SELECT MAX(id) FROM bots")
    await feed(cb_update(OWNER, f"lp:h:{bot_id}:30"))
    row = ctx.db.lave_get(1)
    assert row["payload"] == f"host:{bot_id}:13500" and row["amount"] == 13500
    fake.pay("inv_1")
    await feed(cb_update(OWNER, "lpc:1"))
    assert ctx.db.bot(bot_id)["hosting_balance"] == 3150 + 13500
    await feed(cb_update(OWNER, f"lp:h:{bot_id}:7"))
    assert ctx.db.lave_get(2)["amount"] == 3200  # 7 × 4,50 = 31,50 -> 32 ₽
    await feed(cb_update(OWNER, f"lp:v:{bot_id}"))
    assert ctx.db.lave_get(3)["amount"] == 15000
    fake.pay("inv_3")
    await ctx.lave.poll_once()
    assert ctx.db.bot(bot_id)["vip"] == 1
    # чужой не может проверить чужой счёт
    await feed(cb_update(REFERRER, "lpc:3"))


async def test_webhook_signature_and_credit(lenv):
    ctx, feed, session, fake, sent = lenv
    await feed(cb_update(OWNER, "lp:t:150"))
    fake.pay("inv_1")
    event = {"id": "evt_1", "type": "invoice.paid", "merchant_id": 632, "data": fake.invoices["inv_1"]}
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    async with TestClient(TestServer(ctx.lave.webhook_app())) as client:
        r = await client.post("/lavepay", data=body, headers={"X-LavePay-Timestamp": ts,
                                                             "X-LavePay-Signature": "v1=deadbeef"})
        assert r.status == 401
        old = str(int(time.time()) - 600)
        r = await client.post("/lavepay", data=body, headers={"X-LavePay-Timestamp": old,
                                                             "X-LavePay-Signature": sign(SECRET, old, body)})
        assert r.status == 401
        assert ctx.db.user(OWNER)["balance"] == 0
        for _ in range(2):  # повтор доставки
            r = await client.post("/lavepay", data=body, headers={"X-LavePay-Timestamp": ts,
                                                                 "X-LavePay-Signature": sign(SECRET, ts, body)})
            assert r.status == 200
        import asyncio
        await asyncio.sleep(0.1)
    assert ctx.db.user(OWNER)["balance"] == 15000
    assert ctx.db.user(REFERRER)["balance"] == 1500
