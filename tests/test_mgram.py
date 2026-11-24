"""Заработок на рассылках: экран, порог 50 пользователей, регистрация, ключ один раз."""
from __future__ import annotations

import sqlite3

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import OWNER, TOKEN, create_paid_bot, env  # noqa: F401
from tests.test_help_contacts import kb

STRANGER = 777001


def _seed_users(ctx, bot_id: int, n: int) -> None:
    path = ctx.sup.bot_dir(bot_id) / "bot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM users")
    conn.executemany("INSERT INTO users(id) VALUES (?)", [(i,) for i in range(1, n + 1)])
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_mgram_not_configured_is_graceful(env):  # noqa: F811
    ctx, feed, session = env
    assert not ctx.cfg.mgram_partner_api_key
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:open"))
    card = [m for m in session.requests if getattr(m, "reply_markup", None)][-1]
    assert ("📢 Заработок на рассылках", f"b:{bot_id}:mgram") in kb(card)
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram"))
    screen = session.of(EditMessageText)[-1]
    assert "Заработок на рассылках" in (screen.text or "")
    assert "Сервис пока не подключён в конструкторе" in (screen.text or "")
    assert "Обратитесь к администратору" in (screen.text or "")
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram_reg"))
    alerts = [a for a in session.of(AnswerCallbackQuery) if a.text]
    assert any(a.text == "Сервис не подключён" and a.show_alert for a in alerts)
    assert ctx.db.mgram_get(bot_id) is None


@pytest.mark.asyncio
async def test_mgram_requires_fifty_users(env, monkeypatch):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.mgram_partner_api_key = "test-partner-key"
    called = []

    async def boom(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("register must not be called under the user threshold")

    monkeypatch.setattr("creator.mgram_factory.MgramFactory.register_bot", boom)
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    _seed_users(ctx, bot_id, 49)
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram"))
    screen = session.of(EditMessageText)[-1]
    assert "49" in (screen.text or "")
    assert "50" in (screen.text or "")
    assert "Подключить" not in " ".join(t for t, _ in kb(screen))
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram_reg"))
    alerts = [a for a in session.of(AnswerCallbackQuery) if a.text]
    assert any("минимум 50" in (a.text or "") and a.show_alert for a in alerts)
    assert not called
    assert ctx.db.mgram_get(bot_id) is None


@pytest.mark.asyncio
async def test_mgram_register_shows_access_key_once_and_does_not_store_it(env, monkeypatch):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.mgram_partner_api_key = "test-partner-key"
    calls = []

    async def fake_register(self, token, owner_id, referrer_id=None):
        calls.append({"token": token, "owner_id": owner_id, "referrer_id": referrer_id})
        return {
            "bot_id": 555,
            "username": "demo_bot",
            "status": "на модерации",
            "category": "IT",
            "api_key": "once-only-access-key",
        }

    monkeypatch.setattr("creator.mgram_factory.MgramFactory.register_bot", fake_register)
    await feed(msg_update(OWNER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    _seed_users(ctx, bot_id, 50)
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram"))
    screen = session.of(EditMessageText)[-1]
    assert "достаточно для подключения" in (screen.text or "")
    assert ("✅ Подключить", f"b:{bot_id}:mgram_reg") in kb(screen)
    session.requests.clear()
    await feed(cb_update(OWNER, f"b:{bot_id}:mgram_reg"))
    done = session.of(EditMessageText)[-1]
    assert "подключён к системе заработка на рассылках" in (done.text or "")
    assert "once-only-access-key" in (done.text or "")
    assert "Ключ доступа бота (показывается один раз)" in (done.text or "")
    assert calls and calls[0]["token"] == TOKEN and calls[0]["owner_id"] == OWNER
    row = ctx.db.mgram_get(bot_id)
    assert row is not None
    assert row["mgram_bot_id"] == 555
    assert row["status"] == "на модерации"
    assert row["api_key_shown"] == 1
    assert "once-only-access-key" not in str(row)
    assert "api_key" not in row or row.get("api_key") in (None, "", 0, 1)
    from creator.integration_keys import value
    assert value(ctx.cfg, "mgram_hub") == ""
    assert not (ctx.cfg.data_dir / "mgram_hub.json").exists()
    assert not (ctx.cfg.data_dir / "integration_keys" / "mgram_hub.json").exists()


@pytest.mark.asyncio
async def test_mgram_rejects_stranger(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.mgram_partner_api_key = "test-partner-key"
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(STRANGER, "/start"))
    bot_id = await create_paid_bot(ctx, feed, session)
    session.requests.clear()
    await feed(cb_update(STRANGER, f"b:{bot_id}:mgram"))
    alerts = [a for a in session.of(AnswerCallbackQuery) if a.text]
    assert any(a.text == "Бот не найден" and a.show_alert for a in alerts)
    assert not session.of(EditMessageText) or all(
        "Заработок на рассылках" not in (m.text or "") for m in session.of(EditMessageText)
    )


def test_mgram_schema_is_additive():
    from creator.db import SCHEMA

    assert "CREATE TABLE IF NOT EXISTS mgram_bots" in SCHEMA
    assert "DROP TABLE" not in SCHEMA
    # The access key must never be a stored column.
    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS mgram_bots")
    end = SCHEMA.index(";", start)
    table = SCHEMA[start:end]
    assert "api_key_shown" in table
    assert "api_key INTEGER" not in table and "api_key TEXT" not in table


def test_load_config_reads_partner_key_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MGRAM_PARTNER_API_KEY", "  env-test-key  ")
    from creator.config import load_config

    cfg = load_config(tmp_path / "missing.env")
    assert cfg.mgram_partner_api_key == "env-test-key"
    assert cfg.mgram_enabled is True
    monkeypatch.setenv("MGRAM_PARTNER_API_KEY", "")
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.mgram_partner_api_key == ""
    assert cfg.mgram_enabled is False


def test_bot_card_includes_mgram_button():
    from creator.keyboards import bot_card_kb

    kb_markup = bot_card_kb(
        {"id": 4, "template": "clicker", "desired": 1, "status": "running", "sale_price": None},
        False,
    )
    texts = [b.text for row in kb_markup.inline_keyboard for b in row]
    assert "📢 Заработок на рассылках" in texts
    assert "💳 LavePay выплаты" in texts
