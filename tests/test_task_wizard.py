"""D2: step-by-step own task wizard (Star bot); same fields/validation, nothing saved before confirmation."""
from aiogram.methods import AnswerCallbackQuery, SendMessage

from child.files import read_config, write_config
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, make


def last(session):
    return session.of(SendMessage)[-1]


def buttons(message):
    m = message.reply_markup
    return [(b.text, b.callback_data) for row in m.inline_keyboard for b in row] if m and hasattr(m, "inline_keyboard") else []


def count(app):
    return app.db.scalar("SELECT COUNT(*) FROM tasks")


async def start(tmp_path, bot):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "adm:tasks"))
    await feed(cb_update(OWNER, "tadd"))
    return app, feed


async def test_full_wizard_without_channel(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    assert "Шаг 1/4" in last(session).text
    await feed(msg_update(OWNER, "<b>Канал</b>"))
    assert "Шаг 2/4" in last(session).text
    await feed(msg_update(OWNER, "ftp://x"))
    assert "http://" in last(session).text and "Шаг 2/4" in last(session).text
    await feed(msg_update(OWNER, "https://t.me/x"))
    assert "Шаг 3/4" in last(session).text and "⭐" in last(session).text
    for bad in ("0", "-1", "abc", "inf", "nan"):
        await feed(msg_update(OWNER, bad))
        assert "Шаг 3/4" in last(session).text
    await feed(msg_update(OWNER, "1,5"))
    assert "Шаг 4/4" in last(session).text
    assert ("⏭ Без проверки подписки", "tskip") in buttons(last(session))
    await feed(cb_update(OWNER, "tskip"))
    preview = last(session)
    assert "&lt;b&gt;Канал&lt;/b&gt;" in preview.text and "https://t.me/x" in preview.text
    assert "1.5 ⭐" in preview.text and "без проверки подписки" in preview.text
    assert buttons(preview) == [("✅ Сохранить", "tsave"), ("❌ Отмена", "tcancel")]
    assert count(app) == 0
    await feed(cb_update(OWNER, "tsave"))
    row = app.db.one("SELECT title,url,channel,reward,active FROM tasks")
    assert tuple(row) == ("<b>Канал</b>", "https://t.me/x", "", 1.5, 1)
    await feed(cb_update(OWNER, "tsave"))  # stale: nothing more saved
    assert count(app) == 1 and "устарел" in session.of(AnswerCallbackQuery)[-1].text


async def test_channel_step_validation(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    for text in ("Подписка", "https://t.me/chan", "2"):
        await feed(msg_update(OWNER, text))
    await feed(msg_update(OWNER, "chan"))
    assert "Шаг 4/4" in last(session).text
    await feed(msg_update(OWNER, "@chan"))
    assert "@chan" in last(session).text
    await feed(cb_update(OWNER, "tsave"))
    assert tuple(app.db.one("SELECT channel,reward FROM tasks")) == ("@chan", 2.0)


async def test_cancel_paths_save_nothing(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    await feed(msg_update(OWNER, "Название"))
    await feed(msg_update(OWNER, "/cancel"))
    await feed(msg_update(OWNER, "https://t.me/x"))  # not treated as a wizard step any more
    assert count(app) == 0
    await feed(cb_update(OWNER, "tadd"))
    await feed(msg_update(OWNER, "A | https://t.me/x | 1"))
    await feed(cb_update(OWNER, "tcancel"))
    assert "не сохранено" in last(session).text
    await feed(cb_update(OWNER, "tsave"))
    assert count(app) == 0


async def test_one_line_format_goes_to_preview(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    await feed(msg_update(OWNER, "Подписка | https://t.me/x | 2 | @x"))
    assert "Проверьте задание" in last(session).text and count(app) == 0
    await feed(cb_update(OWNER, "tsave"))
    assert tuple(app.db.one("SELECT title,url,channel,reward FROM tasks")) == ("Подписка", "https://t.me/x", "@x", 2.0)


async def test_menu_button_cancels_wizard(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    await feed(msg_update(OWNER, "📋 Задания"))
    assert "отменено" in session.texts()[-1] or "отменено" in " ".join(session.texts()[-2:])
    await feed(msg_update(OWNER, "https://t.me/x"))
    assert count(app) == 0


async def test_non_owner_and_owner_change(tmp_path, bot, session):
    app, feed = await start(tmp_path, bot)
    await feed(msg_update(A, "/start"))
    await feed(cb_update(A, "tadd"))
    await feed(msg_update(A, "X | https://t.me/x | 1"))
    await feed(cb_update(A, "tsave"))
    assert count(app) == 0
    await feed(msg_update(OWNER, "Y | https://t.me/y | 1"))
    cfg = read_config(tmp_path)
    cfg["owner_id"] = 999
    write_config(tmp_path, cfg)
    await feed(cb_update(OWNER, "tsave"))
    assert count(app) == 0
