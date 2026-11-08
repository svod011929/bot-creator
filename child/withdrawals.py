"""Private withdrawal history; viewing never performs a financial operation.

Owner permissions deliberately come from current config, not startup router filters.
The old wd:ok|no callbacks still work, with the same guarded manual resolution.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from sqlite3 import Row

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from child.core import ChildApp, admin_kb, safe_send, send_profile
from child.db import fmt
from child.files import read_config

SCOPES = {'mine': '📜 Мои заявки', 'pending': '💸 Ожидают', 'completed': '💸 Завершённые'}
STATUSES = {'pending': 'Ожидает', 'paid': 'Отмечена выплаченной', 'rejected': 'Отклонена'}
EMPTY = {'mine': 'У вас пока нет заявок', 'pending': 'Ожидающих заявок нет',
         'completed': 'Завершённых заявок пока нет'}
STALE = 'Действие устарело, откройте историю заново'


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def history_entry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button('📜 Мои заявки', 'wh:list:mine:0')]])


def format_date(epoch: int | None) -> str:
    if epoch is None:
        return 'Дата неизвестна'
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    except (ValueError, OverflowError, OSError, TypeError):
        return 'Дата неизвестна'


def format_unit(unit: str | None) -> str:
    if unit is None:
        label = 'Историческая единица баланса не сохранена'
    elif unit == '':
        label = 'единица не задана'
    else:
        label = unit[:80] + ('…' if len(unit) > 80 else '')
    return html.escape(label)


def format_amount(row: Row) -> str:
    return f"{fmt(row['amount'])} {format_unit(row['currency_at_request'])}"


def is_owner(app: ChildApp, uid: int) -> bool:
    try:
        cfg = read_config(app.dir)
        owner = cfg.get('owner_id') if isinstance(cfg, dict) else None
        return type(owner) is int and owner == uid
    except (OSError, ValueError):
        return False  # unreadable config is never permission to see payment details


def number(value: str, *, page: bool = False) -> int:
    pattern = r'(?:0|[1-9][0-9]{0,6})' if page else r'[1-9][0-9]{0,18}'
    if not re.fullmatch(pattern, value):
        raise ValueError
    result = int(value)
    if result > (1_000_000 if page else 2**63 - 1):
        raise ValueError
    return result


async def render(cb: CallbackQuery, text: str, rows: list[list[InlineKeyboardButton]]) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest as exc:
        if 'message is not modified' not in exc.message.lower():
            await cb.answer('Не удалось обновить экран. Откройте историю заново.', show_alert=True)
    except TelegramAPIError:
        # Do not allow exceptions containing request text/payment details into handler logs.
        await cb.answer('Не удалось обновить экран. Откройте историю заново.', show_alert=True)


async def show_list(cb: CallbackQuery, app: ChildApp, scope: str, page: int) -> None:
    records, page, total = app.db.withdrawal_page(scope, page, uid=cb.from_user.id if scope == 'mine' else None)
    pages = max(1, (total + 9) // 10)
    lines = [f'<b>{SCOPES[scope]}</b>', f'Всего: {total} · Страница {page + 1} / {pages}', '']
    rows = []
    if scope != 'mine':
        rows.append([button('Ожидают', 'wh:list:pending:0'), button('Завершённые', 'wh:list:completed:0')])
    for w in records:
        status = STATUSES.get(w['status'], 'Статус неизвестен')
        lines.append(f"№{w['id']} · {format_date(w['created_at'])}\n{format_amount(w)} · {status}")
        rows.append([button(f"№{w['id']} · {status}", f"wh:detail:{scope}:{page}:{w['id']}")])
    if not records:
        lines.append(EMPTY[scope])
    nav = []
    if page > 0:
        nav.append(button('⬅️ Назад', f'wh:list:{scope}:{page-1}'))
    if page + 1 < pages:
        nav.append(button('Далее ➡️', f'wh:list:{scope}:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([button('Обновить', f'wh:list:{scope}:0')])
    rows.append([button('К профилю', 'wh:profile') if scope == 'mine' else button('К админке', 'wh:admin')])
    await render(cb, '\n'.join(lines), rows)


async def show_detail(cb: CallbackQuery, app: ChildApp, scope: str, page: int, wid: int) -> None:
    w = app.db.withdrawal(wid, uid=cb.from_user.id if scope == 'mine' else None)
    if not w:
        await cb.answer('Заявка недоступна', show_alert=True)
        return
    processed = ('Ещё не обработана' if w['status'] == 'pending' else
                 'Дата обработки неизвестна' if w['resolved_at'] is None else format_date(w['resolved_at']))
    lines = [f"💸 <b>Заявка №{w['id']}</b>", f"Создана: {format_date(w['created_at'])}",
             f'Сумма: {format_amount(w)}', f"Статус: {STATUSES.get(w['status'], 'Статус неизвестен')}",
             f'Обработка: {processed}']
    if scope != 'mine':
        lines.append(f"Заявитель: <code>{w['user_id']}</code>")
    if w['status'] == 'paid' or (scope != 'mine' and w['status'] == 'pending'):
        lines.append('«Выплачено» — ручная отметка владельца, не подтверждение денежного перевода.')
    details = w['details'] or ''
    lines.append(f"Реквизиты: <code>{html.escape(details[:600])}{'…' if len(details) > 600 else ''}</code>")
    rows = []
    if scope != 'mine' and w['status'] == 'pending':
        rows.append([button('✅ Выплачено', f'wd:ok:{wid}:{page}'), button('❌ Отклонить', f'wd:no:{wid}:{page}')])
    rows.append([button('К списку', f'wh:list:{scope}:{page}')])
    rows.append([button('К профилю', 'wh:profile') if scope == 'mine' else button('К админке', 'wh:admin')])
    await render(cb, '\n'.join(lines), rows)


def build_history_router(app: ChildApp) -> Router:
    r = Router(name='withdrawal-history')

    @r.callback_query(F.data.startswith('wh:') | F.data.startswith('wd:') | (F.data == 'adm:wd'))
    async def history(cb: CallbackQuery) -> None:
        if not isinstance(cb.message, Message) or cb.message.chat.type != 'private' or cb.message.chat.id != cb.from_user.id:
            await cb.answer('Откройте личный чат с ботом.', show_alert=True)
            return
        parts = (cb.data or '').split(':')
        try:
            if cb.data == 'wh:profile':
                await cb.answer()
                await send_profile(cb.message, app, cb.from_user.id)
                return
            if cb.data == 'wh:admin':
                if not is_owner(app, cb.from_user.id):
                    await cb.answer('Доступ закрыт', show_alert=True)
                    return
                await cb.answer()
                if is_owner(app, cb.from_user.id):
                    await render(cb, '⚙️ <b>Панель владельца</b>', admin_kb(app).inline_keyboard)
                return
            if cb.data == 'adm:wd':
                scope, page, action = 'pending', 0, 'list'
            elif len(parts) in (3, 4) and parts[0] == 'wd' and parts[1] in {'ok', 'no'}:
                scope, action = 'pending', 'resolve'
                wid = number(parts[2])
                page = number(parts[3], page=True) if len(parts) == 4 else 0
            elif len(parts) in (4, 5) and parts[0] == 'wh':
                action, scope = parts[1:3]
                if scope not in SCOPES or (action, len(parts)) not in {('list', 4), ('detail', 5)}:
                    raise ValueError
                page = number(parts[3], page=True)
                if action == 'detail':
                    wid = number(parts[4])
            else:
                raise ValueError
        except ValueError:
            await cb.answer(STALE, show_alert=True)
            return

        # No await between this fresh permission check and the guarded database operation/read.
        if scope != 'mine' and not is_owner(app, cb.from_user.id):
            await cb.answer('Заявка недоступна', show_alert=True)
            return
        if action == 'resolve':
            w = app.db.resolve_withdrawal(wid, parts[1] == 'ok')
            if not w:
                await cb.answer('Заявка уже обработана', show_alert=True)
            else:
                await cb.answer()
                status = '✅ выплачена' if parts[1] == 'ok' else '❌ отклонена, средства возвращены на баланс'
                # Existing notification, only on the first actual decision. No notifications on reads.
                await safe_send(cb.bot, w['user_id'], f"Заявка на вывод №{w['id']} {status}.")
            # Notification is an await: revalidate after it before rendering sensitive details.
            if is_owner(app, cb.from_user.id):
                await show_detail(cb, app, scope, page, wid)
            return
        await cb.answer()
        # Callback acknowledgement can yield while ownership changes.
        if scope != 'mine' and not is_owner(app, cb.from_user.id):
            return
        if action == 'list':
            await show_list(cb, app, scope, page)
        else:
            await show_detail(cb, app, scope, page, wid)

    return r
