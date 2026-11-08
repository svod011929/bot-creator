"""Integration: soft-pass never becomes a referral/subscription confirmation."""
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChat, GetChatMember

from child.files import write_setting
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import A, B, make


@pytest.mark.parametrize('action', ['start', 'chk_sub', 'tg_chk', 'pf_chk'])
async def test_soft_pass_does_not_pay_referrer(tmp_path, bot, session, monkeypatch, action):
    app, feed = make(tmp_path, 'refbot', bot)
    await feed(msg_update(A, '/start'))
    write_setting(tmp_path, 'required_channel', '@chan')
    original = session.make_request
    async def flaky(bot, method, timeout=None):
        if isinstance(method, (GetChat, GetChatMember)):
            raise TelegramNetworkError(method=method, message='SECRET')
        return await original(bot, method, timeout)
    monkeypatch.setattr(session, 'make_request', flaky)
    await feed(msg_update(B, f'/start r{A}'))
    if action != 'start':
        from child.core import PF_CHECK_CB, TG_CHECK_CB
        callback = {'chk_sub': 'chk_sub', 'tg_chk': TG_CHECK_CB, 'pf_chk': PF_CHECK_CB}[action]
        await feed(cb_update(B, callback))
    assert app.db.user(A)['balance'] == 0
    assert app.db.user(B)['ref_rewarded'] == 0
    assert any('60 секунд' in t for t in session.texts())


async def test_child_admin_deep_link_and_legacy_editor(tmp_path, bot, session):
    from tests.test_child_bots import OWNER
    app, feed = make(tmp_path, 'refbot', bot)
    # config fixture deliberately lacks bot_id: no guessed deep link.
    await feed(cb_update(OWNER, 'set:required_channel'))
    assert any('недоступ' in t for t in session.texts())
    app.cfg['bot_id'] = 7
    await feed(cb_update(OWNER, 'adm:set'))
    links = [b.url for req in session.requests for row in getattr(getattr(req, 'reply_markup', None),
             'inline_keyboard', []) or [] for b in row if b.url]
    assert 'https://t.me/creator_bot?start=sub_7' in links
    await feed(cb_update(OWNER, 'set:required_channel'))
    await feed(msg_update(OWNER, '@arbitrary'))
    assert not app.s['required_channel']


async def test_bad_legacy_join_link_not_shown(tmp_path, bot, session):
    app, feed = make(tmp_path, 'refbot', bot)
    write_setting(tmp_path, 'required_channel', '@chan')
    write_setting(tmp_path, 'channel_url', 'https://evil.invalid/leak')
    urls = [b.url for row in app.gate_keyboard().inline_keyboard for b in row if b.url]
    assert urls == ['https://t.me/chan']
