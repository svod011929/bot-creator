"""Owner-only required channel wizard. No sponsor/payment actions."""
from __future__ import annotations

import asyncio
import hashlib
import html
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from child.files import invalidate_subscription, settings_pair, strict_settings, write_settings_pair
from child.subscription import join_url, parse_channel, temporary_error, validate_channel
from creator.keyboards import btn, ikb
from creator.services import Ctx

router = Router(name='subscription-wizard')


class Wizard(StatesGroup):
    active = State()


@dataclass
class Checks:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_at: float = 0


@asynccontextmanager
async def child_client(ctx: Ctx, row):
    child = Bot(row['token'], session=AiohttpSession(timeout=10))
    try:
        yield child
    finally:
        await child.session.close()


def fingerprint(row) -> str:
    return hashlib.sha256(str(row['token']).encode()).hexdigest()


def owned(ctx, bot_id, uid):
    row = ctx.db.bot(bot_id)
    if not row or row['owner_id'] != uid:
        raise ValueError('Not owner')
    return row


def data_valid(data, bot_id, uid, nonce=None):
    return (data.get('bot_id') == bot_id and data.get('user_id') == uid
            and data.get('expires', 0) > time.monotonic()
            and (nonce is None or secrets.compare_digest(str(data.get('nonce', '')), nonce)))


def kb(data, *actions):
    rows = [[btn(label, f"sub:{data['bot_id']}:{action}:{data['nonce']}")] for label, action in actions]
    if data['stage'] == 'status':
        rows.append([btn('⬅️ К настройкам бота', f"b:{data['bot_id']}:set")])
    else:
        rows.append([btn('❌ Отмена', f"sub:{data['bot_id']}:open")])
    return ikb(*rows)


async def reply(target, text, markup=None):
    message = target.message if isinstance(target, CallbackQuery) else target
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def screen(target, state: FSMContext, ctx: Ctx, bot_id: int, notice: str = ''):
    uid = target.from_user.id
    try:
        row = owned(ctx, bot_id, uid)
        pair = settings_pair(strict_settings(ctx.sup.bot_dir(bot_id)))
    except (ValueError, OSError):
        await state.clear()
        await reply(target, 'Бот не найден или настройки недоступны.')
        return
    await state.clear()
    data = dict(bot_id=bot_id, user_id=uid, nonce=secrets.token_hex(6), expires=time.monotonic() + 900,
                stage='status', expected=pair, identity=fingerprint(row))
    await state.set_state(Wizard.active)
    await state.set_data(data)
    text = '🔒 <b>Обязательная подписка</b>\n'
    status = 'не перепроверена'
    if notice.startswith('✅'):
        status = 'права проверены'
    elif notice.startswith('❌'):
        status = 'ошибка доступа'
    elif notice.startswith('⏳'):
        status = 'проверка временно недоступна'
    text += (f'Настроена ({status}).\nКанал: <code>' + html.escape(pair[0])
             + '</code>\nСсылка: ' + html.escape(pair[1])) if pair[0] else 'Выключена.'
    if notice:
        text += '\n\n' + notice
    await reply(target, text, kb(data, ('Настроить / Изменить', 'begin'),
                                ('Перепроверить', 'recheck'), ('Отключить', 'disable')))


async def current(state, ctx, bot_id, uid, nonce=None):
    data = await state.get_data()
    row = owned(ctx, bot_id, uid)
    if not data_valid(data, bot_id, uid, nonce) or fingerprint(row) != data.get('identity'):
        raise ValueError('Stale wizard')
    return data, row


async def checked(ctx, row, target):
    checks = ctx.subscription_checks.setdefault(row['id'], Checks())
    if checks.lock.locked() or checks.retry_at > time.monotonic():
        raise RuntimeError('Check not available')
    async with checks.lock:
        try:
            async with child_client(ctx, row) as child:
                return await validate_channel(child, target)
        except Exception as exc:
            if isinstance(exc, TelegramRetryAfter):
                checks.retry_at = time.monotonic() + exc.retry_after
            elif temporary_error(exc):
                checks.retry_at = time.monotonic() + 2
            raise


def check_error(exc):
    if temporary_error(exc) or isinstance(exc, RuntimeError):
        return '⏳ Проверка временно недоступна. Повторите позже; настройки не изменены.'
    return '❌ Нужен канал и доступ дочернего бота: добавьте его администратором. Настройки не изменены.'


async def channel_step(target, state, data):
    data.update(stage='channel', nonce=secrets.token_hex(6))
    await state.set_data(data)
    await reply(target, 'Шаг 1/3. Перешлите публикацию канала, пришлите @username, '
                'https://t.me/username или числовой ID. Добавьте дочернего бота администратором.',
                kb(data))


async def summary(target, data):
    text = ('Шаг 3/3. <b>Проверьте настройки</b>\n'
            f"Канал: {html.escape(data['title'])} (<code>{data['channel']}</code>)\n"
            f"Ссылка: {html.escape(data['url'])}\n")
    if not data['username']:
        text += ('⚠️ Подтверждая сохранение, вы подтверждаете, что invite-ссылка ведёт именно '
                 'в выбранный канал. Синтаксис не проверяет её назначение или действительность. '
                 'Заявка на вступление не равна подписке до одобрения.\n')
    await reply(target, text, kb(data, ('✅ Сохранить', 'save'), ('Другая ссылка', 'url'),
                                ('Другой канал', 'begin')))


@router.callback_query(F.data.startswith('sub:'))
async def action(cb: CallbackQuery, state: FSMContext, ctx: Ctx):
    try:
        parts = cb.data.split(':')
        bot_id = int(parts[1])
        command = parts[2]
        owned(ctx, bot_id, cb.from_user.id)
        if command == 'open':
            await screen(cb, state, ctx, bot_id)
            await cb.answer()
            return
        if len(parts) != 4:
            raise ValueError('Missing nonce')
        data, row = await current(state, ctx, bot_id, cb.from_user.id, parts[3])
    except (ValueError, IndexError):
        await cb.answer('Действие устарело или бот не найден. Откройте настройки заново.', show_alert=True)
        return

    # Acknowledge before child API work: callbacks may expire during network timeouts.
    await cb.answer()
    if command == 'begin' and data['stage'] in {'status', 'confirm', 'channel', 'link', 'check'}:
        await channel_step(cb, state, data)
    elif command == 'url' and data['stage'] == 'confirm':
        data.update(stage='link', nonce=secrets.token_hex(6))
        await state.set_data(data)
        await reply(cb, 'Пришлите ссылку вступления: публичную на этот канал или приватную '
                    'https://t.me/+token / https://t.me/joinchat/token.', kb(data))
    elif command == 'retry' and data['stage'] == 'check':
        await accept_channel(cb, state, ctx, data, row, data['target'])
    elif command == 'disable' and data['stage'] == 'status':
        data['stage'] = 'disable'
        await state.set_data(data)
        await reply(cb, 'Отключить обязательную подписку?', kb(data, ('Да, отключить', 'disable_ok')))
    elif command == 'disable_ok' and data['stage'] == 'disable':
        try:
            commit_pair(ctx, row, data, '', '')
        except (ValueError, OSError):
            await reply(cb, 'Не сохранено: настройки или владелец изменились либо файл недоступен.')
        else:
            await screen(cb, state, ctx, bot_id, '✅ Обязательная подписка отключена.')
    elif command == 'recheck' and data['stage'] == 'status':
        if not data['expected'][0]:
            await reply(cb, 'Подписка выключена')
            return
        try:
            # Invalidate before API. A failed check must not retain known positive caches.
            with ctx.db.owned_bot(bot_id, cb.from_user.id):
                invalidate_subscription(ctx.sup.bot_dir(bot_id))
            chat = await checked(ctx, row, parse_channel(data['expected'][0]))
            await current(state, ctx, bot_id, cb.from_user.id, data['nonce'])
            now_pair = settings_pair(strict_settings(ctx.sup.bot_dir(bot_id)))
            if tuple(data['expected']) != now_pair:
                raise ValueError('Settings changed')
            try:
                join_url(data['expected'][1], chat.username)
            except ValueError:
                notice = '⚠️ Права доступны, но ссылка/публичность изменились. Откройте мастер.'
            else:
                notice = '✅ Канал доступен, дочерний бот — администратор. Назначение приватной ссылки не проверяется.'
        except Exception as exc:
            notice = check_error(exc)
        # screen rechecks ownership, never restores another session's draft after await.
        try:
            await current(state, ctx, bot_id, cb.from_user.id, data['nonce'])
        except ValueError:
            await reply(cb, 'Действие устарело')
            return
        await screen(cb, state, ctx, bot_id, notice)
    elif command == 'save' and data['stage'] == 'confirm':
        try:
            chat = await checked(ctx, row, int(data['channel']))
            await current(state, ctx, bot_id, cb.from_user.id, data['nonce'])
            if str(chat.id) != data['channel'] or (chat.username or '') != data['username']:
                await reply(cb, 'Публичность канала изменилась. Выберите канал заново.',
                            kb(data, ('Другой канал', 'begin')))
            else:
                url = join_url(data['url'], chat.username)
                commit_pair(ctx, row, data, str(chat.id), url)
                notice = '✅ Настройки сохранены вместе. Канал: ' + html.escape(chat.title or 'Канал') + '.'
                await screen(cb, state, ctx, bot_id, notice)
        except (ValueError, OSError):
            await reply(cb, 'Не сохранено: проверьте права, настройки и владение; при конфликте откройте мастер заново.')
        except Exception as exc:
            await reply(cb, check_error(exc), kb(data, ('Повторить сохранение', 'save')))
    else:
        await reply(cb, 'Действие устарело')
        return


def commit_pair(ctx, row, data, channel, url):
    # No await inside the DB ownership lock; transfer/delete use this same RLock.
    with ctx.db.owned_bot(row['id'], data['user_id']) as latest:
        if fingerprint(latest) != data['identity']:
            raise ValueError('Child identity changed')
        write_settings_pair(ctx.sup.bot_dir(row['id']), expected=tuple(data['expected']), channel=channel, url=url)


async def accept_channel(target, state, ctx, data, row, channel):
    data.update(stage='check', target=channel)
    await state.set_data(data)
    await reply(target, 'Шаг 2/3. Проверяем доступ дочернего бота к каналу…')
    try:
        chat = await checked(ctx, row, channel)
        await current(state, ctx, row['id'], data['user_id'], data['nonce'])
    except Exception as exc:
        # Never overwrite FSM or publish a stale result after cancel/transfer.
        try:
            await current(state, ctx, row['id'], data['user_id'], data['nonce'])
        except ValueError:
            return
        await reply(target, check_error(exc), kb(data, ('Повторить', 'retry'), ('Другой канал', 'begin')))
        return
    data.update(channel=str(chat.id), username=chat.username or '', title=chat.title or 'Канал')
    if chat.username:
        data.update(url=join_url('@' + chat.username, chat.username), stage='confirm')
        await state.set_data(data)
        await summary(target, data)
    else:
        data.update(stage='link', nonce=secrets.token_hex(6))
        await state.set_data(data)
        await reply(target, 'Шаг 3/3. Канал приватный. Пришлите invite-ссылку '
                    'https://t.me/+token или https://t.me/joinchat/token.', kb(data))


@router.message(Wizard.active)
async def value(message: Message, state: FSMContext, ctx: Ctx):
    data = await state.get_data()
    try:
        data, row = await current(state, ctx, data.get('bot_id'), message.from_user.id)
    except (ValueError, TypeError):
        await state.clear()
        await message.answer('Мастер устарел или бот не найден. Откройте настройки заново.')
        return
    if data['stage'] == 'channel':
        origin = message.forward_origin
        try:
            channel = (origin.chat.id if origin and origin.type == 'channel'
                       else parse_channel(message.text or ''))
        except ValueError:
            await message.answer('Источник канала недоступен. Пришлите @username, числовой ID '
                                 'или публичную ссылку канала, не invite-ссылку.')
            return
        await accept_channel(message, state, ctx, data, row, channel)
    elif data['stage'] == 'link':
        try:
            data['url'] = join_url(message.text or '', data['username'] or None)
        except ValueError:
            await message.answer('Некорректная ссылка: нужен URL выбранного публичного канала '
                                 'или приватный https://t.me/+token / https://t.me/joinchat/token.')
            return
        data['stage'] = 'confirm'
        await state.set_data(data)
        await summary(message, data)
