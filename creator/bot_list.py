"""Bounded owner-only list query, ephemeral view and safe presentation. No live checks."""
from __future__ import annotations

import html
import re
import secrets
import time
from dataclasses import dataclass, replace

from child import templates
from creator.bot_setup import bot_url
from creator.keyboards import STATUS_ICON, btn, ikb

STATUSES = {"all": "Все", "running": "Запущен", "stopped": "Остановлен",
            "restarting": "Перезапускается", "error": "Ошибка"}
SIZE, TTL, CACHE_LIMIT = 10, 900, 128
NONCE = re.compile(r"[0-9a-f]{8}")
QUERY = re.compile(r"[A-Za-z0-9_]{1,32}")


@dataclass(frozen=True)
class View:
    nonce: str
    query: str = ""
    status: str = "all"
    page: int = 0
    expires: float = 0


def fresh() -> View:
    return View(secrets.token_hex(4), expires=time.monotonic() + TTL)


def normalize_query(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 33:
        raise ValueError("Invalid search")
    query = raw.strip().removeprefix("@")
    if not QUERY.fullmatch(query):
        raise ValueError("Invalid search")
    if query.isascii() and query.isdigit():
        if len(query) > 19 or not 0 < int(query) <= 2 ** 63 - 1:
            raise ValueError("Invalid ID")
        return str(int(query))
    return query.lower()


def valid(view) -> bool:
    if (not isinstance(view, View) or not isinstance(view.nonce, str) or not NONCE.fullmatch(view.nonce)
            or not isinstance(view.query, str) or not isinstance(view.status, str) or view.status not in STATUSES
            or type(view.page) is not int or view.page < 0 or type(view.expires) not in (float, int)
            or not time.monotonic() < view.expires <= time.monotonic() + TTL):
        return False
    try:
        return not view.query or normalize_query(view.query) == view.query
    except ValueError:
        return False


def cached(ctx, uid: int) -> View | None:
    view = ctx.bot_lists.get(uid)
    if valid(view):
        return view
    ctx.bot_lists.pop(uid, None)
    return None


def remember(ctx, uid: int, view: View) -> None:
    ctx.bot_lists.pop(uid, None)
    ctx.bot_lists[uid] = replace(view, expires=time.monotonic() + TTL)
    while len(ctx.bot_lists) > CACHE_LIMIT:
        ctx.bot_lists.pop(next(iter(ctx.bot_lists)))


def page(db, uid: int, view: View):
    if view.status not in STATUSES or type(view.page) is not int:
        raise ValueError("Invalid list")
    where, args = "owner_id=? AND deleted=0", [uid]
    if view.status != "all":
        where += " AND status=?"
        args.append(view.status)
    if view.query:
        query = normalize_query(view.query)
        if query.isdigit():
            where += " AND id=?"
            args.append(int(query))
        else:
            where += " AND LOWER(username) LIKE ? ESCAPE '\\'"
            args.append("%" + query.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%")
    total = db.one("SELECT COUNT(*) AS n FROM bots WHERE " + where, *args)["n"]
    view = replace(view, page=min(max(0, view.page), max(0, (total - 1) // SIZE)))
    rows = db.all("SELECT id,template,username,status FROM bots WHERE " + where
                  + " ORDER BY id LIMIT ? OFFSET ?", *args, SIZE, view.page * SIZE)
    return rows, total, view


def data(view: View, action: str) -> str:
    return f"bl:{view.nonce}:{action}"


def render(ctx, uid: int, view: View):
    rows, total, view = page(ctx.db, uid, view)
    lines = [f"🤖 <b>Ваши боты</b> ({total})", f"Страница {view.page + 1} из {max(1, (total + SIZE - 1) // SIZE)}",
             "Статус записи: " + STATUSES[view.status], "Поиск: " + ("включён" if view.query else "выключен"),
             "Это записанный статус, не проверка ответа Telegram."]
    buttons = []
    for row in rows:
        try:
            title = templates.info(row["template"]).title
        except KeyError:
            title = "Неизвестный шаблон"
        name = "@" + row["username"] if bot_url(row) else "username недоступен"
        lines.append(f"ID {row['id']} · {html.escape(name)}")
        buttons.append([btn(f"ID {row['id']} | {title} {STATUS_ICON.get(row['status'], '⚪')}", f"b:{row['id']}:open")])
    if not rows:
        lines.append("Боты не найдены.")
        if not view.query and view.status == "all":
            lines.append("Нажмите «🔨 Создать бота», чтобы создать первого.")
            buttons.append([btn("🔨 Создать бота", "create")])
    nav = []
    if view.page:
        nav.append(btn("⬅️ Предыдущая", data(view, f"p:{view.page - 1}")))
    if (view.page + 1) * SIZE < total:
        nav.append(btn("Следующая ➡️", data(view, f"p:{view.page + 1}")))
    if nav:
        buttons.append(nav)
    filters = [btn(label, data(view, "f:" + key)) for key, label in STATUSES.items()]
    buttons.extend(filters[i:i + 2] for i in range(0, len(filters), 2))
    buttons.extend(([btn("🔎 Поиск", data(view, "search"))], [btn("Сбросить поиск и фильтр", data(view, "reset"))]))
    return "\n".join(lines), ikb(*buttons), view
