"""B4: help screen and validated support links; nothing is sent, nothing is stored."""
from pathlib import Path

import pytest
from aiogram.methods import EditMessageText, SendMessage

from creator.config import Config, load_config, support_url
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, env  # noqa: F401

CHAT = "https://t.me/+lXe1stAMZGZmMjhi"
CHANNEL = "https://t.me/+g76YNBcBSjNlMzEy"


@pytest.mark.parametrize("raw,expected", [
    (CHAT, CHAT), (CHANNEL, CHANNEL), ("t.me/DDDDEVELOPER", "https://t.me/DDDDEVELOPER"),
    (" https://t.me/DDDDEVELOPER/ ", "https://t.me/DDDDEVELOPER"), ("https://telegram.me/chat", "https://t.me/chat"),
    ("http://t.me/chat", ""), ("https://evil.example/chat", ""), ("javascript:alert(1)", ""),
    ("https://t.me/a b", ""), ("https://t.me/ab", ""), ("https://t.me/chat?start=x", ""), ("", ""),
    ("https://t.me/+short", ""), ("https://t.me.evil.example/chat", ""),
])
def test_support_url(raw, expected):
    assert support_url(raw) == expected


def test_load_config_validates(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_URL", CHAT)
    monkeypatch.setenv("CHANNEL_URL", "https://evil.example/x")
    monkeypatch.setenv("ADMIN_URL", "t.me/DDDDEVELOPER")
    cfg = load_config(tmp_path / "missing.env")
    assert (cfg.chat_url, cfg.channel_url, cfg.admin_url) == (CHAT, "", "https://t.me/DDDDEVELOPER")


def kb(message):
    return [(b.text, b.url or b.callback_data) for row in message.reply_markup.inline_keyboard for b in row]


async def test_help_screen_with_links(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.chat_url, ctx.cfg.channel_url, ctx.cfg.admin_url = CHAT, CHANNEL, "https://t.me/DDDDEVELOPER"
    await feed(msg_update(OWNER, "/start"))
    assert "«📰 Информация» → «❓ Помощь»" in session.of(SendMessage)[-1].text
    await feed(msg_update(OWNER, "📰 Информация"))
    assert ("❓ Помощь", "info:help") in kb(session.of(SendMessage)[-1])
    assert ("📄 Документы", "info:docs") in kb(session.of(SendMessage)[-1])
    before = len(session.requests)
    await feed(cb_update(OWNER, "info:help"))
    screen = session.of(EditMessageText)[-1]
    from creator.lavepay_signup import LAVEPAY_SIGNUP_URL
    for part in ("Как создать бота", "Хостинг", "Токен", "Выплаты пользователям", "/cancel", "заданий"):
        assert part in screen.text
    assert f'<a href="{LAVEPAY_SIGNUP_URL}">LavePay</a>' in screen.text
    assert LAVEPAY_SIGNUP_URL not in screen.text.replace(f'href="{LAVEPAY_SIGNUP_URL}"', "")
    assert screen.disable_web_page_preview is True
    assert kb(screen) == [("💬 Чат", CHAT), ("📢 Канал", CHANNEL), ("👑 Администратор", "https://t.me/DDDDEVELOPER"),
                          ("⛔ Оповестить о проблеме", "info:report"), ("⬅️ Назад", "info:back")]
    new = session.requests[before:]
    assert not [m for m in new if isinstance(m, SendMessage)]  # nothing sent to anyone
    await feed(cb_update(OWNER, "info:back"))
    assert "Работаем:" in session.of(EditMessageText)[-1].text


async def test_help_without_links_invents_nothing(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.chat_url = ctx.cfg.channel_url = ctx.cfg.admin_url = ""
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "info:help"))
    screen = session.of(EditMessageText)[-1]
    assert "Оповестить о проблеме" in screen.text
    assert kb(screen) == [("⛔ Оповестить о проблеме", "info:report"), ("⬅️ Назад", "info:back")]


async def test_invalid_configured_link_is_not_rendered(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.chat_url = "https://evil.example/x"
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "📰 Информация"))
    assert all("evil" not in (v or "") for _, v in kb(session.of(SendMessage)[-1]))


async def test_admin_unaffected(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/start"))
    await feed(cb_update(ADMIN, "info:help"))
    assert "Помощь" in session.of(EditMessageText)[-1].text
    assert isinstance(Config, type)


@pytest.mark.asyncio
async def test_help_command_and_card_tips(env):  # noqa: F811
    from aiogram.methods import EditMessageText, SendMessage

    from creator.lavepay_signup import LAVEPAY_SIGNUP_URL
    from tests.test_creator_flow import OWNER, create_paid_bot

    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.requests.clear()
    await feed(msg_update(OWNER, "/help"))
    help_msg = session.of(SendMessage)[-1]
    assert "Как создать бота" in (help_msg.text or "")
    assert f'<a href="{LAVEPAY_SIGNUP_URL}">LavePay</a>' in (help_msg.text or "")
    assert help_msg.disable_web_page_preview is True
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:tips"))
    tips = [m for m in session.requests if isinstance(m, (SendMessage, EditMessageText))
            and "Подсказки по карточке" in (m.text or "")][-1]
    assert f'<a href="{LAVEPAY_SIGNUP_URL}">LavePay</a>' in (tips.text or "")
    assert LAVEPAY_SIGNUP_URL not in (tips.text or "").replace(f'href="{LAVEPAY_SIGNUP_URL}"', "")
    assert tips.disable_web_page_preview is True


def test_legal_pdf_files_present():
    from creator.lavepay_signup import LAVEPAY_SIGNUP_URL
    from creator.legal import DOCS, FILES

    assert set(FILES) == set(DOCS) == {"agreement", "privacy", "offer"}
    for path in FILES.values():
        assert path.is_file()
        assert path.read_bytes()[:5] == b"%PDF-"
    for _title, body in DOCS.values():
        assert LAVEPAY_SIGNUP_URL not in body


@pytest.mark.asyncio
async def test_documents_menu_and_pdf(env):  # noqa: F811
    from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage

    from creator.legal import FILES

    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OWNER, "📰 Информация"))
    assert ("📄 Документы", "info:docs") in kb(session.of(SendMessage)[-1])

    await feed(cb_update(OWNER, "info:docs"))
    menu = session.of(EditMessageText)[-1]
    assert "Документы" in (menu.text or "")
    labels = kb(menu)
    assert ("📜 Пользовательское соглашение", "info:doc:agreement") in labels
    assert ("🔒 Политика конфиденциальности", "info:doc:privacy") in labels
    assert ("📋 Публичная оферта", "info:doc:offer") in labels

    await feed(cb_update(OWNER, "info:doc:agreement"))
    doc = session.of(EditMessageText)[-1]
    assert "Пользовательское соглашение" in (doc.text or "")
    assert "1. Общие положения" in (doc.text or "")
    assert ("📥 Скачать полную версию (PDF)", "info:pdf:agreement") in kb(doc)

    session.requests.clear()
    await feed(cb_update(OWNER, "info:pdf:agreement"))
    sent = session.of(SendDocument)[-1]
    path = getattr(sent.document, "path", None)
    assert path is not None and Path(path).resolve() == FILES["agreement"].resolve()
    assert "Пользовательское соглашение" in (sent.caption or "")

    session.requests.clear()
    await feed(cb_update(OWNER, "info:doc:missing"))
    alerts = [a for a in session.of(AnswerCallbackQuery) if a.text]
    assert any(a.text == "Документ не найден" and a.show_alert for a in alerts)

    session.requests.clear()
    await feed(cb_update(OWNER, "info:pdf:missing"))
    alerts = [a for a in session.of(AnswerCallbackQuery) if a.text]
    assert any(a.text == "Файл временно недоступен" and a.show_alert for a in alerts)
