"""Конкурсы ботов: очки = пользователи + заработок (ОП) владельца."""
from __future__ import annotations

import html
import time
from typing import Any

from creator.tgrass import child_users_for_card


def _users_of(ctx, bot_id: int) -> int:
    n = child_users_for_card(ctx.sup.bot_dir(bot_id))
    return int(n or 0)


def _earnings_kop(ctx, bot_id: int) -> int:
    try:
        _tc, tk = ctx.db.tgrass_stats(bot_id, 0)
        _pc, pk = ctx.db.pf_stats(bot_id, 0)
        return int(tk or 0) + int(pk or 0)
    except (TypeError, ValueError, ArithmeticError, AttributeError):
        return 0


def score_owners(ctx) -> list[dict[str, Any]]:
    """Считает очки по владельцам: users + earnings_rub (копейки/100)."""
    bots = ctx.db.all("SELECT id, owner_id FROM bots WHERE deleted=0")
    by_owner: dict[int, dict] = {}
    for b in bots:
        oid = int(b["owner_id"])
        slot = by_owner.setdefault(oid, {"owner_id": oid, "users": 0, "earnings_kop": 0, "bots": 0})
        slot["users"] += _users_of(ctx, int(b["id"]))
        slot["earnings_kop"] += _earnings_kop(ctx, int(b["id"]))
        slot["bots"] += 1
    result = []
    for slot in by_owner.values():
        earnings_rub = slot["earnings_kop"] // 100
        slot["points"] = slot["users"] + earnings_rub
        slot["earnings_rub"] = earnings_rub
        result.append(slot)
    result.sort(key=lambda x: (-x["points"], -x["users"], x["owner_id"]))
    return result


def leaderboard_text(ctx, uid: int | None = None, limit: int = 10) -> str:
    contest = ctx.db.contest_active()
    if not contest:
        return "🏆 Сейчас нет активного конкурса."
    scores = score_owners(ctx)
    left = max(0, contest["ends_at"] - int(time.time()))
    days = left // 86400
    hours = (left % 86400) // 3600
    lines = [
        f"🏆 <b>{html.escape(str(contest['title']))}</b>",
        "",
        "💰 <b>Призовой фонд</b>",
        html.escape(str(contest["prize_text"])),
        "",
        f"⏳ Осталось: <b>{days} дн. {hours} ч.</b>",
        "",
        "Побеждает тот, чей бот наберёт больше очков:",
        "<b>очки = пользователи + заработок бота</b>",
        "",
        "<b>📊 Таблица лидеров</b>",
    ]
    if not scores:
        lines.append("Пока нет участников с ботами.")
    else:
        for i, s in enumerate(scores[:limit], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            name = f"ID {s['owner_id']}"
            u = ctx.db.user(s["owner_id"])
            if u and u["username"]:
                name = f"@{html.escape(u['username'])}"
            lines.append(
                f"{medal} {name} — <b>{s['points']}</b> "
                f"(польз. {s['users']} · заработок {s['earnings_rub']} ₽)"
            )
    if uid is not None:
        me = next((s for s in scores if s["owner_id"] == uid), None)
        if me:
            pos = scores.index(me) + 1
            lines += [
                "",
                f"Ваше место: <b>{pos}</b> · очки: <b>{me['points']}</b> "
                f"(польз. {me['users']} · заработок {me['earnings_rub']} ₽)",
            ]
        else:
            lines += ["", "У вас пока нет ботов в зачёте."]
    return "\n".join(lines)
