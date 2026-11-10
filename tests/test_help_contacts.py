"""B4: help screen and validated support links; nothing is sent, nothing is stored."""
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
    assert "«Информация» → «❓ Помощь»" in session.of(SendMessage)[-1].text
    await feed(msg_update(OWNER, "📰 Информация"))
    assert ("❓ Помощь", "info:help") in kb(session.of(SendMessage)[-1])
    before = len(session.requests)
    await feed(cb_update(OWNER, "info:help"))
    screen = session.of(EditMessageText)[-1]
    for part in ("Запуск", "Хостинг", "Токен", "Выплаты в ваших ботах", "/cancel"):
        assert part in screen.text
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
