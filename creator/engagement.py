"""Owner-only, bounded read-only activity reports from child observations."""

from __future__ import annotations

import html
import sqlite3
import time
from datetime import datetime, timedelta

from child.engagement import STEPS, TZ
from creator.content import ContentError, _guard, decode, encode
from creator.keyboards import btn, ikb

TABLES = {"engagement_meta", "engagement_people", "engagement_activity", "engagement_events"}


def read(path, template, *, days=30, now=None, exclude_uid=0):
    if template not in STEPS or days not in (7, 30):
        return {"status": "error"}
    if not path.is_file():
        return {"status": "missing"}
    now = int(time.time()) if now is None else int(now)
    local = datetime.fromtimestamp(now, TZ)
    today = local.date()
    start = today - timedelta(days=days - 1)
    at_start = int(datetime.combine(start, datetime.min.time(), TZ).timestamp())
    conn = None
    try:
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES & tables:
            return {"status": "not_started"}
        if not TABLES <= tables:
            return {"status": "error"}
        meta = conn.execute("SELECT version,started_at FROM engagement_meta WHERE id=1").fetchone()
        if not meta or meta[0] != 1:
            return {"status": "error"}
        result = {
            "status": "ok",
            "started_at": meta[1],
            "now": now,
            "days": days,
            "since": start.isoformat(),
            "until": today.isoformat(),
            "active": {},
            "steps": [],
            "retention": {},
        }
        for window in (1, 7, 30):
            first = (today - timedelta(days=window - 1)).isoformat()
            result["active"][window] = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM engagement_activity WHERE day>=? AND day<=? AND first_at<=? AND user_id<>?",
                (first, today.isoformat(), now, exclude_uid),
            ).fetchone()[0]
        result["observed"], result["registered"] = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(registered_here),0) FROM engagement_people "
            "WHERE first_at>=? AND first_at<=? AND user_id<>?",
            (at_start, now, exclude_uid),
        ).fetchone()
        for kind, title in STEPS[template]:
            count = conn.execute(
                "SELECT COUNT(DISTINCT p.user_id) FROM engagement_people p JOIN engagement_events e ON e.user_id=p.user_id "
                "WHERE p.first_at>=? AND p.first_at<=? AND e.kind=? AND e.first_at>=p.first_at "
                "AND e.first_at<=? AND p.user_id<>?",
                (at_start, now, kind, now, exclude_uid),
            ).fetchone()[0]
            result["steps"].append((title, count))
        # Only fully closed target days enter a retention denominator.
        for lag in (1, 7):
            last = today - timedelta(days=lag + 1)
            cohort_start = today - timedelta(days=30)
            denominator, returned = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(EXISTS(SELECT 1 FROM engagement_activity a WHERE a.user_id=p.user_id "
                "AND a.day=date(p.first_day,?) AND a.first_at<=?)),0) FROM engagement_people p "
                "WHERE p.first_day>=? AND p.first_day<=? AND p.first_at<=? AND p.user_id<>?",
                (f"+{lag} day", now, cohort_start.isoformat(), last.isoformat(), now, exclude_uid),
            ).fetchone()
            result["retention"][lag] = {
                "eligible": denominator,
                "returned": returned,
                "from": cohort_start.isoformat(),
                "through": last.isoformat(),
            }
        return result
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return {"status": "error"}
    finally:
        if conn:
            conn.close()


def read_owned(ctx, uid, bot_id, *, days=30, now=None):
    try:
        with ctx.db.owned_bot(bot_id, uid) as row:
            row = dict(row)
            path = _guard(ctx, row, uid)
            data = read(path / "bot.db", row["template"], days=days, now=now, exclude_uid=uid)
            _guard(ctx, row, uid)
            identity = (row["owner_id"], row["template"], row["token"], row["username"])
            return data, identity
    except (ValueError, OSError, sqlite3.Error):
        raise ContentError("Аналитика недоступна. Откройте карточку своего бота заново.") from None


def request(bot_id, section="overview", days=30):
    return f"eng:{encode(bot_id)}:{section}:{days}"


def parse(raw):
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != "eng" or parts[2] not in {"overview", "steps", "retention"} or parts[3] not in {"7", "30"}:
        raise ContentError("Кнопка устарела. Откройте аналитику заново.")
    return decode(parts[1]), parts[2], int(parts[3])


def ratio(value, total):
    return "—" if not total else f"{100 * value / total:.1f}%".replace(".", ",")


def render(bot_id, data, *, section="overview", days=30):
    text = f"📈 <b>Активность · бот {bot_id}</b>\n"
    status = data.get("status")
    if status in {"missing", "not_started"}:
        text += (
            "\nНаблюдений пока нет. Сбор начинается при работе дочернего бота с новым пакетом. "
            "Старые действия задним числом не добавляются."
        )
    elif status != "ok":
        text += "\nДанные сейчас недоступны. Это не означает отсутствие участников."
    else:
        since = datetime.fromtimestamp(data["started_at"], TZ).strftime("%d.%m.%Y %H:%M")
        text += f"Наблюдения с {since} МСК.\n"
        if section != "retention":
            text += f"Период: {data['since']} — {data['until']} (МСК).\n"
        if section == "overview":
            active = data["active"]
            text += (
                f"\n👥 Активны сегодня: <b>{active[1]}</b>\n"
                f"За 7 календарных дней: {active[7]} · за 30: {active[30]}\n\n"
                f"Впервые наблюдались за период: {data['observed']}\n"
                f"Из них зарегистрировались при наблюдаемом /start: {data['registered']}\n\n"
                "Активность — личный /start, допущенные взаимодействия и собственные игровые действия. "
                "Владелец бота не учитывается. Пассивная награда за друга не создаёт визит."
            )
        elif section == "steps":
            text += f"\n<b>Ключевые шаги</b>\nСреди {data['observed']} участников, впервые наблюдаемых в выбранном периоде:\n"
            text += "\n".join(
                f"• {html.escape(title)}: {value} · {ratio(value, data['observed'])}" for title, value in data["steps"]
            )
            text += "\n\nДоля показывает охват шага среди этих участников. Шаги могут проходиться в разном порядке."
        else:
            text += "\n<b>Возвращения D1 / D7</b>\nКогорты первого наблюдаемого дня за последние 30 дней.\n"
            for lag in (1, 7):
                r = data["retention"][lag]
                text += (
                    f"\nD{lag}: {r['returned']}/{r['eligible']} · {ratio(r['returned'], r['eligible'])}"
                    if r["eligible"]
                    else f"\nD{lag}: пока нет созревших когорт"
                )
            text += (
                "\n\nD1 — взаимодействие ровно на следующий календарный день; D7 — ровно через семь дней. "
                "Текущий незакрытый день проверки в знаменатель не включается. "
                "Участники, зарегистрированные раньше установки, входят по первому наблюдаемому дню."
            )
        text += (
            "\n\n<i>Считаются локальные наблюдения. Доставка ответов Telegram и доход здесь не измеряются. "
            "При сбоях сбора данные могут быть неполными.</i>"
        )
    keyboard = ikb(
        [
            btn("Обзор", request(bot_id, "overview", days)),
            btn("Шаги", request(bot_id, "steps", days)),
            btn("Возвращения", request(bot_id, "retention", days)),
        ],
        [btn("7 дней", request(bot_id, section, 7)), btn("30 дней", request(bot_id, section, 30))]
        if section != "retention"
        else [],
        [btn("🔄 Обновить", request(bot_id, section, days))],
        [btn("📊 Сводка по базе", f"bst:{bot_id}")],
        [btn("⬅️ К боту", f"b:{bot_id}:open")],
    )
    return text, keyboard
