"""PiarFlow: задания в дочернем боте (замоканный HTTP API) и заявки/начисления в конструкторе."""
import json

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiohttp import web
from aiohttp.test_utils import TestServer

from child.__main__ import build_dispatcher as child_dispatcher
from child.core import ChildApp
from child.db import ChildDB
from child.files import write_config
from child.piarflow import SCHEMA as CHILD_PF_SCHEMA
from creator.piarflow import PiarFlowClient, PiarFlowError, PiarFlowService
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, create_paid_bot, env  # noqa: F401

KEY = "pf-bot-key"
L1, L2 = "https://t.me/c1", "https://t.me/b2?start=x"


HUB_KEY = "pf-hub-key"


class FakePiarFlow:
    def __init__(self):
        self.links = [L1, L2]
        self.done: dict[int, dict[str, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.down = False
        self.bots: dict[int, dict] = {}
        self.conflict = False  # POST /factory/bots → 409
        self.fail_add_msg: str | None = None  # POST /factory/bots → 500/other
        self.add_calls: list[str] = []

    def app(self):
        a = web.Application()
        a.router.add_post("/sponsors", self.h_sponsors)
        a.router.add_post("/sponsors/check", self.h_check)
        a.router.add_post("/factory/bots", self.h_add)
        a.router.add_patch("/factory/bots/{bid}", self.h_patch)
        a.router.add_get("/factory/bots/{bid}", self.h_get)
        a.router.add_get("/traffic_bot/stats", self.h_stats)
        a.router.add_get("/balance", self.h_balance)
        return a

    def _auth(self, req, key):
        return req.headers.get("Authorization") == f"Bearer {key}"

    def st(self, uid, link):
        return self.done.get(uid, {}).get(link, "unsubscribed")

    async def h_sponsors(self, req):
        if self.down:
            return web.json_response({"status": "error", "message": "boom"}, status=500)
        auth = req.headers.get("Authorization", "")
        if auth not in (f"Bearer {KEY}", f"Bearer {HUB_KEY}"):
            return web.json_response({"status": "error", "message": "Invalid API key"}, status=401)
        body = await req.json()
        self.calls.append(("sponsors", body))
        assert {"user_id", "chat_id"} <= set(body)
        uid = body["user_id"]
        return web.json_response({"status": "ok", "sponsors": [
            {"link": link, "status": self.st(uid, link), "price": 1} for link in self.links]})

    async def h_check(self, req):
        assert req.headers.get("Authorization") in (f"Bearer {KEY}", f"Bearer {HUB_KEY}")
        body = await req.json()
        self.calls.append(("check", body))
        return web.json_response({"status": "ok", "sponsors": [
            {"link": link, "status": self.st(body["user_id"], link)} for link in body["links"]]})

    async def h_add(self, req):
        if not self._auth(req, "acct"):
            return web.json_response({"status": "error", "message": "Invalid API token"}, status=401)
        body = await req.json()
        self.add_calls.append(body["bot_token"])
        if self.conflict:
            return web.json_response({"status": "error",
                                      "message": "Bot already registered by another user"}, status=409)
        if self.fail_add_msg:
            return web.json_response({"status": "error", "message": self.fail_add_msg}, status=500)
        bid = int(body["bot_token"].split(":")[0])
        # хаб-креатор (токен без ':'-цифры в тестах может быть другим) — ключ HUB_KEY если token ends with HUB
        api_key = HUB_KEY if body["bot_token"].endswith(":HUB") else KEY
        self.bots[bid] = {"bot_id": bid, "is_active": True, "sold_subs": 0, "not_counted": 0, "earned": "0.00",
                          "api_key": api_key}
        return web.json_response({"status": "ok", "message": "Bot registered", "bot": self.bots[bid]})

    async def h_patch(self, req):
        assert self._auth(req, "acct")
        bid = int(req.match_info["bid"])
        self.bots[bid].update(await req.json())
        return web.json_response({"status": "ok", "bot": self.bots[bid]})

    async def h_get(self, req):
        assert self._auth(req, "acct")
        return web.json_response({"status": "ok", "bot": self.bots[int(req.match_info["bid"])]})

    async def h_stats(self, req):
        assert self._auth(req, "acct") and req.query["bot_id"] and req.query["date"]
        return web.json_response({"status": "ok", "stats": {"sold_subs": 2, "earned": "3.40"}})

    async def h_balance(self, req):
        if not self._auth(req, "acct"):
            return web.json_response({"status": "error", "message": "Invalid API token"}, status=401)
        return web.json_response({"status": "ok", "balance": {"earned": "10.00", "available_to_withdraw": "9.00"},
                                  "totals": {"earned_all_time": "12.00"}, "bots": []})


@pytest.fixture
async def pf_server():
    api = FakePiarFlow()
    server = TestServer(api.app())
    await server.start_server()
    yield api, str(server.make_url("")).rstrip("/")
    await server.close()


@pytest.fixture
async def pf_child(tmp_path, bot, session, pf_server):
    api, url = pf_server
    write_config(tmp_path, {"token": "42:TEST", "template": "refbot", "owner_id": 1, "username": "kid_bot",
                            "vip": False, "creator_username": "creator_bot", "piarflow_key": KEY,
                            "piarflow_base_url": url})
    app = ChildApp(tmp_path)
    dp = child_dispatcher(app)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield app, feed, session, api
    await app.close_gates()


def done(app):
    return [tuple(r) for r in app.db.all("SELECT user_id, link, counted FROM pf_done ORDER BY id")]


async def test_child_gate_and_completion(pf_child):
    app, feed, session, api = pf_child
    api.done[10] = {L2: "subscribed"}  # выполнено раньше — не показываем и не засчитываем
    await feed(msg_update(10, "/start"))
    m = session.of(SendMessage)[-1]
    assert "подпишитесь на спонсоров" in m.text
    assert [r[0].url for r in m.reply_markup.inline_keyboard if r[0].url] == [L1]
    assert m.reply_markup.inline_keyboard[-1][0].callback_data == "pf_chk"

    await feed(msg_update(10, "👤 Профиль"))
    assert "подпишитесь на спонсоров" in session.of(SendMessage)[-1].text

    await feed(cb_update(10, "pf_chk"))
    assert done(app) == [] and api.calls[-1] == ("check", {"user_id": 10, "links": [L1]})
    api.done[10][L1] = "subscribed"
    await feed(cb_update(10, "pf_chk"))
    welcome = session.of(SendMessage)[-1]
    assert "спонсоров" not in welcome.text
    assert done(app) == [(10, L1, 1)]
    n = len(api.calls)
    await feed(msg_update(10, "👤 Профиль"))  # кэш — без запросов
    assert len(api.calls) == n and "Профиль" in session.of(SendMessage)[-1].text


async def test_child_not_counted_owner_outage(pf_child):
    app, feed, session, api = pf_child
    await feed(msg_update(20, "/start"))
    api.done[20] = {L1: "not_counted", L2: "subscribed"}
    await feed(cb_update(20, "pf_chk"))
    assert done(app) == [(20, L1, 0), (20, L2, 1)]
    n = len(api.calls)
    await feed(msg_update(1, "/start"))  # владелец заданий не видит
    assert len(api.calls) == n
    api.down = True
    await feed(msg_update(30, "/start"))
    assert "спонсоров" not in session.of(SendMessage)[-1].text


async def test_child_done_via_sponsors_is_confirmed(pf_child):
    app, feed, session, api = pf_child
    await feed(msg_update(40, "/start"))
    api.done[40] = {L1: "subscribed", L2: "subscribed"}
    app.piarflow._pass_until.clear()
    app.piarflow._pending.clear()
    await feed(msg_update(40, "👤 Профиль"))  # без «Проверить»: /sponsors → /sponsors/check
    assert ("check", {"user_id": 40, "links": [L1, L2]}) in api.calls
    assert done(app) == [(40, L1, 1), (40, L2, 1)]


# ---------- конструктор ----------
@pytest.fixture
async def penv(env, pf_server):  # noqa: F811
    ctx, feed, session = env
    api, url = pf_server
    ctx.cfg.piarflow_token = "acct"
    ctx.cfg.piarflow_base_url = url
    client = PiarFlowClient("acct", url)
    ctx.piarflow = PiarFlowService(ctx, client)
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    yield ctx, feed, session, api, bot_id
    await client.close()


def kb_texts(m):
    return [b.text for row in m.reply_markup.inline_keyboard for b in row]


def last(session):
    return [r for r in session.requests if isinstance(r, (SendMessage, EditMessageText)) and r.reply_markup][-1]


def add_child_users(ctx, bot_id, n):
    db = ChildDB(ctx.sup.bot_dir(bot_id) / "bot.db")
    for i in range(n):
        db.upsert_user(5000 + i, None, "x", None)
    db.conn.close()


def add_done(ctx, bot_id, rows):
    db = ChildDB(ctx.sup.bot_dir(bot_id) / "bot.db")
    db.conn.executescript(CHILD_PF_SCHEMA)
    for uid, link, counted in rows:
        db.exec("INSERT OR IGNORE INTO pf_done(user_id,link,counted,created_at) VALUES(?,?,?,?)", uid, link, counted, 1)
    db.conn.close()


async def test_apply_approve_credit_disable(penv):
    ctx, feed, session, api, bot_id = penv
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    assert "💸 Доп заработок" in kb_texts(last(session))
    await feed(cb_update(OWNER, f"b:{bot_id}:tg"))  # только PiarFlow — сразу его экран
    screen = last(session)
    assert "PiarFlow" in screen.text and "<b>0,50 ₽</b>" in screen.text
    assert "не удалось проверить" in screen.text and "минимум для заявки: 50" in screen.text
    assert "Режим:" not in screen.text and "Трансляция спонсоров креатора" not in kb_texts(screen)
    await feed(cb_update(OWNER, f"b:{bot_id}:pfapply"))
    assert ctx.db.pf(bot_id) is None  # мало пользователей
    add_child_users(ctx, bot_id, 50)
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:pfapply"))  # сразу заявка, без выбора режима
    assert ctx.db.pf(bot_id)["status"] == "pending" and ctx.db.pf(bot_id)["mode"] == "factory"
    note = [m for m in session.of(SendMessage) if m.chat_id == ADMIN][-1]
    assert "Заявка PiarFlow" in note.text and "Фабрика" not in note.text

    await feed(cb_update(OWNER, f"pf:ok:{bot_id}"))  # не админ
    assert ctx.db.pf(bot_id)["status"] == "pending"
    await feed(cb_update(ADMIN, "a:pf"))
    assert "Заявок: 1" in last(session).text and "к выводу 9.00" in last(session).text

    n_add = len(api.add_calls)
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))
    t = ctx.db.pf(bot_id)
    assert t["status"] == "approved" and t["api_key"] == KEY and t["pf_bot_id"] and t["mode"] == "factory"
    assert len(api.add_calls) == n_add + 1
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["piarflow_key"] == KEY

    bal0 = ctx.db.user(OWNER)["balance"]
    add_done(ctx, bot_id, [(1, L1, 1), (1, L2, 1), (2, L1, 1), (3, L1, 0)])
    assert ctx.piarflow.sync_all() == 3
    assert ctx.db.user(OWNER)["balance"] == bal0 + 150
    assert ctx.piarflow.sync_all() == 0
    assert ctx.db.pf_credit(bot_id, 1, L1, 50) is False
    assert ctx.db.history(OWNER)[0]["kind"] == "piarflow"

    await feed(cb_update(ADMIN, f"a:pfb:{bot_id}"))
    assert "сегодня (МСК) 2 подписок, 3.40 ₽" in last(session).text
    assert "Эффект. режим:" in last(session).text and "Фабрика" in last(session).text

    await feed(cb_update(ADMIN, f"pf:off:{bot_id}"))
    assert ctx.db.pf(bot_id)["status"] == "disabled" and api.bots[t["pf_bot_id"]]["is_active"] is False
    assert json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())["piarflow_key"] == ""
    n_add2 = len(api.add_calls)
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))  # re-enable без повторной регистрации
    assert ctx.db.pf(bot_id)["status"] == "approved" and api.bots[t["pf_bot_id"]]["is_active"] is True
    assert len(api.add_calls) == n_add2


async def test_both_factory_and_hub_fail_keeps_pending(penv):
    ctx, feed, session, api, bot_id = penv
    ctx.db.kv_set("price:PIARFLOW_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:pfapply"))
    ctx.piarflow.client.token = "wrong"  # factory и авто-хаб через тот же клиент падают
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))
    t = ctx.db.pf(bot_id)
    assert t["status"] == "pending" and t["error"] and "factory:" in t["error"] and "hub:" in t["error"]
    await feed(cb_update(ADMIN, f"pf:no:{bot_id}"))
    assert ctx.db.pf(bot_id)["status"] == "rejected"
    with pytest.raises(PiarFlowError):
        await ctx.piarflow.client.balance()


async def test_factory_409_falls_back_to_relay(penv):
    ctx, feed, session, api, bot_id = penv
    ctx.cfg.piarflow_hub_api_key = HUB_KEY
    ctx.db.kv_set("price:PIARFLOW_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:pfapply"))
    api.conflict = True
    n_add = len(api.add_calls)
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))
    t = ctx.db.pf(bot_id)
    assert t["status"] == "approved" and t["mode"] == "relay" and t["api_key"] == HUB_KEY
    assert t["pf_bot_id"] is None
    assert t["error"] and t["error"].startswith("factory→relay")
    assert len(api.add_calls) == n_add + 1
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["piarflow_key"] == HUB_KEY


async def test_factory_other_error_falls_back_to_relay(penv):
    ctx, feed, session, api, bot_id = penv
    ctx.cfg.piarflow_hub_api_key = HUB_KEY
    ctx.db.kv_set("price:PIARFLOW_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:pfapply"))
    api.fail_add_msg = "upstream timeout"
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))
    t = ctx.db.pf(bot_id)
    assert t["status"] == "approved" and t["mode"] == "relay" and t["api_key"] == HUB_KEY
    assert t["error"] == "factory→relay: ошибка регистрации"  # raw provider details are secret-unsafe


async def test_stale_pfmod_applies_as_factory_first(penv):
    """Старые кнопки pfmod:relay всё равно создают заявку; approve идёт factory-first."""
    ctx, feed, session, api, bot_id = penv
    ctx.db.kv_set("price:PIARFLOW_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:pfmod:relay"))
    assert ctx.db.pf(bot_id)["status"] == "pending" and ctx.db.pf(bot_id)["mode"] == "factory"
    await feed(cb_update(ADMIN, f"pf:ok:{bot_id}"))
    assert ctx.db.pf(bot_id)["status"] == "approved" and ctx.db.pf(bot_id)["mode"] == "factory"


async def test_hub_when_both_enabled(penv):
    from creator.tgrass import TGrassService
    from tests.test_tgrass import FakeClient
    ctx, feed, session, api, bot_id = penv
    ctx.tgrass = TGrassService(ctx, FakeClient())
    await feed(cb_update(OWNER, f"b:{bot_id}:tg"))
    assert kb_texts(last(session))[:2] == ["TGrass — заявка не записана", "PiarFlow — заявка не записана"]
    await feed(cb_update(OWNER, f"b:{bot_id}:tgr"))
    assert "TGrass" in last(session).text
    await feed(cb_update(ADMIN, "a:main"))
    assert {"💸 TGrass", "🚀 PiarFlow"} <= set(kb_texts(last(session)))
