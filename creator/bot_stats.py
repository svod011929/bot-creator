"""C4 minimal: owner statistics read on demand from the bot's own database (read-only, no new storage)."""
from __future__ import annotations

import html
import sqlite3
import time
from pathlib import Path

DAY = 86400
MISSING, ERROR = "missing", "error"


def _row(conn: sqlite3.Connection, sql: str, *args) -> tuple | None:
    """Aggregate row or None when the table/columns are not available (unknown is not zero)."""
    try:
        return tuple(int(v or 0) for v in conn.execute(sql, args).fetchone())
    except (sqlite3.Error, TypeError, ValueError):
        return None


def read(path: Path, now: int | None = None) -> dict | str:
    """Bounded aggregate queries on one bot database; opens read-only and never creates the file."""
    if not path.is_file():
        return MISSING
    now = int(time.time()) if now is None else now
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return ERROR
    try:
        users = _row(conn, "SELECT COUNT(*), COALESCE(SUM(created_at>=?),0), COALESCE(SUM(created_at>=?),0), "
                           "COALESCE(SUM(created_at>=?),0), COALESCE(SUM(referrer_id IS NOT NULL),0), "
                           "COALESCE(SUM(blocked=1),0) FROM users", now - DAY, now - 7 * DAY, now - 30 * DAY)
        if users is None:
            return ERROR
        return {
            "users": users,
            "tasks": _row(conn, "SELECT COALESCE(SUM(active=1),0) FROM tasks"),
            "done": _row(conn, "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM task_done"),
            "withdrawals": _row(conn, "SELECT COALESCE(SUM(status='pending'),0), COALESCE(SUM(status='paid'),0), "
                                      "COALESCE(SUM(status='rejected'),0) FROM withdrawals"),
        }
    finally:
        conn.close()


NOTE = ("<i>Окна считаются от текущего момента. «Недоступны» — доставка рассылки этому пользователю была "
        "отклонена и он ещё не вернулся в бота. Суммы не показываются: у бота своя единица баланса, "
        "а «выплачено» — отметка владельца, не подтверждение перевода.\n"
        "Не измеряется этой сводкой: активность и возвраты пользователей, удержание, конверсия, доход, "
        "живой ответ бота в Telegram. Наблюдаемая активность доступна отдельной кнопкой после начала сбора.</i>")


def render(bot_id: int, username: str | None, data: dict | str) -> str:
    head = f"📊 <b>Статистика бота @{html.escape(username or '—')}</b> (ID {bot_id})\n"
    if data == MISSING:
        return head + "\nДанных пока нет: бот ещё не создал свою базу (обычно появляется после первого запуска)."
    if data == ERROR or not isinstance(data, dict):
        return head + "\nНе удалось прочитать данные бота. Попробуйте позже — это не значит, что пользователей нет."
    total, d1, d7, d30, refs, blocked = data["users"]
    tasks, done, wd = data["tasks"], data["done"], data["withdrawals"]
    tasks_line = ("нет данных" if tasks is None or done is None
                  else f"активных {tasks[0]}; выполнений {done[0]}, пользователей {done[1]}")
    wd_line = ("нет данных" if wd is None
               else f"на рассмотрении {wd[0]} · отмечено выплаченными {wd[1]} · отклонено {wd[2]}")
    return (head + "Сводка по базе бота на момент открытия.\n\n"
            f"👥 Пользователей всего: {total}\n"
            f"🆕 Новых за последние 24 ч — {d1} · 7 дней — {d7} · 30 дней — {d30}\n"
            f"🔗 Пришли по реферальной ссылке: {refs}\n"
            f"🚫 Отмечены недоступными при рассылке: {blocked}\n"
            f"📋 Свои задания: {tasks_line}\n"
            f"💸 Заявки на вывод: {wd_line}\n\n" + NOTE)
