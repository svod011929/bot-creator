"""Creator wizard through real dispatcher, with a fake CHILD client only."""
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat

from child.files import read_json, settings_pair, write_setting
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import BUYER, OWNER, create_paid_bot, env  # noqa: F401


def callback(session, action):
    for request in reversed(session.requests):
        kb = getattr(request, 'reply_markup', None)
        for row in getattr(kb, 'inline_keyboard', []) or []:
            for b in row:
                if b.callback_data and b.callback_data.startswith('sub:') and b.callback_data.split(':')[2] == action:
                    return b.callback_data
    raise AssertionError(f'No {action} callback')


@pytest.fixture
async def wizard(env, monkeypatch):  # noqa: F811
    from creator.handlers import subscription as sub
    from tests.test_subscription import Fake
    ctx, feed, session = env
    fake = Fake()
    @asynccontextmanager
    async def child_client(ctx, row):
        assert row['owner_id'] == OWNER
        yield fake
    monkeypatch.setattr(sub, 'child_client', child_client)
    await feed(msg_update(OWNER, '/start'))
    bot_id = await create_paid_bot(ctx, feed, session)
    path = ctx.sup.bot_dir(bot_id)
    await feed(cb_update(OWNER, f'sub:{bot_id}:open'))
    yield NS(ctx=ctx, feed=feed, session=session, fake=fake, bot_id=bot_id, path=path, sub=sub)


async def begin(w):
    await w.feed(cb_update(OWNER, callback(w.session, 'begin')))


async def enter(w, channel='@mychan'):
    await begin(w)
    await w.feed(msg_update(OWNER, channel))


async def save(w):
    await w.feed(cb_update(OWNER, callback(w.session, 'save')))


async def test_public_canonical_save_cancel_and_disable(wizard):
    w = wizard
    await enter(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')
    # Public URL prefilled, save still requires explicit confirmation.
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('-100123', 'https://t.me/mychan')
    write_setting(w.path, 'welcome', 'keep')
    await enter(w)
    await w.feed(msg_update(OWNER, '/cancel'))
    assert read_json(w.path / 'settings.json')['welcome'] == 'keep'
    assert settings_pair(read_json(w.path / 'settings.json')) == ('-100123', 'https://t.me/mychan')
    await w.feed(cb_update(OWNER, f'sub:{w.bot_id}:open'))
    await w.feed(cb_update(OWNER, callback(w.session, 'disable')))
    assert settings_pair(read_json(w.path / 'settings.json'))[0] == '-100123'
    await w.feed(cb_update(OWNER, callback(w.session, 'disable_ok')))
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '')


async def test_private_manual_link_and_hidden_forward(wizard):
    w = wizard
    async def private(target):
        return NS(id=-100123, type='channel', username=None, title='Private')
    w.fake.get_chat = private
    await begin(w)
    await w.feed(msg_update(OWNER, None))  # no usable channel metadata
    assert any('@username' in t for t in w.session.texts())
    await w.feed(msg_update(OWNER, '-100123'))
    await w.feed(msg_update(OWNER, 'https://t.me/+private_invite'))
    assert any('именно' in t for t in w.session.texts())
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('-100123', 'https://t.me/+private_invite')


async def test_channel_forward_metadata(wizard):
    w = wizard
    await begin(w)
    await w.feed(msg_update(OWNER, None, forward_origin={'type': 'channel', 'date': 1700000000,
        'chat': {'id': -100123, 'type': 'channel', 'title': 'Real'}, 'message_id': 1}))
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json'))[0] == '-100123'


@pytest.mark.parametrize('failure', ['admin', 'badrequest', 'type', 'save_rights'])
async def test_invalid_child_rights_never_save(wizard, failure):
    w = wizard
    if failure == 'save_rights':
        await enter(w)
        w.fake.admin = False
        await save(w)
    else:
        w.fake.admin = failure != 'admin'
        w.fake.type = 'supergroup' if failure == 'type' else 'channel'
        if failure == 'badrequest':
            w.fake.error = TelegramBadRequest(method=GetChat(chat_id=-100123), message='SECRET')
        await enter(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')
    assert all('SECRET' not in t for t in w.session.texts())


async def test_ownership_change_during_validation(wizard):
    w = wizard
    w.fake.hook = lambda: w.ctx.db.transfer_bot(w.bot_id, BUYER)
    await enter(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')
    assert w.ctx.db.bot(w.bot_id)['owner_id'] == BUYER


async def test_stale_and_legacy_callbacks_no_bypass(wizard):
    w = wizard
    await enter(w)
    stale = callback(w.session, 'save')
    await w.feed(msg_update(OWNER, '/cancel'))
    await w.feed(cb_update(OWNER, stale))
    await w.feed(cb_update(OWNER, f'b:{w.bot_id}:sk:required_channel'))
    await w.feed(msg_update(OWNER, '@malicious'))
    await w.feed(cb_update(OWNER, f'b:{w.bot_id}:sk:channel_url'))
    await w.feed(msg_update(OWNER, 'https://evil.invalid'))
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_deep_link_owner_check(wizard):
    w = wizard
    await w.feed(msg_update(OWNER, f'/start sub_{w.bot_id}'))
    assert 'Обязательная подписка' in w.session.texts()[-1]
    await w.feed(msg_update(BUYER, f'/start sub_{w.bot_id}'))
    assert 'не найден' in w.session.texts()[-1].lower()


async def test_pair_conflict_and_failed_save(wizard, monkeypatch):
    w = wizard
    await enter(w)
    write_setting(w.path, 'channel_url', 'https://t.me/+other')
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', 'https://t.me/+other')


async def test_recheck_does_not_change_pair(wizard):
    w = wizard
    await enter(w)
    await save(w)
    before = (w.path / 'settings.json').read_bytes()
    w.fake.admin = False
    await w.feed(cb_update(OWNER, callback(w.session, 'recheck')))
    assert (w.path / 'settings.json').read_bytes() == before
    assert (w.path / '.subscription-cache-revision').exists()


async def test_previous_confirm_callback_not_valid_in_new_wizard(wizard):
    w = wizard
    await enter(w)
    stale = callback(w.session, 'save')
    await w.feed(cb_update(OWNER, callback(w.session, 'begin')))
    await w.feed(msg_update(OWNER, '@mychan'))
    await w.feed(cb_update(OWNER, stale))
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_expired_draft(wizard, monkeypatch):
    w = wizard
    await enter(w)
    save_cb = callback(w.session, 'save')
    real = w.sub.time.monotonic
    monkeypatch.setattr(w.sub.time, 'monotonic', lambda: real() + 1000)
    await w.feed(cb_update(OWNER, save_cb))
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_actual_file_save_failure_is_retryable(wizard, monkeypatch):
    w = wizard
    await enter(w)
    original = w.sub.write_settings_pair
    def fail(*args, **kwargs):
        raise PermissionError('SECRET')
    monkeypatch.setattr(w.sub, 'write_settings_pair', fail)
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')
    monkeypatch.setattr(w.sub, 'write_settings_pair', original)
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json'))[0] == '-100123'


async def test_status_has_exit_to_bot_settings(wizard):
    w = wizard
    assert any(b.callback_data == f'b:{w.bot_id}:set' for req in w.session.requests
               for row in getattr(getattr(req, 'reply_markup', None), 'inline_keyboard', []) or [] for b in row)


async def test_cancel_during_final_await_does_not_save(wizard):
    w = wizard
    await enter(w)
    original = w.fake.get_chat
    async def cancel_then_return(target):
        await w.feed(msg_update(OWNER, '/cancel'))
        return await original(target)
    w.fake.get_chat = cancel_then_return
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_wizard_rate_limit_respected(wizard):
    from aiogram.exceptions import TelegramRetryAfter
    w = wizard
    w.fake.error = TelegramRetryAfter(method=GetChat(chat_id=-100123), message='SECRET', retry_after=120)
    await enter(w)
    await w.feed(cb_update(OWNER, callback(w.session, 'retry')))
    assert w.fake.calls == 1
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_owner_transfer_during_final_check_never_writes(wizard):
    w = wizard
    await enter(w)
    w.fake.hook = lambda: w.ctx.db.transfer_bot(w.bot_id, BUYER)
    await save(w)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_legacy_fsm_values_cannot_write(wizard, bot):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from creator.handlers.mybots import setting_value
    from creator.states import BotEdit
    w = wizard
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=OWNER, user_id=OWNER))
    await state.set_state(BotEdit.setting)
    await state.set_data({'bot_id': w.bot_id, 'key': 'required_channel'})
    message = msg_update(OWNER, '@unsafe').message.as_(bot)
    await setting_value(message, state, w.ctx)
    assert settings_pair(read_json(w.path / 'settings.json')) == ('', '-')


async def test_saved_screen_has_truthful_verified_status(wizard):
    w = wizard
    await enter(w)
    await save(w)
    assert 'права проверены' in w.session.texts()[-1]


async def test_callback_acknowledged_before_slow_child_api(wizard):
    from aiogram.methods import AnswerCallbackQuery
    w = wizard
    await enter(w)
    saved = callback(w.session, 'save')
    w.session.clear()
    def during_api():
        assert w.session.of(AnswerCallbackQuery), 'callback not acknowledged before child API'
    w.fake.hook = during_api
    await w.feed(cb_update(OWNER, saved))
    assert settings_pair(read_json(w.path / 'settings.json'))[0] == '-100123'
