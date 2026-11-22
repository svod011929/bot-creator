"""Сквозные сценарии главного бота на фейковой сессии Telegram."""
import json
import sys

import pytest
from aiogram.methods import DeleteMessage, EditMessageText, SendInvoice, SendMessage

from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update

ADMIN = 777
OWNER = 100
REFERRER = 50
BUYER = 200
TOKEN = "123456789:AAEexampleexampleexampleexample12345"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
async def env(tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path, admin_ids={ADMIN}, chat_url="https://t.me/chat", channel_url="https://t.me/chan")
    db = DB(cfg.db_path)
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")

    async def validator(token):
        return ("my_clicker_bot", "My Clicker") if token == TOKEN else None

    ctx = Ctx(db=db, cfg=cfg, sup=sup, validate_token=validator, bot_username="creator_bot")
    dp = build_dispatcher(ctx)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield ctx, feed, session
    await sup.shutdown()
    db.close()


async def create_paid_bot(ctx, feed, session, uid=OWNER):
    """Создание бота с оплатой с баланса (баланс пополняется напрямую, как после оплаты LavePay)."""
    ctx.db.change_balance(uid, ctx.template_price("clicker"), "topup", "test")
    await feed(cb_update(uid, "tpl:clicker"))
    await feed(msg_update(uid, TOKEN))
    draft_id = ctx.db.scalar("SELECT MAX(id) FROM drafts WHERE user_id=?", uid)
    await feed(cb_update(uid, f"bal:create:{draft_id}"))
    return ctx.db.scalar("SELECT MAX(id) FROM bots WHERE owner_id=?", uid)


def all_buttons(session):
    out = []
    for m in session.requests:
        kb = getattr(m, "reply_markup", None)
        for row in getattr(kb, "inline_keyboard", None) or []:
            out += [b.text for b in row]
    return out


async def test_start_menu_and_referral(env):
    ctx, feed, session = env
    await feed(msg_update(REFERRER, "/start"))
    await feed(msg_update(OWNER, f"/start ref{REFERRER}"))
    assert ctx.db.user(OWNER)["referrer_id"] == REFERRER
    start_msg = session.of(SendMessage)[0]
    kb = [b.text for row in start_msg.reply_markup.keyboard for b in row]
    assert kb == ["🔨 Создать бота", "🤖 Мои боты", "💼 Мой кабинет", "👥 Партнёры", "🛒 Перепродажа", "📰 Информация",
                  "📚 Контент ботов"]
    assert any(m.chat_id == REFERRER for m in session.of(SendMessage))


async def test_create_bot_rub_from_balance(env):
    ctx, feed, session = env
    await feed(msg_update(REFERRER, "/start"))
    await feed(msg_update(OWNER, f"/start ref{REFERRER}"))
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    choose = session.of(SendMessage)[-1]
    assert "Выберите шаблон" in choose.text
    assert [r[0].text for r in choose.reply_markup.inline_keyboard] == ["⛏ Кликер бот", "👥 Реф. бот", "🌟 Звёздный бот"]

    # неверный токен
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert "Стоимость: <b>225 ₽</b>" in session.texts()[-1] and "4,50 ₽/сутки" in session.texts()[-1]
    await feed(msg_update(OWNER, "999999:BADBADBADBADBADBADBADBADBADBADBAD123"))
    assert "недействителен" in session.texts()[-1]
    assert session.of(DeleteMessage), "сообщение с токеном должно удаляться"

    # денег нет и LavePay выключен -> только «Пополнить баланс»
    await feed(cb_update(OWNER, "tpl:clicker"))
    await feed(msg_update(OWNER, TOKEN))
    kb = session.of(EditMessageText)[-1].reply_markup.inline_keyboard
    assert [b.callback_data for row in kb for b in row] == ["cab:topup", "cancel"]
    assert "К оплате: <b>225 ₽</b>" in session.of(EditMessageText)[-1].text

    bot_id = await create_paid_bot(ctx, feed, session)
    b = ctx.db.bot(bot_id)
    assert b["owner_id"] == OWNER and b["username"] == "my_clicker_bot"
    assert b["hosting_balance"] == 450 * 7 and b["status"] == "running"
    assert ctx.sup.is_running(bot_id)
    assert ctx.db.user(OWNER)["balance"] == 0
    assert ctx.db.user(REFERRER)["balance"] == 0  # партнёрские — только с реальных оплат
    # повторное нажатие не создаёт второго бота
    draft = ctx.db.scalar("SELECT MAX(id) FROM drafts")
    await feed(cb_update(OWNER, f"bal:create:{draft}"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    # тот же токен второй раз не принять
    await feed(cb_update(OWNER, "tpl:refbot"))
    await feed(msg_update(OWNER, TOKEN))
    assert "уже подключён" in session.texts()[-1]
    # Нет Stars-платежей креатора; описание единицы child starbot допустимо.
    assert not session.of(SendInvoice)


async def test_no_stars_payment_handlers(env):
    ctx, feed, session = env
    dp = build_dispatcher(ctx)
    used = dp.resolve_used_update_types()
    assert "pre_checkout_query" not in used
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "cab:pay:250"))  # старые кнопки Stars больше ничего не делают
    assert not session.of(SendInvoice) and ctx.db.user(OWNER)["balance"] == 0


async def test_my_bots_card_and_actions(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.clear()
    await feed(msg_update(OWNER, "🤖 Мои боты"))
    lst = session.of(SendMessage)[-1]
    assert lst.reply_markup.inline_keyboard[0][0].text == f"ID {bot_id} | Кликер бот 🟢"
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    card = session.texts()[-1]
    for part in ("Статус: 🟢 Запущен", "@my_clicker_bot", "Шаблон: ⛏ Кликер бот", "Использование диска",
                 "Баланс хостинга: 31,50 ₽ (≈7 дн.)"):
        assert part in card

    # чужой пользователь не может управлять ботом
    await feed(msg_update(BUYER, "/start"))
    await feed(cb_update(BUYER, f"b:{bot_id}:stop"))
    assert ctx.sup.is_running(bot_id)

    await feed(cb_update(OWNER, f"b:{bot_id}:stop"))
    assert not ctx.sup.is_running(bot_id) and ctx.db.bot(bot_id)["status"] == "stopped"
    await feed(cb_update(OWNER, f"b:{bot_id}:start"))
    assert ctx.sup.is_running(bot_id)

    # хостинг с баланса
    ctx.db.change_balance(OWNER, 4000, "topup")
    await feed(cb_update(OWNER, f"b:{bot_id}:host"))
    host_kb = [b.text for row in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in row]
    assert "💼 +7 дн. — 32 ₽ с баланса" in host_kb and "💼 +90 дн. — 405 ₽ с баланса" in host_kb
    await feed(cb_update(OWNER, f"b:{bot_id}:hbal:7"))
    assert ctx.db.bot(bot_id)["hosting_balance"] == 3150 + 3200 and ctx.db.user(OWNER)["balance"] == 800
    await feed(cb_update(OWNER, f"b:{bot_id}:hbal:30"))  # 135 ₽ — не хватает
    assert ctx.db.user(OWNER)["balance"] == 800

    # настройки
    await feed(cb_update(OWNER, f"b:{bot_id}:sk:click_reward"))
    await feed(msg_update(OWNER, "abc"))
    assert "Неверное" in session.texts()[-1]
    await feed(msg_update(OWNER, "5"))
    settings = json.loads((ctx.sup.bot_dir(bot_id) / "settings.json").read_text())
    assert settings["click_reward"] == 5

    # VIP с баланса → config.json обновлён
    ctx.db.change_balance(OWNER, 15000, "topup")
    await feed(cb_update(OWNER, f"b:{bot_id}:vbal"))
    cfg = json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())
    assert cfg["vip"] is True and ctx.db.bot(bot_id)["vip"] == 1 and ctx.db.user(OWNER)["balance"] == 800


async def test_transfer_and_delete(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(BUYER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    await feed(cb_update(OWNER, f"b:{bot_id}:transfer"))
    await feed(msg_update(OWNER, "31337"))
    assert "не найден" in session.texts()[-1]
    await feed(msg_update(OWNER, str(BUYER)))
    await feed(cb_update(OWNER, f"b:{bot_id}:transfer_ok:{BUYER}"))
    assert ctx.db.bot(bot_id)["owner_id"] == BUYER
    assert json.loads((ctx.sup.bot_dir(bot_id) / "config.json").read_text())["owner_id"] == BUYER

    await feed(cb_update(BUYER, f"b:{bot_id}:del"))
    assert ctx.db.bot(bot_id) is not None
    await feed(cb_update(BUYER, f"b:{bot_id}:delok"))
    assert ctx.db.bot(bot_id) is None and not ctx.sup.is_running(bot_id)
    assert not ctx.sup.bot_dir(bot_id).exists()


async def test_resale(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(BUYER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    await feed(cb_update(OWNER, f"b:{bot_id}:sell"))
    await feed(msg_update(OWNER, "abc"))
    assert ctx.db.bot(bot_id)["sale_price"] is None
    await feed(msg_update(OWNER, "200,50"))
    assert ctx.db.bot(bot_id)["sale_price"] == 20050
    session.clear()
    await feed(msg_update(BUYER, "🛒 Перепродажа"))
    market = session.of(SendMessage)[-1]
    assert market.reply_markup.inline_keyboard[0][0].callback_data == f"rs:view:{bot_id}"
    await feed(cb_update(BUYER, f"rs:buy:{bot_id}"))
    assert ctx.db.bot(bot_id)["owner_id"] == OWNER  # денег нет
    ctx.db.change_balance(BUYER, 25000, "topup")
    await feed(cb_update(BUYER, f"rs:buy:{bot_id}"))
    assert ctx.db.bot(bot_id)["owner_id"] == BUYER
    # комиссия 5%: 200,50 - 10,02 = 190,48
    assert ctx.db.user(OWNER)["balance"] == 19048 and ctx.db.user(BUYER)["balance"] == 25000 - 20050
    assert any("за 200,50 ₽" in t for t in session.texts()) and "190,48 ₽" in session.texts()[-1]


async def test_cabinet_partners_info(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "💼 Мой кабинет"))
    assert "Баланс: <b>0 ₽</b>" in session.texts()[-1]
    await feed(cb_update(OWNER, "cab:topup"))  # LavePay выключен — пополнение недоступно
    assert not session.of(SendInvoice)
    ctx.db.change_balance(OWNER, 25050, "topup")
    await feed(cb_update(OWNER, "cab:history"))
    assert "+250,50 ₽ | Пополнение" in session.texts()[-1]

    await feed(msg_update(OWNER, "👥 Партнёры"))
    assert f"https://t.me/creator_bot?start=ref{OWNER}" in session.texts()[-1]

    await feed(msg_update(OWNER, "📰 Информация"))
    info = session.of(SendMessage)[-1]
    assert "Работаем:" in info.text and "Пользователей всего: 1" in info.text and "Ботов запущено: 0" in info.text
    labels = [b.text for row in info.reply_markup.inline_keyboard for b in row]
    assert labels == ["💬 Чат", "📢 Канал", "❓ Помощь", "⛔ Оповестить о проблеме", "👑 Администратор"]

    session.clear()
    await feed(cb_update(OWNER, "info:report"))
    await feed(msg_update(OWNER, "Бот не запускается"))
    assert any(m.chat_id == ADMIN and "Обращение" in m.text for m in session.of(SendMessage))


async def test_admin_panel(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    session.clear()
    await feed(msg_update(OWNER, "/admin"))
    assert not any("Админ-панель" in (t or "") for t in session.texts())

    await feed(msg_update(ADMIN, "/admin"))
    assert "Админ-панель" in session.texts()[-1]
    await feed(cb_update(ADMIN, "a:prices"))
    labels = [b.text for row in session.of(EditMessageText)[-1].reply_markup.inline_keyboard for b in row]
    assert "Шаблон «Кликер бот», ₽: 225 ₽" in labels and "Хостинг, ₽ в сутки: 4,50 ₽" in labels
    assert "VIP для бота, ₽: 150 ₽" in labels and not any("⭐" in t for t in labels)
    await feed(cb_update(ADMIN, "a:price:PRICE_CLICKER"))
    await feed(msg_update(ADMIN, "99,9"))
    assert ctx.template_price("clicker") == 9990
    await feed(cb_update(ADMIN, "a:price:REF_PERCENT"))
    await feed(msg_update(ADMIN, "1,5"))
    assert "целое число" in session.texts()[-1]
    await feed(msg_update(ADMIN, "15"))
    assert ctx.price("REF_PERCENT") == 15
    await feed(cb_update(ADMIN, "a:stats"))
    assert "Пользователей: 2" in session.texts()[-1]
    await feed(cb_update(ADMIN, "a:users"))
    await feed(msg_update(ADMIN, str(OWNER)))
    await feed(cb_update(ADMIN, f"a:uban:{OWNER}"))
    assert ctx.db.user(OWNER)["banned"] == 1
    session.clear()
    await feed(msg_update(OWNER, "💼 Мой кабинет"))
    assert session.of(SendMessage) == []  # забаненный игнорируется
    await feed(cb_update(ADMIN, f"a:ubal:{OWNER}"))
    await feed(msg_update(ADMIN, "40,25"))
    assert ctx.db.user(OWNER)["balance"] == 4025
    await feed(cb_update(ADMIN, f"a:ubal:{OWNER}"))
    await feed(msg_update(ADMIN, "-0,25"))
    assert ctx.db.user(OWNER)["balance"] == 4000


async def test_menu_button_resets_state(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "tpl:clicker"))
    await feed(msg_update(OWNER, "📰 Информация"))  # вместо токена нажали кнопку меню
    assert "Информация" in session.texts()[-1]
