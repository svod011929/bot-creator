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


def bot_card_kb(b, for_sale: bool, tgrass: bool = False) -> InlineKeyboardMarkup:
    """Основные действия сверху; передача и удаление — отдельным блоком снизу."""
    i = b["id"]
    running = b["desired"] and b["status"] in ("running", "restarting")
    return ikb(
        [btn("⏹ Остановить", f"b:{i}:stop") if running else btn("▶️ Запустить", f"b:{i}:start")],
        [btn("🔄 Перезапустить", f"b:{i}:restart")] if running else [],
        [btn("💳 Хостинг", f"b:{i}:host"), btn("👑 VIP", f"b:{i}:vip")],
        [btn("📋 Первый запуск", f"setup:{i}:{b['template']}:ready")],
        [btn("⚙️ Настройки", f"b:{i}:set"),
         btn("🚫 Снять с продажи", f"b:{i}:unsell") if for_sale else btn("🛒 Продать", f"b:{i}:sell")],
        [btn("💸 Доп заработок", f"b:{i}:tg")] if tgrass else [],
        [btn("🔁 Передать бота", f"b:{i}:transfer")],
        [btn("🗑 Удалить", f"b:{i}:del")],
        [btn("⬅️ Назад", "mybots")],
    )
