"""💸 Доп заработок: подключение дочерних ботов к PiarFlow (Factory API, продажа трафика)."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from creator.db import day_start
from creator.handlers.access import private_admin
from creator.keyboards import btn, ikb
from creator.money import rub
from creator.piarflow import PiarFlowError
from creator.services import Ctx

log = logging.getLogger("piarflow.ui")

router = Router(name="piarflow_admin")

STATUS = {"pending": "⏳ заявка на рассмотрении", "approved": "✅ подключён", "rejected": "❌ заявка отклонена",
          "disabled": "⛔ отключён администратором"}
MODE = {"relay": "📡 Трансляция спонсоров креатора", "factory": "🏭 Фабрика (свой бот в сети)"}


class IsAdminAndEnabled(Filter):
    async def __call__(self, event, ctx: Ctx) -> bool:
        return ctx.piarflow is not None and private_admin(event, ctx)


router.callback_query.filter(IsAdminAndEnabled())


# ---------- владелец бота ----------
def hub_screen(ctx: Ctx, b):
    from creator.earnings_status import hub_screen as render
    return render(ctx, b)


def owner_screen(ctx: Ctx, b):
    from creator.earnings_status import owner_screen as render
    return render(ctx, b, "pf")


def app_text(ctx: Ctx, t) -> str:
    owner = ctx.db.user(t["owner_id"])
    who = (f"@{html.escape(owner['username'])}" if owner and owner["username"]
           else html.escape(owner["full_name"] or "") if owner else "")
    users = ctx.piarflow.users(t["bot_id"])
    text = (f"🚀 <b>Заявка PiarFlow</b>: бот ID {t['bot_id']} @{html.escape(t['username'] or '—')}\n"
            f"Владелец: <code>{t['owner_id']}</code> {who}\n"
            f"Пользователей: {users} (на момент заявки {t['users_at_apply']})")
    if t["error"]:
        text += "\n⚠️ Записана ошибка подключения; причина не проверена."
    return text


def app_kb(bot_id: int):
    return ikb([btn("✅ Одобрить", f"pf:ok:{bot_id}"), btn("❌ Отклонить", f"pf:no:{bot_id}")])


async def owner_apply(cb: CallbackQuery, ctx: Ctx, b, mode: str | None = None) -> None:
    """Подача заявки. mode из старых кнопок pfmod игнорируется — всегда factory-first на approve."""
    if mode is not None and mode not in ("relay", "factory"):
        await cb.answer("Устаревшая кнопка. Откройте «💸 Доп заработок» снова.", show_alert=True)
        return
    svc = ctx.piarflow
    users, need = svc.users(b["id"]), svc.min_users()
    if users < need:
        await cb.answer(f"Нужно минимум {need} пользователей в боте, сейчас {users}.", show_alert=True)
        return
    if not ctx.db.pf_apply(b["id"], cb.from_user.id, users, mode="factory"):
        await cb.answer("Заявка уже подана или бот уже подключён.", show_alert=True)
        return
    await cb.answer("✅ Заявка отправлена администратору", show_alert=True)
    t = next((x for x in ctx.db.pf_list("pending") if x["bot_id"] == b["id"]), None)
    if t is not None:
        for admin in ctx.cfg.admin_ids:
            try:
                await cb.bot.send_message(admin, app_text(ctx, t), reply_markup=app_kb(b["id"]))
            except Exception as e:  # noqa: BLE001
                log.info("notify admin %s failed: %s", admin, e)
    text, kb = owner_screen(ctx, b)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ---------- админ ----------
async def _edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("pf:"))
async def decide(cb: CallbackQuery, ctx: Ctx) -> None:
    _, action, raw = cb.data.split(":")
    bot_id = int(raw)
    t = ctx.db.pf(bot_id)
    b = ctx.db.bot(bot_id)
    if not t or not b:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if action == "ok":
        if t["status"] not in ("pending", "disabled"):
            await cb.answer(f"Уже обработано: {STATUS.get(t['status'], t['status'])}", show_alert=True)
            return
        try:
            await ctx.piarflow.approve(bot_id, cb.from_user.id)
        except (PiarFlowError, LookupError):
            await cb.answer("PiarFlow: операция не выполнена; проверьте источник и заявку", show_alert=True)
            return
        reward = rub(ctx.piarflow.reward())
        t2 = ctx.db.pf(bot_id)
        mode = (t2["mode"] if t2 and "mode" in t2.keys() and t2["mode"] else "factory")
        note = ("Трансляция спонсоров креатора (хаб)." if mode == "relay"
                else "Если PiarFlow отправит бота на модерацию, задания появятся после её прохождения.")
        extra = ""
        if t2 and t2["error"] and str(t2["error"]).startswith("factory→relay"):
            extra = "\n⚠️ Фабрика не удалась — записано подключение через источник креатора."
        await _edit(cb, f"✅ Бот ID {bot_id} @{html.escape(b['username'] or '')} подключён к PiarFlow "
                        f"[{MODE.get(mode, mode)}].\n{note}{extra}",
                    ikb([btn("🚀 PiarFlow", "a:pf")]))
        try:
            await cb.bot.send_message(b["owner_id"], f"✅ Бот ID {bot_id} подключён к PiarFlow! "
                                                     f"За каждое засчитанное задание вы получаете {reward} "
                                                     "на баланс конструктора. Статистика — в карточке бота → "
                                                     "«💸 Доп заработок».")
        except Exception:  # noqa: BLE001
            pass
    elif action == "no":
        if not await ctx.piarflow.reject(bot_id, cb.from_user.id):
            await cb.answer(f"Уже обработано: {STATUS.get(t['status'], t['status'])}", show_alert=True)
            return
        await _edit(cb, f"❌ Заявка бота ID {bot_id} в PiarFlow отклонена.", ikb([btn("🚀 PiarFlow", "a:pf")]))
        try:
            await cb.bot.send_message(b["owner_id"], f"❌ Заявка на подключение бота ID {bot_id} к PiarFlow отклонена.")
        except Exception:  # noqa: BLE001
            pass
    elif action == "off":
        if not await ctx.piarflow.disable(bot_id, cb.from_user.id):
            await cb.answer("Бот не подключён", show_alert=True)
            return
        await cb.answer("⛔ Задания PiarFlow в боте отключены")
        await bot_card(cb, ctx, bot_id)
        try:
            await cb.bot.send_message(b["owner_id"], f"⛔ Администратор отключил PiarFlow в боте ID {bot_id}.")
        except Exception:  # noqa: BLE001
            pass
        return
    await cb.answer()


@router.callback_query(F.data == "a:pf")
async def pf_main(cb: CallbackQuery, ctx: Ctx) -> None:
    pending = ctx.db.pf_list("pending")
    approved = ctx.db.pf_list("approved")
    disabled = ctx.db.pf_list("disabled")
    ops = ctx.db.scalar("SELECT COUNT(*) FROM pf_ops") or 0
    paid = ctx.db.scalar("SELECT COALESCE(SUM(reward_kop),0) FROM pf_ops") or 0
    text = ("🚀 <b>PiarFlow — Доп заработок</b>\n\n"
            f"⏳ Заявок: {len(pending)}\n✅ Подключено ботов: {len(approved)}\n⛔ Отключено: {len(disabled)}\n"
            f"📊 Заданий всего: {ops}, выплачено владельцам: {rub(paid)}\n\n"
            f"Награда за задание: {rub(ctx.piarflow.reward())}, минимум пользователей: "
            f"{ctx.piarflow.min_users()} (меняется в «💰 Цены»)")
    try:
        bal = await ctx.piarflow.client.balance()
        b = bal.get("balance") or {}
        tot = bal.get("totals") or {}
        text += (f"\n\n💼 Аккаунт PiarFlow: заработано {b.get('earned', '—')} ₽, к выводу "
                 f"{b.get('available_to_withdraw', '—')} ₽, за всё время {tot.get('earned_all_time', '—')} ₽")
    except PiarFlowError as e:
        text += f"\n\n⚠️ PiarFlow: не удалось получить баланс ({html.escape(e.message[:100])})"
    await _edit(cb, text, ikb([btn(f"⏳ Заявки ({len(pending)})", "a:pfl:pending")],
                              [btn(f"✅ Подключённые ({len(approved)})", "a:pfl:approved")],
                              [btn(f"⛔ Отключённые ({len(disabled)})", "a:pfl:disabled")] if disabled else [],
                              [btn("⬅️ Назад", "a:main")]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:pfl:"))
async def pf_list(cb: CallbackQuery, ctx: Ctx) -> None:
    status = cb.data.split(":")[2]
    rows = ctx.db.pf_list(status)
    if status == "pending":
        if not rows:
            await cb.answer("Заявок нет", show_alert=True)
            return
        await cb.answer()
        for t in rows[:20]:
            await cb.message.answer(app_text(ctx, t), reply_markup=app_kb(t["bot_id"]))
        return
    kb = [[btn(f"ID {t['bot_id']} @{t['username'] or '—'} | заданий {ctx.db.pf_stats(t['bot_id'])[0]}",
               f"a:pfb:{t['bot_id']}")] for t in rows[:40]]
    kb.append([btn("⬅️ Назад", "a:pf")])
    await _edit(cb, f"{STATUS.get(status, status).capitalize()}: {len(rows)}", ikb(*kb))
    await cb.answer()


async def bot_card(cb: CallbackQuery, ctx: Ctx, bot_id: int) -> None:
    t = ctx.db.pf(bot_id)
    b = ctx.db.bot(bot_id)
    if not t or not b:
        await cb.answer("Не найден", show_alert=True)
        return
    total, earned = ctx.db.pf_stats(bot_id)
    today, _ = ctx.db.pf_stats(bot_id, day_start())
    mode = t["mode"] if "mode" in t.keys() and t["mode"] else "factory"
    lines = [f"🚀 <b>PiarFlow: бот ID {bot_id}</b> @{html.escape(b['username'] or '—')}",
             f"Владелец: <code>{b['owner_id']}</code>", f"Статус: {STATUS.get(t['status'], t['status'])}",
             f"Эффект. режим: {MODE.get(mode, mode)}",
             f"Пользователей: {ctx.piarflow.users(bot_id)}",
             f"Заданий: {total} (сегодня {today}), выплачено владельцу: {rub(earned)}"]
    if mode == "factory" and t["pf_bot_id"] and t["status"] == "approved":
        try:
            info = await ctx.piarflow.client.bot(t["pf_bot_id"])
            st = await ctx.piarflow.client.stats(t["pf_bot_id"])
            lines.append(f"PiarFlow: {'активен' if info.get('is_active') else 'не активен (модерация/выключен)'}, "
                         f"продано подписок {info.get('sold_subs', 0)}, не оплачено {info.get('not_counted', 0)}, "
                         f"заработок {info.get('earned', '0')} ₽; сегодня (МСК) {st.get('sold_subs', 0)} подписок, "
                         f"{st.get('earned', '0')} ₽")
        except PiarFlowError as e:
            lines.append(f"PiarFlow: не удалось получить статистику ({html.escape(e.message[:100])})")
    if t["error"]:
        lines.append("⚠️ Записана ошибка подключения; причина не проверена.")
    kb = []
    if t["status"] == "approved":
        kb.append([btn("⛔ Отключить задания в боте", f"pf:off:{bot_id}")])
    elif t["status"] in ("disabled", "pending"):
        kb.append([btn("✅ Включить" if t["status"] == "disabled" else "✅ Одобрить", f"pf:ok:{bot_id}")])
    kb.append([btn("⬅️ Назад", "a:pf")])
    await _edit(cb, "\n".join(lines), ikb(*kb))


@router.callback_query(F.data.startswith("a:pfb:"))
async def pf_bot(cb: CallbackQuery, ctx: Ctx) -> None:
    await bot_card(cb, ctx, int(cb.data.split(":")[2]))
    await cb.answer()
