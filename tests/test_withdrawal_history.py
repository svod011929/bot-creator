"""History through real child dispatcher + fake Telegram, never production data."""
import os
import time

import pytest
from aiogram.methods import SendMessage
from aiogram.types import Update

from child.files import read_config, write_config
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import OWNER, A, B, make

SECRET = 'fake_payment_<secret>'


def seed(app, uid=A, status='pending', created=100, currency='⭐'):
    app.db.exec('INSERT INTO withdrawals(user_id,amount,details,status,created_at,currency_at_request,resolved_at) '
                'VALUES(?,?,?,?,?,?,?)', uid, 3.5, SECRET, status, created, currency, None)
    return app.db.scalar('SELECT MAX(id) FROM withdrawals')


def last_text(session):
    texts = session.texts()
    assert texts, 'No screen was rendered'
    return texts[-1]


def buttons(session):
    for request in reversed(session.requests):
        keyboard = getattr(getattr(request, 'reply_markup', None), 'inline_keyboard', None)
        if keyboard:
            return [b for row in keyboard for b in row]
    return []


def snapshot(app):
    return ([tuple(r) for r in app.db.all('SELECT * FROM withdrawals ORDER BY id')],
            [tuple(r) for r in app.db.all('SELECT * FROM users ORDER BY id')])


async def test_zero_balance_profile_history_and_empty_state(tmp_path, bot, session):
    app, feed = make(tmp_path, 'clicker', bot)
    await feed(msg_update(A, '/start'))
    await feed(msg_update(A, '👤 Профиль'))
    assert any(b.text == '📜 Мои заявки' for b in buttons(session))
    before = snapshot(app)
    await feed(cb_update(A, 'wh:list:mine:0'))
    assert 'пока нет заявок' in last_text(session)
    assert snapshot(app) == before
    await feed(cb_update(A, 'wh:profile'))
    assert 'Профиль' in last_text(session)


@pytest.mark.parametrize('count', [1, 10, 11, 21])
async def test_ten_per_page_navigation_no_details_or_balance_changes(tmp_path, bot, session, count):
    app, feed = make(tmp_path, 'refbot', bot)
    for _ in range(count):
        seed(app)
    seed(app, uid=B, status='paid')
    before = snapshot(app)
    await feed(cb_update(A, 'wh:list:mine:0'))
    text = last_text(session)
    assert 'Мои заявки' in text and 'UTC' in text and SECRET not in text
    details = [b.callback_data for b in buttons(session) if b.callback_data.startswith('wh:detail:')]
    assert len(details) == min(count, 10)
    assert details[0].endswith(':' + str(count))
    assert any(b.text == 'Далее ➡️' for b in buttons(session)) == (count > 10)
    await feed(cb_update(A, 'wh:list:mine:9999'))
    assert f'Страница {max(1, (count+9)//10)}' in last_text(session)
    assert snapshot(app) == before


async def test_owner_tabs_orders_and_stale_last_page(tmp_path, bot, session):
    app, feed = make(tmp_path, 'refbot', bot)
    for _ in range(11):
        seed(app)
    completed = seed(app, status='paid', created=200)
    await feed(cb_update(OWNER, 'adm:wd'))
    assert 'Ожидают' in last_text(session)
    details = [b.callback_data for b in buttons(session) if b.callback_data.startswith('wh:detail:')]
    assert details[0].endswith(':1') and len(details) == 10
    assert SECRET not in last_text(session)
    await feed(cb_update(OWNER, 'wh:list:completed:0'))
    assert 'Завершённые' in last_text(session) and any(b.callback_data.endswith(':'+str(completed)) for b in buttons(session))
    app.db.resolve_withdrawal(11, True)
    await feed(cb_update(OWNER, 'wh:list:pending:1'))
    assert 'Страница 1' in last_text(session)
    app.db.exec("UPDATE withdrawals SET status='paid'")
    await feed(cb_update(OWNER, 'wh:list:pending:0'))
    assert 'Ожидающих заявок нет' in last_text(session)


async def test_authorized_details_legacy_unknowns_and_completed_buttons(tmp_path, bot, session):
    app, feed = make(tmp_path, 'starbot', bot)
    wid = seed(app, status='paid', currency=None)
    await feed(cb_update(A, f'wh:detail:mine:0:{wid}'))
    text = last_text(session)
    assert 'единица баланса не сохранена' in text and 'Дата обработки неизвестна' in text
    assert 'fake_payment_&lt;secret&gt;' in text
    assert not any(b.callback_data.startswith('wd:') for b in buttons(session))
    await feed(cb_update(OWNER, f'wh:detail:completed:0:{wid}'))
    assert 'Отмечена выплаченной' in last_text(session)
    assert not any(b.callback_data.startswith('wd:') for b in buttons(session))


@pytest.mark.parametrize('data', ['wh:detail:mine:0:1', 'wh:detail:pending:0:1',
    'wh:list:pending:0', 'wh:list:completed:0', 'wd:no:1', 'wd:wat:1',
    'wd:ok', 'wd:no:-1', 'wh:list:mine:-1', 'wh:list:mine:9999999999999999',
    'wh:detail:mine:0:99999999999999999999999', 'wh:list:all:0', 'wh:list:mine:0:extra'])
async def test_forged_foreign_and_bad_callbacks_no_details_or_mutations(tmp_path, bot, session, data):
    app, feed = make(tmp_path, 'refbot', bot)
    seed(app, uid=A)
    before = snapshot(app)
    await feed(cb_update(B, data))
    assert all('fake_payment' not in text for text in session.texts())
    assert snapshot(app) == before


@pytest.mark.parametrize('actor', [A, OWNER])
async def test_group_context_never_leaks_history_or_resolves(tmp_path, bot, session, actor):
    app, feed = make(tmp_path, 'refbot', bot)
    seed(app)
    data = cb_update(actor, 'wh:detail:mine:0:1' if actor == A else 'wd:no:1').model_dump(mode='json')
    data['callback_query']['message']['chat'] = {'id': -999, 'type': 'group', 'title': 'fake group'}
    await feed(Update.model_validate(data))
    assert all('fake_payment' not in text for text in session.texts())
    assert app.db.withdrawal(1)['status'] == 'pending'


async def test_unknown_owner_action_not_rejection_and_resolution_once(tmp_path, bot, session, monkeypatch):
    import child.db as mod
    app, feed = make(tmp_path, 'clicker', bot)
    app.db.upsert_user(A, None, 'fake', None)
    app.db.add_balance(A, 100)
    wid = app.db.create_withdrawal(A, 10, SECRET, currency='🪙')
    session.clear()
    await feed(cb_update(OWNER, f'wd:unknown:{wid}'))
    assert app.db.withdrawal(wid)['status'] == 'pending' and app.db.user(A)['balance'] == 90
    monkeypatch.setattr(mod, 'now', lambda: 200)
    await feed(cb_update(OWNER, f'wd:no:{wid}'))
    assert app.db.withdrawal(wid)['resolved_at'] == 200
    assert app.db.user(A)['balance'] == 100
    notices = [m for m in session.of(SendMessage) if m.chat_id == A]
    assert len(notices) == 1
    assert '200' not in notices[0].text  # same existing notification, not new metadata broadcast
    monkeypatch.setattr(mod, 'now', lambda: 300)
    await feed(cb_update(OWNER, f'wd:no:{wid}'))
    assert app.db.withdrawal(wid)['resolved_at'] == 200 and app.db.user(A)['balance'] == 100
    assert len([m for m in session.of(SendMessage) if m.chat_id == A]) == 1


async def test_transfer_revokes_old_owner_and_preserves_personal_history(tmp_path, bot, session):
    app, feed = make(tmp_path, 'starbot', bot)
    seed(app)
    cfg = read_config(tmp_path)
    cfg['owner_id'] = B
    write_config(tmp_path, cfg)
    before = snapshot(app)
    await feed(cb_update(OWNER, 'wd:no:1'))
    assert snapshot(app) == before
    session.clear()
    await feed(cb_update(B, 'wh:detail:pending:0:1'))
    assert 'fake_payment_&lt;secret&gt;' in last_text(session)
    await feed(cb_update(A, 'wh:detail:mine:0:1'))
    assert 'fake_payment_&lt;secret&gt;' in last_text(session)


@pytest.mark.parametrize('template,unit', [('clicker', '🪙'), ('refbot','₽'), ('starbot','⭐')])
async def test_creation_captures_unit_at_insert_then_config_change_not_relabel(tmp_path, bot, session, template, unit):
    app, feed = make(tmp_path, template, bot)
    app.set('min_withdraw', 1)
    await feed(msg_update(A, '/start'))
    app.db.add_balance(A, 10)
    await feed(msg_update(A, '💸 Вывод'))
    await feed(msg_update(A, '3'))
    app.set('currency', unit + '<new>')
    await feed(msg_update(A, SECRET))
    assert app.db.withdrawal(1)['currency_at_request'] == unit + '<new>'
    app.set('currency', 'different')
    await feed(cb_update(A, 'wh:detail:mine:0:1'))
    assert unit + '&lt;new&gt;' in last_text(session) and 'different' not in last_text(session)


async def test_dates_are_utc_despite_process_timezone():
    from child.withdrawals import format_date
    previous = os.environ.get('TZ')
    try:
        results = []
        for zone in ('UTC', 'Europe/Helsinki', 'Europe/Amsterdam'):
            os.environ['TZ'] = zone
            time.tzset()
            results.append(format_date(100))
        assert results == ['01.01.1970 00:01:40 UTC'] * 3
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


async def test_owner_pending_card_explains_manual_mark_and_keeps_back_context(tmp_path, bot, session):
    app, feed = make(tmp_path, 'clicker', bot)
    for _ in range(11):
        seed(app)
    await feed(cb_update(OWNER, 'wh:detail:pending:1:11'))
    assert 'ручная отметка' in last_text(session)
    assert any(b.callback_data == 'wd:ok:11:1' for b in buttons(session))
    assert any(b.callback_data == 'wh:list:pending:1' for b in buttons(session))
    await feed(cb_update(OWNER, 'wd:ok:11:1'))
    assert app.db.withdrawal(11)['status'] == 'paid'
    assert 'Отмечена выплаченной' in last_text(session)
    assert not any(b.callback_data.startswith('wd:') for b in buttons(session))
    assert any(b.callback_data == 'wh:list:pending:1' for b in buttons(session))
    await feed(cb_update(OWNER, 'wh:list:pending:1'))
    assert 'Страница 1' in last_text(session)


async def test_history_keeps_withdraw_fsm_and_does_not_notify(tmp_path, bot, session):
    from child.__main__ import build_dispatcher
    from child.core import Withdraw
    app, _ = make(tmp_path, 'refbot', bot)
    seed(app)
    dp = build_dispatcher(app)
    state = dp.fsm.get_context(bot=bot, chat_id=A, user_id=A)
    await state.set_state(Withdraw.details)
    await state.set_data({'amount': 7})
    before = snapshot(app)
    for data in ('wh:list:mine:0', 'wh:detail:mine:0:1', 'wh:list:mine:1'):
        await dp.feed_update(bot, cb_update(A, data))
    assert await state.get_state() == Withdraw.details.state
    assert await state.get_data() == {'amount': 7}
    assert snapshot(app) == before
    assert not session.of(SendMessage)


async def test_subscription_gate_still_blocks_history(tmp_path, bot, session):
    app, feed = make(tmp_path, 'clicker', bot)
    seed(app)
    app.set('required_channel', '-100123')
    app.set('channel_url', 'https://t.me/chan')
    session.member_status = 'left'
    await feed(cb_update(A, 'wh:detail:mine:0:1'))
    assert not session.texts()
    assert app.db.withdrawal(1)['status'] == 'pending'


@pytest.mark.parametrize('content', ['{}', '[]', '{bad', '{"owner_id":"1"}'])
async def test_missing_or_invalid_current_owner_fails_closed(tmp_path, bot, session, content):
    app, feed = make(tmp_path, 'clicker', bot)
    seed(app)
    (tmp_path / 'config.json').write_text(content)
    before = snapshot(app)
    for data in ('adm:wd', 'wh:detail:pending:0:1', 'wd:no:1'):
        await feed(cb_update(OWNER, data))
    assert not session.texts() and snapshot(app) == before


@pytest.mark.parametrize('data', ['wh:detail:pending:0:1', 'wh:admin'])
async def test_transfer_during_acknowledgement_blocks_sensitive_read(tmp_path, bot, session, monkeypatch, data):
    from aiogram.methods import AnswerCallbackQuery
    app, feed = make(tmp_path, 'clicker', bot)
    seed(app)
    original = session.make_request
    async def transfer(bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            cfg = read_config(tmp_path)
            cfg['owner_id'] = B
            write_config(tmp_path, cfg)
        return await original(bot, method, timeout)
    monkeypatch.setattr(session, 'make_request', transfer)
    await feed(cb_update(OWNER, data))
    assert not session.texts()


@pytest.mark.parametrize('unit,label', [(None, 'единица баланса не сохранена'), ('','единица не задана'),
                                      ('<b>unit</b>', '&lt;b&gt;unit&lt;/b&gt;')])
async def test_unit_unknown_empty_and_html_distinct_without_storage_changes(tmp_path, bot, session, unit, label):
    app, feed = make(tmp_path, 'clicker', bot)
    seed(app, currency=unit)
    before = snapshot(app)
    await feed(cb_update(A, 'wh:detail:mine:0:1'))
    assert label in last_text(session) and snapshot(app) == before


async def test_unavailable_card_and_old_completed_callback_no_notifications(tmp_path, bot, session):
    from aiogram.methods import AnswerCallbackQuery
    app, feed = make(tmp_path, 'clicker', bot)
    wid = seed(app, status='paid', currency=None)
    before = snapshot(app)
    await feed(cb_update(OWNER, f'wd:no:{wid}'))
    assert any(m.text == 'Заявка уже обработана' for m in session.of(AnswerCallbackQuery))
    assert 'Дата обработки неизвестна' in last_text(session)
    assert not session.of(SendMessage)
    assert snapshot(app) == before
    session.clear()
    await feed(cb_update(A, 'wh:detail:mine:0:99999'))
    assert any(m.text == 'Заявка недоступна' for m in session.of(AnswerCallbackQuery))
    assert not session.texts()


async def test_new_owner_can_resolve_old_queue_old_owner_keeps_own_rows(tmp_path, bot, session):
    app, feed = make(tmp_path, 'clicker', bot)
    await feed(msg_update(A, '/start'))
    app.db.add_balance(A, 20)
    wid = app.db.create_withdrawal(A, 3.5, SECRET, currency='🪙')
    own_id = seed(app, uid=OWNER)
    cfg = read_config(tmp_path)
    cfg['owner_id'] = B
    write_config(tmp_path, cfg)
    await feed(cb_update(B, f'wd:no:{wid}'))
    assert app.db.withdrawal(wid)['status'] == 'rejected' and app.db.user(A)['balance'] == 20
    await feed(cb_update(OWNER, 'wh:list:mine:0'))
    details = [b.callback_data for b in buttons(session) if b.callback_data.startswith('wh:detail:')]
    assert details == [f'wh:detail:mine:0:{own_id}']
    await feed(cb_update(OWNER, f'wh:detail:mine:0:{own_id}'))
    assert 'fake_payment_&lt;secret&gt;' in last_text(session)
    assert not any(b.callback_data.startswith('wd:') for b in buttons(session))


async def test_private_callback_from_different_chat_rejected(tmp_path, bot, session):
    app, feed = make(tmp_path, 'refbot', bot)
    seed(app)
    data = cb_update(A, 'wh:detail:mine:0:1').model_dump(mode='json')
    data['callback_query']['message']['chat']['id'] = B
    await feed(Update.model_validate(data))
    assert not session.texts()


async def test_failed_detail_render_logs_no_payment_details(tmp_path, bot, session, monkeypatch, caplog):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    app, feed = make(tmp_path, 'clicker', bot)
    seed(app)
    before = snapshot(app)
    original = session.make_request
    async def fail(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message=SECRET)
        return await original(bot, method, timeout)
    monkeypatch.setattr(session, 'make_request', fail)
    await feed(cb_update(A, 'wh:detail:mine:0:1'))
    assert SECRET not in caplog.text and 'fake_payment' not in caplog.text
    assert not session.of(SendMessage) and snapshot(app) == before


async def test_empty_configured_unit_creation_notification_labels_not_unknown(tmp_path, bot, session):
    app, feed = make(tmp_path, 'clicker', bot)
    app.set('min_withdraw', 1)
    await feed(msg_update(A, '/start'))
    app.db.add_balance(A, 10)
    await feed(msg_update(A, '💸 Вывод'))
    await feed(msg_update(A, '3'))
    app.set('currency', '')
    session.clear()
    await feed(msg_update(A, SECRET))
    assert app.db.withdrawal(1)['currency_at_request'] == ''
    owner_notices = [m for m in session.of(SendMessage) if m.chat_id == OWNER]
    assert len(owner_notices) == 1
    assert 'единица не задана' in owner_notices[0].text
