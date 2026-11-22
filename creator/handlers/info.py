"""📰 Информация и «Оповестить о проблеме»."""
from __future__ import annotations

import html
import time
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator.config import support_url
from creator.keyboards import BTN_INFO, btn, cancel_kb, ikb, url_btn
from creator.services import Ctx
from creator.states import Info

router = Router(name="info")

SEP = "➖➖➖➖➖➖➖➖"


def days_running(ctx: Ctx) -> int:
    try:
        start = date.fromisoformat(ctx.cfg.launch_date) if ctx.cfg.launch_date else None
    except ValueError:
        start = None
    if start is None:
        start = date.fromtimestamp(int(ctx.db.kv_get("created_at") or time.time()))
    return max(1, (date.today() - start).days + 1)


def info_text(ctx: Ctx) -> str:
    s = ctx.db.stats()
    return (
        f"🗃 <b>Информация</b>\n{SEP}\n"
        f"⏱ <b>Работаем:</b> {days_running(ctx)} дн.\n{SEP}\n"
        f"▪️ <b>Пользователи:</b>\n"
        f"👤 Пользователей всего: {s['users_total']}\n"
        f"👤 Пользователей сегодня: {s['users_today']}\n{SEP}\n"
        f"▪️ <b>Боты:</b>\n"
        f"🤖 Ботов всего: {s['bots_total']}\n"
        f"🚀 Ботов запущено: {s['bots_running']}\n{SEP}"
    )


HELP = (
    "❓ <b>Помощь</b>\n" + SEP + "\n"
    "🚀 <b>Запуск.</b> «🔨 Создать бота» → выберите шаблон → пришлите токен от @BotFather. "
    "Бот появится в «🤖 Мои боты»; в его карточке — «📋 Первый запуск».\n\n"
    "💳 <b>Хостинг.</b> При платном тарифе оплачивается в рублях с баланса хостинга бота: карточка бота → «💳 Хостинг». "
    "Когда баланс заканчивается, бот останавливается — пополните его и запустите снова.\n\n"
    "🔑 <b>Токен.</b> Это ключ от бота: отправляйте его только конструктору. "
    "Если токен перевыпущен в @BotFather, бот остановится с ошибкой «Неверный токен» — напишите нам.\n\n"
    "💸 <b>Выплаты в ваших ботах.</b> Заявки ваших пользователей на вывод обрабатываете вы сами; "
    "«Отметить выплаченной» — только отметка, конструктор деньги не переводит.\n\n"
    "↩️ <b>Если запутались.</b> /cancel или любая кнопка меню отменяет текущий ввод."
)


def contact_buttons(ctx: Ctx) -> tuple[list, str]:
    chat, channel, admin = (support_url(ctx.cfg.chat_url), support_url(ctx.cfg.channel_url),
                            support_url(ctx.cfg.admin_url))
    links = [url_btn(t, u) for t, u in (("💬 Чат", chat), ("📢 Канал", channel)) if u]
    return links, admin


def info_kb(ctx: Ctx):
    links, admin_url = contact_buttons(ctx)
    admin = url_btn("👑 Администратор", admin_url) if admin_url else btn("👑 Администратор", "info:admin")
    return ikb(links, [btn("❓ Помощь", "info:help")], [btn("⛔ Оповестить о проблеме", "info:report")], [admin])


def help_kb(ctx: Ctx):
    links, admin_url = contact_buttons(ctx)
    rows = [links] if links else []
    if admin_url:
        rows.append([url_btn("👑 Администратор", admin_url)])
    rows += [[btn("⛔ Оповестить о проблеме", "info:report")], [btn("⬅️ Назад", "info:back")]]
    return ikb(*rows)


def help_text(ctx: Ctx) -> str:
    links, admin_url = contact_buttons(ctx)
    if links or admin_url:
        return HELP + "\n\nСвязь — кнопками ниже. Описать проблему можно через «⛔ Оповестить о проблеме»."
    return HELP + "\n\nСвязь с администратором — через «⛔ Оповестить о проблеме»."


@router.message(F.text == BTN_INFO)
async def info(message: Message, ctx: Ctx) -> None:
    await message.answer(info_text(ctx), reply_markup=info_kb(ctx))


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "info:help")
async def help_screen(cb: CallbackQuery, ctx: Ctx) -> None:
    await cb.answer()
    await _show(cb, help_text(ctx), help_kb(ctx))


@router.callback_query(F.data == "info:back")
async def info_back(cb: CallbackQuery, ctx: Ctx) -> None:
    await cb.answer()
    await _show(cb, info_text(ctx), info_kb(ctx))


@router.callback_query(F.data == "info:admin")
async def admin_contact(cb: CallbackQuery) -> None:
    await cb.answer("Напишите через «Оповестить о проблеме» — сообщение придёт администратору.", show_alert=True)


@router.callback_query(F.data == "info:report")
async def report(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Info.report)
    await cb.message.answer("⛔ Опишите проблему одним сообщением (можно с фото/скриншотом).", reply_markup=cancel_kb())
    await cb.answer()


@router.message(Info.report)
async def report_text(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    u = message.from_user
    text = message.text or message.caption or "(вложение)"
    rid = ctx.db.add_report(u.id, text)
    header = (f"⛔ <b>Обращение №{rid}</b> от {html.escape(u.full_name)} "
              f"(<code>{u.id}</code>{', @' + u.username if u.username else ''})")
    sent = 0
    for admin in ctx.cfg.admin_ids:
        try:
            await message.bot.send_message(admin, header)
            await message.forward(admin)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    await message.answer("✅ Спасибо! Сообщение передано администратору." if sent
                         else "✅ Сообщение сохранено, администратор его увидит.")
