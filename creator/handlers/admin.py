"""Админ-панель конструктора (только для ADMIN_IDS)."""
from __future__ import annotations

import asyncio
import html
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child import templates
from creator import bot_broadcast as bcb
from creator.config import MONEY_KEYS, PRICE_KEYS
from creator.handlers.access import private_admin
from creator.keyboards import BTN_ADMIN, STATUS_TEXT, btn, cancel_kb, ikb, status_icon
from creator.money import parse_rub, rub
from creator.services import Ctx
from creator.states import Admin
from creator.tgrass import child_users_for_card

router = Router(name="admin")


class IsAdmin(Filter):
    """Re-checked on every admin message, callback and admin FSM input."""
    async def __call__(self, event, ctx: Ctx) -> bool:
        return private_admin(event, ctx)


PAGE = 10
BOT_ACTIONS = ("", "start", "stop", "del", "delask")

def _users_label(ctx: Ctx, bot_id: int) -> str:
    n = child_users_for_card(ctx.sup.bot_dir(bot_id))
    return "н/д" if n is None else str(n)

router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _group_rows(ctx: Ctx | None, uid: int | None) -> dict[str, list[list]]:
    """Submenus of the creator admin panel; every former top-level button lives in one group."""
    groups = {
        "people": [[btn("👤 Пользователи", "a:users"), btn("🤖 Боты", "a:bots")],
                   [btn("🔎 Найти бота", "a:botfind")]],
        "bc": [[btn("📢 Рассылка по конструктору", "a:bc")], [btn("📢 Рассылка по ботам", "a:bcb")],
                   [btn("📰 Новость (бот + канал)", "a:news")]],
        "fin": [[btn("💰 Цены и тарифы", "a:prices")],
                   [btn("💳 LavePay", "a:lave:0")]],
        "tools": [[btn("📜 Журнал", "a:alog:0"), btn("📡 Источники ОП", "src:home")],
                   [btn("💡 Идеи пользователей", "a:ideas")],
                   [btn("🔄 Перезапустить все боты", "a:restartall")]],
        "settings": [[btn("🔒 Обязательная подписка", "a:set:gate")],
                     [btn("💡 Награда за идеи", "a:set:reward")],
                     [btn("📞 Контакты поддержки", "a:set:contacts")]],
    }
    integ = []
    if ctx is not None and ctx.tgrass is not None:
        integ.append([btn("💸 TGrass", "a:tg")])
    if ctx is not None and ctx.piarflow is not None:
        integ.append([btn("🚀 PiarFlow", "a:pf")])
    if ctx is not None and getattr(ctx, "unsub", None):
        integ.append([btn("🔗 Вебхуки трансляции", "a:whhub")])
    if integ:
        groups["integ"] = integ
    return groups


GROUP_TITLES = {
    "people": "👥 Люди и боты",
    "bc": "📢 Рассылки",
    "fin": "💰 Финансы",
    "tools": "🛠 Инструменты",
    "settings": "⚙️ Настройки",
    "integ": "🔌 Интеграции",
}


def panel_kb(ctx: Ctx | None = None, uid: int | None = None):
    groups = _group_rows(ctx, uid)
    rows = [[btn("📊 Статистика", "a:stats")],
            [btn(GROUP_TITLES["people"], "a:g:people"), btn(GROUP_TITLES["bc"], "a:g:bc")],
            [btn(GROUP_TITLES["fin"], "a:g:fin"), btn(GROUP_TITLES["tools"], "a:g:tools")],
            [btn(GROUP_TITLES["settings"], "a:g:settings")]]
    if "integ" in groups:
        rows.append([btn(GROUP_TITLES["integ"], "a:g:integ")])
    if ctx is not None and uid is not None and uid == ctx.cfg.approver:
        rows.append([btn("💸 Заявки на вывод", "a:wd:0")])
    return ikb(*rows)


def group_kb(ctx: Ctx | None, uid: int | None, key: str):
    rows = _group_rows(ctx, uid).get(key)
    if rows is None:
        return None
    return ikb(*rows, [btn("⬅️ В панель", "a:main")])


def panel_text(ctx: Ctx) -> str:
    """Recorded counts only (no money aggregates); recorded status is not a live Telegram check."""
    q = ctx.db.scalar
    users = q("SELECT COUNT(*) FROM users") or 0
    banned = q("SELECT COUNT(*) FROM users WHERE banned=1") or 0
    bots = q("SELECT COUNT(*) FROM bots WHERE deleted=0") or 0
    running = q("SELECT COUNT(*) FROM bots WHERE deleted=0 AND status='running'") or 0
    errors = q("SELECT COUNT(*) FROM bots WHERE deleted=0 AND status='error'") or 0
    deleted = q("SELECT COUNT(*) FROM bots WHERE deleted=1") or 0
    alive = sum(1 for i in list(ctx.sup.children) if ctx.sup.is_running(i))
    shops = q("SELECT COUNT(*) FROM owner_lavepay_shops") or 0
    return ("🛠 <b>Админ-панель</b>\n\n"
            f"Пользователей: {users} (забаненных {banned})\n"
            f"ботов {bots} · running {running} · error {errors} · удалённых {deleted}\n"
            f"Процессов: {alive} · магазинов LavePay: {shops}\n"
            f"<i>Это записанный статус и процессы супервизора, не проверка ответа Telegram.</i>")


@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN)
async def panel(message: Message, state: FSMContext, ctx: Ctx) -> None:
    ctx.source_inputs.pop(message.from_user.id, None)
    bcb.clear_payload(message.from_user.id)
    await state.clear()
    await message.answer(panel_text(ctx) + "\n\nОтветить пользователю: <code>/reply ID текст</code>",
                         reply_markup=panel_kb(ctx, message.from_user.id))


@router.callback_query(F.data.startswith("a:g:"))
async def panel_group(cb: CallbackQuery, ctx: Ctx) -> None:
    key = cb.data[4:]
    kb = group_kb(ctx, cb.from_user.id, key)
    if kb is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await cb.message.edit_text(f"🛠 <b>Админ-панель</b> → {GROUP_TITLES[key]}", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "a:main")
async def panel_cb(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    ctx.source_inputs.pop(cb.from_user.id, None)
    bcb.clear_payload(cb.from_user.id)
    await state.clear()
    await cb.message.edit_text(panel_text(ctx), reply_markup=panel_kb(ctx, cb.from_user.id))
    await cb.answer()


@router.message(Command("reply"))
async def reply(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2 or not args[0].isdigit():
        await message.answer("Формат: /reply ID текст")
        return
    try:
        await message.bot.send_message(int(args[0]), f"📩 <b>Ответ администратора:</b>\n\n{html.escape(args[1])}")
        await message.answer("✅ Отправлено")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Не доставлено: {html.escape(str(e))}")


@router.callback_query(F.data == "a:stats")
async def stats(cb: CallbackQuery, ctx: Ctx) -> None:
    s = ctx.db.stats()
    alive = sum(1 for i in ctx.sup.children if ctx.sup.is_running(i))
    await cb.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {s['users_total']} (сегодня +{s['users_today']})\n"
        f"🤖 Ботов: {s['bots_total']}, запущено: {s['bots_running']} (процессов: {alive})\n"
        f"💳 Оплат всего: {rub(s['payments_total'])}, сегодня: {rub(s['payments_today'])}\n"
        f"💼 Сумма балансов пользователей: {rub(s['balances'])}",
        reply_markup=ikb([btn("⬅️ Назад", "a:main")]),
    )
    await cb.answer()


def show_price(key: str, value: int) -> str:
    return rub(value) if key in MONEY_KEYS else str(value)


PRICE_GROUPS = [
    ("Шаблоны", ["PRICE_CLICKER", "PRICE_REFBOT", "PRICE_STARBOT", "PRICE_MICROTASKS"]),
    ("Хостинг и VIP", ["HOSTING_DAY_PRICE", "START_HOSTING_DAYS", "VIP_PRICE"]),
    ("Проценты", ["REF_PERCENT", "RESALE_FEE_PERCENT"]),
    ("LavePay", ["LAVE_USD_RUB"]),
    ("Вывод", ["WITHDRAW_MIN", "WITHDRAW_FEE_PERCENT"]),
    ("TGrass", ["TGRASS_REWARD", "TGRASS_MIN_USERS"]),
    ("PiarFlow", ["PIARFLOW_REWARD", "PIARFLOW_MIN_USERS"]),
]


@router.callback_query(F.data == "a:prices")
async def prices(cb: CallbackQuery, ctx: Ctx) -> None:
    rows = []
    for title, keys in PRICE_GROUPS:
        visible = [k for k in keys if k in PRICE_KEYS
                   and (ctx.tgrass is not None or not k.startswith("TGRASS_"))
                   and (ctx.piarflow is not None or not k.startswith("PIARFLOW_"))]
        if not visible:
            continue
        rows.append([btn(f"— {title} —", "a:prices")])
        for k in visible:
            rows.append([btn(f"{PRICE_KEYS[k]}: {show_price(k, ctx.price(k))}", f"a:price:{k}")])
    rows.append([btn("⬅️ Назад", "a:g:fin")])
    await cb.message.edit_text(
        "💰 <b>Цены и тарифы</b>\nНажмите на значение, чтобы изменить.",
        reply_markup=ikb(*rows),
        disable_web_page_preview=True,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:price:"))
async def price_pick(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in PRICE_KEYS:
        await cb.answer()
        return
    await state.set_state(Admin.price)
    await state.update_data(key=key)
    current = show_price(key, ctx.price(key))
    hint = "сумма в ₽ (225 или 4,50)" if key in MONEY_KEYS else "целое число ≥ 0"
    await cb.message.edit_text(
        f"💰 <b>{PRICE_KEYS[key]}</b>\n\n"
        f"Сейчас: <b>{current}</b>\n"
        f"Введите новое значение ({hint}):",
        reply_markup=cancel_kb("a:prices"),
        disable_web_page_preview=True,
    )
    await cb.answer()


@router.message(Admin.price, F.text)
async def price_set(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip()
    key = (await state.get_data())["key"]
    if key in MONEY_KEYS:
        value = parse_rub(raw)
        if value is None:
            await message.answer("Введите сумму в ₽, например 225 или 4,50.")
            return
    else:
        if not raw.isdigit():
            await message.answer("Введите целое число ≥ 0.")
            return
        value = int(raw)
    if key == "LAVE_USD_RUB" and value < 1:
        await message.answer("Значение должно быть ≥ 1.")
        return
    await state.clear()
    ctx.db.kv_set(f"price:{key}", str(value))
    ctx.db.log_admin(message.from_user.id, "price", target=key, detail=f"new={value}")
    await message.answer(f"✅ {PRICE_KEYS[key]}: {show_price(key, value)}",
                         reply_markup=ikb([btn("💰 К ценам", "a:prices")]))


@router.callback_query(F.data == "a:bc")
async def bc(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.broadcast)
    await cb.message.edit_text("📢 Пришлите сообщение для рассылки всем пользователям конструктора.",
                               reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.broadcast)
async def bc_preview(message: Message, state: FSMContext, ctx: Ctx) -> None:
    ids = ctx.db.all_user_ids()
    await state.update_data(bc_ids=len(ids), bc_chat=message.chat.id, bc_msg=message.message_id)
    await message.answer(
        f"👁 Предпросмотр. Отправить {len(ids)} пользователям?",
        reply_markup=ikb(
            [btn(f"✅ Отправить {len(ids)}", "a:bcgo")],
            [btn("❌ Отмена", "a:main")],
        ),
    )


@router.callback_query(F.data == "a:bcgo")
async def bc_send(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    await state.clear()
    chat_id = data.get("bc_chat")
    msg_id = data.get("bc_msg")
    if not chat_id or not msg_id:
        await cb.answer("Сначала пришлите сообщение", show_alert=True)
        return
    ids = ctx.db.all_user_ids()
    await cb.message.edit_text(f"🚀 Рассылка запущена: {len(ids)} получателей.")
    await cb.answer()

    async def run() -> None:
        ok = fail = 0
        for uid in ids:
            try:
                await cb.bot.copy_message(uid, chat_id, msg_id)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                fail += 1
            except TelegramForbiddenError:
                fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            await asyncio.sleep(0.05)
        await cb.bot.send_message(cb.from_user.id, f"✅ Рассылка завершена: доставлено {ok}, ошибок {fail}.")

    asyncio.create_task(run())



@router.callback_query(F.data == "a:bcb")
async def bcb_start(cb: CallbackQuery, state: FSMContext) -> None:
    bcb.clear_payload(cb.from_user.id)
    await state.set_state(Admin.broadcast_bots)
    await cb.message.edit_text(
        "📢 Рассылка по пользователям <b>всех дочерних ботов</b> (кроме удалённых).\n"
        "Сообщение уйдёт от имени каждого бота его игрокам.\n"
        "Пришлите текст или медиа. Дальше будет подсчёт получателей и подтверждение. "
        "Автоматических рассылок нет.",
        reply_markup=cancel_kb("a:main"),
    )
    await cb.answer()


@router.message(Admin.broadcast_bots)
async def bcb_preview(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not bcb.can_relay(message):
        await message.answer(
            "Этот тип сообщения не поддерживается. Пришлите текст, фото, видео, документ, "
            "анимацию, аудио или голосовое.",
            reply_markup=cancel_kb("a:main"),
        )
        return
    targets = bcb.collect_recipient_counts(ctx.db, ctx.sup.bot_dir)
    total = sum(len(ids) for _bot, ids in targets)
    bcb.store_payload(message.from_user.id, message)
    await state.update_data(bcb_ready=True, bcb_total=total, bcb_bots=len(targets))
    await message.answer(
        f"👁 Предпросмотр рассылки по дочерним ботам.\n\n"
        f"Ботов с получателями: {len(targets)}\n"
        f"Всего получателей: {total}\n\n"
        "Сообщение уйдёт от имени каждого бота. Запуск только этой кнопкой, "
        "после перезапуска рассылка сама не продолжится.",
        reply_markup=ikb(
            [btn(f"✅ Отправить {total}", "a:bcbgo")],
            [btn("❌ Отмена", "a:main")],
        ),
    )


@router.callback_query(F.data == "a:bcbgo")
async def bcb_send(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    source = bcb.pop_payload(cb.from_user.id)
    await state.clear()
    if not data.get("bcb_ready") or source is None:
        await cb.answer("Сначала пришлите сообщение и подтвердите отправку", show_alert=True)
        return
    if bcb.is_busy():
        await cb.answer("Уже идёт другая рассылка по ботам", show_alert=True)
        return
    targets = bcb.collect_recipient_counts(ctx.db, ctx.sup.bot_dir)
    total = sum(len(ids) for _b, ids in targets)
    await cb.message.edit_text(
        f"🚀 Рассылка по ботам запущена: {len(targets)} ботов, {total} получателей."
    )
    await cb.answer()
    ctx.db.log_admin(
        cb.from_user.id, "broadcast_bots",
        detail=f"bots={len(targets)} recipients={total}",
    )
    notify_bot = cb.bot
    admin_id = cb.from_user.id
    session = cb.bot.session

    async def run() -> None:
        async with bcb.lock():
            ok, fail = await bcb.run_broadcast(
                db=ctx.db,
                session=session,
                source=source,
                targets=targets,
                admin_id=admin_id,
            )
        await notify_bot.send_message(
            admin_id,
            f"✅ Рассылка по ботам завершена: доставлено {ok}, ошибок {fail}.",
        )

    bcb.spawn(run())


FILTERS = ("all", "banned", "withbots", "nonzero")
FILTER_LABEL = {"all": "все", "banned": "забаненные", "withbots": "с ботами", "nonzero": "баланс ≠ 0"}

def _user_where(flt: str) -> tuple[str, list]:
    if flt == "banned":
        return "WHERE banned=1", []
    if flt == "withbots":
        return "WHERE id IN (SELECT owner_id FROM bots WHERE deleted=0)", []
    if flt == "nonzero":
        return "WHERE balance != 0", []
    return "", []

@router.callback_query(F.data == "a:users")
async def users(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await _show_user_list(cb, ctx, page=0, flt="all")

async def _show_user_list(cb: CallbackQuery, ctx: Ctx, page: int, flt: str, search: str = "") -> None:
    if flt not in FILTERS:
        flt = "all"
    where, args = _user_where(flt)
    if search:
        like = f"%{search}%"
        extra = "(id=? OR username LIKE ? OR full_name LIKE ?)"
        where = (where + " AND " if where else "WHERE ") + extra
        args = args + [int(search) if search.isdigit() else -1, like, like]
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM users {where}", *args) or 0
    pages = max(1, -(-total // PAGE))
    page = max(0, min(page, pages - 1))
    rows = ctx.db.all(
        f"SELECT id, username, full_name, balance, banned FROM users {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        *args, PAGE, page * PAGE,
    )
    lines = [f"👤 <b>Пользователи</b> · {FILTER_LABEL.get(flt, flt)} · стр. {page + 1}/{pages} · всего {total}"]
    if search:
        lines.append(f"Поиск: <code>{html.escape(search)}</code>")
    kb_rows = []
    for r in rows:
        mark = "🚫" if r["banned"] else "✅"
        name = (r["username"] and f"@{r['username']}") or (r["full_name"] or str(r["id"]))
        kb_rows.append([btn(f"{mark} {name[:24]} · {rub(r['balance'])}", f"a:user:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"a:ulist:{page - 1}:{flt}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"a:ulist:{page + 1}:{flt}"))
    if nav:
        kb_rows.append(nav)
    filters = [btn(FILTER_LABEL[f], f"a:ulist:0:{f}") for f in FILTERS]
    kb_rows.append(filters[:2])
    kb_rows.append(filters[2:])
    kb_rows.append([btn("🔎 Поиск", "a:usearch"), btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(*kb_rows))
    await cb.answer()

@router.callback_query(F.data.startswith("a:ulist:"))
async def user_list(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or parts[3] not in FILTERS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await _show_user_list(cb, ctx, int(parts[2]), parts[3])

@router.callback_query(F.data == "a:usearch")
async def user_search_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.user_lookup)
    await cb.message.edit_text("🔎 Пришлите ID, @username или фрагмент имени:", reply_markup=cancel_kb("a:main"))
    await cb.answer()


def user_card(ctx: Ctx, uid: int, bot_page: int = 0):
    u = ctx.db.user(uid)
    if not u:
        return None, None
    bots = ctx.db.user_bots(uid)
    total_bots = len(bots)
    pages = max(1, -(-total_bots // PAGE)) if total_bots else 1
    bot_page = max(0, min(bot_page, pages - 1))
    slice_ = bots[bot_page * PAGE:(bot_page + 1) * PAGE]
    ids = ", ".join(str(b["id"]) for b in slice_) or "—"
    created = time.strftime("%Y-%m-%d", time.localtime(u["created_at"])) if u["created_at"] else "—"
    text = (f"👤 <b>{html.escape(u['full_name'] or '')}</b> "
            f"{('@' + html.escape(u['username'])) if u['username'] else ''}\n"
            f"ID: <code>{uid}</code>\nБаланс: {rub(u['balance'])}\nРефералов: {ctx.db.referrals_count(uid)}\n"
            f"Ботов: {total_bots} (стр. {bot_page + 1}/{pages}: {ids})\n"
            f"Регистрация: {created}\n"
            f"Статус: {'🚫 забанен' if u['banned'] else '✅ активен'}")
    links = [btn(f"🤖 ID {b['id']}", f"a:bot:{b['id']}") for b in slice_]
    nav = []
    if bot_page > 0:
        nav.append(btn("⬅️ боты", f"a:uc:{uid}:{bot_page - 1}"))
    if bot_page < pages - 1:
        nav.append(btn("➡️ боты", f"a:uc:{uid}:{bot_page + 1}"))
    rows = [
        [btn("💰 Изменить баланс", f"a:ubal:{uid}")],
        [btn("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"a:uban:{uid}")],
        [btn("📜 История операций", f"a:uhist:{uid}:0")],
    ]
    if links:
        rows.extend([links[i:i + 2] for i in range(0, len(links), 2)])
    if nav:
        rows.append(nav)
    rows.append([btn("⬅️ К списку", "a:ulist:0:all")])
    rows.append([btn("⬅️ В админку", "a:main")])
    return text, ikb(*rows)


@router.message(Admin.user_lookup, F.text)
async def user_lookup(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip().lstrip("@")
    if raw.isdigit():
        uid = int(raw)
        text, kb = user_card(ctx, uid)
        if text:
            await state.clear()
            await message.answer(text, reply_markup=kb)
            return
    # fragment search
    like = f"%{raw}%"
    rows = ctx.db.all(
        "SELECT id FROM users WHERE username LIKE ? OR full_name LIKE ? OR id=? ORDER BY id DESC LIMIT 5",
        like, like, int(raw) if raw.isdigit() else -1,
    )
    if not rows:
        await message.answer("Ничего не найдено. Попробуйте другой запрос или /cancel.")
        return
    if len(rows) == 1:
        await state.clear()
        text, kb = user_card(ctx, rows[0]["id"])
        await message.answer(text, reply_markup=kb)
        return
    kb = ikb(*[[btn(f"ID {r['id']}", f"a:user:{r['id']}")] for r in rows],
              [btn("⬅️ К списку", "a:ulist:0:all")])
    await message.answer(f"Найдено несколько ({len(rows)}). Выберите:", reply_markup=kb)
    await state.clear()


@router.callback_query(F.data.startswith("a:uban:"))
async def user_ban(cb: CallbackQuery, ctx: Ctx) -> None:
    uid = int(cb.data.rsplit(":", 1)[1])
    u = ctx.db.user(uid)
    if u:
        new_banned = not u["banned"]
        ctx.db.set_banned(uid, new_banned)
        ctx.db.log_admin(cb.from_user.id, "ban" if new_banned else "unban",
                         target=str(uid), detail="banned" if new_banned else "unbanned")
        text, kb = user_card(ctx, uid)
        await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("a:ubal:"))
async def user_bal(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.user_balance)
    await state.update_data(uid=int(cb.data.rsplit(":", 1)[1]))
    await cb.message.answer("Введите изменение баланса в ₽ (например 100, -50 или 12,50):", reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.user_balance, F.text)
async def user_bal_set(message: Message, state: FSMContext, ctx: Ctx) -> None:
    delta = parse_rub(message.text, allow_negative=True)
    if delta is None:
        await message.answer("Введите сумму в ₽, например 100, -50 или 12,50.")
        return
    data = await state.get_data()
    uid = data["uid"]
    await state.clear()
    new = ctx.db.change_balance(uid, delta, "admin", f"Администратор {message.from_user.id}", allow_negative=True)
    ctx.db.log_admin(message.from_user.id, "balance", target=str(uid),
                     detail=f"delta={delta} new={new}")
    text, kb = user_card(ctx, uid)
    await message.answer(f"✅ Баланс <code>{uid}</code>: {rub(new)}")
    if text:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("a:uc:"))
async def user_card_page(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    text, kb = user_card(ctx, int(parts[2]), int(parts[3]))
    if not text:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("a:uhist:"))
async def user_history(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    uid, page = int(parts[2]), int(parts[3])
    u = ctx.db.user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    rows = ctx.db.history(uid, limit=50)
    PAGEH = 8
    pages = max(1, -(-len(rows) // PAGEH))
    page = max(0, min(page, pages - 1))
    slice_ = rows[page * PAGEH:(page + 1) * PAGEH]
    lines = [f"📜 <b>История операций</b> · <code>{uid}</code> · стр. {page + 1}/{pages}"]
    for r in slice_:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
        lines.append(f"{when} · {rub(r['amount'])} · {html.escape(r['kind'] or '')} · {html.escape((r['comment'] or '')[:40])}")
    if not slice_:
        lines.append("Пусто")
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"a:uhist:{uid}:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"a:uhist:{uid}:{page + 1}"))
    kb = ikb(*( [nav] if nav else [] ), [btn("⬅️ К карточке", f"a:user:{uid}")])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "a:user")
@router.callback_query(F.data.startswith("a:user:"))
async def user_open(cb: CallbackQuery, ctx: Ctx) -> None:
    raw = (cb.data or "").split(":")
    if len(raw) != 3 or not re.fullmatch(r"[0-9]{1,18}", raw[2]):
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    text, kb = user_card(ctx, int(raw[2]))
    if not text:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


BOT_FILTERS = ("all", "clicker", "refbot", "starbot", "microtasks", "running", "stopped", "error", "deleted")
BOT_FILTER_LABEL = {
    "all": "все", "clicker": "кликер", "refbot": "реф", "starbot": "звезда", "microtasks": "задания",
    "running": "running", "stopped": "stopped", "error": "error", "deleted": "удалённые",
}

def _bot_where(flt: str) -> tuple[str, list]:
    if flt == "deleted":
        return "WHERE deleted=1", []
    if flt in ("clicker", "refbot", "starbot", "microtasks"):
        return "WHERE deleted=0 AND template=?", [flt]
    if flt in ("running", "stopped", "error"):
        return "WHERE deleted=0 AND status=?", [flt]
    return "WHERE deleted=0", []

@router.callback_query(F.data == "a:bots")
@router.callback_query(F.data.startswith("a:bots:"))
async def bots(cb: CallbackQuery, ctx: Ctx) -> None:
    raw = (cb.data or "").split(":")
    # a:bots  | a:bots:page  | a:bots:page:filter
    if len(raw) > 4:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    page = 0
    flt = "all"
    if len(raw) >= 3:
        if not re.fullmatch(r"[0-9]{1,6}", raw[2]):
            await cb.answer("Кнопка устарела", show_alert=True)
            return
        page = int(raw[2])
    if len(raw) == 4:
        if raw[3] not in BOT_FILTERS:
            await cb.answer("Кнопка устарела", show_alert=True)
            return
        flt = raw[3]
    where, args = _bot_where(flt)
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM bots {where}", *args) or 0
    pages = max(1, -(-total // PAGE))
    page = min(page, pages - 1)
    rows = ctx.db.all(f"SELECT * FROM bots {where} ORDER BY id DESC LIMIT ? OFFSET ?", *args, PAGE, page * PAGE)
    def _bot_btn(b):
        label = (f"ID {b['id']} {status_icon(b['status'])} @{b['username']} "
                 f"· {_users_label(ctx, b['id'])} 👤 | {templates.info(b['template']).title}")
        return [btn(label, f"a:bot:{b['id']}")]
    kb = [_bot_btn(b) for b in rows]
    def _page_cb(p):
        return f"a:bots:{p}" if flt == "all" else f"a:bots:{p}:{flt}"
    nav = ([btn("⬅️", _page_cb(page - 1))] if page > 0 else []) + \
          ([btn("➡️", _page_cb(page + 1))] if page < pages - 1 else [])
    filters = [btn(BOT_FILTER_LABEL[f], f"a:bots:0:{f}") for f in BOT_FILTERS]
    kb += [nav] if nav else []
    kb += [filters[:4], filters[4:]]
    kb += [[btn("🔎 Найти бота", "a:botfind")], [btn("⬅️ Назад", "a:main")]]
    label = BOT_FILTER_LABEL.get(flt, flt)
    await cb.message.edit_text(
        f"🤖 <b>Боты</b> · {label} · страница {page + 1}/{pages}, всего {total}",
        reply_markup=ikb(*kb))
    await cb.answer()


@router.callback_query(F.data == "a:botfind")
async def bot_find(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.bot_lookup)
    await cb.message.edit_text("🔎 Пришлите ID бота, ID владельца или @username:", reply_markup=cancel_kb("a:main"))
    await cb.answer()


@router.message(Admin.bot_lookup, F.text)
async def bot_lookup(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = message.text.strip()
    if re.fullmatch(r"[0-9]{1,18}", raw):
        found = ctx.db.scalar("SELECT id FROM bots WHERE id=? AND deleted=0", int(raw))
        if not found:
            owned = ctx.db.all(
                "SELECT id, username, status, template FROM bots "
                "WHERE owner_id=? AND deleted=0 ORDER BY id DESC LIMIT 15",
                int(raw),
            )
            if owned:
                await state.clear()
                kb = ikb(
                    *[[btn(f"ID {b['id']} @{b['username'] or '—'} · {_users_label(ctx, b['id'])} 👤",
                           f"a:bot:{b['id']}")] for b in owned],
                    [btn("⬅️ Боты", "a:bots")],
                )
                await message.answer(f"Боты владельца <code>{raw}</code> ({len(owned)}):", reply_markup=kb)
                return
    elif re.fullmatch(r"@?[A-Za-z0-9_]{1,64}", raw):
        found = ctx.db.scalar("SELECT id FROM bots WHERE deleted=0 AND username=? COLLATE NOCASE "
                              "ORDER BY id DESC LIMIT 1", raw.lstrip("@"))
    else:
        await message.answer("Введите ID бота, ID владельца или @username (или /cancel).")
        return
    view = bot_view(ctx, found, "") if found else None
    if not view:
        await message.answer("Бот не найден. Пришлите другой ID или @username (или /cancel).")
        return
    await state.clear()
    await message.answer(view[0], reply_markup=view[1])


def bot_view(ctx: Ctx, bot_id: int, action: str):
    b = ctx.db.bot(bot_id)
    if not b:
        return None
    users_s = _users_label(ctx, b["id"])
    text = (f"🤖 <b>Бот ID {b['id']}</b> @{html.escape(b['username'] or '')}\nВладелец: <code>{b['owner_id']}</code>\n"
            f"Шаблон: {templates.info(b['template']).title}\nСтатус: {STATUS_TEXT.get(b['status'], b['status'])}\n"
            f"Пользователей: {users_s}\n"
            f"Хостинг: {rub(b['hosting_balance'])}\nVIP: {'да' if b['vip'] else 'нет'}\n"
            f"Ошибка: {html.escape(b['last_error'] or '—')}")
    rows = [
        [btn("▶️ Запустить", f"a:bot:{bot_id}:start"), btn("⏹ Остановить", f"a:bot:{bot_id}:stop")],
        [btn("🗑 Удалить", f"a:bot:{bot_id}:delask")] if action != "delask"
        else [btn("⚠️ Точно удалить", f"a:bot:{bot_id}:del")],
        [btn("👤 Владелец", f"a:user:{b['owner_id']}")],
    ]
    if getattr(ctx, "unsub", None):
        rows.append([btn("🔗 Вебхуки отписок", f"a:wh:{bot_id}")])
    rows.append([btn("⬅️ Боты", "a:bots")])
    kb = ikb(*rows)
    return text, kb


@router.callback_query(F.data == "a:whhub")
async def webhook_hub(cb: CallbackQuery, ctx: Ctx) -> None:
    from creator.handlers.webhooks import show_hub
    await show_hub(cb, ctx)


@router.callback_query(F.data.startswith("a:wh:"))
async def webhook_urls(cb: CallbackQuery, ctx: Ctx) -> None:
    from creator.handlers.webhooks import show_admin
    raw = cb.data.split(":")[-1]
    if not re.fullmatch(r"[0-9]{1,18}", raw):
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await show_admin(cb, ctx, int(raw))


@router.callback_query(F.data.startswith("a:bot:"))
async def bot_card(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) not in (3, 4) or not re.fullmatch(r"[0-9]{1,18}", parts[2]) \
            or (parts[3] if len(parts) > 3 else "") not in BOT_ACTIONS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    bot_id = int(parts[2])
    action = parts[3] if len(parts) > 3 else ""
    if action == "start":
        await ctx.sup.start(bot_id)
        ctx.db.log_admin(cb.from_user.id, "bot_start", target=str(bot_id))
    elif action == "stop":
        await ctx.sup.stop(bot_id)
        ctx.db.log_admin(cb.from_user.id, "bot_stop", target=str(bot_id))
    elif action == "del":
        await ctx.delete_bot(bot_id)
        ctx.db.log_admin(cb.from_user.id, "bot_delete", target=str(bot_id))
        await cb.message.edit_text(f"🗑 Бот ID {bot_id} удалён.", reply_markup=ikb([btn("⬅️ Боты", "a:bots")]))
        await cb.answer()
        return
    view = bot_view(ctx, bot_id, action)
    if not view:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer()

LOG_FILTERS = ("all", "ban", "balance", "bot", "price", "broadcast")
LOG_LABELS = {"all": "все", "ban": "бан", "balance": "баланс", "bot": "боты", "price": "цены", "broadcast": "рассылки"}


def _log_where(flt: str) -> str:
    return {
        "ban": "WHERE action IN ('ban','unban')",
        "balance": "WHERE action='balance'",
        "bot": "WHERE action LIKE 'bot_%'",
        "price": "WHERE action='price'",
        "broadcast": "WHERE action LIKE 'broadcast%'",
    }.get(flt, "")


@router.callback_query(F.data.startswith("a:alog:"))
async def admin_log(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    page = int(parts[2])
    flt = parts[3] if len(parts) > 3 and parts[3] in LOG_FILTERS else "all"
    where = _log_where(flt)
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM admin_actions {where}") or 0
    pages = max(1, -(-total // PAGE))
    page = max(0, min(page, pages - 1))
    rows = ctx.db.all(f"SELECT * FROM admin_actions {where} ORDER BY id DESC LIMIT ? OFFSET ?", PAGE, page * PAGE)
    lines = [f"📜 <b>Журнал</b> · {LOG_LABELS[flt]} · стр. {page + 1}/{pages} · всего {total}"]
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
        action = html.escape(r["action"])
        target = html.escape(r["target"] or "")
        detail = html.escape((r["detail"] or "")[:50])
        lines.append(f"{when} · <code>{r['admin_id']}</code> · {action} · {target} · {detail}")
    if not rows:
        lines.append("Пусто")
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"a:alog:{page - 1}:{flt}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"a:alog:{page + 1}:{flt}"))
    filters = [btn(lbl, f"a:alog:0:{key}") for key, lbl in LOG_LABELS.items()]
    kb = ikb(*([nav] if nav else []), filters[:3], filters[3:], [btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("a:lave:"))
async def lavepay_summary(cb: CallbackQuery, ctx: Ctx) -> None:
    parts = cb.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    page = int(parts[2])
    total = ctx.db.lavepay_shops_count()
    pages = max(1, -(-total // PAGE))
    page = max(0, min(page, pages - 1))
    rows = ctx.db.lavepay_shops_summary(limit=PAGE, offset=page * PAGE)
    lines = [f"💳 <b>Магазины LavePay</b> · стр. {page + 1}/{pages} · всего {total}"]
    lines.append("<i>Ключи не показываются. Выплаты отсюда не отправляются.</i>")
    for r in rows:
        lines.append(
            f"Бот {r['bot_id']} · владелец <code>{r['owner_id']}</code>\n"
            f"  {html.escape(str(r['merchant_name']))} · {html.escape(str(r['status']))} · "
            f"выплаты {'вкл' if r['payouts_enabled'] else 'выкл'} · баланс {html.escape(str(r['usdt_balance']))}"
        )
    if not rows:
        lines.append("Нет подключённых магазинов")
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"a:lave:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"a:lave:{page + 1}"))
    kb = ikb(*( [nav] if nav else [] ), [btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "a:news")
async def news_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.news)
    await cb.message.edit_text(
        "📰 Пришлите новость.\n\n"
        "Она будет опубликована и в боте (всем пользователям), и в канале новостей.",
        reply_markup=cancel_kb("a:main"),
    )
    await cb.answer()


@router.message(Admin.news)
async def news_preview(message: Message, state: FSMContext, ctx: Ctx) -> None:
    from creator.handlers.gate import channel_dest
    channel = channel_dest(ctx)
    await state.update_data(news_chat=message.chat.id, news_msg=message.message_id, news_channel=channel)
    where = "в бот и в канал" if channel else "только в бот (канал не настроен)"
    await message.answer(
        f"👁 Предпросмотр. Опубликовать {where}?",
        reply_markup=ikb(
            [btn("✅ Опубликовать", "a:newsgo")],
            [btn("❌ Отмена", "a:main")],
        ),
    )


@router.callback_query(F.data == "a:newsgo")
async def news_send(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    await state.clear()
    chat_id = data.get("news_chat")
    msg_id = data.get("news_msg")
    channel = data.get("news_channel")
    if not chat_id or not msg_id:
        await cb.answer("Сначала пришлите сообщение", show_alert=True)
        return
    await cb.answer()
    ch_ok = False
    if channel:
        try:
            await cb.bot.copy_message(channel, chat_id, msg_id)
            ch_ok = True
        except Exception as exc:  # noqa: BLE001
            await cb.message.answer(f"⚠️ Не удалось опубликовать в канал: {exc}")
    ids = ctx.db.all_user_ids()
    await cb.message.edit_text(f"🚀 Публикация: канал {'✅' if ch_ok else '—'}, бот — {len(ids)} чел.")

    async def run() -> None:
        ok = fail = 0
        for uid in ids:
            try:
                await cb.bot.copy_message(uid, chat_id, msg_id)
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
            await asyncio.sleep(0.05)
        await cb.bot.send_message(
            cb.from_user.id,
            f"✅ Новость опубликована.\nКанал: {'да' if ch_ok else 'нет'}\nБот: {ok} доставлено, {fail} ошибок.",
        )

    asyncio.create_task(run())


@router.callback_query(F.data == "a:ideas")
async def admin_ideas(cb: CallbackQuery, ctx: Ctx) -> None:
    items = ctx.db.idea_list(15)
    if not items:
        await cb.answer("Идей пока нет", show_alert=True)
        return
    lines = ["💡 <b>Идеи пользователей</b>\n"]
    rows = []
    for it in items[:10]:
        lines.append(f"#{it['id']} · {it['votes']} · {it['status']}\n{html.escape(it['text'][:100])}")
        if it["status"] == "new":
            rows.append([btn(f"✅ Принять #{it['id']}", f"a:ideaok:{it['id']}")])
    rows.append([btn("⬅️ Назад", "a:main")])
    await cb.message.edit_text("\n\n".join(lines), reply_markup=ikb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:ideaok:"))
async def admin_idea_accept(cb: CallbackQuery, ctx: Ctx) -> None:
    iid = int(cb.data.split(":")[2])
    from creator.handlers.ideas import idea_reward_kop
    from creator.money import rub
    reward = idea_reward_kop(ctx)
    idea = ctx.db.idea_accept(iid, reward)
    if not idea:
        await cb.answer("Идея уже обработана", show_alert=True)
        return
    try:
        credited = f"На баланс начислено {rub(reward)}.\n" if reward else ""
        await cb.bot.send_message(
            idea["user_id"],
            f"🎉 Ваша идея #{iid} принята!\n{credited}Спасибо, что помогаете развивать конструктор!",
        )
    except Exception:  # noqa: BLE001
        pass
    await cb.answer("Идея принята, награда начислена")
    await admin_ideas(cb, ctx)


def _kv(ctx: Ctx, key: str, default: str = "") -> str:
    value = ctx.db.kv_get(key)
    return value if value not in (None, "") else default


def _mask_url(url: str) -> str:
    """Show a t.me link in admin UI but keep invite tokens out of logs."""
    return url or "—"


SETTING_LABELS = {
    "gate_channel_url": "ссылку на канал (https://t.me/имя или t.me/+…)",
    "gate_chat_url": "ссылку на чат (https://t.me/имя или t.me/+…)",
    "gate_channel_id": "числовой ID канала",
    "gate_chat_id": "числовой ID чата",
    "idea_reward_kop": "награду в ₽, например 50 (от 0 до 10 000)",
    "support_channel": "ссылку на канал в «Информации»",
    "support_chat": "ссылку на чат в «Информации»",
    "support_admin": "ссылку на администратора",
}
URL_SETTING_KEYS = {
    "gate_channel_url", "gate_chat_url", "support_channel", "support_chat", "support_admin",
}
ID_SETTING_KEYS = {"gate_channel_id", "gate_chat_id"}
ALLOWED_SETTING_KEYS = URL_SETTING_KEYS | ID_SETTING_KEYS | {"idea_reward_kop"}


@router.callback_query(F.data == "a:set:gate")
async def set_gate(cb: CallbackQuery, ctx: Ctx) -> None:
    from creator.handlers.gate import gate_is_on
    enabled = gate_is_on(ctx)
    ch = _kv(ctx, "gate_channel_url", ctx.cfg.channel_url) or "—"
    chat = _kv(ctx, "gate_chat_url", ctx.cfg.chat_url) or "—"
    ch_id = _kv(ctx, "gate_channel_id", str(ctx.cfg.channel_chat_id or "") ) or "—"
    chat_id = _kv(ctx, "gate_chat_id", str(ctx.cfg.chat_chat_id or "") ) or "—"
    text = (
        "🔒 <b>Обязательная подписка</b>\n\n"
        f"Сейчас: {'включена' if enabled else 'выключена'}\n"
        f"Канал: <code>{html.escape(_mask_url(ch))}</code>\n"
        f"ID канала: <code>{html.escape(str(ch_id))}</code>\n"
        f"Чат: <code>{html.escape(_mask_url(chat))}</code>\n"
        f"ID чата: <code>{html.escape(str(chat_id))}</code>\n\n"
        "Для закрытой ссылки бот должен быть администратором, а ID задан здесь "
        "или пересылкой поста из канала/чата конструктору."
    )
    kb = ikb(
        [btn("🔄 Вкл/выкл", "a:set:gate_toggle")],
        [btn("📢 Ссылка канала", "a:set:in:gate_channel_url"),
         btn("🆔 ID канала", "a:set:in:gate_channel_id")],
        [btn("💬 Ссылка чата", "a:set:in:gate_chat_url"),
         btn("🆔 ID чата", "a:set:in:gate_chat_id")],
        [btn("⬅️ Назад", "a:g:settings")],
    )
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(F.data == "a:set:gate_toggle")
async def set_gate_toggle(cb: CallbackQuery, ctx: Ctx) -> None:
    from creator.handlers.gate import gate_is_on
    new = "0" if gate_is_on(ctx) else "1"
    ctx.db.kv_set("gate_enabled", new)
    ctx.db.log_admin(cb.from_user.id, "settings", target="gate_enabled", detail=f"new={new}")
    await set_gate(cb, ctx)


@router.callback_query(F.data == "a:set:reward")
async def set_reward(cb: CallbackQuery, ctx: Ctx) -> None:
    from creator.handlers.ideas import IDEA_REWARD_MAX_KOP, idea_reward_kop
    reward = idea_reward_kop(ctx)
    text = (
        "💡 <b>Награда за идеи</b>\n\n"
        f"Сейчас: <b>{rub(reward)}</b>\n"
        f"Можно поставить от 0 до {rub(IDEA_REWARD_MAX_KOP)}. "
        "Новости и начисление награды остаются включены."
    )
    kb = ikb(
        [btn("✏️ Изменить", "a:set:in:idea_reward_kop")],
        [btn("⬅️ Назад", "a:g:settings")],
    )
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(F.data.startswith("a:set:in:"))
async def set_prompt(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.removeprefix("a:set:in:")
    if key not in ALLOWED_SETTING_KEYS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await state.set_state(Admin.settings_input)
    await state.update_data(set_key=key)
    await cb.message.answer(
        f"Пришлите {SETTING_LABELS.get(key, key)}:",
        reply_markup=cancel_kb("a:g:settings"),
    )
    await cb.answer()


@router.message(Admin.settings_input)
async def set_save(message: Message, state: FSMContext, ctx: Ctx) -> None:
    from creator.config import support_url
    from creator.handlers.ideas import IDEA_REWARD_MAX_KOP
    data = await state.get_data()
    key = data.get("set_key")
    await state.clear()
    if key not in ALLOWED_SETTING_KEYS:
        await message.answer("Сессия устарела.")
        return
    raw = (message.text or "").strip()
    detail = "updated"
    if key == "idea_reward_kop":
        value = parse_rub(raw)
        if value is None or value < 0 or value > IDEA_REWARD_MAX_KOP:
            await message.answer(
                f"Нужна сумма в ₽ от 0 до {rub(IDEA_REWARD_MAX_KOP)}, например 50."
            )
            return
        stored = str(value)
        detail = f"new={value}"
    elif key in ID_SETTING_KEYS:
        if not raw.lstrip("-").isdigit():
            await message.answer("Нужно целое число (ID чата или канала).")
            return
        stored = str(int(raw))
        detail = f"new={stored}"
    elif key in URL_SETTING_KEYS:
        url = support_url(raw)
        if not url:
            await message.answer("Нужна ссылка вида https://t.me/имя или https://t.me/+…")
            return
        stored = url
        detail = "updated"
    else:
        await message.answer("Нельзя изменить это поле.")
        return
    ctx.db.kv_set(key, stored)
    ctx.db.log_admin(message.from_user.id, "settings", target=key, detail=detail)
    await message.answer("✅ Сохранено.", reply_markup=ikb([btn("⬅️ К настройкам", "a:g:settings")]))


@router.callback_query(F.data == "a:set:contacts")
async def set_contacts(cb: CallbackQuery, ctx: Ctx) -> None:
    ch = _kv(ctx, "support_channel", ctx.cfg.channel_url) or "—"
    chat = _kv(ctx, "support_chat", ctx.cfg.chat_url) or "—"
    admin = _kv(ctx, "support_admin", ctx.cfg.admin_url) or "—"
    text = (
        "📞 <b>Контакты поддержки</b>\n\n"
        f"Канал: <code>{html.escape(_mask_url(ch))}</code>\n"
        f"Чат: <code>{html.escape(_mask_url(chat))}</code>\n"
        f"Админ: <code>{html.escape(_mask_url(admin))}</code>\n\n"
        "Эти ссылки показываются в разделе «Информация»."
    )
    kb = ikb(
        [btn("📢 Канал", "a:set:in:support_channel")],
        [btn("💬 Чат", "a:set:in:support_chat")],
        [btn("👑 Админ", "a:set:in:support_admin")],
        [btn("⬅️ Назад", "a:g:settings")],
    )
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()

@router.callback_query(F.data == "a:restartall")
async def restart_all_ask(cb: CallbackQuery, ctx: Ctx) -> None:
    n = len(ctx.db.bots_to_run())
    await cb.message.edit_text(
        f"🔄 Перезапустить все работающие боты ({n})?\n"
        "Они кратко отключатся и запустятся снова.",
        reply_markup=ikb(
            [btn("✅ Да, перезапустить", "a:restartall:yes")],
            [btn("⬅️ Отмена", "a:g:tools")],
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "a:restartall:yes")
async def restart_all_go(cb: CallbackQuery, ctx: Ctx) -> None:
    await cb.answer("Перезапускаю…")
    n = await ctx.sup.restart_all()
    ctx.db.log_admin(cb.from_user.id, "restart_all", detail=f"n={n}")
    await cb.message.edit_text(
        f"🔄 Перезапущено ботов: {n}",
        reply_markup=ikb([btn("⬅️ Назад", "a:g:tools")]),
    )
