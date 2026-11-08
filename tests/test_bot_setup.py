"""First-launch and settings navigation through fake Telegram, real Dispatcher and temp files."""
import json
from types import SimpleNamespace as NS

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from child import templates
from creator.app import build_dispatcher
from creator.config import Config
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.conftest import cb_update, msg_update

OWNER, BUYER = 100, 200
TOKEN = "123456789:AAEexampleexampleexampleexample12345"


@pytest.fixture(params=["clicker", "refbot", "starbot"])
async def setup(request, tmp_path, bot, session, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    db.upsert_user(OWNER, "owner", "Owner")
    db.upsert_user(BUYER, "buyer", "Buyer")
    template = request.param
    draft = db.create_draft(OWNER, template, TOKEN, "fixture_bot", "Fixture")
    bot_id = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bot_id)
    (path / "settings.json").write_text("{}")
    monkeypatch.setattr(sup, "is_running", lambda bot_id: False)
    ctx = Ctx(db, cfg, sup)
    dp = build_dispatcher(ctx)
    state = dp.fsm.get_context(bot=bot, chat_id=OWNER, user_id=OWNER)

    async def feed(update):
        await dp.feed_update(bot, update)

    w = NS(ctx=ctx, feed=feed, session=session, state=state, path=path, id=bot_id, template=template)
    w.cb = lambda action: f"setup:{bot_id}:{template}:{action}"
    yield w
    await dp.storage.close()
    db.close()


def screen(w):
    messages = w.session.of(EditMessageText) + w.session.of(SendMessage)
    assert messages, "Expected an actual navigation screen"
    return messages[-1]


def buttons(message):
    return [b for row in message.reply_markup.inline_keyboard for b in row]


async def test_card_entry_and_readiness_defaults(setup):
    w = setup
    await w.feed(cb_update(OWNER, f"b:{w.id}:open"))
    assert any(b.text == "📋 Первый запуск" and b.callback_data == w.cb("ready") for b in buttons(screen(w)))
    w.session.clear()
    await w.feed(cb_update(OWNER, w.cb("ready")))
    text = screen(w).text
    assert "Первый запуск" in text and "по супервизору" in text
    assert "не запущен" in text and "соответствует записи" in text
    assert "по умолчанию" in text and "выключен (необязательно)" in text
    assert "не проверка ответа Telegram" in text
    assert TOKEN not in text
    assert any(b.url == "https://t.me/fixture_bot" for b in buttons(screen(w)))
    currency = templates.info(w.template).defaults()["currency"]
    assert currency in text
    assert await w.state.get_state() is None


async def test_settings_groups_cover_exact_current_fields(setup):
    w = setup
    await w.feed(cb_update(OWNER, f"b:{w.id}:set"))
    root = screen(w)
    assert "Настройки бота" in root.text
    assert {w.cb("group:appearance"), w.cb("group:rules"), w.cb("channel")} <= {
        b.callback_data for b in buttons(root)
    }
    seen = set()
    for group in ("appearance", "rules"):
        await w.feed(cb_update(OWNER, w.cb("group:" + group)))
        for b in buttons(screen(w)):
            if b.callback_data and ":edit:" in b.callback_data:
                seen.add(b.callback_data.rsplit(":", 1)[1])
    expected = {s.key for s in templates.info(w.template).settings} - {"required_channel", "channel_url"}
    assert seen == expected
    assert await w.state.get_state() is None


@pytest.mark.parametrize("file", ["config.json", "settings.json"])
@pytest.mark.parametrize("content", [None, "broken", "[]", "null", '"text"'])
async def test_unavailable_files_never_look_ready_or_default(setup, file, content):
    w = setup
    if content is None:
        (w.path / file).unlink()
    else:
        (w.path / file).write_text(content)
    await w.feed(cb_update(OWNER, w.cb("ready")))
    text = screen(w).text
    assert "недоступ" in text
    assert "Приветствие: недоступно" in text
    edits = any(":edit:" in (b.callback_data or "") for b in buttons(screen(w)))
    assert edits == (file == "settings.json" and content is None)
    assert TOKEN not in text


@pytest.mark.parametrize("field,value", [("owner_id", BUYER), ("template", "other"), ("token", "OTHER_SECRET"),
                                         ("bot_id", 999), ("username", "different_bot")])
async def test_config_identity_mismatch_suppresses_settings(setup, field, value):
    w = setup
    data = json.loads((w.path / "config.json").read_text())
    data[field] = value
    (w.path / "config.json").write_text(json.dumps(data))
    (w.path / "settings.json").write_text(json.dumps({"welcome": "PRIVATE_GREETING"}))
    await w.feed(cb_update(OWNER, w.cb("ready")))
    text = screen(w).text
    assert "не подтверждена" in text
    assert "PRIVATE_GREETING" not in text and "OTHER_SECRET" not in text


async def test_partial_stored_values_escape_redact_and_do_not_imply_customization(setup):
    w = setup
    (w.path / "settings.json").write_text(json.dumps({
        "welcome": "<b>Hello & text</b> " + TOKEN + " 🌟" * 100,
        "currency": "<coin>", "unknown_secret": "DO_NOT_SHOW",
        "required_channel": "-100123", "channel_url": "https://t.me/+PRIVATE_INVITE",
    }))
    await w.feed(cb_update(OWNER, w.cb("ready")))
    text = screen(w).text
    assert "&lt;b&gt;Hello &amp; text&lt;/b&gt;" in text
    assert "&lt;coin&gt;" in text and "сохранено" in text
    assert "[скрыто]" in text and TOKEN not in text
    assert "DO_NOT_SHOW" not in text and "PRIVATE_INVITE" not in text
    assert "права здесь не проверялись" in text
    assert "готов к работе" not in text and "настроено пользователем" not in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


@pytest.mark.parametrize("username", [
    None, "@fixture_bot", "https://evil.test", "bad_bot?start=PAY", "x", "a" * 33, "no_spaces bot",
])
async def test_unsafe_username_never_becomes_link(setup, username):
    w = setup
    w.ctx.db.update_bot(w.id, username=username)
    w.ctx.sup.write_files(w.id)
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert not any(b.url for b in buttons(screen(w)))
    assert "Ссылка: недоступна" in screen(w).text


async def test_process_and_database_status_are_distinct_facts(setup, monkeypatch):
    w = setup
    w.ctx.db.update_bot(w.id, status="error")
    monkeypatch.setattr(w.ctx.sup, "is_running", lambda i: True)
    await w.feed(cb_update(OWNER, w.cb("ready")))
    text = screen(w).text
    assert "Процесс: запущен (по супервизору)" in text
    assert "Запись состояния: ошибка" in text
    assert "не проверка ответа Telegram" in text


@pytest.mark.parametrize("action", ["ready", "settings", "group:appearance", "group:rules", "edit:welcome", "channel", "card"])
async def test_non_owner_callbacks_do_not_disclose_or_mutate(setup, action):
    w = setup
    await w.feed(cb_update(BUYER, w.cb(action)))
    assert not w.session.of(EditMessageText) and not w.session.of(SendMessage)
    assert w.session.of(AnswerCallbackQuery)[-1].text == "Бот не найден или действие устарело."


@pytest.mark.parametrize("action", ["ready", "settings", "group:appearance"])
async def test_private_chat_only(setup, action):
    from aiogram.types import Update
    w = setup
    raw = cb_update(OWNER, w.cb(action)).model_dump(exclude_none=True)
    raw["callback_query"]["message"]["chat"] = {"id": -100123, "type": "supergroup", "title": "Group"}
    await w.feed(Update.model_validate(raw))
    assert not w.session.of(EditMessageText) and not w.session.of(SendMessage)
    assert w.session.of(AnswerCallbackQuery)[-1].text


@pytest.mark.parametrize("action", ["ready", "settings", "group:appearance", "edit:welcome", "channel", "card"])
@pytest.mark.parametrize("active", ["Info:report", "CreateBot:token", "Wizard:active"])
async def test_foreign_fsm_is_not_interrupted(setup, action, active):
    w = setup
    await w.state.set_state(active)
    data = {"keep": "draft", "bot_id": w.id + 1}
    await w.state.set_data(data)
    await w.feed(cb_update(OWNER, w.cb(action)))
    assert await w.state.get_state() == active and await w.state.get_data() == data
    assert not w.session.of(EditMessageText)
    assert w.session.of(AnswerCallbackQuery)[-1].text == "Завершите текущую настройку или нажмите /cancel."


@pytest.mark.parametrize("change", ["owner", "template", "deleted"])
async def test_stale_buttons_after_identity_change(setup, change):
    w = setup
    if change == "owner":
        w.ctx.db.transfer_bot(w.id, BUYER)
    elif change == "template":
        w.ctx.db.update_bot(w.id, template="starbot" if w.template != "starbot" else "clicker")
    else:
        w.ctx.db.update_bot(w.id, deleted=1)
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert not w.session.of(EditMessageText)
    assert w.session.of(AnswerCallbackQuery)[-1].text == "Бот не найден или действие устарело."


@pytest.mark.parametrize("data", [
    "setup:0:clicker:ready", "setup:-1:clicker:ready", "setup:bad:clicker:ready",
    "setup:9999999999999999999:clicker:ready", "setup:1:unknown:ready", "setup:1:clicker:ready:extra",
    "setup:1:clicker:group:unknown", "setup:1:clicker:edit:token", "setup:1:clicker:edit:required_channel",
    "b:bad:set", "b:1:set:extra", "b:bad:sk:welcome", "b:1:sk", "b:1:sk:token",
])
async def test_malformed_and_forged_callbacks_fail_without_state_mutation(setup, data):
    w = setup
    await w.state.set_data({"preserve": "data"})
    await w.feed(cb_update(OWNER, data))
    assert await w.state.get_state() is None and await w.state.get_data() == {"preserve": "data"}
    assert not w.session.of(EditMessageText)
    assert w.session.of(AnswerCallbackQuery)[-1].text


@pytest.mark.parametrize("legacy", [False, True])
async def test_existing_editor_transition_and_explicit_back(setup, legacy):
    w = setup
    callback = f"b:{w.id}:sk:welcome" if legacy else w.cb("edit:welcome")
    await w.feed(cb_update(OWNER, callback))
    assert "Введите новое значение" in screen(w).text
    assert await w.state.get_state() == "BotEdit:setting"
    assert await w.state.get_data() == {"bot_id": w.id, "key": "welcome", "template": w.template}
    before = (w.path / "settings.json").read_bytes()
    await w.feed(cb_update(OWNER, f"b:{w.id}:set"))
    assert await w.state.get_state() is None and await w.state.get_data() == {}
    assert before == (w.path / "settings.json").read_bytes()
    assert "Настройки бота" in screen(w).text


async def test_existing_editor_saves_only_selected_field_and_channel_uses_wizard(setup):
    w = setup
    await w.feed(cb_update(OWNER, w.cb("edit:welcome")))
    await w.feed(msg_update(OWNER, "Hello new greeting"))
    assert json.loads((w.path / "settings.json").read_text()) == {"welcome": "Hello new greeting"}
    assert await w.state.get_state() is None
    await w.feed(cb_update(OWNER, w.cb("channel")))
    assert await w.state.get_state() == "Wizard:active"
    assert "Обязательная подписка" in screen(w).text
    assert any((b.callback_data or "").startswith("sub:") for b in buttons(screen(w)))
    await w.feed(cb_update(OWNER, f"b:{w.id}:set"))
    assert await w.state.get_state() is None
    await w.feed(cb_update(OWNER, f"b:{w.id}:sk:required_channel"))
    assert await w.state.get_state() == "Wizard:active"


@pytest.mark.parametrize("change", ["owner", "template", "key"])
async def test_edit_submission_revalidates_current_row_and_key(setup, change):
    w = setup
    await w.feed(cb_update(OWNER, w.cb("edit:welcome")))
    if change == "owner":
        w.ctx.db.transfer_bot(w.id, BUYER)
    elif change == "template":
        w.ctx.db.update_bot(w.id, template="starbot" if w.template != "starbot" else "clicker")
    else:
        await w.state.update_data(key="does_not_exist")
    before = (w.path / "settings.json").read_bytes()
    await w.feed(msg_update(OWNER, "MUST_NOT_SAVE"))
    assert (w.path / "settings.json").read_bytes() == before
    assert await w.state.get_state() is None


@pytest.mark.parametrize("kind", ["network", "badrequest", "not_modified"])
async def test_edit_failure_is_safe_and_preserves_own_editor(setup, kind, monkeypatch, caplog):
    w = setup
    await w.state.set_state("BotEdit:setting")
    data = {"bot_id": w.id, "key": "welcome", "template": w.template}
    await w.state.set_data(data)
    original = w.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            error = TelegramNetworkError if kind == "network" else TelegramBadRequest
            message = "Bad Request: message is not modified" if kind == "not_modified" else "SECRET_ERROR"
            raise error(method=method, message=message)
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", fail)
    await w.feed(cb_update(OWNER, w.cb("settings")))
    if kind == "not_modified":
        assert await w.state.get_state() is None
    else:
        assert await w.state.get_state() == "BotEdit:setting" and await w.state.get_data() == data
        assert not w.session.of(SendMessage)
    assert "SECRET_ERROR" not in caplog.text


@pytest.mark.parametrize("change", ["owner", "template"])
async def test_identity_changed_during_render_is_rechecked_before_delivery(setup, change, monkeypatch):
    from creator.handlers import bot_setup as handler
    w = setup
    original = handler.view

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "owner":
            w.ctx.db.transfer_bot(w.id, BUYER)
        else:
            w.ctx.db.update_bot(w.id, template="starbot" if w.template != "starbot" else "clicker")
        return result

    monkeypatch.setattr(handler, "view", changed)
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert not w.session.of(EditMessageText)
    assert w.session.of(AnswerCallbackQuery)[-1].text == "Бот не найден или действие устарело."


async def test_fsm_changed_during_edit_is_not_cleared(setup, monkeypatch):
    w = setup
    await w.state.set_state("BotEdit:setting")
    await w.state.set_data({"bot_id": w.id, "key": "welcome", "template": w.template})
    original = w.session.make_request

    async def changed(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            await w.state.set_state("Info:report")
            await w.state.set_data({"keep": "report"})
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", changed)
    await w.feed(cb_update(OWNER, w.cb("settings")))
    assert await w.state.get_state() == "Info:report" and await w.state.get_data() == {"keep": "report"}


@pytest.mark.parametrize("legacy", [False, True])
async def test_missing_settings_allows_existing_first_edit_without_false_current_values(setup, legacy):
    w = setup
    (w.path / "settings.json").unlink()
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert "Приветствие: недоступно" in screen(w).text
    await w.feed(cb_update(OWNER, f"b:{w.id}:sk:welcome" if legacy else w.cb("edit:welcome")))
    assert await w.state.get_state() == "BotEdit:setting"
    assert not (w.path / "settings.json").exists(), "Opening an editor still must not write"
    await w.feed(msg_update(OWNER, "First saved greeting"))
    assert json.loads((w.path / "settings.json").read_text()) == {"welcome": "First saved greeting"}


async def test_navigation_preserves_entire_database_and_child_file_snapshot(setup, monkeypatch):
    w = setup
    # Warm up the unchanged user middleware before taking a complete DB snapshot.
    await w.feed(cb_update(OWNER, w.cb("ready")))
    before_db = tuple(w.ctx.db.conn.iterdump())

    def files():
        return {str(p.relative_to(w.path)): (p.stat().st_mode, p.stat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None) for p in w.path.rglob("*")}

    before_files = files()

    async def forbidden(*args, **kwargs):
        pytest.fail("Navigation invoked an operational/financial/network action")

    for name in ("start", "stop", "restart"):
        monkeypatch.setattr(w.ctx.sup, name, forbidden)
    for name in ("create_bot", "create_bot_from_balance", "set_vip", "apply_changes", "fulfil_payload"):
        monkeypatch.setattr(type(w.ctx), name, forbidden)
    monkeypatch.setattr(w.ctx, "validate_token", forbidden)
    for action in ("ready", "settings", "group:appearance", "group:rules", "card",
                   "edit:welcome", "settings", "channel", "settings"):
        await w.feed(cb_update(OWNER, w.cb(action)))
    await w.feed(msg_update(OWNER, "/cancel"))
    assert files() == before_files
    assert tuple(w.ctx.db.conn.iterdump()) == before_db
    assert await w.state.get_state() is None
    assert all(isinstance(method, (EditMessageText, SendMessage, AnswerCallbackQuery)) for method in w.session.requests)


@pytest.mark.parametrize("legacy", [False, True])
async def test_authorization_precedes_all_file_reads(setup, monkeypatch, legacy):
    from creator import bot_setup
    w = setup

    def forbidden(*args):
        pytest.fail("An unauthorized callback read files")

    monkeypatch.setattr(bot_setup, "_read_object", forbidden)
    await w.feed(cb_update(BUYER, f"b:{w.id}:set" if legacy else w.cb("ready")))
    assert not w.session.of(EditMessageText)


@pytest.mark.parametrize("value", [None, [], {}, True, -1, float("nan"), float("inf"), "not a number"])
async def test_invalid_rule_value_is_not_presented_as_zero_or_valid(setup, value):
    w = setup
    setting = next(st for st in templates.info(w.template).settings if st.kind in {"int", "float"})
    (w.path / "settings.json").write_text(json.dumps({setting.key: value}))
    await w.feed(cb_update(OWNER, w.cb("group:rules")))
    assert f"{setting.label}: недоступно" in screen(w).text


async def test_zero_is_a_real_setting_not_a_false_default(setup):
    w = setup
    setting = next(st for st in templates.info(w.template).settings if st.kind in {"int", "float"})
    (w.path / "settings.json").write_text(json.dumps({setting.key: 0}))
    await w.feed(cb_update(OWNER, w.cb("group:rules")))
    assert f"{setting.label}: 0 (" in screen(w).text


@pytest.mark.parametrize("filename", ["config.json", "settings.json"])
async def test_oversized_file_is_unavailable_and_not_editable(setup, filename):
    w = setup
    (w.path / filename).write_text(json.dumps({"welcome": "X" * (256 * 1024)}))
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert "Приветствие: недоступно" in screen(w).text
    await w.feed(cb_update(OWNER, w.cb("edit:welcome")))
    assert await w.state.get_state() is None


@pytest.mark.parametrize("kind", ["none", "inaccessible", "other_private"])
async def test_inaccessible_or_wrong_private_message_rejected(setup, kind):
    from aiogram.types import Update
    w = setup
    raw = cb_update(OWNER, w.cb("ready")).model_dump(exclude_none=True)
    if kind == "none":
        raw["callback_query"].pop("message")
        raw["callback_query"]["inline_message_id"] = "unavailable"
    elif kind == "inaccessible":
        raw["callback_query"]["message"]["date"] = 0
        raw["callback_query"]["message"].pop("text", None)
    else:
        raw["callback_query"]["message"]["chat"]["id"] = BUYER
    await w.feed(Update.model_validate(raw))
    assert not w.session.of(EditMessageText) and not w.session.of(SendMessage)
    assert w.session.of(AnswerCallbackQuery)[-1].text


async def test_sub_deep_link_continues_after_new_navigation_and_checks_owner(setup):
    w = setup
    await w.feed(cb_update(OWNER, w.cb("ready")))
    await w.feed(msg_update(OWNER, f"/start sub_{w.id}"))
    assert await w.state.get_state() == "Wizard:active"
    assert "Обязательная подписка" in screen(w).text
    w.session.clear()
    await w.feed(msg_update(BUYER, f"/start sub_{w.id}"))
    assert "Бот не найден" in screen(w).text


@pytest.mark.parametrize("change", ["fsm", "owner", "template"])
async def test_edit_transition_does_not_overwrite_state_after_network_changes(setup, monkeypatch, change):
    w = setup
    original = w.session.make_request

    async def changed(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            if change == "fsm":
                await w.state.set_state("Info:report")
                await w.state.set_data({"keep": "new"})
            elif change == "owner":
                w.ctx.db.transfer_bot(w.id, BUYER)
            else:
                w.ctx.db.update_bot(w.id, template="starbot" if w.template != "starbot" else "clicker")
        return await original(bot, method, timeout)

    monkeypatch.setattr(w.session, "make_request", changed)
    await w.feed(cb_update(OWNER, w.cb("edit:welcome")))
    if change == "fsm":
        assert await w.state.get_state() == "Info:report" and await w.state.get_data() == {"keep": "new"}
    else:
        assert await w.state.get_state() is None


async def test_unfinished_own_channel_wizard_is_not_reset_by_stale_back(setup):
    w = setup
    await w.state.set_state("Wizard:active")
    data = {"bot_id": w.id, "user_id": OWNER, "stage": "channel", "keep": "draft"}
    await w.state.set_data(data)
    await w.feed(cb_update(OWNER, f"b:{w.id}:set"))
    assert await w.state.get_state() == "Wizard:active" and await w.state.get_data() == data


@pytest.mark.parametrize("key", [None, [], {}, True, 17])
async def test_corrupt_editor_key_is_a_safe_stale_input(setup, key):
    w = setup
    await w.state.set_state("BotEdit:setting")
    await w.state.set_data({"bot_id": w.id, "key": key, "template": w.template})
    before = (w.path / "settings.json").read_bytes()
    await w.feed(msg_update(OWNER, "MUST_NOT_SAVE"))
    assert await w.state.get_state() is None
    assert (w.path / "settings.json").read_bytes() == before
    assert "устарела" in screen(w).text


@pytest.mark.parametrize("action", ["channel", "edit:welcome"])
async def test_transitions_do_not_disclose_values_from_mismatched_config(setup, action):
    w = setup
    config = json.loads((w.path / "config.json").read_text())
    config["owner_id"] = BUYER
    (w.path / "config.json").write_text(json.dumps(config))
    (w.path / "settings.json").write_text(json.dumps({
        "required_channel": "-100123", "channel_url": "https://t.me/+PRIVATE_OLD_INVITE"}))
    await w.feed(cb_update(OWNER, w.cb(action)))
    assert not w.session.of(SendMessage) and not w.session.of(EditMessageText)
    assert await w.state.get_state() is None


async def test_back_card_redacts_known_token_before_error_truncation(setup):
    w = setup
    w.ctx.db.update_bot(w.id, status="error", last_error="E" * 295 + TOKEN)
    await w.feed(cb_update(OWNER, w.cb("card")))
    assert TOKEN[:5] not in screen(w).text


@pytest.mark.parametrize("channel,url", [
    ("-100123", ""), ("not a channel", "https://t.me/+invite"),
    ("-100123", "https://evil.test/invite"), ("-100123", None),
])
async def test_invalid_channel_pair_is_not_presented_as_configured(setup, channel, url):
    w = setup
    (w.path / "settings.json").write_text(json.dumps({"required_channel": channel, "channel_url": url}))
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert "Канал: не подтверждён" in screen(w).text


@pytest.mark.parametrize("channel,url", [
    ("-100123", "https://t.me/example_channel"),
    ("@example_channel", "https://t.me/example_channel"),
    ("-100123", "https://t.me/+private_invite"),
])
async def test_configured_channel_is_only_a_configuration_fact(setup, channel, url):
    w = setup
    (w.path / "settings.json").write_text(json.dumps({"required_channel": channel, "channel_url": url}))
    await w.feed(cb_update(OWNER, w.cb("ready")))
    assert "Канал: указан; права здесь не проверялись" in screen(w).text
    assert url not in screen(w).text
