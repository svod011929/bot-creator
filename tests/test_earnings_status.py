"""C3: recorded evidence only, real SQLite/fake Telegram, no provider calls."""
import sqlite3
from types import SimpleNamespace as NS

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update

from child.db import ChildDB
from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update

OWNER, OTHER = 100, 200
SECRET = "123456789:AAEexampleexampleexampleexample12345"


def poison(*args, **kwargs):
    raise AssertionError("View must never call provider/service actions")


@pytest.fixture
async def earning(tmp_path, bot, session):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, OTHER):
        db.upsert_user(uid, f"u{uid}", f"User{uid}")
    ids = []
    for template in ("clicker", "refbot", "starbot"):
        draft = db.create_draft(OWNER, template, SECRET, f"{template}_bot", "Fixture")
        ident = db.materialize_draft(draft, 1000)
        path = cfg.bots_dir / str(ident)
        path.mkdir(parents=True)
        child = ChildDB(path / "bot.db")
        child.upsert_user(500, None, "Fixture", None)
        child.conn.close()
        ids.append(ident)
    ctx = Ctx(db, cfg, Supervisor(db, cfg))
    ctx.tgrass = ctx.piarflow = NS(users=poison, reward=poison, min_users=poison, client=NS(probe=poison))
    dp = build_dispatcher(ctx)
    state = dp.fsm.get_context(bot=bot, chat_id=OWNER, user_id=OWNER)

    async def feed(data, uid=OWNER):
        await dp.feed_update(bot, cb_update(uid, data))

    yield NS(ctx=ctx, db=db, ids=ids, state=state, feed=feed, session=session, dp=dp, bot=bot)
    await dp.storage.close()
    db.close()


def screens(w):
    return w.session.of(EditMessageText)


def last(w):
    return screens(w)[-1]


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.mark.parametrize("status,label", [(None, "статус неизвестен"), ("", "статус неизвестен"),
    ("approved", "одобрение записано"), ("pending", "заявка на рассмотрении"),
    ("disabled", "отключено администратором"), ("rejected", "заявка отклонена"),
    (SECRET, "статус неизвестен")])
def test_allowlisted_record_status_never_infers_live_connection(status, label):
    from creator.earnings_status import record_status
    assert record_status({"status": status, "error": SECRET}) == label
    assert record_status(None) == "заявка не записана"


@pytest.mark.parametrize("network,lookup,setter", [("tgr", "tgrass", "tgrass_set"), ("pf", "pf", "pf_set")])
async def test_recorded_approved_relay_stats_are_not_live_health(earning, network, lookup, setter, caplog):
    from creator.earnings_status import owner_screen
    w = earning
    apply = w.db.tgrass_apply if network == "tgr" else w.db.pf_apply
    apply(w.ids[0], OWNER, 1)
    getattr(w.db, setter)(w.ids[0], status="approved", mode="relay", api_key=SECRET, error="https://secret/" + SECRET)
    text, markup = owner_screen(w.ctx, w.db.bot(w.ids[0]), network)
    for part in ("одобрение записано", "через хаб", "не проверяются", "Начислено в конструкторе: 0 ₽", "Зачтено в учёте: 0"):
        assert part in text
    assert "Подключён" not in text and SECRET not in text + caplog.text and "https://secret" not in text
    assert not any(c.endswith("apply") or "mod:" in c for c in callbacks(markup))
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert all(len(c.encode()) <= 64 for c in callbacks(markup))


@pytest.mark.parametrize("kind", ["missing", "corrupt", "empty"])
async def test_user_count_unknown_is_not_zero_and_empty_is_true_zero(earning, kind):
    from creator.earnings_status import owner_screen
    w = earning
    path = w.ctx.sup.bot_dir(w.ids[0]) / "bot.db"
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_bytes(b"invalid sqlite")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM users")
    text, _ = owner_screen(w.ctx, w.db.bot(w.ids[0]), "tgr")
    assert ("не удалось проверить" in text) == (kind != "empty")
    assert ("0 из 50" in text) == (kind == "empty")
    assert path.exists() == (kind != "missing"), "Viewing cannot create a missing child DB"


async def test_dynamic_prices_zero_and_real_credited_amount_not_child_units(earning):
    from creator.earnings_status import owner_screen
    w = earning
    w.db.kv_set("price:TGRASS_REWARD", "0")
    w.db.kv_set("price:TGRASS_MIN_USERS", "0")
    w.db.tgrass_apply(w.ids[0], OWNER, 1)
    w.db.tgrass_credit(w.ids[0], 500, 8, 175)
    text, _ = owner_screen(w.ctx, w.db.bot(w.ids[0]), "tgr")
    assert "<b>0 ₽</b>" in text and "1 из 0" in text
    assert "Начислено в конструкторе: 1,75 ₽" in text and "Зачтено в учёте: 1" in text
    assert "⭐" not in text and "звёзд" not in text


async def test_disabled_service_record_separate_unknown_mode_and_hub_no_checkmark(earning):
    from creator.earnings_status import hub_screen, owner_screen
    w = earning
    w.db.tgrass_apply(w.ids[0], OWNER, 1)
    w.db.tgrass_set(w.ids[0], status="approved", mode=SECRET, error=SECRET)
    w.ctx.tgrass = None
    text, markup = owner_screen(w.ctx, w.db.bot(w.ids[0]), "tgr")
    assert "Сеть выключена" in text and "одобрение записано" in text and "Способ подключения не подтверждён" in text
    assert SECRET not in text and not any(c.endswith("apply") for c in callbacks(markup))
    text, markup = hub_screen(w.ctx, w.db.bot(w.ids[0]))
    assert "выключена" in text and "не проверяются" in text
    assert "✅" not in repr(markup)


@pytest.mark.parametrize("action", ["tg", "tgr", "pf"])
async def test_guard_preserves_foreign_active_master_and_no_messages(earning, action):
    w = earning
    await w.state.set_state("Info:report")
    await w.state.set_data({"secret": SECRET})
    await w.feed(f"b:{w.ids[0]}:{action}")
    assert not screens(w) and not w.session.of(SendMessage)
    assert await w.state.get_state() == "Info:report" and await w.state.get_data() == {"secret": SECRET}


@pytest.mark.parametrize("kind", ["foreign", "deleted", "invalid-template", "huge-id", "bad-id", "extra", "group", "other-chat"])
async def test_invalid_or_unowned_callbacks_do_not_disclose(earning, kind):
    w = earning
    ident = w.ids[0]
    data = f"b:{ident}:tgr"
    if kind == "foreign":
        w.db.update_bot(ident, owner_id=OTHER)
    elif kind == "deleted":
        w.db.update_bot(ident, deleted=1)
    elif kind == "invalid-template":
        w.db.update_bot(ident, template=SECRET)
    elif kind == "huge-id":
        data = "b:9223372036854775808:tgr"
    elif kind == "bad-id":
        data = "b:invalid:tgr"
    elif kind == "extra":
        data += ":" + SECRET
    update = cb_update(OWNER, data).model_dump()
    if kind == "group":
        update["callback_query"]["message"]["chat"]["type"] = "group"
    elif kind == "other-chat":
        update["callback_query"]["message"]["chat"]["id"] = OTHER
    await w.dp.feed_update(w.bot, Update.model_validate(update))
    assert not screens(w) and not w.session.of(SendMessage)
    assert SECRET not in " ".join(str(r) for r in w.session.requests)


async def test_all_templates_readonly_views_keep_rows_files_and_list_context(earning, monkeypatch):
    w = earning
    await w.feed("mybots")
    context = dict(w.ctx.bot_lists)
    before = "\n".join(w.db.conn.iterdump())
    files = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in w.ctx.cfg.bots_dir.rglob("*") if p.is_file()}
    for name in ("tgrass_apply", "pf_apply", "tgrass_set", "pf_set", "tgrass_credit", "pf_credit", "update_bot"):
        monkeypatch.setattr(w.db, name, poison)
    for ident in w.ids:
        for action in ("tg", "tgr", "pf"):
            await w.feed(f"b:{ident}:{action}")
            assert "не проверяются" in last(w).text
    assert "\n".join(w.db.conn.iterdump()) == before
    assert w.ctx.bot_lists == context and await w.state.get_state() is None
    assert all((p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) == v for p, v in files.items())
    assert not w.session.of(SendMessage)


async def test_owner_identity_rechecked_after_render(earning, monkeypatch):
    from creator import earnings_status
    w = earning
    render = earnings_status.owner_screen

    def race(ctx, b, network):
        result = render(ctx, b, network)
        ctx.db.update_bot(b["id"], owner_id=OTHER)
        return result

    monkeypatch.setattr(earnings_status, "owner_screen", race)
    await w.feed(f"b:{w.ids[0]}:tgr")
    assert not screens(w)


async def test_transfer_or_new_master_during_edit_neutralizes_without_clearing(earning, monkeypatch):
    w = earning
    original = w.session.make_request
    changed = False

    async def race(bot, method, timeout=None):
        nonlocal changed
        result = await original(bot, method, timeout)
        if isinstance(method, EditMessageText) and not changed:
            changed = True
            w.db.update_bot(w.ids[0], owner_id=OTHER)
            await w.state.set_state("Info:report")
            await w.state.set_data({"new": True})
        return result

    monkeypatch.setattr(w.session, "make_request", race)
    await w.feed(f"b:{w.ids[0]}:tgr")
    assert len(screens(w)) == 2 and "Бот не найден" in last(w).text
    assert last(w).reply_markup is None and await w.state.get_state() == "Info:report"
    assert await w.state.get_data() == {"new": True} and not w.session.of(SendMessage)


async def test_state_guidance_mode_apply_visibility_and_limits(earning):
    from creator.earnings_status import owner_screen, status_line
    w = earning
    w.db.tgrass_apply(w.ids[0], OWNER, 1)
    for status in ("pending", "approved", "rejected", "disabled", "unknown " + SECRET):
        for mode in ("factory", "relay", SECRET):
            w.db.tgrass_set(w.ids[0], status=status, mode=mode, error="<b>" + SECRET * 3000)
            text, markup = owner_screen(w.ctx, w.db.bot(w.ids[0]), "tgr")
            assert any(c.endswith("tgapply") for c in callbacks(markup)) == (status not in {"pending", "approved"})
            assert SECRET not in text and "✅" not in status_line("TGrass", w.db.tgrass(w.ids[0]))
            assert len(text.encode("utf-16-le")) // 2 <= 4096
            if mode == "factory":
                assert "В записи: собственный бот (factory)" in text
    assert "<script>" not in status_line("<script>", {"status": "<script>"})


async def test_unavailable_recorded_stats_never_become_false_zero(earning, monkeypatch, caplog):
    from creator.earnings_status import owner_screen
    w = earning
    w.db.pf_apply(w.ids[0], OWNER, 1)

    def broken(*args):
        raise sqlite3.DatabaseError(SECRET)

    monkeypatch.setattr(w.db, "pf_stats", broken)
    text, _ = owner_screen(w.ctx, w.db.bot(w.ids[0]), "pf")
    assert "Сведения о записанных начислениях временно недоступны" in text
    assert "Начислено в конструкторе: 0" not in text and SECRET not in text + caplog.text


@pytest.mark.parametrize("error", ["bad", "network", "not-modified"])
async def test_edit_errors_do_not_echo_raw_error_or_duplicate_send(earning, monkeypatch, caplog, error):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    w = earning
    original = w.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            if error == "network":
                raise TelegramNetworkError(method=method, message=SECRET)
            raise TelegramBadRequest(method=method, message="message is not modified" if error == "not-modified" else SECRET)
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", fail)
    await w.feed(f"b:{w.ids[0]}:pf")
    assert not w.session.of(SendMessage) and await w.state.get_state() is None
    assert SECRET not in caplog.text + " ".join(str(r) for r in w.session.requests)


async def test_disabled_stale_known_callbacks_are_current_and_never_register(earning):
    w = earning
    w.ctx.tgrass = w.ctx.piarflow = None
    await w.feed(f"b:{w.ids[0]}:tg")
    assert last(w).text.count("выключена") == 2
    await w.feed(f"b:{w.ids[0]}:pf")
    assert "Сеть выключена" in last(w).text
    assert not any(c.endswith("apply") for c in callbacks(last(w).reply_markup))
    assert w.db.tgrass(w.ids[0]) is None and w.db.pf(w.ids[0]) is None
