"""Обязательная подписка, новости в бот+канал и идеи с наградой 50 ₽."""
from __future__ import annotations

import asyncio
import sys

import pytest
from aiogram.methods import CopyMessage, GetChatMember, SendMessage

from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.handlers.ideas import REWARD_KOP
from creator.keyboards import BTN_INFO
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update


def inline(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


ADMIN = 777
OWNER = 100
OTHER = 200
SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
async def env(tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path, admin_ids={ADMIN}, chat_url="https://t.me/chat", channel_url="https://t.me/chan")
    db = DB(cfg.db_path)
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup, validate_token=lambda token: None, bot_username="creator_bot")
    dp = build_dispatcher(ctx)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield ctx, feed, session
    await sup.shutdown()
    db.close()


@pytest.fixture
async def invite_env(tmp_path, bot, session):
    cfg = Config(
        data_dir=tmp_path,
        admin_ids={ADMIN},
        chat_url="https://t.me/+inviteChat12",
        channel_url="https://t.me/+inviteChan12",
    )
    db = DB(cfg.db_path)
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup, validate_token=lambda token: None, bot_username="creator_bot")
    dp = build_dispatcher(ctx)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield ctx, feed, session
    await sup.shutdown()
    db.close()


async def test_gate_skips_admin_start_and_docs(env):
    ctx, feed, session = env
    session.member_status = "left"
    await feed(msg_update(ADMIN, BTN_INFO))
    assert any("Документы" in (t or "") or "Информация" in (t or "") or "Администратор" in (t or "")
               for t in session.texts())
    assert not any("Подпишитесь" in (t or "") for t in session.texts())

    session.clear()
    await feed(msg_update(OWNER, "/start"))
    assert any("создать" in (t or "").lower() or "меню" in (t or "").lower() or "бот" in (t or "").lower()
               for t in session.texts())
    assert not any("Подпишитесь" in (t or "") for t in session.texts())

    session.clear()
    await feed(cb_update(OWNER, "info:docs"))
    texts = session.texts()
    assert texts and not any("Подпишитесь" in (t or "") for t in texts)
    assert any("соглаш" in (t or "").lower() or "документ" in (t or "").lower() or "оферт" in (t or "").lower()
               for t in texts)


async def test_gate_blocks_user_until_subscribed(env):
    ctx, feed, session = env
    session.member_status = "left"
    await feed(msg_update(OWNER, BTN_INFO))
    assert any("Подпишитесь" in (t or "") for t in session.texts())
    chats = {m.chat_id for m in session.of(GetChatMember)}
    assert "@chan" in chats and "@chat" in chats

    session.clear()
    session.member_status = "member"
    await feed(cb_update(OWNER, "gate:check"))
    assert any("подтверждена" in (t or "").lower() for t in session.texts())
    session.clear()
    await feed(msg_update(OWNER, BTN_INFO))
    last = session.of(SendMessage)[-1]
    labels = [b.text for row in last.reply_markup.inline_keyboard for b in row]
    assert "📄 Документы" in labels
    assert not any("Подпишитесь" in (t or "") for t in session.texts())


async def test_gate_inactive_for_invite_links(invite_env):
    ctx, feed, session = invite_env
    session.member_status = "left"
    await feed(msg_update(OWNER, BTN_INFO))
    last = session.of(SendMessage)[-1]
    labels = [b.text for row in last.reply_markup.inline_keyboard for b in row]
    assert "📄 Документы" in labels
    assert not any("Подпишитесь" in (t or "") for t in session.texts())
    assert not session.of(GetChatMember)


async def test_user_can_submit_and_vote_idea_once(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(cb_update(OWNER, "ideas:new"))
    await feed(msg_update(OWNER, "Добавить тёмную тему в кабинет"))
    idea = ctx.db.idea_list(1)[0]
    assert idea["text"].startswith("Добавить тёмную")
    iid = idea["id"]
    await feed(cb_update(OWNER, f"ideas:vote:{iid}"))
    await feed(cb_update(OTHER, f"ideas:vote:{iid}"))
    assert ctx.db.idea_get(iid)["votes"] == 2
    await feed(cb_update(OWNER, f"ideas:vote:{iid}"))
    assert ctx.db.idea_get(iid)["votes"] == 2


async def test_accept_credits_reward_once_and_writes_transaction(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    iid = ctx.db.idea_add(OWNER, "Сделать экспорт статистики в CSV файл")
    assert ctx.db.user(OWNER)["balance"] == 0

    session.clear()
    await feed(cb_update(ADMIN, f"a:ideaok:{iid}"))
    user = ctx.db.user(OWNER)
    assert user["balance"] == REWARD_KOP
    idea = ctx.db.idea_get(iid)
    assert idea["status"] == "accepted" and idea["reward_kop"] == REWARD_KOP
    rows = ctx.db.history(OWNER)
    kinds = [r["kind"] for r in rows]
    assert kinds.count("idea_reward") == 1
    assert any(r["amount"] == REWARD_KOP and "Идея" in (r["comment"] or "") for r in rows)
    assert any("принята" in (m.text or "") and m.chat_id == OWNER for m in session.of(SendMessage))

    await feed(cb_update(ADMIN, f"a:ideaok:{iid}"))
    assert ctx.db.user(OWNER)["balance"] == REWARD_KOP
    assert [r["kind"] for r in ctx.db.history(OWNER)].count("idea_reward") == 1
    assert ctx.db.idea_accept(iid, REWARD_KOP) is None


async def test_non_admin_cannot_open_news_or_accept_idea(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    iid = ctx.db.idea_add(OWNER, "Нужна тёмная тема в личном кабинете")
    await feed(cb_update(OWNER, "a:news"))
    await feed(msg_update(OWNER, "Секретная новость для всех"))
    await feed(cb_update(OWNER, "a:newsgo"))
    await feed(cb_update(OWNER, "a:ideas"))
    await feed(cb_update(OWNER, f"a:ideaok:{iid}"))
    assert not any("Пришлите новость" in (t or "") or "Предпросмотр" in (t or "") for t in session.texts())
    assert not session.of(CopyMessage)
    idea = ctx.db.idea_get(iid)
    assert idea["status"] == "new" and idea["reward_kop"] == 0
    assert ctx.db.user(OWNER)["balance"] == 0


async def test_admin_news_preview_and_publish_copies(env):
    ctx, feed, session = env
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(OTHER, "/start"))
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:news"))
    assert any("Пришлите новость" in (t or "") for t in session.texts())
    session.clear()
    await feed(msg_update(ADMIN, "Запустили раздел идей"))
    assert any("Предпросмотр" in (t or "") and "канал" in (t or "") for t in session.texts())
    assert ("✅ Опубликовать", "a:newsgo") in inline(session.of(SendMessage)[-1])
    assert not session.of(CopyMessage)

    await feed(cb_update(ADMIN, "a:newsgo"))
    await asyncio.sleep(0.4)
    copies = session.of(CopyMessage)
    chats = {m.chat_id for m in copies}
    assert "@chan" in chats
    assert {ADMIN, OWNER, OTHER}.issubset(chats)


def _fwd(uid: int, chat_id: int, chat_type: str, title: str):
    chat = {"id": chat_id, "type": chat_type, "title": title}
    if chat_type == "channel":
        origin = {"type": "channel", "date": 1, "message_id": 5, "chat": chat}
    else:
        origin = {"type": "chat", "date": 1, "sender_chat": chat}
    return msg_update(uid, "forwarded", forward_origin=origin)


@pytest.fixture
async def bound_invite_env(tmp_path, bot, session):
    cfg = Config(
        data_dir=tmp_path,
        admin_ids={ADMIN},
        chat_url="https://t.me/+inviteChat12",
        channel_url="https://t.me/+inviteChan12",
        channel_chat_id=-100111,
        chat_chat_id=-100222,
    )
    db = DB(cfg.db_path)
    sup = Supervisor(db, cfg, cmd=SLEEPER, creator_username="creator_bot")
    ctx = Ctx(db=db, cfg=cfg, sup=sup, validate_token=lambda token: None, bot_username="creator_bot")
    dp = build_dispatcher(ctx)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield ctx, feed, session
    await sup.shutdown()
    db.close()


async def test_gate_blocks_when_invite_has_chat_id(bound_invite_env):
    ctx, feed, session = bound_invite_env
    session.member_status = "left"
    await feed(msg_update(OWNER, BTN_INFO))
    assert any("Подпишитесь" in (t or "") for t in session.texts())
    chats = {m.chat_id for m in session.of(GetChatMember)}
    assert chats == {-100111, -100222}
    last = [m for m in session.of(SendMessage) if m.reply_markup][-1]
    urls = [b.url for row in last.reply_markup.inline_keyboard for b in row if getattr(b, "url", None)]
    assert "https://t.me/+inviteChan12" in urls
    assert "https://t.me/+inviteChat12" in urls

    session.clear()
    await feed(cb_update(OWNER, "info:docs"))
    assert any("документ" in (t or "").lower() or "соглаш" in (t or "").lower() or "оферт" in (t or "").lower()
               for t in session.texts())
    assert not any("Подпишитесь" in (t or "") for t in session.texts())


async def test_admin_forward_binds_ids_and_enables_gate(invite_env):
    ctx, feed, session = invite_env
    session.member_status = "left"
    await feed(_fwd(OWNER, -100333, "channel", "News"))
    assert ctx.db.kv_get("gate_channel_id") is None
    await feed(_fwd(ADMIN, -100333, "channel", "News"))
    await feed(_fwd(ADMIN, -100444, "supergroup", "Chat"))
    assert ctx.db.kv_get("gate_channel_id") == "-100333"
    assert ctx.db.kv_get("gate_chat_id") == "-100444"
    assert any("Привязано" in (t or "") for t in session.texts())

    session.clear()
    await feed(msg_update(OWNER, BTN_INFO))
    assert any("Подпишитесь" in (t or "") for t in session.texts())
    assert {-100333, -100444} == {m.chat_id for m in session.of(GetChatMember)}


async def test_news_preview_uses_bound_channel_id(invite_env):
    ctx, feed, session = invite_env
    ctx.db.kv_set("gate_channel_id", "-100555")
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:news"))
    session.clear()
    await feed(msg_update(ADMIN, "Новость в частный канал"))
    assert any("в бот и в канал" in (t or "") for t in session.texts())
    assert not session.of(CopyMessage)
