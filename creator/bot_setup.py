"""Read-only first-launch facts and navigation over existing template settings."""
from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from aiogram.types import InlineKeyboardMarkup

from child import templates
from child.subscription import INVITE_URL, PUBLIC_URL, parse_channel
from creator.keyboards import btn, ikb, url_btn
from creator.services import Ctx

APPEARANCE = frozenset({"welcome", "currency", "about", "adventure_title", "adventure_intro"})
CHANNEL_KEYS = frozenset({"required_channel", "channel_url"})
MAX_JSON = 256 * 1024
TOKEN_LIKE = re.compile(r"\b[0-9]{5,15}:[A-Za-z0-9_-]{20,}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}", re.ASCII)
DISCLAIMER = (
    "Это сведения конфигурации и процесса, не проверка ответа Telegram. "
    "Приветствие и единицу можно оставить по умолчанию. Собственный канал необязателен."
)


@dataclass(frozen=True)
class Snapshot:
    config_ok: bool
    values: dict | None
    stored: dict | None
    editable: bool


def _read_object(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_JSON:
            return None
        with path.open(encoding="utf-8") as source:
            text = source.read(MAX_JSON + 1)
        if len(text) > MAX_JSON:
            return None
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def load_snapshot(ctx: Ctx, row) -> Snapshot:
    path = ctx.sup.bot_dir(row["id"])
    config = _read_object(path / "config.json")
    config_ok = bool(config and all(config.get(key) == row[key] for key in ("template", "owner_id", "token", "username"))
                     and ("bot_id" not in config or type(config["bot_id"]) is int and config["bot_id"] == row["id"]))
    stored = _read_object(path / "settings.json") if config_ok else None
    values = None if stored is None else {
        st.key: stored.get(st.key, st.default) for st in templates.info(row["template"]).settings
    }
    # Existing setters create an absent settings file on explicit first edit.
    # Missing is not corrupt/unreadable, and is never presented as confirmed data.
    missing = False
    if config_ok and stored is None:
        try:
            (path / "settings.json").stat()
        except FileNotFoundError:
            missing = True
        except OSError:
            pass
    return Snapshot(config_ok, values, stored, config_ok and (stored is not None or missing))


def callback(row, action: str) -> str:
    return f"setup:{row['id']}:{row['template']}:{action}"


def preview(value, secret: str) -> str:
    text = str(value)
    if secret:
        text = text.replace(secret, "[скрыто]")
    text = TOKEN_LIKE.sub("[скрыто]", text)
    text = " ".join(text.split())
    if len(text) > 72:
        text = text[:71] + "…"
    return html.escape(text)


def valid_value(setting, value) -> bool:
    if setting.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return False
        if setting.kind == "int" and not isinstance(value, int):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return isinstance(value, str) and bool(value.strip())


def field_text(row, snapshot: Snapshot, setting) -> str:
    if snapshot.values is None:
        return "недоступно"
    value = snapshot.values[setting.key]
    if not valid_value(setting, value):
        return "недоступно"
    source = "по умолчанию" if value == setting.default else "сохранено"
    return f"{preview(value, row['token'])} ({source})"


def _channel_text(snapshot: Snapshot) -> str:
    if snapshot.values is None:
        return "не подтверждён"
    channel = snapshot.values.get("required_channel")
    url = snapshot.values.get("channel_url")
    if channel == "":
        return "выключен (необязательно)"
    if isinstance(channel, str) and isinstance(url, str):
        try:
            parse_channel(channel)
        except ValueError:
            return "не подтверждён"
        if PUBLIC_URL.fullmatch(url.strip()) or INVITE_URL.fullmatch(url.strip()):
            return "указан; права здесь не проверялись"
    return "не подтверждён"


def bot_url(row) -> str | None:
    name = row["username"]
    if isinstance(name, str) and USERNAME.fullmatch(name) and name.lower().endswith("bot"):
        return f"https://t.me/{name}"
    return None


def group_settings(info, group: str):
    return [st for st in info.settings if st.key not in CHANNEL_KEYS
            and ((st.key in APPEARANCE) == (group == "appearance"))]


def view(ctx: Ctx, row, action: str, arg: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    info = templates.info(row["template"])
    snap = load_snapshot(ctx, row)
    heading = "Первый запуск" if action == "ready" else "Настройки бота"
    lines = [f"<b>{heading} · бот ID {row['id']}</b>", f"Шаблон: {html.escape(info.title)}"]
    rows = []
    if action == "group":
        lines.append("\nОформление" if arg == "appearance" else "\nНаграды и вывод")
        for st in group_settings(info, arg):
            lines.append(f"• {html.escape(st.label)}: {field_text(row, snap, st)}")
            if snap.editable:
                rows.append([btn(st.label, callback(row, "edit:" + st.key))])
        rows.append([btn("⬅️ Все настройки", callback(row, "settings"))])
    else:
        if action == "ready":
            process = "запущен" if ctx.sup.is_running(row["id"]) else "не запущен"
            status = {"running": "запущен", "stopped": "остановлен", "restarting": "перезапускается",
                      "error": "ошибка"}.get(row["status"], "неизвестно")
            lines.extend((f"Процесс: {process} (по супервизору)", f"Запись состояния: {status}",
                          "Конфигурация: " + ("соответствует записи" if snap.config_ok else "недоступна или не подтверждена")))
            url = bot_url(row)
            lines.append("Ссылка: " + ("доступна" if url else "недоступна"))
            if url:
                rows.append([url_btn("Открыть бота", url)])
        if snap.values is None:
            lines.append("Сохранённые настройки недоступны или не подтверждены.")
        for key, label in (("welcome", "Приветствие"), ("currency", "Единица баланса")):
            setting = info.setting(key)
            if setting:
                lines.append(f"{label}: {field_text(row, snap, setting)}")
                if action == "ready" and snap.editable:
                    rows.append([btn(label, callback(row, "edit:" + key))])
        lines.append("Канал: " + _channel_text(snap))
        if action == "settings":
            rows.extend(([btn("Оформление", callback(row, "group:appearance"))],
                         [btn("Награды и вывод", callback(row, "group:rules"))]))
        rows.append([btn("Обязательная подписка", callback(row, "channel"))])
        if action == "ready":
            lines.extend(("", DISCLAIMER))
            rows.append([btn("Все настройки", callback(row, "settings"))])
        else:
            rows.append([btn("Первый запуск", callback(row, "ready"))])
    rows.append([btn("⬅️ К карточке", callback(row, "card"))])
    text = "\n".join(lines)
    visible = html.unescape(re.sub(r"</?b>", "", text))
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("View too long")
    return text, ikb(*rows)
