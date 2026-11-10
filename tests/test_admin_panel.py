"""Admin-only main-menu entry and read-only admin navigation (fake Telegram session)."""
import hashlib
import time

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, env  # noqa: F401

MENU = ["🔨 Создать бота", "🤖 Мои боты", "💼 Мой кабинет", "👥 Партнёры", "🛒 Перепродажа", "📰 Информация",
        "📚 Контент ботов"]
ADMIN_BTN = "🛠 Админ-панель"


def reply_kb(m):
    return [b.text for row in m.reply_markup.keyboard for b in row]


def inline(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


def seed_bots(ctx, n, owner=OWNER, status="running"):
    ctx.db.upsert_user(owner, "sample_owner", "Sample Owner")
    with ctx.db.tx() as c:
        for i in range(n):
            c.execute("INSERT INTO bots(owner_id,template,token,username,title,status,desired,created_at) "
                      "VALUES(?,?,?,?,?,?,1,?)", (owner, "clicker", f"t{i}", f"Sample_{i}_bot", "x", status, int(time.time())))
    return [r["id"] for r in ctx.db.all("SELECT id FROM bots ORDER BY id")]


def snapshot(ctx):
    rows = []
    for (name,) in ctx.db.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows.append((name, sorted(map(repr, map(tuple, ctx.db.all(f'SELECT * FROM "{name}"'))))))
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def group(uid, text):
    return msg_update(uid, text, chat={"id": -100123, "type": "group", "title": "g"})


async def test_menu_shows_admin_button_only_to_admins(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    assert reply_kb(session.of(SendMessage)[-1]) == MENU
    await feed(msg_update(ADMIN, "/start"))
    assert reply_kb(session.of(SendMessage)[-1]) == MENU + [ADMIN_BTN]
    await feed(msg_update(ADMIN, "/cancel"))
    assert reply_kb(session.of(SendMessage)[-1]) == MENU + [ADMIN_BTN]
    await feed(msg_update(OWNER, "/cancel"))
    assert reply_kb(session.of(SendMessage)[-1]) == MENU
    ctx.cfg.admin_ids.discard(ADMIN)
    await feed(msg_update(ADMIN, "/start"))
    assert reply_kb(session.of(SendMessage)[-1]) == MENU


async def test_admin_button_opens_panel_and_resets_own_state(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/start"))
    await feed(cb_update(ADMIN, "tpl:clicker"))  # waiting for a token
    session.clear()
    await feed(msg_update(ADMIN, ADMIN_BTN))
    assert "Админ-панель" in session.texts()[-1]
    assert not ctx.db.scalar("SELECT COUNT(*) FROM drafts")
    await feed(msg_update(ADMIN, "123456789:AAEexampleexampleexampleexample12345"))
    assert not ctx.db.scalar("SELECT COUNT(*) FROM drafts")  # token FSM was left


async def test_non_admin_and_removed_admin_get_nothing(env):  # noqa: F811
    ctx, feed, session = env
    seed_bots(ctx, 2)
    await feed(msg_update(OWNER, "/start"))
    ctx.db.upsert_user(ADMIN, None, "Admin")
    before = snapshot(ctx)
    session.clear()
    for upd in (msg_update(OWNER, ADMIN_BTN), msg_update(OWNER, "/admin"), cb_update(OWNER, "a:main"),
                cb_update(OWNER, "a:bots:0"), cb_update(OWNER, "a:bot:1"), cb_update(OWNER, f"a:user:{OWNER}"),
                cb_update(OWNER, "a:botfind")):
        await feed(upd)
    assert not any("Админ" in (t or "") or "Бот ID" in (t or "") for t in session.texts())
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:botfind"))
    ctx.cfg.admin_ids.discard(ADMIN)
    session.clear()
    await feed(msg_update(ADMIN, "1"))
    await feed(cb_update(ADMIN, "a:bot:1"))
    await feed(msg_update(ADMIN, ADMIN_BTN))
    assert not any("Бот ID" in (t or "") or "Админ-панель" in (t or "") for t in session.texts())
    assert snapshot(ctx) == before


async def test_group_chat_is_refused(env):  # noqa: F811
    ctx, feed, session = env
    await feed(group(ADMIN, "/admin"))
    await feed(group(ADMIN, ADMIN_BTN))
    assert not any("Админ-панель" in (t or "") for t in session.texts())


async def test_overview_counts_recorded_status_without_money(env):  # noqa: F811
    ctx, feed, session = env
    seed_bots(ctx, 3)
    seed_bots(ctx, 1, status="error")
    await feed(msg_update(ADMIN, "/admin"))
    text = session.texts()[-1]
    assert "ботов 4" in text and "running 3" in text and "error 1" in text
    assert "записанный статус" in text and "₽" not in text


async def test_bot_list_paging_is_bounded(env):  # noqa: F811
    ctx, feed, session = env
    ids = seed_bots(ctx, 23)
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:bots"))
    first = inline(session.of(EditMessageText)[-1])
    shown = [d for _, d in first if d.startswith("a:bot:")]
    assert shown == [f"a:bot:{i}" for i in sorted(ids, reverse=True)[:10]]
    assert ("➡️", "a:bots:1") in first and "1/3" in session.of(EditMessageText)[-1].text
    await feed(cb_update(ADMIN, "a:bots:2"))
    last = inline(session.of(EditMessageText)[-1])
    assert [d for _, d in last if d.startswith("a:bot:")] == [f"a:bot:{i}" for i in sorted(ids, reverse=True)[20:]]
    assert ("⬅️", "a:bots:1") in last and not any(d == "a:bots:3" for _, d in last)
    await feed(cb_update(ADMIN, "a:bots:99"))
    assert "3/3" in session.of(EditMessageText)[-1].text
    await feed(cb_update(ADMIN, "a:bots:x"))
    assert session.of(AnswerCallbackQuery)[-1].text == "Кнопка устарела"


async def test_bot_lookup_by_id_and_username(env):  # noqa: F811
    ctx, feed, session = env
    ids = seed_bots(ctx, 3)
    await feed(msg_update(ADMIN, "/admin"))
    before = snapshot(ctx)
    await feed(cb_update(ADMIN, "a:botfind"))
    await feed(msg_update(ADMIN, str(ids[1])))
    assert f"Бот ID {ids[1]}" in session.texts()[-1]
    await feed(cb_update(ADMIN, "a:botfind"))
    await feed(msg_update(ADMIN, "@sample_2_BOT"))
    assert f"Бот ID {ids[2]}" in session.texts()[-1]
    await feed(cb_update(ADMIN, "a:botfind"))
    await feed(msg_update(ADMIN, "999999"))
    assert "не найден" in session.texts()[-1]
    await feed(msg_update(ADMIN, "<b>bad name</b>"))
    assert "<b>" not in session.texts()[-1] and "ID" in session.texts()[-1]
    await feed(msg_update(ADMIN, "/cancel"))
    session.clear()
    await feed(msg_update(ADMIN, str(ids[0])))
    assert not any("Бот ID" in (t or "") for t in session.texts())
    assert snapshot(ctx) == before


async def test_owner_and_bot_cross_links_are_read_only(env):  # noqa: F811
    ctx, feed, session = env
    ids = seed_bots(ctx, 12)
    await feed(msg_update(ADMIN, "/admin"))
    before = snapshot(ctx)
    await feed(cb_update(ADMIN, f"a:bot:{ids[0]}"))
    card = inline(session.of(EditMessageText)[-1])
    assert ("👤 Владелец", f"a:user:{OWNER}") in card
    await feed(cb_update(ADMIN, f"a:user:{OWNER}"))
    user = inline(session.of(EditMessageText)[-1])
    assert "sample_owner" in session.of(EditMessageText)[-1].text
    links = [d for _, d in user if d.startswith("a:bot:")]
    assert len(links) == 10 and links[0] == f"a:bot:{ids[0]}"
    assert ("💰 Изменить баланс", f"a:ubal:{OWNER}") in user  # existing actions unchanged
    await feed(cb_update(ADMIN, "a:user:424242"))
    assert session.of(AnswerCallbackQuery)[-1].text == "Не найден"
    for bad in ("a:user:x", "a:bot:x", "a:bot:", "a:bot:1:zzz:1"):
        await feed(cb_update(ADMIN, bad))
    assert snapshot(ctx) == before
