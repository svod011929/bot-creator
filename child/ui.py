"""Единый слой оформления сообщений и клавиатур дочерних ботов.

Используйте везде вместо ручной сборки текстов и стен кнопок.
"""
from __future__ import annotations

from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def title(text: str) -> str:
    return f"<b>{text}</b>"


def details(*lines: str, expandable: bool = False) -> str:
    body = "\n".join(lines)
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>{body}</{tag}>"


def screen(header: str, *main: str, details_lines: list[str] | None = None, expandable: bool = False) -> str:
    parts = [title(header)]
    if main:
        parts.append("\n".join(main))
    if details_lines:
        parts.append(details(*details_lines, expandable=expandable))
    return "\n\n".join(parts)


async def show(target, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует на месте; при ошибке — answer. Превью отключено."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            await target.message.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    else:
        await target.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def btn(text: str, data: str, style: ButtonStyle | None = None) -> InlineKeyboardButton:
    kw = {"text": text, "callback_data": data}
    if style is not None:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def copy_btn(text: str, value: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value))


def back(text: str = "⬅️ Назад", data: str = "back") -> InlineKeyboardButton:
    return btn(text, data)


def home(text: str = "⬅️ В меню", data: str = "home") -> InlineKeyboardButton:
    return btn(text, data)


def grid(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), cols):
        rows.append(buttons[i:i + cols])
    return rows


def ikb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def with_nav(rows: list[list[InlineKeyboardButton]], back_data: str | None = None,
             home_data: str | None = None) -> InlineKeyboardMarkup:
    nav = []
    if back_data:
        nav.append(back(data=back_data))
    if home_data:
        nav.append(home(data=home_data))
    if nav:
        rows = list(rows) + [nav]
    return ikb(*rows)
