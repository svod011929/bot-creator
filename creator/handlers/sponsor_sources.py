"""Admin-only source setup. Candidate secrets live only in bounded, expiring RAM."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from creator.integrations import ensure_services
from creator.keyboards import btn, ikb
from creator.services import Ctx
from creator.sponsor_sources import SourceError, SourceStore, validate_key

router = Router(name='sponsor_sources')
NETWORKS = {'tg': ('tgrass', 'TGrass'), 'pf': ('piarflow', 'PiarFlow')}
TTL, CAP = 600, 128


class SourceEdit(StatesGroup):
    key = State()
    confirm = State()
    delete = State()


@dataclass
class Pending:
    code: str
    nonce: str
    revision: str
    expires: float
    key: str = field(default='', repr=False)


def private_admin(event, ctx: Ctx) -> bool:
    message = event.message if isinstance(event, CallbackQuery) else event
    return (isinstance(message, Message) and event.from_user is not None
            and message.chat.type == 'private' and message.chat.id == event.from_user.id
            and ctx.is_admin(event.from_user.id))


def cleanup(ctx: Ctx) -> None:
    for uid, pending in list(ctx.source_inputs.items()):
        if not isinstance(pending, Pending) or pending.expires <= time.monotonic():
            ctx.source_inputs.pop(uid, None)
    while len(ctx.source_inputs) >= CAP:
        ctx.source_inputs.pop(next(iter(ctx.source_inputs)))


def screen(ctx: Ctx, code: str):
    provider, title = NETWORKS[code]
    store = SourceStore(ctx.cfg, provider)
    try:
        origin = store.resolve().origin
    except SourceError:
        origin = 'error'
    labels = {'env': 'серверная настройка (env)', 'manual': 'ключ сохранён в креаторе',
              'auto': 'прежний сохранённый автокэш хаба', 'unset': 'ключ не задан',
              'error': 'настройку не удалось прочитать; источник не подтверждён'}
    factory = bool(ctx.cfg.tgrass_factory_token if code == 'tg' else ctx.cfg.piarflow_token)
    text = (f'📡 <b>Источник ОП: {title}</b>\n\nИсточник: {labels[origin]}.\n'
            f'Токен фабрики: {"заданы" if factory else "не заданы"}.\n'
            'Это задания, которые сеть выдаёт по ключу источника креатора, '
            'не фильтр только ваших рекламных кампаний.\n\n'
            'Наличие ключа не подтверждает работу у провайдера: live-проверка не выполнялась. '
            'Сначала регистрация дочернего бота через фабрику; при ошибке или отсутствии '
            'данных фабрики — источник креатора. Без ключа может использоваться прежняя '
            'авторегистрация хаба, если есть данные фабрики.\n\n'
            'Изменение — для следующих одобрений и повторного включения отключённых relay-ботов. '
            'Уже подключённые боты сохраняют свои ключи; массовой замены нет.')
    rows = []
    if store.env_key:
        text += '\n\nКлюч задан на сервере и имеет приоритет. Изменение через бот недоступно.'
    else:
        rows.append([btn('🔑 Задать / заменить ключ', f'src:{code}:edit')])
        if store.manual_path.exists():
            rows.append([btn('🗑 Удалить сохранённый ключ', f'src:{code}:remove')])
    rows.append([btn('⬅️ К источникам', 'src:home')])
    return text, ikb(*rows)


async def edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        pass  # No exception/provider details or candidate secret in logs/fallback messages.


async def discard(state: FSMContext, ctx: Ctx, uid: int) -> None:
    ctx.source_inputs.pop(uid, None)
    await state.clear()


@router.callback_query(F.data.startswith('src:'))
async def callbacks(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if not private_admin(cb, ctx):
        await cb.answer('Недоступно', show_alert=True)
        return
    current = await state.get_state()
    ours = {SourceEdit.key.state, SourceEdit.confirm.state, SourceEdit.delete.state}
    if current is not None and current not in ours:
        await cb.answer('Завершите текущий ввод или используйте /cancel', show_alert=True)
        return
    uid = cb.from_user.id
    data = (cb.data or '').split(':')
    if cb.data == 'src:home':
        await discard(state, ctx, uid)
        await edit(cb, '📡 <b>Источники ОП креатора</b>\nВыберите сеть. '
                   'Просмотр не проверяет провайдера и не меняет подключённые боты.',
                   ikb([btn('TGrass', 'src:tg:view'), btn('PiarFlow', 'src:pf:view')],
                       [btn('⬅️ В админ-панель', 'a:main')]))
        await cb.answer()
        return
    if len(data) not in (3, 4) or data[1] not in NETWORKS:
        await cb.answer('Кнопка устарела', show_alert=True)
        return
    code, action = data[1:3]
    provider, title = NETWORKS[code]
    store = SourceStore(ctx.cfg, provider)
    if len(data) == 3 and action in ('view', 'back'):
        await discard(state, ctx, uid)
        text, kb = screen(ctx, code)
        await edit(cb, text, kb)
    elif len(data) == 3 and action in ('edit', 'remove'):
        if store.env_key:
            await cb.answer('Ключ задан на сервере; изменение через бот недоступно', show_alert=True)
            return
        try:
            revision = store.revision()
        except SourceError:
            await cb.answer('Настройка недоступна; проверьте права файла', show_alert=True)
            return
        cleanup(ctx)
        nonce = secrets.token_hex(8)
        ctx.source_inputs[uid] = Pending(code, nonce, revision, time.monotonic() + TTL)
        await state.set_state(SourceEdit.key if action == 'edit' else SourceEdit.delete)
        await state.set_data({'source_nonce': nonce, 'source_code': code})
        if action == 'edit':
            text = (f'🔑 <b>{title}: ключ источника</b>\n\n'
                    'Telegram-чат не является хранилищем секретов и не имеет сквозного шифрования. '
                    'Сообщение попробуем удалить, но гарантировать удаление нельзя. '
                    'Введите API key источника, не токен Telegram-бота. '
                    'Ключ не проверяется у провайдера. Затем потребуется подтверждение сохранения.')
            kb = ikb([btn('⬅️ Отмена', f'src:{code}:back')])
        else:
            text = ('Удалить только ручной ключ этого источника? Вернётся прежний автокэш хаба '
                    'или авторегистрация при наличии данных фабрики. Уже подключённые боты '
                    'сохранят свои ключи. Это не отключение сети и не отзыв ключа у провайдера.')
            kb = ikb([btn('🗑 Подтвердить удаление', f'src:{code}:drop:{nonce}')],
                     [btn('⬅️ Отмена', f'src:{code}:back')])
        await edit(cb, text, kb)
    elif len(data) == 4 and action in ('save', 'drop'):
        cleanup(ctx)
        pending = ctx.source_inputs.get(uid)
        values = await state.get_data()
        expected = SourceEdit.confirm.state if action == 'save' else SourceEdit.delete.state
        # Last awaited state read immediately precedes synchronous CAS/write.
        current = await state.get_state()
        if (current != expected or not isinstance(pending, Pending) or pending.code != code
                or pending.nonce != data[3] or values.get('source_nonce') != data[3]
                or values.get('source_code') != code or not ctx.is_admin(uid)):
            await cb.answer('Подтверждение устарело; откройте источник заново', show_alert=True)
            return
        try:
            if action == 'save':
                store.save(pending.key, pending.revision)
            else:
                store.remove(pending.revision)
        except SourceError:
            await discard(state, ctx, uid)
            await edit(cb, 'Изменение не выполнено: настройка изменилась или недоступна. '
                       'Откройте источник заново. Ключ в ответе не показывается.',
                       ikb([btn('⬅️ К источнику', f'src:{code}:view')]))
            await cb.answer()
            return
        ensure_services(ctx)  # No network, sync, child writes or provider registration.
        await discard(state, ctx, uid)
        text, kb = screen(ctx, code)
        await edit(cb, '✅ Настройка сохранена. Это не проверка провайдера.\n\n' + text, kb)
    else:
        await cb.answer('Кнопка устарела', show_alert=True)
        return
    await cb.answer()


@router.message(SourceEdit.key)
async def key_input(message: Message, state: FSMContext, ctx: Ctx) -> None:
    # Delete before validation; do not echo even invalid, expired or revoked-admin input.
    if message.from_user is not None and message.chat.type == 'private' and message.chat.id == message.from_user.id:
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    if not private_admin(message, ctx):
        return
    cleanup(ctx)
    pending = ctx.source_inputs.get(message.from_user.id)
    values = await state.get_data()
    if (await state.get_state() != SourceEdit.key.state or not isinstance(pending, Pending)
            or values.get('source_nonce') != pending.nonce or not ctx.is_admin(message.from_user.id)):
        return
    try:
        key = validate_key(message.text)
    except SourceError:
        await message.answer('Неверный формат ключа источника. Введите API key без пробелов '
                             'или отмените ввод командой /cancel.')
        return
    pending.key = key
    await state.set_state(SourceEdit.confirm)
    await message.answer('Сохранить ключ источника? Он применяется при следующих одобрениях '
                         'и повторном включении отключённых relay-ботов. Уже подключённые '
                         'боты не изменятся. Проверка провайдера не выполняется.',
                         reply_markup=ikb([btn('💾 Сохранить', f'src:{pending.code}:save:{pending.nonce}')],
                                          [btn('⬅️ Отмена', f'src:{pending.code}:back')]))


@router.message(SourceEdit.confirm)
@router.message(SourceEdit.delete)
async def extra_input(message: Message, ctx: Ctx) -> None:
    if message.from_user is None or message.chat.type != 'private' or message.chat.id != message.from_user.id:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if private_admin(message, ctx):
        await message.answer('Используйте кнопку подтверждения или /cancel. Новое значение не сохранено.')
