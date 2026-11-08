"""Read-only presentation for the existing template selection and token instruction."""
from __future__ import annotations

import html
import re

from child import templates
from creator.money import rub
from creator.services import Ctx

CONDITIONS_UNAVAILABLE = "Условия временно недоступны. Откройте выбор шаблона позже."
COMPARISON_HEADING = (
    "Выберите шаблон бота. Сравните возможности и стоимость, затем нажмите нужный вариант — "
    "откроется описание с инструкцией по токену"
)
DESCRIPTIONS = {
    "clicker": (
        "Для игровой механики с кликами: пользователи нажимают кнопку, улучшают силу клика и приглашают друзей.\n"
        "Меню: «Кликать», «Улучшения», «Топ», «Пригласить».\n"
        "Единица по умолчанию — монеты."
    ),
    "refbot": (
        "Для реферальной механики: пользователи приглашают друзей и получают настроенную владельцем награду.\n"
        "Меню: «Пригласить друзей», «Топ рефоводов», «О боте».\n"
        "Единица по умолчанию — рубли."
    ),
    "starbot": (
        "Для механики с друзьями, ежедневным бонусом и собственными заданиями на подписку.\n"
        "Меню: «Заработать звёзды», «Ежедневный бонус», «Задания», «Топ».\n"
        "Единица по умолчанию — звёзды."
    ),
}
COMPARISON_FOOTER = (
    "У всех шаблонов есть профиль и заявки на ручную выплату. "
    "Единица баланса внутри бота настраивается владельцем. "
    "Создание и хостинг оплачиваются в рублях. Гарантированного дохода нет."
)
TOKEN_INSTRUCTION = (
    "📝 <b>Как получить токен:</b>\n"
    "1. Откройте @BotFather и отправьте /newbot\n"
    "2. Придумайте имя и username бота\n"
    "3. Скопируйте токен вида <code>123456789:AA...</code> и пришлите сюда\n\n"
    "⚠️ Токен — это ключ от бота, никому кроме конструктора его не отправляйте."
)


def _nonnegative(value: int) -> int:
    if value < 0:
        raise ValueError("Invalid display conditions")
    return value


def _hosting_text(ctx: Ctx) -> str:
    days = _nonnegative(ctx.price("START_HOSTING_DAYS"))
    daily = _nonnegative(ctx.price("HOSTING_DAY_PRICE"))
    lines = []
    if days > 0:
        lines.append(f"В стоимость включён хостинг на {html.escape(str(days))} дн.")
    lines.append(
        f"Хостинг: {html.escape(rub(daily))}/сутки" if daily > 0 else
        "По текущему тарифу ежесуточная плата за хостинг не взимается"
    )
    return "\n".join(lines)


def _bounded(text: str) -> str:
    # Only these fixed tags are emitted; dynamic markup is escaped before insertion.
    visible = html.unescape(re.sub(r"</?(?:b|code)>", "", text))
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Display conditions too long")
    return text


def comparison_text(ctx: Ctx) -> str:
    blocks = [COMPARISON_HEADING]
    for key, description in DESCRIPTIONS.items():
        price = _nonnegative(ctx.template_price(key))
        cost = "бесплатно" if price == 0 else html.escape(rub(price))
        blocks.append(f"<b>{html.escape(templates.info(key).title)}</b>\n{description}\nСоздание: {cost}")
    blocks.extend((_hosting_text(ctx), COMPARISON_FOOTER))
    return _bounded("\n\n".join(blocks))


def detail_text(ctx: Ctx, key: str) -> str:
    t = templates.info(key)
    price = _nonnegative(ctx.template_price(key))
    return _bounded(
        f"{html.escape(t.emoji)} <b>{html.escape(t.title)}</b>\n\n{html.escape(t.description)}\n\n"
        f"💰 Стоимость: <b>{html.escape(rub(price))}</b>\n{_hosting_text(ctx)}\n\n{TOKEN_INSTRUCTION}"
    )
