"""📰 Информация и «Оповестить о проблеме»."""
from __future__ import annotations

import html
import time
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from creator.config import support_url
from creator.keyboards import BTN_INFO, cancel_kb
from creator.lavepay_signup import lavepay_signup_link
from creator.legal import DOCS, FILES
from creator.services import Ctx
from creator.states import Info
from creator.ui import btn, grid, ikb, screen, show, url_btn, with_nav

router = Router(name="info")


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
    return screen(
        "Информация",
        f"⏱ Работаем: {days_running(ctx)} дн.",
        f"👤 Пользователей всего: {s['users_total']}",
        f"👤 Пользователей сегодня: {s['users_today']}",
        f"🤖 Ботов всего: {s['bots_total']}",
        f"🚀 Ботов запущено: {s['bots_running']}",
    )


HELP = screen(
    "Помощь по конструктору",
    "🚀 Как создать бота: «🔨 Создать бота» → шаблон → токен от @BotFather → «📋 Первый запуск».",
    "🤖 Управление: в карточке — запуск, статистика, оформление, контент, хостинг, VIP. Опасные действия внизу.",
    "💳 Хостинг: списывается с баланса бота. Пополняйте в кабинете или карточке. Если закончился — бот остановится.",
    "🔑 Токен: отправляйте только в конструктор. Перевыпуск в @BotFather останавливает бота — пишите в поддержку.",
    "💸 Выплаты пользователям: вы сами обрабатываете заявки. Для кликера, реф-бота и заданий можно подключить свой магазин "
    + lavepay_signup_link("LavePay")
    + " — выплаты в USDT TRC-20 после вашего подтверждения.",
    details_lines=[
        "Без магазина — только ручная отметка «выплачено». Конструктор деньги не переводит.",
        "🎨 Оформление и контент: в карточке бота («🎨 Оформление» и «📚 Контент»).",
        "↩️ Если запутались: /cancel или кнопка меню отменяет ввод. /help показывает эту справку.",
    ],
    expandable=True,
)


def contact_buttons(ctx: Ctx) -> tuple[list, str]:
    def _u(key: str, fallback: str) -> str:
        return support_url(ctx.db.kv_get(key) or fallback)
    chat = _u("support_chat", ctx.cfg.chat_url)
    channel = _u("support_channel", ctx.cfg.channel_url)
    admin = _u("support_admin", ctx.cfg.admin_url)
    links = [url_btn(t, u) for t, u in (("💬 Чат", chat), ("📢 Канал", channel)) if u]
    return links, admin


def info_kb(ctx: Ctx):
    links, admin_url = contact_buttons(ctx)
    buttons = []
    if links:
        buttons.extend(links)
    buttons += [
        btn("🏆 Конкурс", "contest:view"),
        btn("❓ Помощь", "info:help"),
        btn("📄 Документы", "info:docs"),
        btn("💡 Идеи и обратная связь", "ideas"),
        btn("⛔ Оповестить о проблеме", "info:report", style="danger"),
    ]
    if admin_url:
        buttons.append(url_btn("👑 Администратор", admin_url))
    else:
        buttons.append(btn("👑 Администратор", "info:admin"))
    return with_nav(grid(buttons, 2))


def help_kb(ctx: Ctx):
    links, admin_url = contact_buttons(ctx)
    rows = []
    if links:
        rows.append(links)
    if admin_url:
        rows.append([url_btn("👑 Администратор", admin_url)])
    rows.append([btn("⛔ Оповестить о проблеме", "info:report", style="danger")])
    rows.append([btn("⬅️ Назад", "info:back")])
    return ikb(*rows)


def help_text(ctx: Ctx) -> str:
    links, admin_url = contact_buttons(ctx)
    extra = "Связь — кнопками ниже." if (links or admin_url) else "Связь через «⛔ Оповестить о проблеме»."
    return HELP + "\n\n" + extra


@router.message(F.text == BTN_INFO)
async def info(message: Message, ctx: Ctx) -> None:
    await show(message, info_text(ctx), info_kb(ctx))


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    await show(cb, text, markup)


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



@router.callback_query(F.data == "info:docs")
async def docs_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    text = screen("Документы", "Ознакомьтесь с условиями использования сервиса.")
    buttons = [
        btn("📜 Пользовательское соглашение", "info:doc:agreement"),
        btn("🔒 Политика конфиденциальности", "info:doc:privacy"),
        btn("📋 Публичная оферта", "info:doc:offer"),
    ]
    kb = with_nav(grid(buttons, 1), back_data="info:back")
    await _show(cb, text, kb)


@router.callback_query(F.data.startswith("info:doc:"))
async def show_doc(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in DOCS:
        await cb.answer("Документ не найден", show_alert=True)
        return
    _title, body = DOCS[key]
    await cb.answer()
    rows = []
    if key in FILES and FILES[key].is_file():
        rows.append([btn("📥 Скачать полную версию (PDF)", f"info:pdf:{key}")])
    rows.append([btn("⬅️ К документам", "info:docs")])
    await _show(cb, body, ikb(*rows))


@router.callback_query(F.data.startswith("info:pdf:"))
async def send_pdf(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    path = FILES.get(key)
    if not path or not path.is_file():
        await cb.answer("Файл временно недоступен", show_alert=True)
        return
    await cb.answer()
    caption = DOCS[key][0]
    try:
        await cb.message.answer_document(FSInputFile(path), caption=caption)
    except Exception:
        await cb.message.answer("Не удалось отправить файл. Попробуйте позже.")


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
