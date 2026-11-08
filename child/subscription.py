"""Own required channel: strict inputs and Telegram verification (not sponsor tasks)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum
from pathlib import Path

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from child.files import settings_pair, strict_settings, subscription_revision

USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
PUBLIC_URL = re.compile(r"https://t[.]me/([A-Za-z][A-Za-z0-9_]{3,31})/?\Z")
INVITE_URL = re.compile(r"https://t[.]me/(?:\+|joinchat/)[A-Za-z0-9_-]+\Z")


def parse_channel(raw: str) -> str | int:
    raw = raw.strip()
    if re.fullmatch(r"-?[1-9][0-9]*", raw):
        return int(raw)
    match = PUBLIC_URL.fullmatch(raw)
    username = match[1] if match else raw.removeprefix("@") if raw.startswith("@") else ""
    if not USERNAME.fullmatch(username):
        raise ValueError("Invalid channel")
    return "@" + username


def join_url(raw: str, username: str | None) -> str:
    raw = raw.strip()
    if username:
        target = parse_channel(raw)
        if not isinstance(target, str) or target.lower() != "@" + username.lower():
            raise ValueError("URL does not match public channel")
        return "https://t.me/" + username
    if not INVITE_URL.fullmatch(raw):
        raise ValueError("Invalid invite URL")
    return raw

log = logging.getLogger('required_subscription')


class GateResult(str, Enum):
    DISABLED = 'disabled'
    OWNER = 'owner'
    SUBSCRIBED = 'subscribed'
    NOT_SUBSCRIBED = 'not_subscribed'
    TEMPORARY_ACCESS = 'temporary_access'
    CONFIG_ERROR = 'config_error'
    TEMPORARY_UNAVAILABLE = 'temporary_unavailable'

    @property
    def allowed(self) -> bool:
        return self in {self.DISABLED, self.OWNER, self.SUBSCRIBED, self.TEMPORARY_ACCESS}

    @property
    def confirmed(self) -> bool:
        # Disabled and owner preserve pre-existing start/referral behavior.
        return self in {self.DISABLED, self.OWNER, self.SUBSCRIBED}

    @property
    def text(self) -> str:
        return {
            self.TEMPORARY_ACCESS: '⚠️ Проверка временно недоступна. Доступ предоставлен на 60 секунд, подписка не подтверждена.',
            self.TEMPORARY_UNAVAILABLE: '⏳ Проверка временно недоступна. Попробуйте позже.',
            self.CONFIG_ERROR: '⚠️ Настройка канала требует внимания владельца. Попробуйте проверить позже.',
        }.get(self, '🔒 Чтобы пользоваться ботом, подпишитесь на канал и нажмите «Я подписался».')


def temporary_error(exc: Exception) -> bool:
    return isinstance(exc, (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, TimeoutError))


async def validate_channel(bot, target):
    """Read-only checks using this CHILD bot's own client and identity."""
    chat = await bot.get_chat(target)
    if chat.type != 'channel':
        raise ValueError('Channel required')
    member = await bot.get_chat_member(chat.id, bot.id)
    if member.status != 'administrator':
        raise ValueError('Child bot must be administrator')
    return chat


class SubscriptionGate:
    def __init__(self, bot_dir: Path, owner_id: int, clock=time.monotonic):
        self.dir = bot_dir
        self.owner_id = owner_id
        self.clock = clock
        self._version = None
        self._generation = 0
        self._members: dict[int, float] = {}
        self._soft: dict[int, float] = {}
        self._retry_at = 0.0
        self._config_error = False
        self._denied: set[int] = set()
        # One in-flight request across this bot, including shared rate limit handling.
        self._lock = asyncio.Lock()

    def _snapshot(self):
        pair = settings_pair(strict_settings(self.dir))
        version = (pair, subscription_revision(self.dir))
        if version != self._version:
            self._version = version
            self._config_error = False
            self._denied.clear()
            self._clear()
        return pair, (version, self._generation)

    def _clear(self):
        self._members.clear()
        self._soft.clear()
        self._generation += 1

    async def check(self, bot, uid: int) -> GateResult:
        if uid == self.owner_id:
            return GateResult.OWNER
        async with self._lock:
            try:
                pair, stamp = self._snapshot()
            except (OSError, ValueError):
                self._clear()
                return GateResult.CONFIG_ERROR
            channel, _url = pair
            if not channel:
                return GateResult.DISABLED
            now = self.clock()
            if self._members.get(uid, 0) > now:
                return GateResult.SUBSCRIBED
            if self._soft.get(uid, 0) > now:
                return GateResult.TEMPORARY_ACCESS
            if self._retry_at > now:
                return GateResult.TEMPORARY_UNAVAILABLE
            result = GateResult.CONFIG_ERROR
            try:
                chat = await validate_channel(bot, parse_channel(channel))
                self._config_error = False
                member = await bot.get_chat_member(chat.id, uid)
                if member.status not in {'creator', 'administrator', 'member', 'restricted', 'left', 'kicked'}:
                    raise ValueError('Unsupported membership')
                ok = member.status in {'creator', 'administrator', 'member'} or (
                    member.status == 'restricted' and getattr(member, 'is_member', False)
                )
                result = GateResult.SUBSCRIBED if ok else GateResult.NOT_SUBSCRIBED
            except Exception as exc:  # classify, never echo Telegram text or channel/user information
                if isinstance(exc, TelegramRetryAfter):
                    self._retry_at = self.clock() + exc.retry_after
                if temporary_error(exc):
                    result = (GateResult.TEMPORARY_UNAVAILABLE if self._config_error or uid in self._denied
                              else GateResult.TEMPORARY_ACCESS)
                else:
                    self._config_error = True
                    self._clear()
                    log.warning('own_subscription result=config_error')
                    return GateResult.CONFIG_ERROR
            try:
                _, current = self._snapshot()
            except (OSError, ValueError):
                self._clear()
                return GateResult.CONFIG_ERROR
            if current != stamp:
                return GateResult.TEMPORARY_UNAVAILABLE
            if result == GateResult.SUBSCRIBED:
                self._denied.discard(uid)
                self._members[uid] = self.clock() + 600
                self._soft.pop(uid, None)
            elif result == GateResult.TEMPORARY_ACCESS:
                self._soft[uid] = self.clock() + 60
                log.info('own_subscription result=temporary_access')
            else:
                if result == GateResult.NOT_SUBSCRIBED:
                    self._denied.add(uid)
                self._soft.pop(uid, None)
            return result
