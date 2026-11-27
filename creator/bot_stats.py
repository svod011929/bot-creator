"""C4 minimal: owner statistics read on demand from the bot's own database (read-only, no new storage)."""
from __future__ import annotations

import html
import sqlite3
import time
from pathlib import Path

from creator.money import rub
from creator.ui import screen

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
        # Active = not blocked and not banned (banned may be absent on older child DBs).
        active = _row(conn, "SELECT COUNT(*) FROM users WHERE blocked=0 AND COALESCE(banned,0)=0")
        if active is None:
            active = _row(conn, "SELECT COUNT(*) FROM users WHERE blocked=0")
        return {
            "users": users,
            "active": active[0] if active else None,
            "tasks": _row(conn, "SELECT COALESCE(SUM(active=1),0) FROM tasks"),
            "done": _row(conn, "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM task_done"),
            "withdrawals": _row(conn, "SELECT COALESCE(SUM(status='pending'),0), COALESCE(SUM(status='paid'),0), "
                                      "COALESCE(SUM(status='rejected'),0) FROM withdrawals"),
        }
    finally:
        conn.close()


def op_income(db, bot_id: int, now_ts: int | None = None) -> dict:
    """Доход с ОП (TGrass + PiarFlow + Boostly) из базы конструктора. Возвращает число заданий и копейки."""
    from creator.db import day_start

    now_ts = int(time.time()) if now_ts is None else now_ts
    day = day_start(now_ts)
    week = now_ts - 7 * DAY

    def _sum(since: int) -> tuple[int, int]:
        def _one(fn) -> tuple[int, int]:
            try:
                count, kop = fn(bot_id, since)
                return int(count), int(kop)
            except (TypeError, ValueError, ArithmeticError, AttributeError):
                return 0, 0
        t_count, t_kop = _one(db.tgrass_stats)
        p_count, p_kop = _one(db.pf_stats)
        b_count, b_kop = _one(getattr(db, "boostly_stats", lambda *a: (0, 0)))
        return t_count + p_count + b_count, t_kop + p_kop + b_kop

    d_count, d_kop = _sum(day)
    w_count, w_kop = _sum(week)
    a_count, a_kop = _sum(0)
    return {
        "day": (d_count, d_kop),
        "week": (w_count, w_kop),
        "all": (a_count, a_kop),
    }


NOTE = ("<i>Окна считаются от текущего момента. «Недоступны» — доставка рассылки этому пользователю была "
        "отклонена и он ещё не вернулся в бота. Суммы заявок на вывод не показываются: у бота своя единица "
        "баланса, а «выплачено» — отметка владельца, не подтверждение перевода.\n"
        "Не измеряется этой сводкой: активность и возвраты пользователей, удержание, конверсия, доход внутри бота, "
        "живой ответ бота в Telegram. Наблюдаемая активность доступна отдельной кнопкой после начала сбора.</i>")


def render(bot_id: int, username: str | None, data: dict | str, op: dict | None = None) -> str:
    head = f"Статистика бота @{html.escape(username or '—')} (#{bot_id})"
    if data == MISSING:
        return screen(head, "Данных пока нет: бот ещё не создал свою базу. "
                            "Обычно она появляется после первого запуска и первого пользователя.")
    if data == ERROR or not isinstance(data, dict):
        return screen(head, "Не удалось прочитать данные бота. Попробуйте позже — это не значит, что пользователей нет. "
                            "Если ошибка повторяется, напишите в поддержку.")
    total, d1, d7, d30, refs, blocked = data["users"]
    active = data.get("active")
    tasks, done, wd = data["tasks"], data["done"], data["withdrawals"]
    tasks_line = ("нет данных" if tasks is None or done is None
                  else f"активных {tasks[0]}; выполнений {done[0]}, пользователей {done[1]}")
    wd_line = ("нет данных" if wd is None
               else f"на рассмотрении {wd[0]} · отмечено выплаченными {wd[1]} · отклонено {wd[2]}")
    active_line = "нет данных" if active is None else str(active)

    tip = "Совет: если пользователей мало — проверьте оформление, первый шаг и реферальную ссылку внутри бота."
    main = [
        f"👥 Пользователей всего: {total}",
        f"Активных в базе: {active_line}",
        f"Новых за последние 24 ч — {d1} · 7 дней — {d7} · 30 дней — {d30}",
        f"Пришли по реферальной ссылке: {refs}",
        f"Отмечены недоступными при рассылке: {blocked}",
    ]
    det = []
    if op:
        dc, dk = op["day"]
        wc, wk = op["week"]
        ac, ak = op["all"]
        det.append("💰 Доход с ОП")
        det.append(f"• Сегодня: {rub(dk)} ({dc} заданий)")
        det.append(f"• За 7 дней: {rub(wk)} ({wc} заданий)")
        det.append(f"• Всего: {rub(ak)} ({ac} заданий)")
    else:
        det.append("💰 Доход с ОП")
        det.append("• Сегодня: нет данных")
        det.append("• За 7 дней: нет данных")
    det.append(f"📋 Свои задания: {tasks_line}")
    det.append(f"💸 Заявки на вывод: {wd_line}")
    det.append(NOTE)
    det.append(tip)
    return screen(head, *main, details_lines=det, expandable=True)
