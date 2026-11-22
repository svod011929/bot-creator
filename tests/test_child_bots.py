"""Сценарии дочерних ботов (кликер, реф, звёздный) на фейковой сессии."""
import time

import pytest
from aiogram.methods import CopyMessage, SendMessage

from child.__main__ import build_dispatcher
from child.core import ChildApp
from child.files import write_config, write_setting
from tests.conftest import cb_update, msg_update

OWNER = 1
A, B = 10, 20


def make(tmp_path, template, bot):
    write_config(tmp_path, {"token": "42:TEST", "template": template, "owner_id": OWNER, "username": "kid_bot",
                            "vip": False, "creator_username": "creator_bot"})
    app = ChildApp(tmp_path)
    dp = build_dispatcher(app)

    async def feed(update):
        await dp.feed_update(bot, update)

    return app, feed


async def test_clicker(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    await feed(msg_update(A, "/start"))
    welcome = session.of(SendMessage)[-1]
    assert "Бот создан в @creator_bot" in welcome.text
    kb = [b.text for row in welcome.reply_markup.keyboard for b in row]
    assert "👆 Кликать" in kb and "💸 Вывод" in kb and "⚙️ Админка" not in kb

    await feed(cb_update(A, "tap"))
    assert app.db.user(A)["balance"] == 1 and app.db.user(A)["clicks"] == 1
    await feed(cb_update(A, "tap"))  # слишком быстро — антиспам
    assert app.db.user(A)["clicks"] == 1
    time.sleep(0.3)
    app.set("click_reward", 200)
    await feed(cb_update(A, "tap"))
    assert app.db.user(A)["balance"] == 201

    await feed(cb_update(A, "upg"))  # 100 * 1^2
    assert app.db.user(A)["level"] == 2 and app.db.user(A)["balance"] == 101
    await feed(cb_update(A, "upg"))  # 100 * 2^2 = 400 — не хватает
    assert app.db.user(A)["level"] == 2

    await feed(msg_update(A, "🏆 Топ"))
    assert "Топ игроков" in session.texts()[-1]


async def test_withdraw_flow(tmp_path, bot, session):
    app, feed = make(tmp_path, "clicker", bot)
    app.set("min_withdraw", 50)
    await feed(msg_update(A, "/start"))
    await feed(msg_update(A, "💸 Вывод"))
    assert "Минимальная сумма" in session.texts()[-1]
    app.db.add_balance(A, 100)
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "500"))
    assert "Сумма должна быть" in session.texts()[-1]
    await feed(msg_update(A, "60"))
    await feed(msg_update(A, "card 4444"))
    assert app.db.user(A)["balance"] == 40
    owner_msg = [m for m in session.of(SendMessage) if m.chat_id == OWNER][-1]
    assert "Заявка на вывод №1" in owner_msg.text
    # отклонение возвращает деньги
    await feed(cb_update(OWNER, "wd:no:1"))
    assert app.db.user(A)["balance"] == 100
    await feed(cb_update(OWNER, "wd:ok:1"))  # повторно не обрабатывается
    assert app.db.one("SELECT status FROM withdrawals WHERE id=1")["status"] == "rejected"
    # не-владелец не может решать заявки
    await feed(msg_update(A, "💸 Вывод"))
    await feed(msg_update(A, "50"))
    await feed(msg_update(A, "x"))
    await feed(cb_update(A, "wd:ok:2"))
    assert app.db.one("SELECT status FROM withdrawals WHERE id=2")["status"] == "pending"
    await feed(cb_update(OWNER, "wd:ok:2"))
    assert app.db.one("SELECT status FROM withdrawals WHERE id=2")["status"] == "paid"


async def test_refbot_gate_and_reward(tmp_path, bot, session):
    app, feed = make(tmp_path, "refbot", bot)
    write_setting(tmp_path, "required_channel", "@chan")
    await feed(msg_update(A, "/start"))
    session.member_status = "left"
    await feed(msg_update(B, f"/start r{A}"))
    assert "подпишитесь" in session.texts()[-1]
    assert app.db.user(A)["balance"] == 0  # награда только после подписки
    await feed(msg_update(B, "👥 Пригласить друзей"))
    assert "подпишитесь" in session.texts()[-1]
    await feed(cb_update(B, "chk_sub"))
    assert app.db.user(A)["balance"] == 0
    session.member_status = "member"
    await feed(cb_update(B, "chk_sub"))
    assert app.db.user(A)["balance"] == 5
    await feed(cb_update(B, "chk_sub"))  # повторно не начисляется
    assert app.db.user(A)["balance"] == 5
    await feed(msg_update(A, "🏆 Топ рефоводов"))
    assert "— 1 👥" in session.texts()[-1]


async def test_starbot_bonus_tasks_admin(tmp_path, bot, session):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(OWNER, "/start"))
    kb = [b.text for row in session.of(SendMessage)[-1].reply_markup.keyboard for b in row]
    assert "⚙️ Админка" in kb
    await feed(msg_update(A, "/start"))
    await feed(msg_update(A, "🎁 Ежедневный бонус"))
    assert app.db.user(A)["balance"] == 0.5
    await feed(msg_update(A, "🎁 Ежедневный бонус"))
    assert app.db.user(A)["balance"] == 0.5 and "Следующий бонус" in session.texts()[-1]

    # владелец добавляет задание
    await feed(cb_update(OWNER, "adm:tasks"))
    await feed(cb_update(OWNER, "tadd"))
    await feed(msg_update(OWNER, "Подписка | https://t.me/x | 2 | @x"))
    await feed(cb_update(OWNER, "tsave"))
    assert app.db.scalar("SELECT COUNT(*) FROM tasks") == 1
    await feed(msg_update(A, "📋 Задания"))
    session.member_status = "left"
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 0.5
    session.member_status = "member"
    await feed(cb_update(A, "task:1"))
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 2.5

    # не-владелец не видит админку
    session.clear()
    await feed(msg_update(A, "/admin"))
    assert not any("Панель владельца" in t for t in session.texts())

    # настройки владельцем
    await feed(cb_update(OWNER, "set:daily_bonus"))
    await feed(msg_update(OWNER, "1,5"))
    assert app.s["daily_bonus"] == 1.5

    # статистика и рассылка
    await feed(cb_update(OWNER, "adm:stats"))
    assert "Пользователей: 2" in session.texts()[-1]
    await feed(cb_update(OWNER, "adm:bc"))
    await feed(msg_update(OWNER, "Всем привет"))
    import asyncio
    for _ in range(50):
        if len(session.of(CopyMessage)) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(session.of(CopyMessage)) == 2


@pytest.mark.parametrize("template", ["clicker", "refbot", "starbot"])
async def test_vip_hides_footer(tmp_path, bot, session, template):
    write_config(tmp_path, {"token": "42:TEST", "template": template, "owner_id": OWNER, "username": "kid_bot",
                            "vip": True, "creator_username": "creator_bot"})
    app = ChildApp(tmp_path)
    dp = build_dispatcher(app)
    await dp.feed_update(bot, msg_update(A, "/start"))
    assert "Бот создан" not in session.of(SendMessage)[-1].text
