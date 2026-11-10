"""💸 Withdrawals of earned money: requester wizard + single designated approver (manual payouts)."""
from __future__ import annotations

import html
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from creator import withdrawals as wd
from creator.keyboards import btn, ikb
from creator.money import rub
from creator.services import Ctx
from creator.states import Withdraw

router = Router(name="withdrawals")
log = logging.getLogger(__name__)
ERRORS = {
    "pending": "У вас уже есть заявка на вывод в обработке.",
    "minimum": "Сумма меньше минимальной.",
    "available": "Недостаточно заработанных средств для этой суммы.",
    "fee": "Вывод временно недоступен: некорректная комиссия. Сообщите администратору.",
    "method": "Этот способ вывода недоступен.",
}


def _private(event) -> bool:
    m = event.message if isinstance(event, CallbackQuery) else event
    return isinstance(m, Message) and m.chat.type == "private" and m.chat.id == event.from_user.id


def _approver(event, ctx: Ctx) -> bool:
    """Re-checked on every approver callback/message: the designated approver in their own private chat."""
    return ctx.cfg.approver is not None and event.from_user.id == ctx.cfg.approver and _private(event)


def _fee(ctx: Ctx) -> int:
    return ctx.price("WITHDRAW_FEE_PERCENT")


def _when(ts: int | None) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


def _methods(ctx: Ctx) -> list[str]:
    return [m for m in ctx.cfg.withdraw_methods if m in wd.METHOD_TITLES]


# ---------- requester ----------
@router.callback_query(F.data == "cab:wd")
async def start(cb: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    if not _private(cb):
        return await cb.answer()
    await state.clear()
    uid = cb.from_user.id
    a = wd.available(ctx.db, uid)
    minimum = ctx.price("WITHDRAW_MIN")
    lines = ["💸 <b>Вывод заработанного</b>\n",
             f"Доступно к выводу: <b>{rub(a.available)}</b>",
             f"<i>Заработано {rub(a.earned)} − потрачено {rub(a.spent)} − выведено {rub(a.withdrawn)}, "
             "не больше баланса. Пополнения и возвраты не выводятся; траты списываются сначала с заработанного.</i>\n",
             f"Минимум: от {rub(minimum)}. Комиссия {_fee(ctx)}% удерживается из суммы заявки. "
             "Выплату проводит администратор вручную."]
    rows = []
    pending = wd.pending_of(ctx.db, uid)
    if pending:
        lines.append(f"\n⏳ У вас уже есть заявка №{pending['id']} на {rub(pending['amount'])} — дождитесь решения.")
    elif a.available < minimum:
        lines.append("\nСейчас вывести нечего: доступная сумма меньше минимальной.")
    else:
        lines.append("\nВыберите способ получения:")
        rows = [[btn(wd.METHOD_TITLES[m], f"cab:wdm:{m}")] for m in _methods(ctx)]
    rows.append([btn("📄 Мои заявки на вывод", "cab:wdl:0")])
    rows.append([btn("⬅️ Назад", "cab:main")])
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("cab:wdm:"))
async def choose(cb: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    method = cb.data.split(":", 2)[2]
    if not _private(cb) or method not in _methods(ctx):
        return await cb.answer("Способ недоступен", show_alert=True)
    await state.set_state(Withdraw.details)
    await state.set_data({"method": method})
    await cb.message.edit_text(f"💸 {wd.METHOD_TITLES[method]}\n\n{wd.METHOD_PROMPTS[method]}\n\n"
                               "<i>Реквизиты увидит только администратор, проводящий выплату.</i>",
                               reply_markup=ikb([btn("❌ Отмена", "cab:wdno")]))
    await cb.answer()


@router.message(Withdraw.details)
async def got_details(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if not _private(message):
        return
    method = (await state.get_data()).get("method")
    details = wd.validate(method, message.text or "")
    if details is None:
        hint = {"card": "Проверьте номер карты (16–19 цифр, контрольная сумма Luhn)."}.get(
            method, "Неверный формат.")
        return await message.answer(f"{hint} {wd.METHOD_PROMPTS.get(method, '')}",
                                    reply_markup=ikb([btn("❌ Отмена", "cab:wdno")]))
    await state.update_data(details=details)
    await state.set_state(Withdraw.amount)
    a = wd.available(ctx.db, message.from_user.id)
    await message.answer(f"Введите сумму в рублях: от {rub(ctx.price('WITHDRAW_MIN'))} до {rub(a.available)}.",
                         reply_markup=ikb([btn("❌ Отмена", "cab:wdno")]))


@router.message(Withdraw.amount)
async def got_amount(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if not _private(message):
        return
    uid = message.from_user.id
    a = wd.available(ctx.db, uid)
    minimum = ctx.price("WITHDRAW_MIN")
    amount = wd.parse_amount(message.text or "")
    if amount is None or not minimum <= amount <= a.available:
        return await message.answer(f"Введите сумму от {rub(minimum)} до {rub(a.available)}.",
                                    reply_markup=ikb([btn("❌ Отмена", "cab:wdno")]))
    data = await state.get_data()
    fee, net = wd.fee_split(amount, _fee(ctx))
    await state.update_data(amount=amount)
    await state.set_state(Withdraw.confirm)
    await message.answer(
        "💸 <b>Проверьте заявку</b>\n\n"
        f"Способ: {wd.METHOD_TITLES[data['method']]}\n"
        f"Реквизиты: <code>{html.escape(data['details'])}</code>\n"
        f"Сумма: {rub(amount)}\nКомиссия ({_fee(ctx)}%): {rub(fee)}\nК получению: <b>{rub(net)}</b>\n\n"
        f"С баланса будет списано {rub(amount)}. При отклонении сумма вернётся на баланс.",
        reply_markup=ikb([btn("✅ Подтвердить", "cab:wdok"), btn("❌ Отмена", "cab:wdno")]))


@router.callback_query(F.data == "cab:wdno")
async def cancel(cb: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() in (Withdraw.details.state, Withdraw.amount.state, Withdraw.confirm.state):
        await state.clear()
    await cb.message.edit_text("Вывод отменён. Ничего не списано.", reply_markup=ikb([btn("⬅️ Кабинет", "cab:main")]))
    await cb.answer()


@router.callback_query(F.data == "cab:wdok")
async def confirm(cb: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    if not _private(cb) or await state.get_state() != Withdraw.confirm.state:
        return await cb.answer("Заявка устарела. Начните заново.", show_alert=True)
    data = await state.get_data()
    await state.clear()
    try:
        r = wd.create_request(ctx.db, cb.from_user.id, data["amount"], data["method"], data["details"],
                              fee_percent=_fee(ctx), minimum=ctx.price("WITHDRAW_MIN"))
    except wd.WithdrawError as e:
        await cb.message.edit_text(f"❌ {ERRORS.get(str(e), 'Не удалось создать заявку.')} Ничего не списано.",
                                   reply_markup=ikb([btn("⬅️ Кабинет", "cab:main")]))
        return await cb.answer()
    await cb.message.edit_text(
        f"✅ Заявка на вывод №{r['id']} создана: {rub(r['amount'])}, к получению {rub(r['net'])}.\n"
        "Сумма списана с баланса. Выплату проводит администратор вручную; статус — в «📄 Мои заявки на вывод».",
        reply_markup=ikb([btn("📄 Мои заявки на вывод", "cab:wdl:0")], [btn("⬅️ Кабинет", "cab:main")]))
    await cb.answer()
    approver = ctx.cfg.approver
    if approver is None:
        log.warning("withdrawal request %s created but WITHDRAW_APPROVER_ID is not configured", r["id"])
        return
    u = ctx.db.user(cb.from_user.id)
    try:
        await cb.bot.send_message(approver, request_text(r, u, full=True), reply_markup=approver_kb(r))
    except TelegramAPIError:
        log.warning("withdrawal request %s: approver notification failed", r["id"])


@router.callback_query(F.data.startswith("cab:wdl:"))
async def my_requests(cb: CallbackQuery, ctx: Ctx) -> None:
    if not _private(cb):
        return await cb.answer()
    try:
        number = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    rows, total, number = wd.page(ctx.db, number, uid=cb.from_user.id)
    lines = [f"📄 <b>Мои заявки на вывод</b> · всего {total}\n"]
    for r in rows:
        lines.append(f"№{r['id']} · {_when(r['created_at'])[:10]} · {rub(r['amount'])} → {rub(r['net'])} · "
                     f"{wd.METHOD_TITLES.get(r['method'], r['method'])} {wd.mask(r['details'])} · "
                     f"{wd.STATUS_TITLES.get(r['status'], r['status'])}"
                     + (f"\n   Причина: {html.escape(r['reason'])}" if r["reason"] else ""))
    if not rows:
        lines.append("Заявок пока нет.")
    lines.append("\n<i>«Отмечена выплаченной» — ручная отметка администратора.</i>")
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(*_pager("cab:wdl", number, total),
                                                                  [btn("⬅️ Кабинет", "cab:main")]))
    await cb.answer()


def _pager(prefix: str, number: int, total: int, size: int = 10) -> list[list]:
    nav = []
    if number > 0:
        nav.append(btn("⬅️", f"{prefix}:{number - 1}"))
    if (number + 1) * size < total:
        nav.append(btn("➡️", f"{prefix}:{number + 1}"))
    return [nav] if nav else []


# ---------- approver ----------
def request_text(r, u, *, full: bool) -> str:
    who = html.escape((u["full_name"] if u else None) or "—")
    details = r["details"] if full and r["status"] == "pending" else wd.mask(r["details"])
    text = (f"💸 <b>Заявка на вывод №{r['id']}</b> · {wd.STATUS_TITLES.get(r['status'], r['status'])}\n"
            f"От: {who} (<code>{r['user_id']}</code>)\nСоздана: {_when(r['created_at'])}\n"
            f"Сумма: {rub(r['amount'])}\nКомиссия: {rub(r['fee'])}\nК выплате: <b>{rub(r['net'])}</b>\n"
            f"Способ: {wd.METHOD_TITLES.get(r['method'], r['method'])}\nРеквизиты: <code>{html.escape(details)}</code>")
    if r["status"] != "pending":
        text += f"\nРешение: {_when(r['resolved_at'])}"
        if r["reason"]:
            text += f"\nПричина: {html.escape(r['reason'])}"
    return text


def approver_kb(r, back: bool = False):
    rows = []
    if r["status"] == "pending":
        rows.append([btn("✅ Отметить выплаченной", f"wdr:ok:{r['id']}"), btn("❌ Отклонить", f"wdr:no:{r['id']}")])
    if back:
        rows.append([btn("⬅️ Заявки", "a:wd:0")])
    return ikb(*rows) if rows else None


@router.callback_query(F.data.startswith("a:wd:"))
async def admin_list(cb: CallbackQuery, ctx: Ctx) -> None:
    if not _approver(cb, ctx):
        return await cb.answer("Нет доступа", show_alert=True)
    try:
        number = int(cb.data.split(":")[2])
    except ValueError:
        return await cb.answer()
    rows, total, number = wd.page(ctx.db, number)
    pending = ctx.db.scalar("SELECT COUNT(*) FROM withdrawal_requests WHERE status='pending'") or 0
    lines = [f"💸 <b>Заявки на вывод</b> · всего {total}, ожидают {pending}\n"]
    for r in rows:
        lines.append(f"№{r['id']} · {_when(r['created_at'])[:10]} · {rub(r['net'])} · "
                     f"{wd.METHOD_TITLES.get(r['method'], r['method'])} {wd.mask(r['details'])} · "
                     f"{wd.STATUS_TITLES.get(r['status'], r['status'])}")
    if not rows:
        lines.append("Заявок нет.")
    numbers = [btn(f"№{r['id']}", f"wdr:d:{r['id']}") for r in rows]
    await cb.message.edit_text("\n".join(lines), reply_markup=ikb(
        *[numbers[i:i + 5] for i in range(0, len(numbers), 5)], *_pager("a:wd", number, total),
        [btn("⬅️ Панель", "a:main")]))
    await cb.answer()


def _rid(data: str) -> int | None:
    try:
        rid = int(data.split(":")[2])
    except (IndexError, ValueError):
        return None
    return rid if rid > 0 else None


@router.callback_query(F.data.startswith("wdr:"))
async def approver_action(cb: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    if not _approver(cb, ctx):
        return await cb.answer("Нет доступа", show_alert=True)
    action, rid = cb.data.split(":")[1], _rid(cb.data)
    r = wd.get(ctx.db, rid) if rid else None
    if r is None:
        return await cb.answer("Заявка не найдена", show_alert=True)
    if action == "d":
        await cb.message.edit_text(request_text(r, ctx.db.user(r["user_id"]), full=True),
                                   reply_markup=approver_kb(r, back=True))
        return await cb.answer()
    if r["status"] != "pending":
        return await cb.answer("Заявка уже обработана", show_alert=True)
    if action == "ok":
        await _finish(cb.bot, cb.message, ctx, rid, approve=True, by=cb.from_user.id)
        return await cb.answer("Отмечена выплаченной")
    if action == "no":
        await state.set_state(Withdraw.reason)
        await state.set_data({"rid": rid})
        await cb.message.answer(f"Причина отклонения заявки №{rid} (необязательно) — отправьте текстом:",
                                reply_markup=ikb([btn("Без причины", f"wdr:nr:{rid}")]))
        return await cb.answer()
    if action == "nr":
        await state.clear()
        await _finish(cb.bot, cb.message, ctx, rid, approve=False, by=cb.from_user.id)
        return await cb.answer("Отклонена")
    await cb.answer()


@router.message(Withdraw.reason)
async def got_reason(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if not _approver(message, ctx):
        return
    rid = (await state.get_data()).get("rid")
    await state.clear()
    if rid:
        await _finish(message.bot, message, ctx, rid, approve=False, by=message.from_user.id,
                      reason=(message.text or "").strip())


async def _finish(bot, where: Message, ctx: Ctx, rid: int, *, approve: bool, by: int, reason: str | None = None):
    r = wd.resolve(ctx.db, rid, approve=approve, by=by, reason=reason)
    if r is None:
        await where.answer(f"Заявка №{rid} уже обработана.")
        return
    await where.answer(request_text(r, ctx.db.user(r["user_id"]), full=False))
    if approve:
        text = (f"✅ Ваша заявка на вывод №{r['id']} отмечена выплаченной администратором: "
                f"к получению {rub(r['net'])} ({wd.METHOD_TITLES.get(r['method'], r['method'])} {r['details']}). "
                "Если средства не пришли — «Информация» → «Оповестить о проблеме».")
    else:
        text = (f"❌ Заявка на вывод №{r['id']} отклонена. {rub(r['amount'])} возвращены на баланс."
                + (f"\nПричина: {html.escape(r['reason'])}" if r["reason"] else ""))
    try:
        await bot.send_message(r["user_id"], text)
    except TelegramAPIError:
        log.warning("withdrawal request %s: requester notification failed", r["id"])
