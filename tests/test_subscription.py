"""Own-channel access vs subscription; deterministic clock and fake Telegram."""
from types import SimpleNamespace as NS

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe

from child.files import invalidate_subscription, write_setting


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def advance(self, n):
        self.now += n


class Fake:
    id = 42
    def __init__(self):
        self.error = None
        self.status = 'member'
        self.admin = True
        self.type = 'channel'
        self.calls = 0
        self.hook = None
    async def get_chat(self, target):
        self.calls += 1
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.error:
            raise self.error
        return NS(id=-100123, type=self.type, username='mychan', title='Channel')
    async def get_chat_member(self, channel, uid):
        if uid == self.id:
            return NS(status='administrator' if self.admin else 'left')
        return NS(status=self.status, is_member=self.status == 'restricted_true')


@pytest.fixture
def setup(tmp_path):
    from child.subscription import SubscriptionGate
    write_setting(tmp_path, 'required_channel', '@mychan')
    clock = Clock()
    return SubscriptionGate(tmp_path, owner_id=99, clock=clock), Fake(), clock, tmp_path


async def test_admin_and_channel_only(setup):
    gate, fake, clock, path = setup
    fake.admin = False
    assert (await gate.check(fake, 10)).value == 'config_error'
    fake.admin = True
    fake.type = 'supergroup'
    assert (await gate.check(fake, 10)).value == 'config_error'
    fake.type = 'channel'
    assert (await gate.check(fake, 10)).value == 'subscribed'


@pytest.mark.parametrize('status', ['member', 'administrator', 'creator'])
async def test_membership_cache_expires(setup, status):
    gate, fake, clock, path = setup
    fake.status = status
    assert (await gate.check(fake, 10)).confirmed
    fake.status = 'left'
    clock.advance(599)
    assert (await gate.check(fake, 10)).confirmed
    clock.advance(1)
    assert (await gate.check(fake, 10)).value == 'not_subscribed'


@pytest.mark.parametrize('status', ['left', 'kicked', 'restricted'])
async def test_not_subscribed_never_soft_pass(setup, status):
    gate, fake, clock, path = setup
    fake.status = status
    result = await gate.check(fake, 10)
    assert not result.allowed and not result.confirmed


@pytest.mark.parametrize('exc', [TelegramNetworkError, TelegramServerError, TimeoutError])
async def test_temporary_window_not_sliding(setup, exc):
    gate, fake, clock, path = setup
    fake.error = exc() if exc is TimeoutError else exc(method=GetMe(), message='SECRET')
    result = await gate.check(fake, 10)
    assert result.value == 'temporary_access' and result.allowed and not result.confirmed
    clock.advance(59)
    assert (await gate.check(fake, 10)).value == 'temporary_access'
    assert fake.calls == 1
    clock.advance(1)
    fake.error = None
    fake.status = 'left'
    assert not (await gate.check(fake, 10)).allowed
    assert fake.calls == 2


async def test_rate_limit_not_ignored_after_window(setup):
    gate, fake, clock, path = setup
    fake.error = TelegramRetryAfter(method=GetMe(), message='SECRET', retry_after=120)
    assert (await gate.check(fake, 10)).value == 'temporary_access'
    clock.advance(60)
    assert (await gate.check(fake, 10)).value == 'temporary_unavailable'
    assert fake.calls == 1
    clock.advance(60)
    fake.error = None
    assert (await gate.check(fake, 10)).confirmed


@pytest.mark.parametrize('exc', [TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError, ValueError])
async def test_configuration_errors_never_soft_pass(setup, exc, caplog):
    gate, fake, clock, path = setup
    fake.error = exc('SECRET') if exc is ValueError else exc(method=GetMe(), message='SECRET')
    assert (await gate.check(fake, 10)).value == 'config_error'
    assert 'SECRET' not in caplog.text


async def test_pair_change_and_revision_clear_caches(setup):
    gate, fake, clock, path = setup
    assert (await gate.check(fake, 10)).confirmed
    write_setting(path, 'welcome', 'new')
    assert (await gate.check(fake, 10)).confirmed
    assert fake.calls == 1
    invalidate_subscription(path)
    fake.admin = False
    assert (await gate.check(fake, 10)).value == 'config_error'
    fake.admin = True
    assert (await gate.check(fake, 10)).confirmed
    fake.status = 'left'
    write_setting(path, 'required_channel', '@otherchan')
    assert not (await gate.check(fake, 10)).allowed


async def test_stale_await_never_grants_new_channel(setup):
    gate, fake, clock, path = setup
    fake.hook = lambda: write_setting(path, 'required_channel', '@otherchan')
    assert not (await gate.check(fake, 10)).allowed
    fake.status = 'left'
    assert not (await gate.check(fake, 10)).allowed


async def test_owner_disabled_and_corrupt_settings(setup):
    gate, fake, clock, path = setup
    (path / 'settings.json').write_text('invalid')
    assert (await gate.check(fake, 99)).allowed
    assert (await gate.check(fake, 10)).value == 'config_error'
    (path / 'settings.json').unlink()
    assert (await gate.check(fake, 10)).value == 'disabled'
    assert fake.calls == 0


async def test_restricted_member_and_unknown_status(setup):
    gate, fake, clock, path = setup
    original = fake.get_chat_member
    async def restricted(channel, uid):
        return await original(channel, uid) if uid == fake.id else NS(status='restricted', is_member=True)
    fake.get_chat_member = restricted
    assert (await gate.check(fake, 10)).confirmed
    fake.get_chat_member = original
    fake.status = 'unknown'
    assert (await gate.check(fake, 11)).value == 'config_error'


async def test_membership_api_error_and_new_transient_window(setup):
    gate, fake, clock, path = setup
    original = fake.get_chat_member
    async def unavailable(channel, uid):
        if uid == fake.id:
            return await original(channel, uid)
        raise TelegramNetworkError(method=GetMe(), message='SECRET')
    fake.get_chat_member = unavailable
    assert (await gate.check(fake, 10)).value == 'temporary_access'
    clock.advance(60)
    assert (await gate.check(fake, 10)).value == 'temporary_access'
    assert fake.calls == 2


async def test_inflight_recheck_invalidation_denies_old_answer(setup):
    gate, fake, clock, path = setup
    fake.hook = lambda: invalidate_subscription(path)
    assert not (await gate.check(fake, 10)).allowed


async def test_known_missing_admin_not_bypassed_by_followup_network_error(setup):
    gate, fake, clock, path = setup
    fake.admin = False
    assert (await gate.check(fake, 10)).value == 'config_error'
    fake.error = TelegramNetworkError(method=GetMe(), message='SECRET')
    assert not (await gate.check(fake, 10)).allowed
    # No permanent lockout once actual admin access recovers.
    fake.error = None
    fake.admin = True
    assert (await gate.check(fake, 10)).confirmed


async def test_known_nonmember_not_soft_passed_until_success(setup):
    gate, fake, clock, path = setup
    fake.status = 'left'
    assert not (await gate.check(fake, 10)).allowed
    fake.error = TelegramNetworkError(method=GetMe(), message='SECRET')
    assert not (await gate.check(fake, 10)).allowed
    fake.error = None
    fake.status = 'member'
    assert (await gate.check(fake, 10)).confirmed
