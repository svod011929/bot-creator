"""Draft, comparison and participant preview screens for the presentation studio."""

from __future__ import annotations

import html

from child.presentation import LABELS, LIMITS, PRESETS, TITLES, guide
from creator.bot_setup import _read_object, bot_url
from creator.content import callback as content_callback
from creator.content import encode
from creator.keyboards import btn, ikb, url_btn
from creator.presentation import PresentationError


def e(value, limit=1200):
    text = str(value)
    return html.escape(text if len(text) <= limit else text[: limit - 1] + "…")


def button(studio, text, action):
    return btn(text, studio.cb(action))


def home(studio):
    raw, draft = studio.raw_settings(), studio.draft()
    text = f"🎨 <b>Сценарий и оформление · бот {studio.row['id']}</b>\n"
    text += (
        "Настройте приветствие, игровой раздел и карточку первых шагов. Сначала проверьте предпросмотр, затем примените вариант."
    )
    rows = []
    if draft:
        status, v = studio.status(draft, raw), encode(draft["version"])
        labels = {
            "draft": "📝 Есть сохранённый черновик.",
            "applied": "✅ Этот вариант уже применён.",
            "conflict": "⚠️ Действующие тексты изменились после начала черновика.",
            "changed_after_apply": "✅ Вариант применялся, затем оформление изменили другим способом.",
            "stale": "⚠️ Черновик относится к прежней конфигурации бота.",
        }
        text += "\n\n" + labels[status]
        if not draft["stale"]:
            values = studio.clean(draft["values"])
            selected = PRESETS[studio.template].get(values["launch_scene"].removeprefix("off:"))
            if selected:
                text += "\nСценарий первых шагов: " + e(selected["title"], 80)
            for key in studio.keys:
                if key in LABELS:
                    text += f"\n<b>{LABELS[key]}:</b> {e(values[key], 60)}"
            rows += [
                [
                    button(studio, "👁 Приветствие", f"preview:greeting:{v}"),
                    button(studio, "👁 Игровой раздел", f"preview:section:{v}"),
                ],
                [button(studio, "👁 Первый шаг", f"preview:guide:{v}")],
            ]
            if status in {"draft", "conflict"}:
                for key in studio.keys:
                    if key in LABELS:
                        rows.append([button(studio, "✏️ " + LABELS[key], f"field:{key}:{v}")])
                enabled = values["launch_scene"] in PRESETS[studio.template]
                rows.append(
                    [button(studio, "🚀 Первый шаг: " + ("включён" if enabled else "выключен"), f"toggle:{int(not enabled)}:{v}")]
                )
                if status == "draft":
                    rows.append([button(studio, "✅ Проверить и применить", f"confirm:{v}")])
                else:
                    rows.append([button(studio, "🔄 Сверить свежие настройки", f"refresh_view:{v}")])
            if "about" in values:
                rows.append([button(studio, "👁 О боте", f"preview:about:{v}")])
        rows.append(
            [
                button(
                    studio,
                    "Закрыть редактор" if status in {"applied", "changed_after_apply"} else "🗑 Удалить черновик",
                    f"discard:{v}",
                )
            ]
        )
    else:
        text += "\n\nВыберите готовый сценарий или возьмите действующее оформление за основу."
    rows += [
        [button(studio, "🎭 Выбрать сценарий / новый черновик", "presets")],
        [button(studio, "👁 Действующее приветствие", "current:greeting")],
        [btn("📚 Контент бота", content_callback(studio.row, "home"))],
    ]
    url = bot_url(studio.row)
    if url:
        rows.append([url_btn("🔗 Открыть дочернего бота", url)])
    text += "\n\nЧтобы участники увидели события или маршруты, подготовьте и опубликуйте контент в соответствующем разделе."
    text += "\nПрименение сохраняет оформление. Запуск процесса и проверка ответа Telegram выполняются отдельно."
    rows.append([btn("🤖 Карточка бота", f"b:{studio.row['id']}:open")])
    return text, ikb(*rows)


def presets(studio):
    draft = studio.draft()
    v = encode(draft["version"] if draft else 0)
    text = (
        "🎭 <b>Сценарий запуска</b>\nТексты и первые шаги для выбранного шаблона. "
        "Выбор заменит тексты черновика; действующее оформление изменится после отдельного применения."
    )
    rows = [[button(studio, p["title"], f"choose:{key}:{v}")] for key, p in PRESETS[studio.template].items()]
    return text, ikb(
        *rows,
        [button(studio, "📝 Взять действующее оформление", f"choose:current:{v}")],
        [button(studio, "⬅️ Оформление", "home")],
    )


def preview(studio, tab, version=None):
    current = version is None
    values = studio.effective(studio.raw_settings()) if current else studio._version(version)["values"]
    values = studio.clean(values)
    text = "👁 <b>Действующее оформление</b>" if current else "👁 <b>Предпросмотр черновика</b>"
    rows = []
    if tab == "greeting":
        text += "\n\n" + e(values["welcome"])
    elif tab == "section":
        title = TITLES[studio.template]
        text += f"\n\n<b>{e(values[title], 80)}</b>\n{e(values[title.replace('_title', '_intro')], 600)}"
        text += "\n\n<i>Здесь показаны название и описание. Личный прогресс участника появится в дочернем боте.</i>"
    elif tab == "about" and "about" in values:
        text += "\n\n" + e(values["about"], 700)
    elif tab == "guide":
        screen = guide(studio.template, values)
        if screen:
            text += "\n\n" + screen[0]
            if not current:
                rows += [
                    [button(studio, b.text, f"hint:{i}:{encode(version)}")]
                    for i, row in enumerate(screen[1].inline_keyboard)
                    for b in row
                ]
        else:
            text += "\n\nКарточка первого шага выключена. После приветствия останется обычное меню бота."
    else:
        raise PresentationError("Откройте предпросмотр заново.")
    if tab in {"greeting", "about"}:
        cfg = _read_object(studio.guard() / "config.json") or {}
        if not cfg.get("vip") and cfg.get("creator_username"):
            text += "\n\n<i>🤖 Бот создан в @" + e(studio.clean(str(cfg["creator_username"])), 50) + "</i>"
    text += "\n\n<i>Это предпросмотр. Игровые действия, записи прохождения и отправка участникам не выполняются.</i>"
    actions = ("greeting", "section", "guide")
    labels = ("Приветствие", "Раздел", "Первый шаг")
    rows.append(
        [
            button(studio, label, f"current:{name}" if current else f"preview:{name}:{encode(version)}")
            for name, label in zip(actions, labels, strict=True)
        ]
    )
    rows.append([button(studio, "⬅️ Оформление", "home")])
    return text, ikb(*rows)


def confirm(studio, version, *, refresh=False):
    d = studio.editable(version)
    raw = studio.raw_settings()
    if refresh:
        text = (
            "🔄 <b>Обновить основу черновика?</b>\nТексты черновика сохранятся. Основа будет взята из свежих настроек. "
            "Затем ещё раз проверьте предпросмотр: последующее применение заменит перечисленные поля."
        )
        action, label = "refresh", "🔄 Обновить основу"
    else:
        if studio.status(d, raw) != "draft":
            raise PresentationError("Действующее оформление изменилось. Сначала сверяйте свежие настройки.")
        text = "✅ <b>Применить оформление?</b>\nБудут заменены следующие текстовые поля:"
        action, label = "apply", "✅ Применить оформление"
    text += "\n" + "\n".join("• " + LABELS[key] for key in studio.keys if key in LABELS)
    text += "\n• Состояние карточки первого шага"
    v = encode(version)
    return text, ikb(
        [button(studio, "👁 Черновик", f"preview:greeting:{v}"), button(studio, "👁 Действующее", "current:greeting")],
        [button(studio, label, f"{action}:{v}")],
        [button(studio, "⬅️ Оформление", "home")],
    )


def field(studio, key, version):
    studio.editable(version)
    if key not in studio.keys or key not in LABELS:
        raise PresentationError("Поле оформления недоступно.")
    text = f"✏️ <b>{LABELS[key]}</b>\nОтправьте текст до {LIMITS[key]} символов. HTML будет показан как обычный текст."
    if key == "launch_text":
        text += "\nОтправьте «-», чтобы использовать подсказку сценария."
    text += "\n/cancel — выйти; черновик сохранится."
    return text, ikb([button(studio, "⏳ Позже", "home")])
