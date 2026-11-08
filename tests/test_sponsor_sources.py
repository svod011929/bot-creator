"""Source credentials: no real keys/network, fake Telegram and temp files only."""
import json
import os

import pytest
from aiogram.methods import DeleteMessage

from creator.config import Config
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, env  # noqa: F401


@pytest.mark.parametrize('provider', ['tgrass', 'piarflow'])
def test_store_precedence_and_permissions(tmp_path, provider):
    from creator.sponsor_sources import SourceError, SourceStore
    cfg = Config(data_dir=tmp_path)
    s = SourceStore(cfg, provider)
    assert s.resolve().origin == 'unset'
    legacy = tmp_path / f'{provider}_hub.json'
    legacy.write_text(json.dumps({'api_key': 'example-auto-key'}))
    assert s.resolve().origin == 'auto'
    old = s.revision()
    s.save('example-manual-key', old)
    assert s.resolve().key == 'example-manual-key'
    assert s.resolve().origin == 'manual'
    assert s.manual_path.stat().st_mode & 0o777 == 0o600
    assert s.manual_path.parent.stat().st_mode & 0o777 == 0o700
    assert 'example-manual-key' not in repr(s.resolve())
    with pytest.raises(SourceError):
        s.save('example-other-key', old)
    setattr(cfg, f'{provider}_hub_api_key', 'example-env-key')
    assert s.resolve().origin == 'env'
    with pytest.raises(SourceError):
        s.save('example-other-key', s.revision())
    with pytest.raises(SourceError):
        s.remove(s.revision())
    setattr(cfg, f'{provider}_hub_api_key', '')
    s.remove(s.revision())
    assert s.resolve().key == 'example-auto-key'
    assert json.loads(legacy.read_text())['api_key'] == 'example-auto-key'


def test_store_failures_keep_old_key_and_reject_invalid(tmp_path, monkeypatch):
    from creator.sponsor_sources import SourceError, SourceStore
    s = SourceStore(Config(data_dir=tmp_path), 'tgrass')
    s.save('example-working-key', s.revision())
    before = s.manual_path.read_bytes()
    for key in ['', 'bad key', 'x'*513, '42:TEST-TOKEN', '<evil>', 'ключ']:
        with pytest.raises(SourceError):
            s.save(key, s.revision())
    def failed(*a):
        raise OSError('example-working-key is private')
    monkeypatch.setattr(os, 'replace', failed)
    with pytest.raises(SourceError) as error:
        s.save('example-next-key', s.revision())
    assert 'example-working-key' not in str(error.value)
    assert s.manual_path.read_bytes() == before
    assert not list(s.manual_path.parent.glob('*.tmp'))


@pytest.mark.parametrize('content', ['{broken', '{}', 'x'*8193, '[]'])
def test_invalid_manual_fails_closed(tmp_path, content):
    from creator.sponsor_sources import SourceError, SourceStore
    cfg = Config(data_dir=tmp_path)
    s = SourceStore(cfg, 'tgrass')
    s.manual_path.parent.mkdir(mode=0o700)
    s.manual_path.write_text(content)
    (tmp_path/'tgrass_hub.json').write_text('{"api_key":"example-legacy-key"}')
    with pytest.raises(SourceError):
        s.resolve()
    # Explicit replacement may repair corrupt manual file, no accidental fallback.
    s.save('example-fixed-key', s.revision())
    assert s.resolve().key == 'example-fixed-key'


def test_symlinks_and_unknown_provider_are_rejected(tmp_path):
    from creator.sponsor_sources import SourceError, SourceStore
    with pytest.raises(SourceError):
        SourceStore(Config(data_dir=tmp_path), '../bad')
    s = SourceStore(Config(data_dir=tmp_path), 'piarflow')
    target = tmp_path/'outside'
    target.write_text('{"api_key":"example-outside-key"}')
    s.manual_path.parent.mkdir(mode=0o700)
    s.manual_path.symlink_to(target)
    with pytest.raises(SourceError):
        s.resolve()
    with pytest.raises(SourceError):
        s.save('example-key', s.revision())
    assert json.loads(target.read_text())['api_key'] == 'example-outside-key'


@pytest.mark.parametrize('provider', ['tgrass', 'piarflow'])
def test_manual_hub_enables_network_without_factory(tmp_path, provider):
    from creator.sponsor_sources import SourceStore
    cfg = Config(data_dir=tmp_path)
    assert not getattr(cfg, f'{provider}_enabled')
    s = SourceStore(cfg, provider)
    s.save('example-source-key', s.revision())
    assert getattr(cfg, f'{provider}_enabled')


@pytest.mark.parametrize('provider', ['tgrass', 'piarflow'])
async def test_manual_hub_approval_missing_factory_and_rotation(env, provider):  # noqa: F811
    from creator.integrations import ensure_services
    from creator.sponsor_sources import SourceStore
    ctx, feed, session = env
    ctx.db.upsert_user(OWNER, 'example_owner', 'Example')
    draft = ctx.db.create_draft(OWNER, 'clicker', '42:TEST', 'example_child', 'Example')
    bid = ctx.db.materialize_draft(draft, 1000)
    apply = ctx.db.tgrass_apply if provider == 'tgrass' else ctx.db.pf_apply
    get = ctx.db.tgrass if provider == 'tgrass' else ctx.db.pf
    apply(bid, OWNER, 0)
    store = SourceStore(ctx.cfg, provider)
    store.save('example-source-key', store.revision())
    ensure_services(ctx)
    svc = getattr(ctx, provider)
    async def forbidden(*a, **kw):
        pytest.fail('No provider call with manual hub and absent factory')
    for name in ('new_bot', 'add_bot', 'balance'):
        if hasattr(svc.client, name):
            setattr(svc.client, name, forbidden)
    balance = ctx.db.user(OWNER)['balance']
    await svc.approve(bid, ADMIN)
    assert get(bid)['status'] == 'approved' and get(bid)['mode'] == 'relay'
    assert get(bid)['api_key'] == 'example-source-key'
    store.save('example-new-source', store.revision())
    await svc.approve(bid, ADMIN)  # approved is idempotent, not mass rotation
    assert get(bid)['api_key'] == 'example-source-key'
    await svc.disable(bid, ADMIN)
    await svc.approve(bid, ADMIN)
    assert get(bid)['api_key'] == 'example-new-source'
    store.remove(store.revision())
    ctx.sup.write_files(bid)  # saved relay snapshot survives deletion/restart
    config = json.loads((ctx.sup.bot_dir(bid)/'config.json').read_text())
    assert config['tgrass_key' if provider == 'tgrass' else 'piarflow_key'] == 'example-new-source'
    assert ctx.db.user(OWNER)['balance'] == balance
    await svc.client.close()


def action(session, suffix):
    for m in reversed(session.requests):
        kb = getattr(m, 'reply_markup', None)
        for row in getattr(kb, 'inline_keyboard', []) or []:
            for b in row:
                if b.callback_data and suffix in b.callback_data:
                    return b.callback_data
    raise AssertionError(f'Button not found: {suffix}')


@pytest.mark.parametrize('code,provider', [('tg', 'tgrass'), ('pf', 'piarflow')])
async def test_admin_secret_confirmation_delete_and_readonly(env, code, provider):  # noqa: F811
    from creator.sponsor_sources import SourceStore
    ctx, feed, session = env
    await feed(msg_update(ADMIN, '/start'))
    await feed(msg_update(OWNER, '/start'))
    before = '\n'.join(ctx.db.conn.iterdump())
    await feed(msg_update(ADMIN, '/admin'))
    assert any('Источники ОП' in b.text for m in session.requests
               for row in getattr(getattr(m, 'reply_markup', None), 'inline_keyboard', []) or [] for b in row)
    await feed(cb_update(ADMIN, 'src:home'))
    await feed(cb_update(ADMIN, f'src:{code}:view'))
    assert '\n'.join(ctx.db.conn.iterdump()) == before
    await feed(cb_update(ADMIN, f'src:{code}:edit'))
    assert 'сквозного шифрования' in session.texts()[-1]
    await feed(msg_update(ADMIN, 'example-secret-source-key'))
    assert session.of(DeleteMessage)
    assert not SourceStore(ctx.cfg, provider).manual_path.exists()
    save = action(session, ':save:')
    assert len(save.encode()) <= 64
    await feed(cb_update(OWNER, save))
    assert not SourceStore(ctx.cfg, provider).manual_path.exists()
    await feed(cb_update(ADMIN, save))
    s = SourceStore(ctx.cfg, provider)
    assert s.resolve().key == 'example-secret-source-key'
    saved_bytes = s.manual_path.read_bytes()
    await feed(cb_update(ADMIN, save))
    assert s.manual_path.read_bytes() == saved_bytes
    assert 'example-secret-source-key' not in repr(session.requests)
    assert 'example-secret-source-key' not in repr(ctx.source_inputs)
    await feed(cb_update(ADMIN, f'src:{code}:remove'))
    drop = action(session, ':drop:')
    assert s.manual_path.exists()
    await feed(cb_update(ADMIN, drop))
    assert not s.manual_path.exists()
    assert '\n'.join(ctx.db.conn.iterdump()) == before
    svc = getattr(ctx, provider)
    if svc:
        await svc.client.close()


@pytest.mark.parametrize('provider', ['tgrass', 'piarflow'])
async def test_factory_failure_uses_current_manual_and_no_source_is_pending(env, provider, caplog):  # noqa: F811
    from creator.integrations import ensure_services
    from creator.piarflow import PiarFlowError
    from creator.sponsor_sources import SourceStore
    from creator.tgrass import TGrassError
    ctx, feed, session = env
    setattr(ctx.cfg, 'tgrass_factory_token' if provider == 'tgrass' else 'piarflow_token', 'example-account')
    ctx.cfg.tgrass_owner_id = ADMIN
    ctx.db.upsert_user(OWNER, 'example', 'Example')
    bid = ctx.db.materialize_draft(ctx.db.create_draft(OWNER, 'starbot', '42:TEST', 'example', 'Example'), 0)
    apply = ctx.db.tgrass_apply if provider == 'tgrass' else ctx.db.pf_apply
    get = ctx.db.tgrass if provider == 'tgrass' else ctx.db.pf
    apply(bid, OWNER, 0)
    store = SourceStore(ctx.cfg, provider)
    ensure_services(ctx)
    svc = getattr(ctx, provider)
    calls = []
    error = TGrassError if provider == 'tgrass' else PiarFlowError
    async def failed(*a, **kw):
        calls.append(1)
        raise error('example-secret-in-provider-error', 401)
    setattr(svc.client, 'new_bot' if provider == 'tgrass' else 'add_bot', failed)
    ctx.cfg.bot_token = '42:EXAMPLE'
    before = ctx.db.user(OWNER)['balance']
    with pytest.raises(error):
        await svc.approve(bid, ADMIN)
    assert get(bid)['status'] == 'pending'
    assert ctx.db.user(OWNER)['balance'] == before
    assert not (ctx.sup.bot_dir(bid)/'config.json').exists()
    assert 'example-secret-in-provider-error' not in caplog.text
    assert 'example-secret-in-provider-error' not in (get(bid)['error'] or '')
    store.save('example-manual-source', store.revision())
    calls.clear()
    await svc.approve(bid, ADMIN)
    assert len(calls) == 1  # child factory failed; hub came from manual source, no second API call
    assert get(bid)['api_key'] == 'example-manual-source'
    assert get(bid)['mode'] == 'relay'
    assert ctx.db.user(OWNER)['balance'] == before
    await svc.client.close()


async def test_admin_state_replay_expiry_cancel_env_and_groups(env, monkeypatch):  # noqa: F811
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from creator.sponsor_sources import SourceStore
    from creator.states import BotEdit
    ctx, feed, session = env
    await feed(msg_update(ADMIN, '/start'))
    # Obtain the same Dispatcher MemoryStorage via the feed closure.
    dp = next(c.cell_contents for c in feed.__closure__ if hasattr(c.cell_contents, 'storage'))
    state = FSMContext(dp.storage, StorageKey(bot_id=42, chat_id=ADMIN, user_id=ADMIN))
    store = SourceStore(ctx.cfg, 'tgrass')
    await state.set_state(BotEdit.transfer)
    await state.set_data({'bot_id': 123})
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    assert await state.get_state() == BotEdit.transfer.state
    assert await state.get_data() == {'bot_id': 123}
    await state.clear()
    for data in ['src:no:view', 'src:tg:unknown', 'src:tg:save:bad:extra']:
        await feed(cb_update(ADMIN, data))
    assert not store.manual_path.exists()
    group = cb_update(ADMIN, 'src:tg:edit').model_dump()
    group['callback_query']['message']['chat'] = {'id': -100, 'type': 'supergroup'}
    from aiogram.types import Update
    await feed(Update.model_validate(group))
    assert await state.get_state() is None
    ctx.cfg.tgrass_hub_api_key = 'example-env-key'
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    assert await state.get_state() is None
    ctx.cfg.tgrass_hub_api_key = ''
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    await feed(msg_update(ADMIN, 'example-candidate-key'))
    save = action(session, ':save:')
    assert 'example-candidate-key' not in repr(await state.get_data())
    ctx.source_inputs[ADMIN].expires = 0
    await feed(cb_update(ADMIN, save))
    assert not store.manual_path.exists()
    await feed(msg_update(ADMIN, '/cancel'))
    assert not ctx.source_inputs
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    await feed(msg_update(ADMIN, 'example-other-key'))
    save = action(session, ':save:')
    await feed(msg_update(ADMIN, '/start'))
    await feed(cb_update(ADMIN, save))
    assert not store.manual_path.exists() and not ctx.source_inputs
    # Concurrent change after prompt is never silently overwritten.
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    await feed(msg_update(ADMIN, 'example-old-prompt-key'))
    save = action(session, ':save:')
    store.save('example-new-working-key', store.revision())
    await feed(cb_update(ADMIN, save))
    assert store.resolve().key == 'example-new-working-key'
    # Source views and callback payloads remain within Telegram limits.
    for m in session.requests:
        assert len(getattr(m, 'text', '') or '') < 4096
        for row in getattr(getattr(m, 'reply_markup', None), 'inline_keyboard', []) or []:
            assert all(len(b.callback_data.encode()) <= 64 for b in row if b.callback_data)


def test_store_revision_rejects_nonregular_file(tmp_path):
    from creator.sponsor_sources import SourceError, SourceStore
    store = SourceStore(Config(data_dir=tmp_path), 'tgrass')
    store.manual_path.parent.mkdir(mode=0o700)
    store.manual_path.mkdir()
    with pytest.raises(SourceError):
        store.revision()


async def test_second_secret_input_is_deleted_while_confirming(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, '/start'))
    await feed(cb_update(ADMIN, 'src:pf:edit'))
    await feed(msg_update(ADMIN, 'example-first-key'))
    old = len(session.of(DeleteMessage))
    await feed(msg_update(ADMIN, 'example-second-key'))
    assert len(session.of(DeleteMessage)) == old + 1
    assert 'example-second-key' not in repr(session.requests)


def test_revision_fifo_fails_without_blocking(tmp_path):
    import signal

    from creator.sponsor_sources import SourceError, SourceStore
    store = SourceStore(Config(data_dir=tmp_path), 'tgrass')
    store.manual_path.parent.mkdir(mode=0o700)
    os.mkfifo(store.manual_path)
    def timeout(*a):
        raise AssertionError('Source revision blocked on a nonregular file')
    old = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, .2)
    try:
        with pytest.raises(SourceError):
            store.revision()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


async def test_input_deletion_failure_revocation_and_cancel_keep_key(env, monkeypatch):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    from creator.sponsor_sources import SourceStore
    ctx, feed, session = env
    store = SourceStore(ctx.cfg, 'tgrass')
    store.save('example-working-key', store.revision())
    await feed(msg_update(ADMIN, '/start'))
    await feed(cb_update(ADMIN, 'src:tg:edit'))
    original = session.make_request
    async def failed_delete(bot, method, **kw):
        if isinstance(method, DeleteMessage):
            raise TelegramBadRequest(method=method, message='Deletion not allowed')
        return await original(bot, method, **kw)
    monkeypatch.setattr(session, 'make_request', failed_delete)
    await feed(msg_update(ADMIN, 'example-candidate-key'))
    save = action(session, ':save:')
    ctx.cfg.admin_ids.clear()
    await feed(cb_update(ADMIN, save))
    assert store.resolve().key == 'example-working-key'
    ctx.cfg.admin_ids.add(ADMIN)
    await feed(msg_update(ADMIN, '/cancel'))
    await feed(cb_update(ADMIN, save))
    assert store.resolve().key == 'example-working-key'
    assert 'example-candidate-key' not in repr(session.requests)


@pytest.mark.parametrize('provider', ['tgrass', 'piarflow'])
async def test_auto_registration_race_does_not_override_manual_source(env, provider):  # noqa: F811
    from creator.integrations import ensure_services
    from creator.sponsor_sources import SourceStore
    ctx, feed, session = env
    setattr(ctx.cfg, 'tgrass_factory_token' if provider == 'tgrass' else 'piarflow_token', 'example-account')
    ctx.cfg.tgrass_owner_id = ADMIN
    ctx.cfg.bot_token = '42:EXAMPLE'
    ensure_services(ctx)
    svc = getattr(ctx, provider)
    store = SourceStore(ctx.cfg, provider)
    async def registration(*a, **kw):
        store.save('example-concurrent-key', store.revision())
        return 'example-auto-key' if provider == 'tgrass' else {'api_key': 'example-auto-key', 'bot_id': 42}
    setattr(svc.client, 'new_bot' if provider == 'tgrass' else 'add_bot', registration)
    assert await svc.ensure_hub_key() == 'example-concurrent-key'
    assert not store.legacy_path.exists()
    await svc.client.close()
