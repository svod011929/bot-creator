"""C2 with real temporary SQLite and fake Dispatcher; no live Telegram or processes."""
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update

OWNER, OTHER = 100, 200
FAKE_TOKEN = "123456789:AAEexampleexampleexampleexample12345"


@pytest.fixture
async def listing(tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, OTHER):
        db.upsert_user(uid, "fixture", "Fixture")
    ids = []
    for i in range(25):
        uid = OWNER if i < 22 else OTHER
        name = "demo_01_bot" if i == 0 else "demoX01Xbot" if i == 1 else f"demo_{i + 1:02}_bot"
        draft = db.create_draft(uid, ("clicker", "refbot", "starbot")[i % 3], FAKE_TOKEN, name, "Fixture")
        ident = db.materialize_draft(draft, 1000)
        db.update_bot(ident, status=("running", "stopped", "restarting", "error")[i % 4])
        ids.append(ident)
    db.update_bot(ids[21], deleted=1)
    ctx = Ctx(db, cfg, Supervisor(db, cfg))
    dp = build_dispatcher(ctx)
    state = dp.fsm.get_context(bot=bot, chat_id=OWNER, user_id=OWNER)

    async def feed(update):
        await dp.feed_update(bot, update)

    yield NS(ctx=ctx, ids=ids[:21], feed=feed, state=state, session=session, path=tmp_path)
    await dp.storage.close()
    db.close()


def last(w):
    assert w.session.texts(), "Expected a real fake-Dispatcher screen"
    return w.session.texts()[-1]


def keyboard(w):
    return [m for m in w.session.requests if isinstance(m, (EditMessageText, SendMessage))][-1].reply_markup.inline_keyboard


def callback(w, ending):
    return next(b.callback_data for row in keyboard(w) for b in row if (b.callback_data or "").endswith(ending))


async def test_domain_page_owner_scope_selected_columns_and_clamping(listing):
    from creator.bot_list import fresh, page, render
    w = listing
    rows, total, view = page(w.ctx.db, OWNER, fresh())
    assert total == 21 and [r["id"] for r in rows] == w.ids[:10]
    assert set(rows[0].keys()) == {"id", "template", "username", "status"}
    rows, total, view = page(w.ctx.db, OWNER, replace(view, page=10 ** 18))
    assert [r["id"] for r in rows] == [w.ids[-1]] and view.page == 2
    rows, _, view = page(w.ctx.db, OWNER, replace(view, page=-10))
    assert [r["id"] for r in rows] == w.ids[:10] and view.page == 0
    text, markup, _ = render(w.ctx, OWNER, view)
    assert "Страница 1 из 3" in text and "(21)" in text
    assert "demo_25_bot" not in text and FAKE_TOKEN not in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row if b.callback_data)


async def test_domain_search_exact_id_case_optional_at_and_literal_underscore(listing):
    from creator.bot_list import fresh, normalize_query, page
    w = listing
    for raw, expected in (("@DEMO_01_", [w.ids[0]]), ("0001", [w.ids[0]]), ("demoX01", [w.ids[1]])):
        rows, total, _ = page(w.ctx.db, OWNER, replace(fresh(), query=normalize_query(raw)))
        assert [r["id"] for r in rows] == expected and total == 1
    rows, total, _ = page(w.ctx.db, OWNER, replace(fresh(), query="25"))
    assert not rows and total == 0, "Foreign exact IDs must not reveal existence"
    rows, total, _ = page(w.ctx.db, OWNER, replace(fresh(), status="running"))
    assert total == 6 and all(r["status"] == "running" for r in rows)


@pytest.mark.parametrize("raw", ["", "0", "9" * 20, "9223372036854775808", "a" * 34,
                                 FAKE_TOKEN, "a%b", "https://t.me/demo_bot", "two words", "@@demo", "🚀"])
def test_invalid_input_never_becomes_search(raw):
    from creator.bot_list import normalize_query
    with pytest.raises(ValueError):
        normalize_query(raw)


async def test_cache_expiry_corruption_and_bound(listing, monkeypatch):
    from creator import bot_list
    w = listing
    monkeypatch.setattr(bot_list.time, "monotonic", lambda: 100)
    for uid in range(129):
        bot_list.remember(w.ctx, uid, bot_list.fresh())
    assert len(w.ctx.bot_lists) == 128 and bot_list.cached(w.ctx, 0) is None
    assert bot_list.cached(w.ctx, 128) is not None
    w.ctx.bot_lists[128] = {"query": FAKE_TOKEN}
    assert bot_list.cached(w.ctx, 128) is None
    bot_list.remember(w.ctx, OWNER, bot_list.fresh())
    monkeypatch.setattr(bot_list.time, "monotonic", lambda: 1001)
    assert bot_list.cached(w.ctx, OWNER) is None


async def test_dispatcher_page_search_filter_and_card_return(listing):
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    assert "Страница 1 из 3" in last(w)
    await w.feed(cb_update(OWNER, callback(w, ":p:1")))
    assert "Страница 2 из 3" in last(w)
    assert f"b:{w.ids[10]}:open" in {b.callback_data for row in keyboard(w) for b in row}
    await w.feed(cb_update(OWNER, f"b:{w.ids[10]}:open"))
    assert "Бот ID" in last(w)
    await w.feed(cb_update(OWNER, "mybots"))
    assert "Страница 2 из 3" in last(w)
    await w.feed(cb_update(OWNER, callback(w, ":search")))
    assert await w.state.get_state() == "BotList:search"
    await w.feed(msg_update(OWNER, "@DEMO_01_"))
    assert await w.state.get_state() is None
    assert "(1)" in last(w) and "Поиск: включён" in last(w)
    await w.feed(cb_update(OWNER, callback(w, ":f:stopped")))
    assert "Боты не найдены" in last(w)
    await w.feed(cb_update(OWNER, callback(w, ":reset")))
    assert "(21)" in last(w) and "Поиск: выключен" in last(w)


async def test_search_invalid_secret_does_not_echo_and_explicit_back_preserves_view(listing, caplog):
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    await w.feed(cb_update(OWNER, callback(w, ":p:1")))
    await w.feed(cb_update(OWNER, callback(w, ":search")))
    await w.feed(msg_update(OWNER, FAKE_TOKEN))
    assert await w.state.get_state() == "BotList:search"
    assert FAKE_TOKEN not in " ".join(w.session.texts()) + caplog.text
    assert FAKE_TOKEN not in repr(w.ctx.bot_lists)
    await w.feed(cb_update(OWNER, callback(w, ":back")))
    assert await w.state.get_state() is None and "Страница 2 из 3" in last(w)


@pytest.mark.parametrize("active", ["Info:report", "CreateBot:token", "Wizard:active"])
async def test_old_list_search_card_callbacks_cannot_interrupt_other_fsm(listing, active):
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    actions = [callback(w, ":search"), callback(w, ":p:1"), "mybots", f"b:{w.ids[0]}:open"]
    await w.state.set_state(active)
    await w.state.set_data({"keep": "draft", "bot_id": 999})
    w.session.clear()
    for data in actions:
        await w.feed(cb_update(OWNER, data))
        assert await w.state.get_state() == active and (await w.state.get_data())["keep"] == "draft"
    assert not w.session.texts()


@pytest.mark.parametrize("active", ["BotEdit:transfer", "BotEdit:setting", "BotEdit:sell_price"])
async def test_legacy_open_cancel_own_editor_preserved(listing, active):
    w = listing
    await w.state.set_state(active)
    await w.state.set_data({"bot_id": w.ids[0], "template": "clicker"})
    await w.feed(cb_update(OWNER, f"b:{w.ids[0]}:open"))
    assert await w.state.get_state() is None and "Бот ID" in last(w)


async def test_old_owner_search_and_card_after_transfer_or_delete(listing):
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    page_action = callback(w, ":p:1")
    w.ctx.db.transfer_bot(w.ids[0], OTHER)
    w.ctx.db.update_bot(w.ids[1], deleted=1)
    await w.feed(cb_update(OWNER, page_action))
    assert "(19)" in last(w)
    for ident in w.ids[:2]:
        w.session.clear()
        await w.feed(cb_update(OWNER, f"b:{ident}:open"))
        assert not w.session.texts() and w.session.of(AnswerCallbackQuery)[-1].text


async def test_forged_callback_nonce_and_private_chat_do_not_disclose(listing):
    from aiogram.types import Update
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    action = callback(w, ":search")
    w.session.clear()
    await w.feed(cb_update(OTHER, action))
    assert not w.session.texts()
    for data in ("bl:00000000:f:unknown", "bl:bad:p:1", "bl:00000000:p:1:extra", "b:bad:open"):
        await w.feed(cb_update(OWNER, data))
    raw = cb_update(OWNER, action).model_dump(exclude_none=True)
    raw["callback_query"]["message"]["chat"] = {"id": -100, "type": "group", "title": "Fixture"}
    await w.feed(Update.model_validate(raw))
    assert not w.session.texts() and await w.state.get_state() is None


async def test_start_cancel_and_menu_reset_view_without_financial_effects(listing):
    w = listing
    for reset in ("/start", "/cancel", "🤖 Мои боты"):
        await w.feed(msg_update(OWNER, "🤖 Мои боты"))
        await w.feed(cb_update(OWNER, callback(w, ":search")))
        await w.feed(msg_update(OWNER, "demo_01_"))
        await w.feed(msg_update(OWNER, reset))
        assert await w.state.get_state() is None
        if reset != "🤖 Мои боты":
            assert OWNER not in w.ctx.bot_lists
        else:
            assert "Поиск: выключен" in last(w)


async def test_empty_and_page_boundaries_after_concurrent_removals(listing):
    from creator.bot_list import fresh, page, render
    w = listing
    for count in (11, 10, 1, 0):
        for ident in w.ids[count:]:
            w.ctx.db.update_bot(ident, deleted=1)
        rows, total, view = page(w.ctx.db, OWNER, replace(fresh(), page=10 ** 18))
        assert total == count and len(rows) <= 10
        assert view.page == max(0, (count - 1) // 10)
        text, _, _ = render(w.ctx, OWNER, view)
        if count == 0:
            assert "Боты не найдены" in text and "Страница 1 из 1" in text


async def test_unknown_metadata_never_echoes_unsafe_values(listing):
    from creator.bot_list import fresh, render
    w = listing
    w.ctx.db.update_bot(w.ids[0], template="<SECRET_TEMPLATE>", username=FAKE_TOKEN, status="<SECRET_STATUS>")
    text, markup, _ = render(w.ctx, OWNER, fresh())
    assert FAKE_TOKEN not in text and "SECRET_" not in text
    assert "Неизвестный шаблон" in repr(markup)
    assert "SECRET_" not in repr(markup)


async def test_read_only_search_navigation_preserves_database_and_child_files(listing, monkeypatch):
    w = listing
    child = w.ctx.sup.bot_dir(w.ids[0])
    child.mkdir(parents=True)
    settings = child / "settings.json"
    settings.write_text('{"welcome":"UNCHANGED"}')
    before_files = (settings.read_bytes(), settings.stat().st_mode, settings.stat().st_mtime_ns)
    before_db = tuple(w.ctx.db.conn.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("List navigation invoked a child/operational/provider action")

    for name in ("bot_dir", "start", "stop", "restart", "is_running"):
        monkeypatch.setattr(w.ctx.sup, name, forbidden)
    monkeypatch.setattr(w.ctx, "validate_token", forbidden)
    for name in ("create_bot", "create_bot_from_balance", "set_vip", "apply_changes", "fulfil_payload"):
        monkeypatch.setattr(type(w.ctx), name, forbidden)
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    await w.feed(cb_update(OWNER, callback(w, ":p:1")))
    await w.feed(cb_update(OWNER, callback(w, ":search")))
    await w.feed(msg_update(OWNER, "demo_"))
    await w.feed(cb_update(OWNER, callback(w, ":f:error")))
    await w.feed(cb_update(OWNER, callback(w, ":reset")))
    await w.feed(cb_update(OWNER, "mybots"))
    assert tuple(w.ctx.db.conn.iterdump()) == before_db
    assert (settings.read_bytes(), settings.stat().st_mode, settings.stat().st_mtime_ns) == before_files
    assert {p.name for p in child.iterdir()} == {"settings.json"}
    assert all(isinstance(m, (AnswerCallbackQuery, EditMessageText, SendMessage)) for m in w.session.requests)


async def test_expired_and_corrupt_return_context_falls_back_honestly(listing):
    from creator.bot_list import fresh
    w = listing
    for value in ({"query": FAKE_TOKEN}, replace(fresh(), expires=0)):
        w.ctx.bot_lists[OWNER] = value
        await w.feed(cb_update(OWNER, "mybots"))
        assert "Прежний список недоступен" in last(w)
        assert "Поиск: выключен" in last(w) and FAKE_TOKEN not in last(w)


async def test_invalid_filter_same_nonce_and_bounded_huge_pages(listing):
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    prefix = callback(w, ":search").removesuffix("search")
    w.session.clear()
    for tail in ("f:unknown", "p:1:extra", "p:" + "9" * 20):
        await w.feed(cb_update(OWNER, prefix + tail))
    assert not w.session.texts()
    await w.feed(cb_update(OWNER, prefix + "p:9999999999999999999"))
    assert "Страница 3 из 3" in last(w)
    await w.feed(cb_update(OWNER, prefix + "p:-99"))
    assert "Страница 1 из 3" in last(w)


async def test_card_rechecks_identity_after_render_and_preserves_changed_fsm(listing, monkeypatch):
    from creator.handlers import mybots
    w = listing
    original = mybots.card_text

    def transferred(ctx, row):
        text = original(ctx, row)
        ctx.db.transfer_bot(row["id"], OTHER)
        return text

    monkeypatch.setattr(mybots, "card_text", transferred)
    await w.feed(cb_update(OWNER, f"b:{w.ids[0]}:open"))
    assert not w.session.texts()
    monkeypatch.setattr(mybots, "card_text", original)
    make_request = w.session.make_request

    async def changed(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            await w.state.set_state("Info:report")
            await w.state.set_data({"keep": "new"})
        return await make_request(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", changed)
    await w.feed(cb_update(OWNER, f"b:{w.ids[1]}:open"))
    assert await w.state.get_state() == "Info:report" and await w.state.get_data() == {"keep": "new"}


@pytest.mark.parametrize("action", ["search", "page", "card"])
async def test_edit_error_never_clears_fsm_or_sends_duplicate(listing, monkeypatch, caplog, action):
    from aiogram.exceptions import TelegramNetworkError
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    data = callback(w, ":search" if action == "search" else ":p:1")
    if action == "card":
        data = f"b:{w.ids[0]}:open"
        await w.state.set_state("BotEdit:transfer")
        await w.state.set_data({"bot_id": w.ids[0]})
    before = await w.state.get_state(), await w.state.get_data()
    original = w.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramNetworkError(method=method, message="PRIVATE_ERROR_" + FAKE_TOKEN)
        return await original(bot, method, timeout)

    w.session.clear()
    monkeypatch.setattr(w.session, "make_request", fail)
    await w.feed(cb_update(OWNER, data))
    assert (await w.state.get_state(), await w.state.get_data()) == before
    assert not w.session.of(SendMessage) and "PRIVATE_ERROR" not in caplog.text


async def test_new_search_is_not_overwritten_by_old_network_completion(listing, monkeypatch):
    from creator.bot_list import fresh, remember
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    action = callback(w, ":p:1")
    new_view = replace(fresh(), query="demo_01_")
    original = w.session.make_request

    async def changed(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            remember(w.ctx, OWNER, new_view)
            await w.state.set_state("Info:report")
            await w.state.set_data({"keep": "new"})
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", changed)
    await w.feed(cb_update(OWNER, action))
    assert w.ctx.bot_lists[OWNER].query == "demo_01_"
    assert await w.state.get_state() == "Info:report"


async def test_direct_open_masks_unsafe_metadata_and_token_before_truncation(listing):
    w = listing
    w.ctx.db.update_bot(w.ids[0], username=FAKE_TOKEN, status="<UNSAFE_STATUS>",
                       last_error="E" * 295 + FAKE_TOKEN)
    await w.feed(cb_update(OWNER, f"b:{w.ids[0]}:open"))
    assert FAKE_TOKEN not in last(w) and FAKE_TOKEN[:5] not in last(w)
    assert "UNSAFE_STATUS" not in last(w)


@pytest.mark.parametrize("kind", ["none", "inaccessible", "other_private"])
async def test_unavailable_or_other_private_callback_has_no_list_or_card_data(listing, kind):
    from aiogram.types import Update
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    actions = [callback(w, ":search"), f"b:{w.ids[0]}:open"]
    w.session.clear()
    for action in actions:
        raw = cb_update(OWNER, action).model_dump(exclude_none=True)
        if kind == "none":
            raw["callback_query"].pop("message")
            raw["callback_query"]["inline_message_id"] = "unavailable"
        elif kind == "inaccessible":
            raw["callback_query"]["message"]["date"] = 0
            raw["callback_query"]["message"].pop("text", None)
        else:
            raw["callback_query"]["message"]["chat"]["id"] = OTHER
        await w.feed(Update.model_validate(raw))
    assert not w.session.texts() and await w.state.get_state() is None


async def test_corrupt_typed_view_is_a_safe_fallback(listing):
    from creator.bot_list import cached, fresh
    w = listing
    for changes in ({"nonce": "bad"}, {"query": FAKE_TOKEN}, {"status": "unknown"},
                    {"page": -1}, {"expires": float("nan")}, {"expires": 10 ** 400}):
        w.ctx.bot_lists[OWNER] = replace(fresh(), **changes)
        assert cached(w.ctx, OWNER) is None


async def test_fresh_menu_context_wins_over_inflight_old_page(listing, monkeypatch):
    import asyncio
    w = listing
    await w.feed(msg_update(OWNER, "🤖 Мои боты"))
    action = callback(w, ":p:1")
    edit_started, resume_edit = asyncio.Event(), asyncio.Event()
    original = w.session.make_request
    old_task = None

    async def interleaved(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            edit_started.set()
            await resume_edit.wait()
        elif isinstance(method, SendMessage):
            resume_edit.set()
            await old_task
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", interleaved)
    old_task = asyncio.create_task(w.feed(cb_update(OWNER, action)))
    await asyncio.wait_for(edit_started.wait(), 2)
    await asyncio.wait_for(w.feed(msg_update(OWNER, "🤖 Мои боты")), 2)
    assert "Страница 1 из 3" in last(w)
    assert w.ctx.bot_lists[OWNER].page == 0
    assert callback(w, ":search").split(":")[1] == w.ctx.bot_lists[OWNER].nonce
