"""Constructor UI: owner connects a LavePay shop and confirms USDT TRC-20 payouts."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from child.db import fmt
from child.payouts import loads_destination, public_details
from creator.keyboards import btn, cancel_kb, ikb
from creator.lavepay import LavePayError
from creator.owner_payouts import child_flags, disconnect_shop, look_key, refresh_shop, save_shop, set_rate
from creator.services import Ctx
from creator.states import OwnerPayout

router = Router(name="owner_payouts")
log = logging.getLogger("owner_payouts.ui")

TEMPLATES = frozenset({"clicker", "refbot"})


def _owned(ctx: Ctx, uid: int, bot_id: int):
    b = ctx.db.bot(bot_id)
    if not b or b["deleted"] or b["owner_id"] != uid or b["template"] not in TEMPLATES:
        return None
    return b


def shop_text(ctx: Ctx, b) -> tuple[str, object]:
    shop = ctx.db.owner_lavepay_public(b["id"])
    flags = child_flags(ctx.cfg, ctx.db, b["id"])
    lines = [
        f"💳 <b>LavePay выплаты</b> для бота ID {b['id']} (@{html.escape(b['username'] or '—')})",
        "",
        "Пользователи кликера и реф-бота могут выводить USDT в сети TRC-20. "
        "Деньги списываются с вашего магазина LavePay и только после вашего подтверждения курса.",
    ]
    if shop:
        lines += [
            "",
            f"Магазин: {html.escape(str(shop.get('merchant_name') or '—'))}"
            + (f" (№{shop['merchant_id']})" if shop.get("merchant_id") else ""),
            f"Статус: {html.escape(str(shop.get('status') or '—'))}",
            f"Выплаты по API: {'включены' if shop.get('payouts_enabled') else 'выключены — включите в приложении Laveshka'}",
            f"Баланс USDT: {html.escape(str(shop.get('usdt_balance') or '0'))}",
        ]
        if not shop.get("payouts_enabled") or shop.get("status") != "active":
            lines.append("Пока выплаты в магазине не включены, заявки идут как раньше — вручную.")
    else:
        lines += ["", "Магазин ещё не подключён. Заявки на вывод пока обрабатываются вручную."]
    kind, rate = flags.get("lavepay_unit_kind"), flags.get("lavepay_unit_rate")
    if kind == "rub" and rate == "1":
        lines.append("Сумма в рублях будет пересчитана в USDT по курсу LavePay в момент подтверждения.")
    elif kind and rate:
        unit = "USDT" if kind == "usdt" else "₽"
        lines.append(f"Курс бота: 1 единица баланса = {html.escape(str(rate))} {unit}.")
    elif b["template"] == "clicker":
        lines.append(
            "Для кликера задайте, чему равна 1 единица баланса: в USDT или в рублях. "
            "Без этого LavePay не включится — чтобы монеты случайно не стали рублями."
        )
    if flags.get("lavepay_payouts_enabled"):
        lines.append("Путь LavePay для этого бота включён.")
    else:
        lines.append("Путь LavePay для этого бота пока выключен.")

    rows = []
    if shop:
        rows.append([btn("🔄 Обновить баланс", f"olp:{b['id']}:refresh")])
        if b["template"] == "clicker":
            rows.append([btn("📐 Курс монеты", f"olp:{b['id']}:rate")])
        rows.append([btn("🔑 Заменить ключ", f"olp:{b['id']}:key"),
                     btn("🗑 Отключить", f"olp:{b['id']}:off")])
    else:
        rows.append([btn("🔑 Подключить магазин", f"olp:{b['id']}:key")])
        if b["template"] == "clicker":
            rows.append([btn("📐 Курс монеты", f"olp:{b['id']}:rate")])
    pending = _pending_jobs(ctx, b["id"])
    if pending:
        lines.append(f"\nОжидают подтверждения: {len(pending)}")
        for item in pending[:8]:
            rows.append([btn(f"Заявка №{item['id']} · {item['label']}", f"olp:{b['id']}:w:{item['id']}")])
    rows.append([btn("⬅️ К боту", f"b:{b['id']}:open")])
    return "\n".join(lines), ikb(*rows)


def _pending_jobs(ctx: Ctx, bot_id: int) -> list[dict]:
    payouts = _payouts(ctx)
    if payouts is None:
        return []
    path = ctx.cfg.bots_dir / str(bot_id) / "bot.db"
    if not path.exists():
        return []
    try:
        db = payouts._db(bot_id)
        rows = db.all(
            "SELECT w.id, w.amount, w.currency_at_request, j.status FROM withdrawals w "
            "JOIN payout_jobs j ON j.withdrawal_id=w.id "
            "WHERE w.status='pending' AND j.status IN ('awaiting_approval','queued') "
            "ORDER BY w.id"
        )
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        out.append({"id": r["id"], "label": f"{fmt(r['amount'])} {r['currency_at_request'] or ''}".strip()})
    return out


def _payouts(ctx: Ctx):
    return getattr(ctx, "payouts", None) or getattr(getattr(ctx, "lave", None), "payouts", None)


async def show_shop(target, ctx: Ctx, b) -> None:
    text, kb = shop_text(ctx, b)
    from creator.handlers.mybots import show
    await show(target, text, kb)


async def open_from_start(message: Message, ctx: Ctx, bot_id: int, wid: int) -> None:
    b = _owned(ctx, message.from_user.id, bot_id)
    if not b:
        await message.answer("Бот не найден или выплаты LavePay для него недоступны.")
        return
    await show_withdrawal(message, ctx, b, wid, as_message=True)


async def show_withdrawal(target, ctx: Ctx, b, wid: int, *, as_message: bool = False) -> None:
    payouts = _payouts(ctx)
    if payouts is None:
        await _answer(target, "Сервис выплат сейчас недоступен.")
        return
    try:
        db = payouts._db(b["id"])
        w = db.withdrawal(wid)
        job = db.payout_job(wid)
    except Exception:  # noqa: BLE001
        await _answer(target, "Не удалось открыть заявку.")
        return
    if not w:
        await _answer(target, "Заявка не найдена.")
        return
    dest = loads_destination(job["destination"]) if job else {}
    details = public_details(job["method"], dest) if job else (w["details"] or "")
    lines = [
        f"💸 <b>Заявка №{wid}</b> бота ID {b['id']}",
        f"Сумма в боте: {fmt(w['amount'])} {html.escape(str(w['currency_at_request'] or ''))}",
        f"Адрес: {html.escape(details)}",
        f"Статус заявки: {html.escape(w['status'])}",
    ]
    if job:
        lines.append(f"Задача выплаты: {html.escape(job['status'])}")
        if job["error"] and job["status"] != "completed":
            lines.append(f"Последняя ошибка: {html.escape(str(job['error'])[:300])}")
    kb_rows = []
    if w["status"] == "pending" and job and job["status"] in ("awaiting_approval", "queued"):
        try:
            quote = await payouts.quote_withdrawal(b["id"], wid)
        except ValueError:
            lines.append("\nНельзя получить курс: задайте единицу баланса и подключите магазин.")
        except LavePayError as exc:
            lines.append(f"\nНе удалось получить курс: {html.escape(exc.message)}")
        else:
            if quote.get("rate"):
                lines.append(f"Курс LavePay: {html.escape(str(quote['rate']))} ₽ за 1 USDT")
            if quote.get("amount_rub"):
                lines.append(f"К выплате из заявки: {html.escape(str(quote['amount_rub']))} ₽")
            lines.append(f"Получатель получит: <b>{html.escape(str(quote['amount']))} USDT</b> (TRC-20)")
            lines.append(f"Комиссия сети: {html.escape(str(quote['fee']))} USDT (спишется с вашего магазина сверху)")
            lines.append(f"Итого с магазина: {html.escape(str(quote['debit']))} USDT")
            if quote.get("balance"):
                lines.append(f"Баланс магазина: {html.escape(str(quote['balance']))} USDT")
            if not quote.get("enough", True):
                lines.append("USDT на магазине не хватает. Пополните баланс и откройте заявку снова.")
            else:
                kb_rows.append([btn("✅ Подтвердить и отправить", f"olp:{b['id']}:send:{wid}")])
        kb_rows.append([btn("❌ Отклонить", f"olp:{b['id']}:rej:{wid}")])
    kb_rows.append([btn("⬅️ К выплатам", f"olp:{b['id']}")])
    text = "\n".join(lines)
    kb = ikb(*kb_rows)
    if as_message or isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        from creator.handlers.mybots import show
        await show(target, text, kb)


async def _answer(target, text: str) -> None:
    if isinstance(target, Message):
        await target.answer(text)
    else:
        await target.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("olp:"))
async def owner_lavepay(cb: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if not isinstance(cb.message, Message) or cb.message.chat.type != "private":
        await cb.answer()
        return
    parts = (cb.data or "").split(":")
    try:
        bot_id = int(parts[1])
    except (ValueError, IndexError):
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    b = _owned(ctx, cb.from_user.id, bot_id)
    if not b:
        await cb.answer("Бот не найден", show_alert=True)
        return
    action = parts[2] if len(parts) > 2 else ""
    if action == "":
        await state.clear()
        await show_shop(cb, ctx, b)
        await cb.answer()
        return
    if action == "refresh":
        try:
            public = await refresh_shop(ctx.cfg, ctx.db, bot_id)
        except LavePayError as exc:
            await cb.answer(exc.message[:180], show_alert=True)
            return
        if not public:
            await cb.answer("Магазин не подключён", show_alert=True)
            return
        await show_shop(cb, ctx, b)
        await cb.answer("Обновлено")
        return
    if action == "key":
        await state.set_state(OwnerPayout.api_key)
        await state.update_data(olp_bot_id=bot_id)
        await cb.message.answer(
            "Пришлите API-ключ магазина Lave Pay одним сообщением. "
            "Сообщение будет удалено, ключ в чат не вернётся.\n"
            "Ключ выпускается в мини-приложении Laveshka: Сервисы → Для бизнеса → Lave Pay.\n"
            "Включите там «Выплаты по API». /cancel — отмена.",
            reply_markup=cancel_kb(f"olp:{bot_id}"),
        )
        await cb.answer()
        return
    if action == "off":
        disconnect_shop(ctx.cfg, ctx.db, bot_id)
        await show_shop(cb, ctx, b)
        await cb.answer("Магазин отключён, ключ удалён")
        return
    if action == "rate":
        await state.set_state(OwnerPayout.coin_rate)
        await state.update_data(olp_bot_id=bot_id)
        await cb.message.answer(
            "Задайте курс кликера одним из двух способов:\n"
            "• <code>usdt 0.001</code> — 1 единица баланса = 0.001 USDT\n"
            "• <code>rub 1</code> — 1 единица = 1 ₽, в USDT пересчитаем по курсу LavePay при подтверждении\n"
            "/cancel — отмена.",
            reply_markup=cancel_kb(f"olp:{bot_id}"),
        )
        await cb.answer()
        return
    if action == "w" and len(parts) >= 4:
        try:
            wid = int(parts[3])
        except ValueError:
            await cb.answer("Кнопка устарела", show_alert=True)
            return
        await show_withdrawal(cb, ctx, b, wid)
        await cb.answer()
        return
    if action == "rej" and len(parts) >= 4:
        await _reject(cb, ctx, b, int(parts[3]))
        return
    if action == "send" and len(parts) >= 4:
        await _send(cb, ctx, b, int(parts[3]))
        return
    await cb.answer()


async def _reject(cb: CallbackQuery, ctx: Ctx, b, wid: int) -> None:
    payouts = _payouts(ctx)
    if payouts is None:
        await cb.answer("Сервис недоступен", show_alert=True)
        return
    db = payouts._db(b["id"])
    w = db.cancel_payout_and_reject(wid) or db.resolve_withdrawal(wid, False)
    if not w:
        await cb.answer("Заявку уже нельзя отклонить", show_alert=True)
        await show_withdrawal(cb, ctx, b, wid)
        return
    if payouts.notify:
        try:
            await payouts.notify(
                w["user_id"],
                f"❌ Заявка на вывод №{wid} отклонена владельцем, средства возвращены на баланс.",
            )
        except Exception:  # noqa: BLE001
            pass
    await show_shop(cb, ctx, b)
    await cb.answer("Заявка отклонена")


async def _send(cb: CallbackQuery, ctx: Ctx, b, wid: int) -> None:
    payouts = _payouts(ctx)
    if payouts is None:
        await cb.answer("Сервис недоступен", show_alert=True)
        return
    try:
        quote = await payouts.quote_withdrawal(b["id"], wid)
    except (ValueError, LavePayError) as exc:
        message = exc.message if isinstance(exc, LavePayError) else "Нельзя получить курс"
        await cb.answer(message[:180], show_alert=True)
        await show_withdrawal(cb, ctx, b, wid)
        return
    if not quote.get("enough", True):
        await cb.answer("На магазине не хватает USDT. Заявка остаётся в ожидании.", show_alert=True)
        await show_withdrawal(cb, ctx, b, wid)
        return
    try:
        status = await payouts.confirm_and_queue(b["id"], wid, quote)
    except (ValueError, LavePayError) as exc:
        message = exc.message if isinstance(exc, LavePayError) else "Не удалось отправить"
        await cb.answer(message[:180], show_alert=True)
        await show_withdrawal(cb, ctx, b, wid)
        return
    labels = {
        "submitted": "Выплата отправлена в сеть, ждём подтверждения.",
        "completed": "Выплата уже прошла.",
        "queued": "Заявка в очереди. Если на магазине не хватит USDT, она останется ждать.",
        "failed": "Выплата не прошла, средства возвращены пользователю.",
        "pending": "Заявка осталась в ожидании.",
    }
    await show_withdrawal(cb, ctx, b, wid)
    await cb.answer(labels.get(status, status)[:180], show_alert=True)


@router.message(OwnerPayout.api_key, F.text)
async def take_key(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if message.from_user is not None and message.chat.type == "private" and message.chat.id == message.from_user.id:
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    data = await state.get_data()
    bot_id = data.get("olp_bot_id")
    b = _owned(ctx, message.from_user.id, int(bot_id or 0)) if bot_id else None
    if not b:
        await state.clear()
        await message.answer("Бот не найден. Ключ не сохранён.")
        return
    try:
        key = look_key(message.text)
    except ValueError:
        await message.answer(
            "Это не похоже на ключ Lave Pay (ожидается вид LAVE-…). Пришлите ключ ещё раз или /cancel.",
        )
        return
    try:
        public = await save_shop(ctx.cfg, ctx.db, b["id"], message.from_user.id, key)
    except LavePayError as exc:
        await message.answer(f"Ключ не принят: {html.escape(exc.message)}. Он не сохранён.")
        return
    except Exception:  # noqa: BLE001
        log.exception("save owner lavepay shop failed")
        await message.answer("Не удалось сохранить магазин. Ключ в чат не возвращается, попробуйте позже.")
        return
    await state.clear()
    name = html.escape(str(public.get("merchant_name") or "магазин"))
    status = html.escape(str(public.get("status") or "—"))
    payouts = "включены" if public.get("payouts_enabled") else "выключены"
    await message.answer(
        f"✅ Подключён магазин «{name}», статус {status}, выплаты по API {payouts}. "
        f"Баланс USDT: {html.escape(str(public.get('usdt_balance') or '0'))}."
    )
    await show_shop(message, ctx, b)


@router.message(OwnerPayout.coin_rate, F.text)
async def take_rate(message: Message, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    bot_id = data.get("olp_bot_id")
    b = _owned(ctx, message.from_user.id, int(bot_id or 0)) if bot_id else None
    if not b:
        await state.clear()
        await message.answer("Бот не найден.")
        return
    raw = (message.text or "").strip().lower().replace(",", ".")
    parts = raw.split()
    if len(parts) != 2 or parts[0] not in {"usdt", "rub"}:
        await message.answer("Формат: <code>usdt 0.001</code> или <code>rub 1</code>.")
        return
    # Rate can be saved even before a shop is connected; create a placeholder? No, need a shop row.
    # If no shop, tell owner to connect first, but keep the rate input if shop exists.
    if not ctx.db.owner_lavepay_shop(b["id"]):
        await message.answer("Сначала подключите магазин LavePay, затем задайте курс.")
        return
    try:
        ok = set_rate(ctx.cfg, ctx.db, b["id"], message.from_user.id, parts[0], parts[1])
    except ValueError:
        await message.answer("Некорректное число курса.")
        return
    if not ok:
        await message.answer("Не удалось сохранить курс.")
        return
    await state.clear()
    await message.answer("Курс сохранён.")
    await show_shop(message, ctx, b)
