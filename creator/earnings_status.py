"""Owner earnings views: recorded evidence, not provider health or a registration action."""
from __future__ import annotations

import re
import sqlite3

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from child import templates
from creator.db import day_start
from creator.keyboards import btn, ikb
from creator.money import rub
from creator.tgrass import child_users_for_card

STATUS = {
    "pending": "заявка на рассмотрении",
    "approved": "одобрение записано",
    "rejected": "заявка отклонена",
    "disabled": "отключено администратором",
}
NETWORKS = {"tgr": ("TGrass", "tgrass", "TGRASS"), "pf": ("PiarFlow", "piarflow", "PIARFLOW")}
DISCLAIMER = "Доступность сети и сквозное выполнение заданий этим экраном не проверяются."
UNAVAILABLE = "Бот не найден или действие устарело."
REQUEST = re.compile(r"b:([1-9][0-9]{0,18}):(tg|tgr|pf)")


def _field(row, key):
    return row[key] if row is not None and key in row.keys() else None


def record_status(row) -> str:
    if row is None:
        return "заявка не записана"
    status = _field(row, "status")
    return STATUS.get(status, "статус неизвестен") if isinstance(status, str) else "статус неизвестен"


def status_line(title: str, row) -> str:
    title = title if title in {"TGrass", "PiarFlow"} else "Сеть"
    return f"{title}: в учёте — {record_status(row)}"


def _record(ctx, bot_id, network):
    return ctx.db.tgrass(bot_id) if network == "tgr" else ctx.db.pf(bot_id)


def _number(value) -> int:
    if type(value) is not int or not 0 <= value <= 2 ** 63 - 1:
        raise ValueError("Unavailable numeric evidence")
    return value


def _stats(ctx, bot_id, network) -> list[str]:
    stats = ctx.db.tgrass_stats if network == "tgr" else ctx.db.pf_stats
    try:
        total, earned = (_number(x) for x in stats(bot_id))
        today, today_earned = (_number(x) for x in stats(bot_id, day_start()))
    except (sqlite3.Error, ValueError, TypeError):
        return ["Сведения о записанных начислениях временно недоступны."]
    return [f"Зачтено в учёте: {total} (сегодня {today})",
            f"Начислено в конструкторе: {rub(earned)} (сегодня {rub(today_earned)})",
            "Это записанные начисления по боту, не сумма выплаты и не прогноз дохода."]


def owner_screen(ctx, b, network: str):
    if network not in NETWORKS:
        raise ValueError("Unknown network")
    from creator.handlers.tgrass import admin_contact_btn

    title, service, prefix = NETWORKS[network]
    bot_id = _number(b["id"])
    enabled = getattr(ctx, service) is not None
    row = _record(ctx, bot_id, network)
    status = _field(row, "status")
    lines = [f"💸 <b>Доп заработок {title} — бот ID {bot_id}</b>",
             "Сеть включена в креаторе." if enabled else "Сеть выключена в креаторе. Обратитесь к администратору.",
             f"В учёте: {record_status(row)}."]
    if row is not None:
        mode = _field(row, "mode")
        lines.append({"factory": "В записи: собственный бот (factory).",
                      "relay": "В записи: трансляция через хаб креатора (relay)."}.get(mode,
                     "Способ подключения не подтверждён.") if isinstance(mode, str)
                     else "Способ подключения не подтверждён.")
    guidance = {"pending": "Дождитесь решения администратора; повторно подавать заявку не нужно.",
                "approved": "Одобрение записано; выдача и выполнение заданий сейчас не подтверждены.",
                "rejected": "Заявка отклонена. Уточните условия у администратора перед повторной подачей.",
                "disabled": "Подключение отключено администратором. Уточните причину у него."}
    lines.append(guidance.get(status, "Уточните состояние у администратора.") if isinstance(status, str)
                 else "Можно подать заявку при выполнении текущих условий." if row is None
                 else "Уточните состояние у администратора.")
    if enabled:
        try:
            reward = rub(_number(ctx.price(f"{prefix}_REWARD")))
            need = _number(ctx.price(f"{prefix}_MIN_USERS"))
            path = ctx.sup.bot_dir(bot_id)
            users = child_users_for_card(path) if (path / "bot.db").is_file() else None
            count = "не удалось проверить" if users is None else f"{_number(users)} из {need}"
            lines += [f"Текущая ставка за зачтённое задание: <b>{reward}</b> на баланс конструктора.",
                      f"Пользователей в боте: {count}; минимум для заявки: {need}."]
        except (ValueError, TypeError, OSError, sqlite3.Error):
            lines.append("Текущие условия временно недоступны; уточните их у администратора.")
    if row is not None:
        lines += _stats(ctx, bot_id, network)
    lines += ["Для нового подключения: сначала factory; при ошибке регистрации — автоматический relay. Выбирать режим не нужно.",
              DISCLAIMER, "Начисления креатора — в рублях. Гарантированного дохода нет."]
    rows = []
    if enabled and status not in ("pending", "approved"):
        rows.append([btn("📝 Подать заявку", f"b:{bot_id}:{'tgapply' if network == 'tgr' else 'pfapply'}")])
    rows.append([admin_contact_btn(ctx)])
    other = ctx.piarflow if network == "tgr" else ctx.tgrass
    rows.append([btn("⬅️ Назад", f"b:{bot_id}:tg" if other is not None else f"b:{bot_id}:open")])
    return "\n\n".join(lines), ikb(*rows)


def hub_screen(ctx, b):
    bot_id = _number(b["id"])
    lines = [f"💸 <b>Доп заработок — бот ID {bot_id}</b>", "Выберите сеть; показаны только настройки и записи креатора."]
    rows = []
    for network, (title, service, _) in NETWORKS.items():
        enabled = getattr(ctx, service) is not None
        status = record_status(_record(ctx, bot_id, network))
        lines.append(f"{title}: {'включена' if enabled else 'выключена'} в креаторе; {status}.")
        rows.append([btn(f"{title} — {status}", f"b:{bot_id}:{network}")])
    lines += [DISCLAIMER, "Начисления креатора учитываются в рублях; просмотр ничего не подключает и не начисляет."]
    rows.append([btn("⬅️ Назад", f"b:{bot_id}:open")])
    return "\n\n".join(lines), ikb(*rows)


def _identity(row):
    return row["owner_id"], row["template"], row["token"]


async def _current(ctx, state, row, snapshot):
    current = await state.get_state(), await state.get_data()
    latest = ctx.db.bot(row["id"])
    return latest is not None and _identity(latest) == _identity(row) and current == snapshot


async def handle(cb: CallbackQuery, state, ctx) -> None:
    match = REQUEST.fullmatch(cb.data or "")
    if not match or int(match[1]) > 2 ** 63 - 1:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    row = ctx.db.bot(int(match[1]))
    if (not isinstance(cb.message, Message) or cb.message.chat.type != "private"
            or cb.message.chat.id != cb.from_user.id or row is None or row["owner_id"] != cb.from_user.id
            or row["template"] not in templates.TEMPLATE_MODULES):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    snapshot = await state.get_state(), await state.get_data()
    if snapshot[0] is not None:
        await cb.answer("Завершите текущую настройку или нажмите /cancel.", show_alert=True)
        return
    try:
        action = match[2]
        if action == "tg":
            if ctx.tgrass is not None and ctx.piarflow is None:
                action = "tgr"
            elif ctx.piarflow is not None and ctx.tgrass is None:
                action = "pf"
        text, markup = hub_screen(ctx, row) if action == "tg" else owner_screen(ctx, row, action)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        await cb.answer("Сведения временно недоступны.", show_alert=True)
        return
    if not await _current(ctx, state, row, snapshot):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            await cb.answer("Не удалось открыть экран. Откройте «Мои боты» заново.", show_alert=True)
            return
    except TelegramAPIError:
        await cb.answer("Не удалось открыть экран. Откройте «Мои боты» заново.", show_alert=True)
        return
    if not await _current(ctx, state, row, snapshot):
        try:
            await cb.message.edit_text(UNAVAILABLE, reply_markup=None)
        except TelegramAPIError:
            pass
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    await cb.answer()
