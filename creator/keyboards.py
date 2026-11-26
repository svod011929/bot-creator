"""Клавиатуры конструктора."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton as B,
)
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_CREATE = "🔨 Создать бота"
BTN_MY = "🤖 Мои боты"
BTN_CABINET = "💼 Мой кабинет"
BTN_PARTNERS = "👥 Партнёры"
BTN_RESALE = "🛒 Перепродажа"
BTN_INFO = "📰 Информация"
BTN_ADMIN = "🛠 Админ-панель"  # shown only to configured admins; access is re-checked server-side
BTN_CONTENT = "📚 Контент ботов"
MENU_BUTTONS = {BTN_CREATE, BTN_MY, BTN_CABINET, BTN_PARTNERS, BTN_RESALE, BTN_INFO}

STATUS_ICON = {"running": "🟢", "restarting": "🟡", "error": "⚠️"}
STATUS_TEXT = {"running": "🟢 Запущен", "restarting": "🟡 Перезапускается", "error": "⚠️ Ошибка",
               "stopped": "🔴 Остановлен"}


def main_kb(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [[BTN_CREATE, BTN_MY], [BTN_CABINET, BTN_PARTNERS], [BTN_RESALE, BTN_INFO]]
    if admin:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def ikb(*rows: list[B]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def btn(text: str, data: str) -> B:
    return B(text=text, callback_data=data)


def url_btn(text: str, url: str) -> B:
    return B(text=text, url=url)


def status_icon(status: str) -> str:
    return STATUS_ICON.get(status, "🔴")


def cancel_kb(back: str = "cancel") -> InlineKeyboardMarkup:
    return ikb([btn("❌ Отмена", back)])


def bot_card_kb(b, for_sale: bool, tgrass: bool = False, webhooks: bool = False) -> InlineKeyboardMarkup:
    """Компактная карточка: группы 2-го уровня вместо плоской стены кнопок."""
    i = b["id"]

    # Основные входы (уровень 1) — максимум ~6 рядов
    rows = [
        [btn("⚙️ Управление", f"b:{i}:g:manage"),
         btn("💳 Хостинг и VIP", f"b:{i}:g:hosting")],
        [btn("🎨 Оформление и контент", f"b:{i}:g:content"),
         btn("💰 Заработок", f"b:{i}:g:earn")],
        [btn("📊 Статистика", f"bst:{i}"),
         btn("📋 Первый запуск", f"setup:{i}:{b['template']}:ready")],
        [btn("⚙️ Настройки", f"b:{i}:set"),
         btn("🚫 С продажи", f"b:{i}:unsell") if for_sale else btn("🛒 Продать", f"b:{i}:sell")],
    ]
    # Опасные действия отдельно, одна строка
    rows.append([btn("🔁 Передать", f"b:{i}:transfer"), btn("🗑 Удалить", f"b:{i}:del")])
    rows.append([btn("⬅️ Назад", "mybots")])
    return ikb(*rows)


def bot_card_group_kb(b, group: str, for_sale: bool, tgrass: bool = False, webhooks: bool = False) -> InlineKeyboardMarkup | None:
    """Подменю карточки бота. Возвращает None для неизвестной группы."""
    i = b["id"]
    running = b["desired"] and b["status"] in ("running", "restarting")
    from creator.content import callback
    from creator.engagement import request as activity_request
    from creator.presentation import callback as presentation_callback

    if group == "manage":
        rows = [
            [btn("⏹ Остановить", f"b:{i}:stop") if running else btn("▶️ Запустить", f"b:{i}:start")],
        ]
        if running:
            rows.append([btn("🔄 Перезапустить", f"b:{i}:restart")])
        rows.append([btn("💡 Подсказки", f"b:{i}:tips")])
        rows.append([btn("⬅️ К карточке", f"b:{i}:open")])
        return ikb(*rows)

    if group == "hosting":
        rows = [
            [btn("💳 Хостинг", f"b:{i}:host"), btn("👑 VIP", f"b:{i}:vip")],
            [btn("⬅️ К карточке", f"b:{i}:open")],
        ]
        return ikb(*rows)

    if group == "content":
        rows = [
            [btn("🎨 Оформление", presentation_callback(i, "home")),
             btn("📈 Активность", activity_request(i))],
        ]
        if b["template"] in {"refbot", "starbot", "clicker"}:
            rows.append([btn("📚 Контент", callback(b, "home"))])
        rows.append([btn("⬅️ К карточке", f"b:{i}:open")])
        return ikb(*rows)

    if group == "earn":
        rows = [
            [btn("📢 Заработок на рассылках", f"b:{i}:mgram")],
            [btn("🚀 Boostly", f"b:{i}:bl")],
        ]
        if b["template"] in {"clicker", "refbot", "microtasks"}:
            rows.append([btn("💳 LavePay выплаты", f"olp:{i}")])
        if tgrass:
            rows.append([btn("💸 Доп заработок", f"b:{i}:tg")])
        if webhooks:
            rows.append([btn("🔗 Вебхуки", f"b:{i}:wh")])
        rows.append([btn("⬅️ К карточке", f"b:{i}:open")])
        return ikb(*rows)

    return None
