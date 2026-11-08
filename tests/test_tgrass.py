"""TGrass: ОП в дочернем боте (замоканный HTTP API) и заявки/начисления в конструкторе."""
import json

import pytest
from aiogram.methods import SendMessage
from aiohttp import web
from aiohttp.test_utils import TestServer

from child.__main__ import build_dispatcher as child_dispatcher
from child.core import ChildApp
from child.db import ChildDB
from child.files import write_config
from child.tgrass import SCHEMA as CHILD_TG_SCHEMA
from creator.money import parse_rub, rub
from creator.tgrass import TGrassError, TGrassService
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, create_paid_bot, env  # noqa: F401

KEY = "bot-key-123"


class FakeTGrassAPI:
    """Мини-сервер /offers и /check по документации tgrass.space/integration."""

    def __init__(self):
        self.subscribed: dict[int, set[int]] = {}
        self.offers = [{"offer_id": 1, "name": "Канал 1", "link": "https://t.me/c1", "type": "channel"},
                       {"offer_id": 2, "name": "Бот 2", "link": "https://t.me/b2", "type": "bot"}]
        self.fake_users: set[int] = set()
        self.calls: list[tuple[str, dict]] = []
        self.down = False

    def app(self):
        a = web.Application()
        a.router.add_post("/offers", self.h_offers)
        a.router.add_post("/check", self.h_check)
        return a

    async def h_offers(self, req):
        if self.down:
            return web.json_response({"detail": "boom"}, status=500)
        if req.headers.get("Auth") != KEY:
            return web.json_response({"detail": "Invalid auth token"}, status=401)
        body = await req.json()
        self.calls.append(("offers", body))
        assert {"tg_user_id", "is_premium", "lang"} <= set(body)
        uid = body["tg_user_id"]
        subs = self.subscribed.get(uid, set())
        offers = [dict(o, subscribed=o["offer_id"] in subs, channel_id=None, price=1.5) for o in self.offers]
        if not offers:
            return web.json_response({"status": "no_offers", "offers": [], "description": "no"})
        status = "ok" if all(o["subscribed"] for o in offers) else "not_ok"
        return web.json_response({"status": status, "offers": offers, "description": status})

    async def h_check(self, req):
        body = await req.json()
        self.calls.append(("check", body))
        ok = body["offer_id"] in self.subscribed.get(body["tg_user_id"], set())
        return web.json_response({"status": "subscribed" if ok else "not_subscribed",
                                  "is_fake": body["tg_user_id"] in self.fake_users})


@pytest.fixture
async def tg_child(tmp_path, bot, session):
    api = FakeTGrassAPI()
    server = TestServer(api.app())
    await server.start_server()
    write_config(tmp_path, {"token": "42:TEST", "template": "refbot", "owner_id": 1, "username": "kid_bot",
                            "vip": False, "creator_username": "creator_bot", "tgrass_key": KEY,
                            "tgrass_base_url": str(server.make_url("")).rstrip("/")})
    app = ChildApp(tmp_path)
    dp = child_dispatcher(app)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield app, feed, session, api
    await app.tgrass.close()
    await server.close()


def done(app):
    return [tuple(r) for r in app.db.all("SELECT user_id, offer_id, fake FROM tgrass_done ORDER BY id")]


async def test_child_gate_and_completion(tg_child):
    app, feed, session, api = tg_child
    api.subscribed[10] = {2}  # на оффер 2 был подписан раньше — его не засчитываем
    await feed(msg_update(10, "/start"))
    m = session.of(SendMessage)[-1]
    assert "подпишитесь на спонсоров" in m.text
    urls = [r[0].url for r in m.reply_markup.inline_keyboard if r[0].url]
    assert urls == ["https://t.me/c1"]
    assert m.reply_markup.inline_keyboard[-1][0].callback_data == "tg_chk"

    # любое другое действие тоже упирается в ОП
    await feed(msg_update(10, "👤 Профиль"))
    assert "подпишитесь на спонсоров" in session.of(SendMessage)[-1].text

    await feed(cb_update(10, "tg_chk"))  # ещё не подписался
    assert done(app) == []
    api.subscribed[10] = {1, 2}
    await feed(cb_update(10, "tg_chk"))
    from aiogram.types import ReplyKeyboardMarkup
    welcome = session.of(SendMessage)[-1]
    assert "спонсоров" not in welcome.text and isinstance(welcome.reply_markup, ReplyKeyboardMarkup)
    assert done(app) == [(10, 1, 0)]
    await feed(cb_update(10, "tg_chk"))  # повторная проверка — без дублей
    assert done(app) == [(10, 1, 0)]

    # дальше пользователь проходит без запросов (кэш)
    n = len(api.calls)
    await feed(msg_update(10, "👤 Профиль"))
    assert len(api.calls) == n and "Профиль" in session.of(SendMessage)[-1].text


async def test_child_gate_fake_owner_and_outage(tg_child):
    app, feed, session, api = tg_child
    api.fake_users.add(20)
    await feed(msg_update(20, "/start"))
    api.subscribed[20] = {1, 2}
    await feed(cb_update(20, "tg_chk"))
    assert done(app) == [(20, 1, 1), (20, 2, 1)]  # фейк помечен, конструктор не заплатит

    n = len(api.calls)
    await feed(msg_update(1, "/start"))  # владелец бота ОП не видит
    assert len(api.calls) == n

    api.down = True
    await feed(msg_update(30, "/start"))  # TGrass недоступен — пользователя пускаем
    assert "спонсоров" not in session.of(SendMessage)[-1].text


async def test_child_without_key_has_no_gate(tmp_path, bot, session):
    write_config(tmp_path, {"token": "42:TEST", "template": "clicker", "owner_id": 1, "username": "kid_bot"})
    app = ChildApp(tmp_path)
    assert app.tgrass is None


# ---------- конструктор ----------
HUB_KEY = "hub-key-999"


class FakeClient:
    def __init__(self):
        self.new_bots: list[tuple] = []
        self.fail = False
        self.conflict = False
        self.hub_name = "creator_bot"  # имя хаба при авто-регистрации

    async def new_bot(self, username, user_id, bot_name):
        if self.fail:
            raise TGrassError("Invalid auth token", 401)
        if self.conflict and bot_name != self.hub_name:
            raise TGrassError("Bot already registered by another user", 409)
        self.new_bots.append((username, user_id, bot_name))
        if bot_name == self.hub_name:
            return HUB_KEY
        return KEY

    async def balance(self, key):
        return 12.5

    async def statistics(self, key, date=None):
        return {"subs_count": 3, "unsubs_count": 0, "income": 4.5}


@pytest.fixture
async def tenv(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.tgrass_factory_token = "factory"
    ctx.cfg.tgrass_owner_id = ADMIN
    ctx.cfg.tgrass_owner_username = "sample_owner"
    client = FakeClient()
    ctx.tgrass = TGrassService(ctx, client)
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    return ctx, feed, session, client, bot_id


def kb_texts(m):
    return [b.text for row in m.reply_markup.inline_keyboard for b in row]


def add_child_users(ctx, bot_id, n):
    db = ChildDB(ctx.sup.bot_dir(bot_id) / "bot.db")
    for i in range(n):
        db.upsert_user(5000 + i, None, "x", None)
    db.conn.close()


def add_done(ctx, bot_id, rows):
    db = ChildDB(ctx.sup.bot_dir(bot_id) / "bot.db")
    db.conn.executescript(CHILD_TG_SCHEMA)
    for uid, oid, fake in rows:
        db.exec("INSERT OR IGNORE INTO tgrass_done(user_id,offer_id,fake,created_at) VALUES(?,?,?,?)", uid, oid, fake, 1)
    db.conn.close()


async def test_hidden_without_token(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    assert "💸 Доп заработок" not in kb_texts(_last_kb(session))
    await feed(cb_update(OWNER, f"b:{bot_id}:tg"))  # кнопки нет — и экран не открывается
    assert ctx.db.tgrass(bot_id) is None


def _last_kb(session):
    from aiogram.methods import EditMessageText
    return [r for r in session.requests if isinstance(r, (SendMessage, EditMessageText)) and r.reply_markup][-1]


async def test_apply_approve_credit_disable(tenv):
    ctx, feed, session, client, bot_id = tenv
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    assert "💸 Доп заработок" in kb_texts(_last_kb(session))

    await feed(cb_update(OWNER, f"b:{bot_id}:tg"))
    screen = _last_kb(session)
    assert "В учёте: заявка не записана" in screen.text and "не проверяются" in screen.text
    assert "<b>0,50 ₽</b>" in screen.text
    assert "не удалось проверить" in screen.text and "минимум для заявки: 50" in screen.text and "⭐" not in screen.text
    assert kb_texts(screen) == ["📝 Подать заявку", "💬 Связаться с админом", "⬅️ Назад"]
    assert "Режим:" not in screen.text

    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))  # мало пользователей
    assert ctx.db.tgrass(bot_id) is None

    add_child_users(ctx, bot_id, 50)
    session.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))  # сразу заявка
    assert ctx.db.tgrass(bot_id)["status"] == "pending" and ctx.db.tgrass(bot_id)["mode"] == "factory"
    note = [m for m in session.of(SendMessage) if m.chat_id == ADMIN][-1]
    assert "Заявка TGrass" in note.text and "Фабрика" not in note.text
    assert kb_texts(note) == ["✅ Одобрить", "❌ Отклонить"]
    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))  # повторно — нельзя
    assert len([m for m in session.of(SendMessage) if m.chat_id == ADMIN]) == 1

    await feed(cb_update(OWNER, f"tg:ok:{bot_id}"))  # не админ
    assert ctx.db.tgrass(bot_id)["status"] == "pending"

    await feed(cb_update(ADMIN, "a:tg"))
    assert "Заявок: 1" in _last_kb(session).text

    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))
    assert client.new_bots == [("sample_owner", ADMIN, "my_clicker_bot")]
    assert ctx.db.tgrass(bot_id)["status"] == "approved" and ctx.db.tgrass(bot_id)["mode"] == "factory"
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["tgrass_key"] == KEY
    assert any(m.chat_id == OWNER and "подключён к TGrass" in m.text for m in session.of(SendMessage))

    bal0 = ctx.db.user(OWNER)["balance"]
    add_done(ctx, bot_id, [(1, 1, 0), (1, 2, 0), (2, 1, 0), (3, 1, 1)])  # последний — фейк
    assert ctx.tgrass.sync_all() == 3
    assert ctx.db.user(OWNER)["balance"] == bal0 + 150
    assert ctx.tgrass.sync_all() == 0
    assert ctx.db.tgrass_credit(bot_id, 1, 1, 50) is False
    assert ctx.db.user(OWNER)["balance"] == bal0 + 150
    assert ctx.db.tgrass_stats(bot_id) == (3, 150)
    assert ctx.db.history(OWNER)[0]["kind"] == "tgrass"

    await feed(cb_update(OWNER, f"b:{bot_id}:tg"))
    screen = _last_kb(session)
    assert "Зачтено в учёте: 3" in screen.text and "Начислено в конструкторе: 1,50 ₽" in screen.text
    assert "📝 Подать заявку" not in kb_texts(screen)
    assert "Режим:" not in screen.text

    await feed(cb_update(ADMIN, f"a:tgb:{bot_id}"))
    assert "баланс 12.5" in _last_kb(session).text
    assert "Эффект. режим:" in _last_kb(session).text

    await feed(cb_update(ADMIN, f"tg:off:{bot_id}"))
    assert ctx.db.tgrass(bot_id)["status"] == "disabled"
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["tgrass_key"] == ""

    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))  # повторное включение без нового /new_bot
    assert ctx.db.tgrass(bot_id)["status"] == "approved" and len(client.new_bots) == 1


async def test_both_factory_and_hub_fail_keeps_pending(tenv):
    ctx, feed, session, client, bot_id = tenv
    ctx.db.kv_set("price:TGRASS_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))
    client.fail = True  # и factory, и авто-хаб через new_bot
    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))
    t = ctx.db.tgrass(bot_id)
    assert t["status"] == "pending" and t["error"] and "factory:" in t["error"] and "hub:" in t["error"]
    await feed(cb_update(ADMIN, f"tg:no:{bot_id}"))
    assert ctx.db.tgrass(bot_id)["status"] == "rejected"
    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))
    assert ctx.db.tgrass(bot_id)["status"] == "pending"


async def test_factory_conflict_falls_back_to_relay(tenv):
    ctx, feed, session, client, bot_id = tenv
    ctx.cfg.tgrass_hub_api_key = HUB_KEY
    ctx.db.kv_set("price:TGRASS_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:tgapply"))
    client.conflict = True
    n = len(client.new_bots)
    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))
    t = ctx.db.tgrass(bot_id)
    assert t["status"] == "approved" and t["mode"] == "relay" and t["api_key"] == HUB_KEY
    assert t["error"] and t["error"].startswith("factory→relay")
    assert len(client.new_bots) == n  # hub из env, factory упал до append
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["tgrass_key"] == HUB_KEY

    # re-enable relay без нового factory
    await feed(cb_update(ADMIN, f"tg:off:{bot_id}"))
    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))
    assert ctx.db.tgrass(bot_id)["status"] == "approved" and ctx.db.tgrass(bot_id)["mode"] == "relay"
    assert len(client.new_bots) == n


async def test_stale_tgmod_applies_as_factory_first(tenv):
    ctx, feed, session, client, bot_id = tenv
    ctx.db.kv_set("price:TGRASS_MIN_USERS", "0")
    await feed(cb_update(OWNER, f"b:{bot_id}:tgmod:relay"))
    assert ctx.db.tgrass(bot_id)["status"] == "pending" and ctx.db.tgrass(bot_id)["mode"] == "factory"
    await feed(cb_update(ADMIN, f"tg:ok:{bot_id}"))
    assert ctx.db.tgrass(bot_id)["status"] == "approved" and ctx.db.tgrass(bot_id)["mode"] == "factory"


def test_money_format_and_parse():
    assert [rub(x) for x in (0, 5, 50, 100, 450, 3150, 22500, 123456789, -25)] == [
        "0 ₽", "0,05 ₽", "0,50 ₽", "1 ₽", "4,50 ₽", "31,50 ₽", "225 ₽", "1 234 567,89 ₽", "-0,25 ₽"]
    assert [parse_rub(x) for x in ("225", "4,5", "4.50", "0,05", " 1 000 ₽", "abc", "1,234", "-5")] == [
        22500, 450, 450, 5, 100000, None, None, None]
    assert parse_rub("-5,5", allow_negative=True) == -550
