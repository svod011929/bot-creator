"""B1+B2 through the real Dispatcher; Telegram and all external effects are fake."""
import html
import re
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendInvoice, SendMessage

from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.states import CreateBot
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update

OWNER = 100
WELCOME = (
    "Добро пожаловать в конструктор ботов!\n\n"
    "Создайте Telegram-бота из готового шаблона без программирования. "
    "Мы размещаем бота на своём сервере, вы управляете его настройками.\n\n"
    "Впервые здесь? Нажмите «Создать бота» — сравните шаблоны, стоимость и условия хостинга.\n"
    "Уже есть бот? Откройте «Мои боты».\n"
    "Нужна помощь? «Информация» → «❓ Помощь».\n\n"
    "Кабинет, партнёры и перепродажа — в меню ниже."
)
UNAVAILABLE = "Условия временно недоступны. Откройте выбор шаблона позже."
MENU = [
    ["🔨 Создать бота", "🤖 Мои боты"],
    ["💼 Мой кабинет", "👥 Партнёры"],
    ["🛒 Перепродажа", "📰 Информация"],
]


def plain(text):
    return html.unescape(re.sub(r"</?(?:b|code)>", "", text))


@pytest.fixture
async def selection(tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path, admin_ids={777})
    db = DB(cfg.db_path)
    sup = Supervisor(db, cfg)

    async def forbidden_validator(token):
        raise AssertionError("Viewing must not validate tokens")

    ctx = Ctx(db=db, cfg=cfg, sup=sup, validate_token=forbidden_validator)
    dp = build_dispatcher(ctx)
    state = dp.fsm.get_context(bot=bot, chat_id=OWNER, user_id=OWNER)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield ctx, feed, session, state
    await dp.storage.close()
    db.close()


async def test_welcome_exact_same_for_new_and_returning(selection):
    ctx, feed, session, state = selection
    for _ in range(2):
        await feed(msg_update(OWNER, "/start"))
        message = session.of(SendMessage)[-1]
        assert plain(message.text) == WELCOME
        assert [[b.text for b in row] for row in message.reply_markup.keyboard] == MENU
        assert await state.get_state() is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM users") == 1


async def test_comparison_copy_and_buttons(selection):
    ctx, feed, session, state = selection
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    message = session.of(SendMessage)[-1]
    text = plain(message.text)
    assert text.startswith(
        "Выберите шаблон бота. Сравните возможности и стоимость, затем нажмите нужный вариант — "
        "откроется описание с инструкцией по токену"
    )
    for copy in (
        "Кликер с экспедициями, миссиями, навыками, коллекцией и рейтингом игрового опыта.",
        "Меню: «Приключение», «Кликать», «Улучшения», «Топ», «Пригласить».",
        "Баланс по умолчанию — монеты; отдельный XP используется для развития игры и не выводится.",
        "Для реферальной механики: пользователи приглашают друзей и получают настроенную владельцем награду.",
        "Меню: «Пригласить друзей», «Топ рефоводов», «О боте».",
        "Единица по умолчанию — рубли.",
        "Для механики с друзьями, ежедневным бонусом и собственными заданиями на подписку.",
        "Меню: «Заработать звёзды», «Ежедневный бонус», «Задания», «Топ».",
        "Единица по умолчанию — звёзды.",
        "У всех шаблонов есть профиль и заявки на ручную выплату. "
        "Единица баланса внутри бота настраивается владельцем. "
        "Создание и хостинг оплачиваются в рублях. Гарантированного дохода нет.",
    ):
        assert copy in text
    buttons = [b for row in message.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["⛏ Кликер бот", "👥 Реф. бот", "🌟 Звёздный бот"]
    assert [b.callback_data for b in buttons] == ["tpl:clicker", "tpl:refbot", "tpl:starbot"]
    assert text.index("Кликер бот") < text.index("Реф. бот") < text.index("Звёздный бот")
    assert "@BotFather" not in text and "VIP" not in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert await state.get_state() is None
    assert not session.of(SendInvoice)


@pytest.mark.parametrize("source", ["cfg", "db"])
async def test_comparison_uses_current_prices_and_rerenders(selection, source):
    ctx, feed, session, state = selection
    values = {"PRICE_CLICKER": 101, "PRICE_REFBOT": 202, "PRICE_STARBOT": 303,
              "START_HOSTING_DAYS": 11, "HOSTING_DAY_PRICE": 125}
    for key, value in values.items():
        if source == "db":
            ctx.db.kv_set("price:" + key, str(value))
        else:
            ctx.cfg.prices[key] = value
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    first = session.texts()[-1]
    assert "Создание: 1,01 ₽" in first
    assert "Создание: 2,02 ₽" in first
    assert "Создание: 3,03 ₽" in first
    assert "В стоимость включён хостинг на 11 дн." in first
    assert "Хостинг: 1,25 ₽/сутки" in first
    ctx.db.kv_set("price:PRICE_CLICKER", "999")
    await feed(cb_update(OWNER, "create"))
    assert "Создание: 9,99 ₽" in session.texts()[-1]
    assert "Создание: 1,01 ₽" not in session.texts()[-1]
    assert await state.get_state() is None


@pytest.mark.parametrize("creation", [0, 1234])
@pytest.mark.parametrize("days", [0, 3])
@pytest.mark.parametrize("daily", [0, 105])
async def test_independent_zero_conditions(selection, creation, days, daily):
    ctx, feed, session, state = selection
    for key in ("PRICE_CLICKER", "PRICE_REFBOT", "PRICE_STARBOT"):
        ctx.db.kv_set("price:" + key, str(creation))
    ctx.db.kv_set("price:START_HOSTING_DAYS", str(days))
    ctx.db.kv_set("price:HOSTING_DAY_PRICE", str(daily))
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    text = plain(session.texts()[-1])
    assert text.count("Создание: бесплатно" if creation == 0 else "Создание: 12,34 ₽") == 3
    assert ("В стоимость включён хостинг на 3 дн." in text) == (days > 0)
    assert "на 0 дн." not in text
    assert ("Хостинг: 1,05 ₽/сутки" in text) == (daily > 0)
    assert ("По текущему тарифу ежесуточная плата за хостинг не взимается" in text) == (daily == 0)


@pytest.mark.parametrize("key", ["clicker", "refbot", "starbot"])
async def test_detail_has_current_conditions_and_existing_token_step(selection, key):
    from child import templates
    from creator.states import CreateBot

    ctx, feed, session, state = selection
    ctx.db.kv_set("price:" + templates.info(key).price_key, "0")
    ctx.db.kv_set("price:START_HOSTING_DAYS", "0")
    ctx.db.kv_set("price:HOSTING_DAY_PRICE", "0")
    await feed(cb_update(OWNER, "tpl:" + key))
    message = session.of(EditMessageText)[-1]
    text = plain(message.text)
    assert "Стоимость: 0 ₽" in text
    assert "на 0 дн." not in text
    assert "По текущему тарифу ежесуточная плата за хостинг не взимается" in text
    assert "@BotFather" in text and "/newbot" in text
    assert message.reply_markup.inline_keyboard[0][0].callback_data == "create"
    assert await state.get_state() == CreateBot.token.state
    assert await state.get_data() == {"template": key}


@pytest.mark.parametrize("route", ["menu", "create", "tpl:clicker"])
@pytest.mark.parametrize("key,value", [
    ("PRICE_CLICKER", "-1"), ("HOSTING_DAY_PRICE", "-1"), ("START_HOSTING_DAYS", "-1"),
    ("START_HOSTING_DAYS", "9" * 4100), ("PRICE_CLICKER", "9" * 5000),
])
async def test_invalid_or_oversized_conditions_fail_closed(selection, route, key, value, caplog):
    ctx, feed, session, state = selection
    ctx.db.kv_set("price:" + key, value)
    if route == "menu":
        await feed(msg_update(OWNER, "🔨 Создать бота"))
    else:
        await feed(cb_update(OWNER, route))
    assert UNAVAILABLE in session.texts() or any(
        getattr(r, "text", None) == UNAVAILABLE for r in session.requests
    )
    assert await state.get_state() is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM drafts") == 0
    assert value not in "\n".join(session.texts())
    assert value not in caplog.text


FOREIGN_STATES = ["Info:report", "BotEdit:setting", "BotEdit:transfer", "Cabinet:lave_amount", "Admin:price", "Wizard:active"]
STALE = "Действие устарело. Завершите текущую настройку или нажмите /cancel."


async def test_back_leaves_token_state(selection):
    ctx, feed, session, state = selection
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert await state.get_state() == CreateBot.token.state
    await feed(cb_update(OWNER, "create"))
    assert await state.get_state() is None
    assert await state.get_data() == {}
    session.clear()
    await feed(msg_update(OWNER, "ordinary text"))
    assert not session.of(DeleteMessage)
    assert ctx.db.scalar("SELECT COUNT(*) FROM drafts") == 0


@pytest.mark.parametrize("active", FOREIGN_STATES)
@pytest.mark.parametrize("callback", ["create", "tpl:starbot", "tpl:INVALID"])
async def test_old_selection_cannot_interrupt_other_wizard(selection, active, callback):
    ctx, feed, session, state = selection
    await state.set_state(active)
    await state.set_data({"bot_id": 10, "draft": "KEEP"})
    await feed(cb_update(OWNER, callback))
    assert await state.get_state() == active
    assert await state.get_data() == {"bot_id": 10, "draft": "KEEP"}
    assert session.of(AnswerCallbackQuery)[-1].text == STALE
    assert not session.of(EditMessageText)


@pytest.mark.parametrize("active", [CreateBot.token.state, *FOREIGN_STATES])
@pytest.mark.parametrize("action", ["/start", "/cancel", "cancel"])
async def test_global_start_cancel_reset_state(selection, active, action):
    ctx, feed, session, state = selection
    await state.set_state(active)
    await state.set_data({"template": "starbot", "other": "stale"})
    await feed(cb_update(OWNER, action) if action == "cancel" else msg_update(OWNER, action))
    assert await state.get_state() is None and await state.get_data() == {}
    if action == "/start":
        assert plain(session.texts()[-1]) == WELCOME
    if action != "cancel":
        assert [[b.text for b in row] for row in session.of(SendMessage)[-1].reply_markup.keyboard] == MENU


@pytest.mark.parametrize("key", ["", "bogus", "clicker:extra", "Clicker", "<b>SECRET</b>", "123456:SECRET"])
@pytest.mark.parametrize("active", [None, CreateBot.token.state])
async def test_unknown_template_does_not_echo_or_change_state(selection, key, active, caplog):
    ctx, feed, session, state = selection
    await state.set_state(active)
    await state.set_data({"template": "refbot"})
    await feed(cb_update(OWNER, "tpl:" + key))
    assert await state.get_state() == active and await state.get_data() == {"template": "refbot"}
    assert session.of(AnswerCallbackQuery)[-1].text == "Шаблон не найден. Откройте «Создать бота» заново."
    assert not session.of(EditMessageText)
    assert "SECRET" not in caplog.text
    assert ctx.db.scalar("SELECT COUNT(*) FROM drafts") == 0


async def test_known_old_buttons_and_reselection_use_current_conditions(selection):
    ctx, feed, session, state = selection
    await feed(cb_update(OWNER, "create"))
    assert "Создание:" in session.texts()[-1]
    await feed(cb_update(OWNER, "tpl:clicker"))
    ctx.db.kv_set("price:PRICE_STARBOT", "9876")
    await feed(cb_update(OWNER, "tpl:starbot"))
    assert "Стоимость: 98,76 ₽" in plain(session.texts()[-1])
    assert await state.get_data() == {"template": "starbot"}
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 0


@pytest.mark.parametrize("route", ["create", "tpl:starbot"])
@pytest.mark.parametrize("failure", ["bad_request", "network"])
async def test_failed_edit_preserves_previous_state_and_hides_error(selection, route, failure, monkeypatch, caplog):
    ctx, feed, session, state = selection
    await state.set_state(CreateBot.token)
    await state.set_data({"template": "refbot"})
    original = session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            error = TelegramBadRequest if failure == "bad_request" else TelegramNetworkError
            raise error(method=method, message="SECRET_FAILURE")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", fail)
    await feed(cb_update(OWNER, route))
    assert await state.get_state() == CreateBot.token.state
    assert await state.get_data() == {"template": "refbot"}
    assert "Не удалось обновить экран" in session.of(AnswerCallbackQuery)[-1].text
    assert "SECRET_FAILURE" not in caplog.text


@pytest.mark.parametrize("route", ["create", "tpl:starbot"])
async def test_not_modified_edit_is_success(selection, route, monkeypatch):
    ctx, feed, session, state = selection
    original = session.make_request

    async def unchanged(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", unchanged)
    await feed(cb_update(OWNER, route))
    assert await state.get_state() == (None if route == "create" else CreateBot.token.state)
    assert not session.of(AnswerCallbackQuery)[-1].text


@pytest.mark.parametrize("route", ["create", "tpl:starbot"])
@pytest.mark.parametrize("message_kind", ["missing", "inaccessible"])
async def test_uneditable_callback_message_has_safe_answer(selection, route, message_kind):
    ctx, feed, session, state = selection
    update = cb_update(OWNER, route).model_dump(exclude_none=True)
    query = update["callback_query"]
    if message_kind == "missing":
        query.pop("message")
        query["inline_message_id"] = "old-inline"
    else:
        query["message"]["date"] = 0
        query["message"].pop("text")  # InaccessibleMessage contains no message body.
    from aiogram.types import Update
    await feed(Update.model_validate(update))
    assert await state.get_state() is None
    assert "Не удалось обновить экран" in session.of(AnswerCallbackQuery)[-1].text


@pytest.mark.parametrize("route", ["create", "tpl:starbot"])
async def test_changed_fsm_during_edit_is_not_overwritten(selection, route, monkeypatch):
    ctx, feed, session, state = selection
    await state.set_state(CreateBot.token)
    await state.set_data({"template": "refbot"})
    original = session.make_request

    async def change(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            await state.set_state("Wizard:active")
            await state.set_data({"keep": "current master"})
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", change)
    await feed(cb_update(OWNER, route))
    assert await state.get_state() == "Wizard:active"
    assert await state.get_data() == {"keep": "current master"}
    assert session.of(AnswerCallbackQuery)[-1].text == STALE


@pytest.mark.parametrize("route", ["menu", "create", "tpl:clicker"])
async def test_db_read_error_is_secret_safe(selection, route, monkeypatch, caplog):
    ctx, feed, session, state = selection

    def broken(key):
        raise sqlite3.OperationalError("SECRET_DB_ERROR")

    monkeypatch.setattr(ctx, "template_price", broken)
    await feed(msg_update(OWNER, "🔨 Создать бота") if route == "menu" else cb_update(OWNER, route))
    assert any(getattr(r, "text", None) == UNAVAILABLE for r in session.requests)
    assert await state.get_state() is None
    assert "SECRET_DB_ERROR" not in caplog.text
    assert not any("SECRET_DB_ERROR" in (getattr(r, "text", None) or "") for r in session.requests)


async def test_dynamic_template_metadata_is_escaped(selection, monkeypatch):
    from child import templates

    ctx, feed, session, state = selection
    original = templates.info("clicker")
    monkeypatch.setattr(templates.load("clicker"), "INFO", replace(
        original, title='<b>Title & "quoted"</b>', description="<script>description & text</script>", emoji="<"
    ))
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    assert "&lt;b&gt;Title &amp; &quot;quoted&quot;&lt;/b&gt;" in session.texts()[-1]
    await feed(cb_update(OWNER, "tpl:clicker"))
    text = session.texts()[-1]
    assert "&lt;script&gt;description &amp; text&lt;/script&gt;" in text
    assert "<script>" not in text
    assert text.startswith("&lt; <b>&lt;b&gt;")
    assert len(plain(text).encode("utf-16-le")) // 2 <= 4096


async def test_oversized_escaped_metadata_is_not_truncated_or_accepted(selection, monkeypatch):
    from child import templates

    ctx, feed, session, state = selection
    original = templates.info("clicker")
    monkeypatch.setattr(templates.load("clicker"), "INFO", replace(original, description="🌟" * 3000))
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert session.of(AnswerCallbackQuery)[-1].text == UNAVAILABLE
    assert await state.get_state() is None


async def test_largest_normal_prices_fit_without_rounding(selection):
    ctx, feed, session, state = selection
    for key in ("PRICE_CLICKER", "PRICE_REFBOT", "PRICE_STARBOT", "HOSTING_DAY_PRICE"):
        ctx.db.kv_set("price:" + key, "99999999999")
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    text = plain(session.texts()[-1])
    assert text.count("999 999 999,99 ₽") == 4
    assert len(text.encode("utf-16-le")) // 2 <= 4096


async def test_viewing_does_not_change_financial_tables_or_call_services(selection, monkeypatch):
    from creator.lavepay import LavePayService
    from tests.test_lavepay import FakeLave

    ctx, feed, session, state = selection
    await feed(msg_update(OWNER, "/start"))
    ctx.db.change_balance(OWNER, 100000, "topup", "fake fixture")
    ctx.db.create_draft(OWNER, "refbot", "FAKE-UNSENT-TOKEN", "fixture_bot", "Fixture")
    ctx.db.lave_new(OWNER, "topup:5000", 5000, "1.00")
    fake = FakeLave()
    ctx.lave = LavePayService(ctx, fake)
    start = AsyncMock(side_effect=AssertionError("No child should start while viewing"))
    monkeypatch.setattr(ctx.sup, "start", start)

    def snapshot():
        return {table: [tuple(row) for row in ctx.db.all("SELECT * FROM " + table + " ORDER BY 1")]
                for table in ("transactions", "drafts", "payments", "lave_invoices", "bots", "users", "kv")}

    before = snapshot()
    session.clear()
    for update in (
        msg_update(OWNER, "🔨 Создать бота"), cb_update(OWNER, "create"), cb_update(OWNER, "tpl:starbot"),
        cb_update(OWNER, "tpl:clicker"), cb_update(OWNER, "create"), msg_update(OWNER, "/start"),
        cb_update(OWNER, "cancel"),
    ):
        await feed(update)
    assert snapshot() == before
    assert not fake.calls
    start.assert_not_awaited()
    assert all(isinstance(r, (SendMessage, EditMessageText, AnswerCallbackQuery)) for r in session.requests)


async def test_referral_is_preserved_and_repeat_start_does_not_notify_twice(selection):
    ctx, feed, session, state = selection
    await feed(msg_update(50, "/start"))
    session.clear()
    await feed(msg_update(OWNER, "/start ref50"))
    assert ctx.db.user(OWNER)["referrer_id"] == 50
    assert len([m for m in session.of(SendMessage) if m.chat_id == 50]) == 1
    await feed(msg_update(OWNER, "/start ref50"))
    assert len([m for m in session.of(SendMessage) if m.chat_id == 50]) == 1
    assert ctx.db.user(50)["balance"] == 0


async def test_ban_and_admin_exception_remain(selection):
    ctx, feed, session, state = selection
    await feed(msg_update(OWNER, "/start"))
    ctx.db.set_banned(OWNER, True)
    session.clear()
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert not session.of(SendMessage) and not session.of(EditMessageText)
    assert session.of(AnswerCallbackQuery)[-1].text == "Доступ ограничен"
    ctx.db.upsert_user(777, "admin", "Admin")
    ctx.db.set_banned(777, True)
    await feed(msg_update(777, "/start"))
    assert plain(session.texts()[-1]) == WELCOME


@pytest.mark.parametrize("argument", ["sub_999", "sub_notanid"])
async def test_subscription_deep_link_not_intercepted_by_welcome(selection, argument):
    ctx, feed, session, state = selection
    await state.set_state(CreateBot.token)
    await state.set_data({"template": "clicker"})
    await feed(msg_update(OWNER, "/start " + argument))
    assert "Бот не найден" in session.texts()[-1]
    assert "Добро пожаловать" not in session.texts()[-1]
    assert await state.get_state() is None and await state.get_data() == {}
    assert ctx.db.scalar("SELECT COUNT(*) FROM drafts") == 0


async def test_help_path_without_invented_links(selection):
    ctx, feed, session, state = selection
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "📰 Информация"))
    buttons = [b for row in session.of(SendMessage)[-1].reply_markup.inline_keyboard for b in row]
    assert not any(b.url for b in buttons)
    assert any(b.callback_data == "info:report" and "Оповестить о проблеме" in b.text for b in buttons)
    await feed(cb_update(OWNER, "info:admin"))
    assert "Оповестить о проблеме" in session.of(AnswerCallbackQuery)[-1].text
    await feed(cb_update(OWNER, "info:report"))
    assert await state.get_state() == "Info:report"
    assert ctx.db.scalar("SELECT COUNT(*) FROM reports") == 0


async def test_price_changes_keep_existing_token_and_balance_semantics(selection, monkeypatch):
    ctx, feed, session, state = selection
    await feed(msg_update(OWNER, "/start"))
    ctx.db.change_balance(OWNER, 100000, "topup", "fake fixture")
    ctx.db.kv_set("price:PRICE_CLICKER", "12300")
    await feed(msg_update(OWNER, "🔨 Создать бота"))
    assert "Создание: 123 ₽" in session.texts()[-1]
    ctx.db.kv_set("price:PRICE_CLICKER", "23400")
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert "Стоимость: 234 ₽" in plain(session.texts()[-1])
    ctx.db.kv_set("price:PRICE_CLICKER", "34500")
    validator = AsyncMock(return_value=("fixture_bot", "Fixture"))
    monkeypatch.setattr(ctx, "validate_token", validator)
    await feed(msg_update(OWNER, "123456789:AAEexampleexampleexampleexample12345"))
    assert "К оплате: 345 ₽" in plain(session.texts()[-1])
    draft = ctx.db.scalar("SELECT MAX(id) FROM drafts")
    assert ctx.expected_amount(OWNER, f"create:{draft}")[0] == 34500
    await feed(cb_update(200, f"bal:create:{draft}"))
    assert ctx.db.user(OWNER)["balance"] == 100000
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 0
    ctx.db.kv_set("price:PRICE_CLICKER", "45600")
    start = AsyncMock()
    monkeypatch.setattr(ctx.sup, "start", start)  # Actual supervised processes are never launched here.
    await feed(cb_update(OWNER, f"bal:create:{draft}"))
    assert ctx.db.user(OWNER)["balance"] == 54400
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    start.assert_awaited_once()
    await feed(cb_update(OWNER, f"bal:create:{draft}"))
    assert ctx.db.user(OWNER)["balance"] == 54400
    assert ctx.db.scalar("SELECT COUNT(*) FROM bots") == 1
    assert validator.await_count == 1


async def test_existing_lave_invoice_is_not_repriced_by_views(selection, monkeypatch):
    from creator.lavepay import LavePayService
    from tests.test_lavepay import FakeLave

    ctx, feed, session, state = selection
    fake = FakeLave()
    ctx.lave = LavePayService(ctx, fake)
    monkeypatch.setattr(ctx, "validate_token", AsyncMock(return_value=("fixture_bot", "Fixture")))
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "tpl:clicker"))
    await feed(msg_update(OWNER, "123456789:AAEexampleexampleexampleexample12345"))
    draft = ctx.db.scalar("SELECT MAX(id) FROM drafts")
    ctx.db.kv_set("price:PRICE_CLICKER", "34500")
    await feed(cb_update(OWNER, f"lp:c:{draft}"))
    invoice = dict(ctx.db.lave_get(1))
    assert invoice["amount"] == 34500
    calls = list(fake.calls)
    ctx.db.kv_set("price:PRICE_CLICKER", "56700")
    await feed(cb_update(OWNER, "create"))
    await feed(cb_update(OWNER, "tpl:clicker"))
    assert "Стоимость: 567 ₽" in plain(session.texts()[-1])
    assert dict(ctx.db.lave_get(1)) == invoice
    assert fake.calls == calls
    assert not session.of(SendInvoice)
