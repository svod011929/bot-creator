"""Админка ключей интеграций. Полные значения не попадают в чат, журнал и логи."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from creator.handlers.access import private_admin
from creator.integration_keys import (
    SPECS,
    KeyErrorSafe,
    all_statuses,
    apply_runtime,
    env_blocked,
    has_admin_file,
    probe,
    remove,
    revision,
    save,
    spec,
    status_line,
    validate_key,
)
from creator.keyboards import btn, ikb
from creator.services import Ctx
from creator.sponsor_sources import SourceError

router = Router(name="integration_keys")
TTL, CAP = 600, 128

PRIORITY = (
    "Приоритет значения: переменная окружения на сервере (из бота не меняется) → "
    "ключ, сохранённый в этой админке → прежний файл (например data/mgram_hub.json "
    "или автокэш хаба источников ОП).\n\n"
    "Токен конструктора, секрет вебхука LavePay и ключ шифрования выплат владельцев "
    "здесь только для просмотра: их смена остановит сервис или сделает магазины нечитаемыми.\n\n"
    "В чат выводится только статус, источник и при длинном ключе последние 4 символа. "
    "Сообщение с ключом бот сразу удаляет. Полные значения не пишутся в журнал "
    "и не передаются дочерним ботам."
)


class KeyEdit(StatesGroup):
    key = State()
    confirm = State()
    delete = State()


@dataclass
class Pending:
    key_id: str
    nonce: str
    revision: str
    expires: float
    key: str = field(default="", repr=False)
    probe: str = ""


def cleanup(ctx: Ctx) -> None:
    for uid, pending in list(ctx.key_inputs.items()):
        if not isinstance(pending, Pending) or pending.expires <= time.monotonic():
            ctx.key_inputs.pop(uid, None)
    while len(ctx.key_inputs) >= CAP:
        ctx.key_inputs.pop(next(iter(ctx.key_inputs)))


async def edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        pass  # No exception details or candidate secret in logs/fallback messages.


async def discard(state: FSMContext, ctx: Ctx, uid: int) -> None:
    ctx.key_inputs.pop(uid, None)
    await state.clear()


def home_screen(ctx: Ctx):
    lines = ["🔑 <b>Ключи интеграций</b>\n", PRIORITY, ""]
    for st in all_statuses(ctx.cfg):
        lines.append("• " + status_line(st))
    rows = [[btn(st.title, f"ik:v:{st.id}")] for st in SPECS]
    rows.append([btn("⬅️ К настройкам", "a:g:settings")])
    return "\n".join(lines), ikb(*rows)


def detail_screen(ctx: Ctx, key_id: str):
    item = spec(key_id)
    from creator.integration_keys import resolve
    try:
        st = resolve(ctx.cfg, key_id)
        text = f"🔑 <b>{item.title}</b>\n\n{status_line(st)}"
    except KeyErrorSafe:
        st = None
        text = f"🔑 <b>{item.title}</b>\n\nне удалось прочитать"
    if not item.editable:
        text += "\n\n" + item.readonly_reason
    elif env_blocked(ctx.cfg, item):
        text += "\n\nКлюч задан на сервере и имеет приоритет. Изменение через бот недоступно."
    rows = []
    if item.editable and not env_blocked(ctx.cfg, item):
        rows.append([btn("🔑 Задать / заменить ключ", f"ik:edit:{item.id}")])
        if has_admin_file(ctx.cfg, item):
            rows.append([btn("🗑 Удалить сохранённый ключ", f"ik:remove:{item.id}")])
    rows.append([btn("⬅️ К списку ключей", "ik:home")])
    return text, ikb(*rows)


@router.callback_query(F.data.startswith("ik:"))
async def callbacks(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if not private_admin(cb, ctx):
        await cb.answer("Недоступно", show_alert=True)
        return
    current = await state.get_state()
    ours = {KeyEdit.key.state, KeyEdit.confirm.state, KeyEdit.delete.state}
    if current is not None and current not in ours:
        await cb.answer("Завершите текущий ввод или используйте /cancel", show_alert=True)
        return
    uid = cb.from_user.id
    data = (cb.data or "").split(":")
    if cb.data == "ik:home":
        await discard(state, ctx, uid)
        text, kb = home_screen(ctx)
        await edit(cb, text, kb)
        await cb.answer()
        return
    if len(data) < 3:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    action = data[1]
    key_id = data[2]
    try:
        item = spec(key_id)
    except KeyErrorSafe:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    if action == "v":
        await discard(state, ctx, uid)
        text, kb = detail_screen(ctx, key_id)
        await edit(cb, text, kb)
        await cb.answer()
        return
    if action in {"edit", "remove"}:
        if not item.editable or env_blocked(ctx.cfg, item):
            await cb.answer("Этот ключ нельзя изменить через бота", show_alert=True)
            return
        if action == "remove" and not has_admin_file(ctx.cfg, item):
            await cb.answer("Сохранённого в админке ключа нет", show_alert=True)
            return
        try:
            rev = revision(ctx.cfg, item)
        except KeyErrorSafe:
            await cb.answer("Настройка недоступна; проверьте права файла", show_alert=True)
            return
        cleanup(ctx)
        nonce = secrets.token_hex(8)
        ctx.key_inputs[uid] = Pending(key_id, nonce, rev, time.monotonic() + TTL)
        await state.set_state(KeyEdit.key if action == "edit" else KeyEdit.delete)
        await state.set_data({"key_nonce": nonce, "key_id": key_id})
        if action == "edit":
            text = (
                f"🔑 <b>{item.title}</b>\n\n"
                "Telegram-чат не является хранилищем секретов и не имеет сквозного шифрования. "
                "Сообщение попробуем удалить, но гарантировать удаление нельзя. "
                "Введите ключ (8–512 символов: латиница, цифры и _./+=:-), не токен Telegram-бота. "
                "Затем потребуется подтверждение сохранения."
            )
            kb = ikb([btn("⬅️ Отмена", f"ik:v:{item.id}")])
        else:
            text = (
                f"Удалить сохранённый в админке ключ «{item.title}»? "
                "Если ключ задан переменной окружения, она по-прежнему будет использоваться. "
                "Иначе вернётся прежний файл, если он есть. Это не отзыв ключа у провайдера."
            )
            kb = ikb([btn("🗑 Подтвердить удаление", f"ik:drop:{item.id}:{nonce}")],
                     [btn("⬅️ Отмена", f"ik:v:{item.id}")])
        await edit(cb, text, kb)
        await cb.answer()
        return
    if action in {"save", "drop"} and len(data) == 4:
        cleanup(ctx)
        pending = ctx.key_inputs.get(uid)
        values = await state.get_data()
        expected = KeyEdit.confirm.state if action == "save" else KeyEdit.delete.state
        current = await state.get_state()
        if (current != expected or not isinstance(pending, Pending) or pending.key_id != key_id
                or pending.nonce != data[3] or values.get("key_nonce") != data[3]
                or values.get("key_id") != key_id or not ctx.is_admin(uid)):
            await cb.answer("Подтверждение устарело; откройте ключ заново", show_alert=True)
            return
        try:
            if action == "save":
                save(ctx.cfg, key_id, pending.key, pending.revision)
                audit = "integration_key_save"
            else:
                remove(ctx.cfg, key_id, pending.revision)
                audit = "integration_key_remove"
        except KeyErrorSafe:
            await discard(state, ctx, uid)
            await edit(cb, "Изменение не выполнено: настройка изменилась или недоступна. "
                       "Откройте ключ заново. Значение в ответе не показывается.",
                       ikb([btn("⬅️ К ключу", f"ik:v:{key_id}")]))
            await cb.answer()
            return
        ctx.db.log_admin(uid, audit, target=key_id)
        note = await apply_runtime(ctx, key_id)
        await discard(state, ctx, uid)
        text, kb = detail_screen(ctx, key_id)
        prefix = "✅ Настройка сохранена."
        if note:
            prefix += " " + note
        await edit(cb, prefix + "\n\n" + text, kb)
        await cb.answer()
        return
    await cb.answer("Кнопка устарела", show_alert=True)


@router.message(KeyEdit.key)
async def key_input(message: Message, state: FSMContext, ctx: Ctx) -> None:
    # Delete before validation; do not echo even invalid, expired or revoked-admin input.
    if message.from_user is not None and message.chat.type == "private" and message.chat.id == message.from_user.id:
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    if not private_admin(message, ctx):
        return
    cleanup(ctx)
    pending = ctx.key_inputs.get(message.from_user.id)
    values = await state.get_data()
    if (await state.get_state() != KeyEdit.key.state or not isinstance(pending, Pending)
            or values.get("key_nonce") != pending.nonce or values.get("key_id") != pending.key_id
            or not ctx.is_admin(message.from_user.id)):
        return
    try:
        key = validate_key(message.text or "")
    except SourceError:
        await message.answer("Неверный формат ключа. Введите ключ без пробелов "
                             "или отмените ввод командой /cancel.")
        return
    pending.key = key
    pending.probe = (await probe(ctx.cfg, pending.key_id, key)) or ""
    await state.set_state(KeyEdit.confirm)
    extra = f"\n\n{pending.probe}" if pending.probe else ""
    await message.answer(
        "Сохранить этот ключ? Проверка у провайдера не обязательна для сохранения."
        + extra,
        reply_markup=ikb([btn("💾 Сохранить", f"ik:save:{pending.key_id}:{pending.nonce}")],
                         [btn("⬅️ Отмена", f"ik:v:{pending.key_id}")]),
    )


@router.message(KeyEdit.confirm)
@router.message(KeyEdit.delete)
async def extra_input(message: Message, ctx: Ctx) -> None:
    if message.from_user is None or message.chat.type != "private" or message.chat.id != message.from_user.id:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if private_admin(message, ctx):
        await message.answer("Используйте кнопку подтверждения или /cancel. Новое значение не сохранено.")
