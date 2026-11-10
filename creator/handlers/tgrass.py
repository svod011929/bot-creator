"""💸 Доп заработок: подключение дочерних ботов к TGrass (режим фабрики ботов)."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from creator.db import day_start
from creator.handlers.access import private_admin
from creator.keyboards import btn, ikb, url_btn
from creator.money import rub
from creator.services import Ctx
from creator.tgrass import TGrassError

log = logging.getLogger("tgrass.ui")

router = Router(name="tgrass_admin")

STATUS = {"pending": "⏳ заявка на рассмотрении", "approved": "✅ подключён", "rejected": "❌ заявка отклонена",
          "disabled": "⛔ отключён администратором"}
MODE = {"relay": "📡 Трансляция спонсоров креатора", "factory": "🏭 Фабрика (свой бот в сети)"}


class IsAdminAndEnabled(Filter):
    async def __call__(self, event, ctx: Ctx) -> bool:
        return ctx.tgrass is not None and private_admin(event, ctx)


router.callback_query.filter(IsAdminAndEnabled())


def admin_contact_btn(ctx: Ctx):
    if ctx.cfg.admin_url:
        return url_btn("💬 Связаться с админом", ctx.cfg.admin_url)
    return btn("💬 Связаться с админом", "info:admin")


# ---------- владелец бота ----------
def owner_screen(ctx: Ctx, b):
    from creator.earnings_status import owner_screen as render
    return render(ctx, b, "tgr")


def app_text(ctx: Ctx, t) -> str:
    owner = ctx.db.user(t["owner_id"])
    who = (f"@{html.escape(owner['username'])}" if owner and owner["username"]
           else html.escape(owner["full_name"] or "") if owner else "")
    users = ctx.tgrass.users(t["bot_id"])
    text = (f"💸 <b>Заявка TGrass</b>: бот ID {t['bot_id']} @{html.escape(t['username'] or '—')}\n"
            f"Владелец: <code>{t['owner_id']}</code> {who}\n"
            f"Пользователей: {users} (на момент заявки {t['users_at_apply']})")
    if t["error"]:
        text += "\n⚠️ Записана ошибка подключения; причина не проверена."
    return text


def app_kb(bot_id: int):
    return ikb([btn("✅ Одобрить", f"tg:ok:{bot_id}"), btn("❌ Отклонить", f"tg:no:{bot_id}")])


async def owner_apply(cb: CallbackQuery, ctx: Ctx, b, mode: str | None = None) -> None:
    """Подача заявки. mode из старых кнопок tgmod игнорируется — всегда factory-first на approve."""
    if mode is not None and mode not in ("relay", "factory"):
        await cb.answer("Устаревшая кнопка. Откройте «💸 Доп заработок» снова.", show_alert=True)
        return
    svc = ctx.tgrass
    users, need = svc.users(b["id"]), svc.min_users()
    if users < need:
        await cb.answer(f"Нужно минимум {need} пользователей в боте, сейчас {users}.", show_alert=True)
        return
    if not ctx.db.tgrass_apply(b["id"], cb.from_user.id, users, mode="factory"):
        await cb.answer("Заявка уже подана или бот уже подключён.", show_alert=True)
        return
    await cb.answer("✅ Заявка отправлена администратору", show_alert=True)
    t = next((x for x in ctx.db.tgrass_list("pending") if x["bot_id"] == b["id"]), None)
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


@router.callback_query(F.data.startswith("tg:"))
async def decide(cb: CallbackQuery, ctx: Ctx) -> None:
    _, action, raw = cb.data.split(":")
    bot_id = int(raw)
    t = ctx.db.tgrass(bot_id)
    b = ctx.db.bot(bot_id)
    if not t or not b:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if action == "ok":
        if t["status"] not in ("pending", "disabled"):
            await cb.answer(f"Уже обработано: {STATUS.get(t['status'], t['status'])}", show_alert=True)
            return
        try:
            await ctx.tgrass.approve(bot_id, cb.from_user.id)
        except (TGrassError, LookupError):
            await cb.answer("TGrass: операция не выполнена; проверьте источник и заявку", show_alert=True)
            return
        reward = rub(ctx.tgrass.reward())
        t2 = ctx.db.tgrass(bot_id)
        mode = (t2["mode"] if t2 and "mode" in t2.keys() and t2["mode"] else "factory")
        extra = ""
        if t2 and t2["error"] and str(t2["error"]).startswith("factory→relay"):
            extra = "\n⚠️ Фабрика не удалась — записано подключение через источник креатора."
        await _edit(cb, f"✅ Бот ID {bot_id} @{html.escape(b['username'] or '')} подключён к TGrass "
                        f"[{MODE.get(mode, mode)}].{extra}",
                    ikb([btn("💸 TGrass", "a:tg")]))
        try:
            await cb.bot.send_message(b["owner_id"], f"✅ Бот ID {bot_id} подключён к TGrass! "
                                                     f"За каждую выполненную ОП вы получаете {reward} "
                                                     "на баланс конструктора. Статистика — в карточке бота → "
                                                     "«💸 Доп заработок».")
        except Exception:  # noqa: BLE001
            pass
    elif action == "no":
        if not await ctx.tgrass.reject(bot_id, cb.from_user.id):
            await cb.answer(f"Уже обработано: {STATUS.get(t['status'], t['status'])}", show_alert=True)
            return
        await _edit(cb, f"❌ Заявка бота ID {bot_id} отклонена.", ikb([btn("💸 TGrass", "a:tg")]))
        try:
            await cb.bot.send_message(b["owner_id"], f"❌ Заявка на подключение бота ID {bot_id} к TGrass отклонена.")
        except Exception:  # noqa: BLE001
            pass
    elif action == "off":
        if not await ctx.tgrass.disable(bot_id, cb.from_user.id):
            await cb.answer("Бот не подключён", show_alert=True)
            return
        await cb.answer("⛔ ОП TGrass в боте отключена")
        await bot_card(cb, ctx, bot_id)
        try:
            await cb.bot.send_message(b["owner_id"], f"⛔ Администратор отключил TGrass в боте ID {bot_id}.")
        except Exception:  # noqa: BLE001
            pass
        return
    await cb.answer()


@router.callback_query(F.data == "a:tg")
async def tg_main(cb: CallbackQuery, ctx: Ctx) -> None:
    pending = ctx.db.tgrass_list("pending")
    approved = ctx.db.tgrass_list("approved")
    disabled = ctx.db.tgrass_list("disabled")
    ops = ctx.db.scalar("SELECT COUNT(*) FROM tgrass_ops") or 0
    paid = ctx.db.scalar("SELECT COALESCE(SUM(reward_kop),0) FROM tgrass_ops") or 0
    text = ("💸 <b>TGrass — Доп заработок</b>\n\n"
            f"⏳ Заявок: {len(pending)}\n✅ Подключено ботов: {len(approved)}\n⛔ Отключено: {len(disabled)}\n"
            f"📊 ОП всего: {ops}, выплачено владельцам: {rub(paid)}\n\n"
            f"Награда за ОП: {rub(ctx.tgrass.reward())}, минимум пользователей: "
            f"{ctx.tgrass.min_users()} (меняется в «💰 Цены»)")
    if not ctx.cfg.tgrass_owner_id:
        text += ("\n\n⚠️ Не задан TGRASS_OWNER_ID: регистрация фабрики/автохаба недоступна. "
                 "Можно задать готовый ключ в «Источники ОП».")
    await _edit(cb, text, ikb([btn(f"⏳ Заявки ({len(pending)})", "a:tgl:pending")],
                              [btn(f"✅ Подключённые ({len(approved)})", "a:tgl:approved")],
                              [btn(f"⛔ Отключённые ({len(disabled)})", "a:tgl:disabled")] if disabled else [],
                              [btn("⬅️ Назад", "a:main")]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgl:"))
async def tg_list(cb: CallbackQuery, ctx: Ctx) -> None:
    status = cb.data.split(":")[2]
    rows = ctx.db.tgrass_list(status)
    if status == "pending":
        if not rows:
            await cb.answer("Заявок нет", show_alert=True)
            return
        await cb.answer()
        for t in rows[:20]:
            await cb.message.answer(app_text(ctx, t), reply_markup=app_kb(t["bot_id"]))
        return
    kb = [[btn(f"ID {t['bot_id']} @{t['username'] or '—'} | ОП {ctx.db.tgrass_stats(t['bot_id'])[0]}",
               f"a:tgb:{t['bot_id']}")] for t in rows[:40]]
    kb.append([btn("⬅️ Назад", "a:tg")])
    await _edit(cb, f"{STATUS.get(status, status).capitalize()}: {len(rows)}", ikb(*kb))
    await cb.answer()


async def bot_card(cb: CallbackQuery, ctx: Ctx, bot_id: int) -> None:
    t = ctx.db.tgrass(bot_id)
    b = ctx.db.bot(bot_id)
    if not t or not b:
        await cb.answer("Не найден", show_alert=True)
        return
    total, earned = ctx.db.tgrass_stats(bot_id)
    today, _ = ctx.db.tgrass_stats(bot_id, day_start())
    mode = t["mode"] if "mode" in t.keys() and t["mode"] else "factory"
    lines = [f"💸 <b>TGrass: бот ID {bot_id}</b> @{html.escape(b['username'] or '—')}",
             f"Владелец: <code>{b['owner_id']}</code>", f"Статус: {STATUS.get(t['status'], t['status'])}",
             f"Эффект. режим: {MODE.get(mode, mode)}",
             f"Пользователей: {ctx.tgrass.users(bot_id)}",
             f"ОП: {total} (сегодня {today}), выплачено владельцу: {rub(earned)}"]
    if mode == "factory" and t["api_key"] and t["status"] == "approved":
        try:
            bal = await ctx.tgrass.client.balance(t["api_key"])
            st = await ctx.tgrass.client.statistics(t["api_key"])
            lines.append(f"TGrass: баланс {bal}, сегодня (UTC) подписок {st.get('subs_count', 0)}, "
                         f"отписок {st.get('unsubs_count', 0)}, доход {st.get('income', 0)}")
        except TGrassError as e:
            lines.append(f"TGrass: не удалось получить статистику ({html.escape(e.message[:100])})")
    if t["error"]:
        lines.append("⚠️ Записана ошибка подключения; причина не проверена.")
    kb = []
    if t["status"] == "approved":
        kb.append([btn("⛔ Отключить ОП в боте", f"tg:off:{bot_id}")])
    elif t["status"] in ("disabled", "pending"):
        kb.append([btn("✅ Включить" if t["status"] == "disabled" else "✅ Одобрить", f"tg:ok:{bot_id}")])
    kb.append([btn("⬅️ Назад", "a:tg")])
    await _edit(cb, "\n".join(lines), ikb(*kb))


@router.callback_query(F.data.startswith("a:tgb:"))
async def tg_bot(cb: CallbackQuery, ctx: Ctx) -> None:
    await bot_card(cb, ctx, int(cb.data.split(":")[2]))
    await cb.answer()
