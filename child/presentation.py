"""Text-only launch presets and a pure first-step card shared by runtime and owner preview."""

from __future__ import annotations

import html
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

TZ = ZoneInfo("Europe/Moscow")
TEXT_FIELDS = {
    "clicker": ("welcome", "adventure_title", "adventure_intro"),
    "refbot": ("welcome", "club_title", "club_intro", "about"),
    "starbot": ("welcome", "sky_title", "sky_intro"),
}
TITLES = {"clicker": "adventure_title", "refbot": "club_title", "starbot": "sky_title"}
LABELS = {
    "welcome": "Приветствие",
    "adventure_title": "Название приключения",
    "adventure_intro": "Описание приключения",
    "club_title": "Название клуба",
    "club_intro": "Описание клуба",
    "about": "О боте",
    "sky_title": "Название созвездий",
    "sky_intro": "Описание созвездий",
    "launch_text": "Первый шаг",
}
LIMITS = {
    key: (1200 if key == "welcome" else 80 if key in TITLES.values() else 600 if key.endswith("_intro") else 700)
    for key in LABELS
}
PRESETS = {
    "clicker": {
        "explore": {
            "title": "Экспедиция исследователей",
            "welcome": "🧭 Добро пожаловать в экспедицию! "
            "Исследуй маршруты, выбирай навыки и собирай находки. Начни с первой миссии.",
            "heading": "Экспедиция исследователей",
            "intro": "Небольшие шаги открывают новые маршруты. "
            "Выполняй миссии, копи игровой XP и решай, какой навык развивать следующим.",
            "guide": "1. Сделай пять кликов и забери XP стартовой миссии.\n"
            "2. Отправься по первой тропе и вернись за находкой.\n3. Выбери навык и открой следующую цель.",
            "buttons": (("🧭 Открыть приключение", "adv:home"), ("🎯 Первая миссия", "adv:missions:story")),
        },
        "events": {
            "title": "Клуб испытаний",
            "welcome": "🎉 Добро пожаловать в клуб испытаний! "
            "Выбирай цели, участвуй в событиях и сохраняй значки своих достижений.",
            "heading": "Клуб испытаний",
            "intro": "Здесь встречаются личный прогресс и события владельца. "
            "Пока событие готовится, исследуй маршруты и выполняй миссии.",
            "guide": "1. Посмотри события: условия и срок указаны в карточке.\n"
            "2. Вступи, чтобы начать личный прогресс.\n3. Если событий пока нет, начни со стартовых миссий.",
            "buttons": (("🎉 События", "evt:home:0"), ("🎯 Миссии", "adv:missions:story")),
        },
    },
    "refbot": {
        "club": {
            "title": "Клуб амбассадоров",
            "welcome": "🚀 Добро пожаловать в клуб! Познакомься с правилами, выбери кампанию и помогай сообществу расти.",
            "heading": "Клуб амбассадоров",
            "intro": "Начни со знакомства и задания кампании. В клубе видны личные цели, шаги приглашённых друзей и достижения.",
            "guide": "1. Пройди короткое знакомство.\n2. Выбери кампанию и её задание.\n"
            "3. При желании пригласи заинтересованного друга; его шаги видны в клубе.",
            "buttons": (("👋 Знакомство", "club:onboard"), ("📣 Кампании", "club:list:0")),
        },
        "teams": {
            "title": "Сообщество команд",
            "welcome": "🤝 Добро пожаловать в сообщество команд! "
            "Объединяйся с участниками, выполняй недельную цель и собирай памятные значки.",
            "heading": "Сообщество команд",
            "intro": "Командный прогресс складывается из ежедневного участия, "
            "заданий и подтверждённых друзей. Недельную цель можно выполнить без приглашений.",
            "guide": "1. Пройди знакомство в клубе.\n2. Выбери команду на текущую неделю.\n"
            "3. Делай ежедневный вклад и выполняй задания кампаний; следи за общей целью.",
            "buttons": (("👋 Знакомство", "club:onboard"), ("🤝 Команды недели", "rt:list:0")),
        },
    },
    "starbot": {
        "academy": {
            "title": "Академия открытий",
            "welcome": "🧭 Добро пожаловать в академию открытий! "
            "Изучай темы по шагам, проверяй себя и сохраняй значки пройденных маршрутов.",
            "heading": "Академия открытий",
            "intro": "Тематические маршруты ведут от первого урока к итоговому значку. "
            "В созвездиях также доступны карточки дня, память и коллекция.",
            "guide": "1. Выбери опубликованный тематический маршрут.\n2. Проходи уроки по порядку.\n"
            "3. Если маршрутов пока нет, начни с карточек дня и карты созвездий.",
            "buttons": (("🧭 Тематические маршруты", "trk:list:0"), ("🌌 Созвездия", "sky:home")),
        },
        "focus": {
            "title": "Мастерская внимания",
            "welcome": "🧠 Добро пожаловать в мастерскую внимания! "
            "Тренируй память, открывай карточки знаний и отмечай свой прогресс.",
            "heading": "Мастерская внимания",
            "intro": "Короткая тренировка, новое знание и шаг на карте. "
            "Возвращайся к занятиям и собирай свою коллекцию созвездий.",
            "guide": "1. Запомни символы в тренировке памяти.\n2. Открой карточки дня.\n"
            "3. Проверь миссии и посмотри, какое созвездие можно зажечь следующим.",
            "buttons": (("🧠 Память", "sky:memory:{day}"), ("📚 Карточки дня", "sky:deck")),
        },
    },
}


def preset_values(template, key):
    p = PRESETS[template][key]
    title = TITLES[template]
    values = {
        "welcome": p["welcome"],
        title: p["heading"],
        title.replace("_title", "_intro"): p["intro"],
        "launch_scene": key,
        "launch_text": p["guide"],
    }
    if template == "refbot":
        values["about"] = (
            "Участвуй в кампаниях и жизни клуба. Клубные и командные баллы отражают участие; "
            "денежный баланс учитывается отдельно, заявки на выплату рассматривает владелец."
        )
    return values


def guide(template, values, *, at=None):
    scene = values.get("launch_scene")
    p = PRESETS.get(template, {}).get(scene) if isinstance(scene, str) else None
    if not p:
        return None
    title = values.get(TITLES[template])
    text = values.get("launch_text")
    title = (title if isinstance(title, str) and title.strip() else p["heading"])[:80]
    text = (text if isinstance(text, str) and text.strip() else p["guide"])[:700]
    day = datetime.fromtimestamp(time.time() if at is None else at, TZ).strftime("%Y%m%d")
    return (
        "🚀 <b>Первые шаги · " + html.escape(title) + "</b>\n\n" + html.escape(text),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=data.format(day=day))] for label, data in p["buttons"]
            ]
        ),
    )


def runtime_guide(app):
    try:
        path = app.dir / "settings.json"
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            return None
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            return None
        return guide(
            app.info.key,
            {**app.s, "launch_scene": values.get("launch_scene"), "launch_text": values.get("launch_text")},
            at=app.sky.now() if getattr(app, "sky", None) else None,
        )
    except (OSError, ValueError, TypeError):
        return None
