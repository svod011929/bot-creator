"""Creator content screens reuse the child editors with bot-bound callback data."""

from __future__ import annotations

import html
import re
from types import SimpleNamespace

from child import clicker_events_views as events
from child import referral_teams_views as teams
from child import referral_views as club
from child import star_tracks_views as tracks
from child import star_views as star
from creator.bot_setup import TOKEN_LIKE
from creator.content import ContentError, callback, encode
from creator.keyboards import btn, ikb


def e(value, limit=1000):
    return html.escape(str(value)[:limit])


def content_callback(content, action):
    return callback(content.row, action, code=content.code)


def bind(content, text, keyboard):
    text = text.replace("/cancel — удалить этот черновик мастера.", "/cancel — выйти; форма сохранится.")
    text = text.replace("/cancel — удалить незавершённую форму.", "/cancel — выйти; форма сохранится.")
    rows = []
    prefix = {"r": "club:", "s": "sky:", "c": "evt:", "t": "trk:"}[content.code]
    for row in keyboard.inline_keyboard:
        buttons = []
        for original in row:
            if not original.callback_data or not original.callback_data.startswith(prefix):
                raise ContentError("Откройте каталог заново.")
            parts = original.callback_data.removeprefix(prefix).split(":")
            if parts[0] in {"later", "discard"}:
                parts.append(str(content.form_get()["version"]))
            action = ":".join(encode(int(part)) if part.isascii() and part.isdigit() else part for part in parts)
            label = "📚 Контент бота" if parts[0] == "home" else original.text
            buttons.append(btn(label, content_callback(content, action)))
        rows.append(buttons)
    heading = f"📚 <b>Контент · бот {content.row['id']}</b>\n\n"
    # The proxy redacts full source values before the child view can truncate them.
    text = heading + text
    if content.row["token"]:
        text = text.replace(content.row["token"], "[скрыто]")
    text = TOKEN_LIKE.sub("[скрыто]", text)
    return bounded(text), ikb(*rows)


def bounded(text):
    visible = html.unescape(re.sub(r"</?(?:b|i|code)>", "", text))
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise ContentError("Экран слишком большой. Откройте каталог заново.")
    return text


def home(content):
    row = content.row
    text = f"📚 <b>Контент · бот {row['id']}</b>\n"
    rows = []
    if content.engine is None:
        text += (
            "Каталог ещё не подготовлен. Можно создать стартовые материалы и затем редактировать их здесь.\n"
            "Это не запускает бот и не оплачивает хостинг."
        )
        if content.code == "t":
            text += "\nМаршруты собираются из опубликованных карточек. Сначала подготовьте каталог карточек."
        if content.code != "t" or (content.db and content.db.one("SELECT 1 FROM sqlite_master WHERE name='star_cards'")):
            rows.append([btn("📚 Подготовить каталог", content_callback(content, "prepare"))])
    else:
        table = {"r": "ref_campaigns", "s": "star_cards", "c": "clicker_events", "t": "star_tracks"}[content.code]
        counts = {r[0]: r[1] for r in content.db.all(f"SELECT status,COUNT(*) FROM {table} GROUP BY status")}
        text += {
            "r": "🚀 Кампании реферального клуба",
            "s": "🌌 Карточки созвездий",
            "c": "🎉 События кликера",
            "t": "🧭 Тематические маршруты",
        }[content.code]
        text += (
            f"\nЧерновиков: {counts.get('draft', 0)} · опубликовано: {counts.get('active', 0)}"
            f" · на паузе: {counts.get('paused', 0)}\n\n"
            "Создайте материал, проверьте предпросмотр и сохраните черновик. Публикация подтверждается отдельно.\n"
            "Каталог общий с дочерним ботом; форма ввода в креаторе сохраняется отдельно."
        )
        rows += [
            [btn("📋 Каталог", content_callback(content, "admin:0")), btn("➕ Создать", content_callback(content, "new"))],
            [btn("📅 Календарь", content_callback(content, "calendar:0"))],
        ]
        if content.code == "r":
            rows.append([btn("🤝 Команды клуба", callback(row, "teams:0:0"))])
        if content.code == "s":
            rows.append([btn("🧭 Тематические маршруты", callback(row, "home", code="t"))])
        if content.form_get():
            text += "\n\n📝 В креаторе есть незавершённая форма."
            rows.append([btn("📝 Продолжить форму", content_callback(content, "resume"))])
        text += (
            "\n\nИгровые награды и денежный баланс учитываются отдельно. "
            "Публикация не отправляет рассылку. Статус процесса и доставка Telegram здесь не проверяются."
        )
    if content.code == "t":
        rows.append([btn("🌌 Каталог карточек", callback(row, "home", code="s"))])
    rows += [[btn("⬅️ Все боты с контентом", "content:all:0")], [btn("🤖 Карточка бота", f"b:{row['id']}:open")]]
    return bounded(text), ikb(*rows)


def catalog(content, page):
    if content.code == "r":
        screen = club.campaign_list(content.proxy, content.uid, page, owner=True)
    elif content.code == "c":
        screen = events.admin_list(content.proxy, content.uid, page)
    elif content.code == "t":
        screen = tracks.admin_list(content.proxy, content.uid, page)
    else:
        screen = star.admin_list(content.proxy, content.uid, page)
    return bind(content, *screen)


def team_screen(content, engine, *, tid=None, offset=0, page=0):
    if engine is None:
        return (
            "🤝 <b>Команды клуба</b>\nКаталог команд ещё не подготовлен. Подготовка создаёт только новую схему; "
            "игроки сами создадут команды и выберут состав.",
            ikb(
                [btn("🤝 Подготовить команды", callback(content.row, "team_prepare"))],
                [btn("📚 Контент бота", callback(content.row, "home"))],
            ),
        )
    proxy = SimpleNamespace(
        teams=SimpleNamespace(
            owner_list=lambda *a: content.clean(engine.owner_list(*a)),
            owner_team=lambda *a: content.clean(engine.owner_team(*a)),
            _now=engine._now,
        )
    )
    text, keyboard = (
        teams.owner_team(proxy, content.uid, tid) if tid is not None else teams.owner_list(proxy, content.uid, offset, page)
    )
    actions = {"admin": "teams", "manage": "team", "pause": "team_pause", "activate": "team_activate", "home": "home"}
    rows = []
    for row in keyboard.inline_keyboard:
        buttons = []
        for original in row:
            parts = original.callback_data.split(":")
            if parts[0] != "rt" or parts[1] not in actions:
                raise ContentError("Откройте команды заново.")
            action = ":".join([actions[parts[1]], *(encode(int(part)) for part in parts[2:])])
            buttons.append(btn(original.text, callback(content.row, action)))
        rows.append(buttons)
    return bounded(f"📚 <b>Контент · бот {content.row['id']}</b>\n\n" + text), ikb(*rows)


def owner_card(content, cid, *, confirm=False):
    if content.code == "r":
        screen = club.owner_card(content.proxy, content.uid, cid, confirm=confirm)
    elif content.code == "c":
        screen = events.owner_event_view(content.proxy, content.uid, cid, confirm=confirm)
    elif content.code == "t":
        screen = tracks.owner_track_view(content.proxy, content.uid, cid, confirm=confirm)
    else:
        screen = star.owner_card_view(content.proxy, content.uid, cid, confirm=confirm)
    text, keyboard = bind(content, *screen)
    keyboard.inline_keyboard.insert(0, [btn("👁 Как увидит участник", content_callback(content, f"preview:{encode(cid)}"))])
    return text, keyboard


def form(content):
    renderer = {"r": club.wizard_view, "s": star.editor_view, "c": events.editor_view, "t": tracks.editor_view}[content.code]
    return bind(content, *renderer(content.proxy, content.uid))


def preview(content, cid):
    if content.code == "t":
        return track_preview(content, cid)
    c = content.clean(content.owner_data(cid))
    text = f"👁 <b>Предпросмотр для участника</b>\n\n<b>{e(c['title'], 80)}</b>\n"
    if content.code == "r":
        text += (
            f"{e(c['description'], 900)}\n\nЦель: {c['goal']} подтверждений. "
            f"За подтверждённого друга: {c['reward']} клубных баллов.\nЛимит: {c['quota']} подтверждений.\n"
        )
    elif content.code == "s":
        text += f"{e(c['lesson'], 700)}\n\nНаграда: 15 игровых искр.\n"
    else:
        text = "👁 <b>Предпросмотр для участника</b>\n\n" + events.event_text(c)
    if content.code != "c":
        text += f"\n<b>{e(c['question'], 180)}</b>\n" + "\n".join(f"{i + 1}. {e(a, 80)}" for i, a in enumerate(c["answers"]))
    text += "\n\n<i>Только предпросмотр: прохождение не записывается, награды не начисляются.</i>"
    return bounded(text), ikb(
        [btn("⬅️ К материалу", content_callback(content, f"manage:{encode(cid)}"))],
        [btn("📚 Контент бота", content_callback(content, "home"))],
    )


def track_preview(content, tid, position=None):
    return bind(content, *tracks.preview(content.proxy, content.uid, tid, position))


def track_sources(content, page):
    return bind(content, *tracks.source_view(content.proxy, content.uid, page))


def calendar(content, page):
    if not 0 <= page <= 1000000:
        raise ContentError("Откройте календарь заново.")
    table = {"r": "ref_campaigns", "s": "star_cards", "c": "clicker_events", "t": "star_tracks"}[content.code]
    total = content.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE days>0")
    page = min(page, max(0, (total - 1) // 6))
    order = "CASE WHEN ends_at>0 THEN ends_at ELSE 9223372036854775807 END,id" if content.code == "r" else "start_day,id"
    records = content.db.all(f"SELECT * FROM {table} WHERE days>0 ORDER BY {order} LIMIT 6 OFFSET ?", page * 6)
    lines, rows = ["📅 <b>Календарь контента · МСК</b>", f"Страница {page + 1} · материалов со сроком: {total}"], []
    if content.code == "r":
        lines.append("Кампании начинаются после публикации. Здесь показаны сроки завершения и длительность черновиков.")
    elif content.code == "s":
        lines.append("Карточки доступны по расписанию после публикации. Личные маршруты участников на день уже закреплены.")
    elif content.code == "t":
        lines.append("Маршруты проходятся в указанный срок. Получить заработанный значок можно ещё семь суток после конца.")
    else:
        lines.append("Начало и конец включены в срок. После завершения есть 7 дней для получения заработанной награды.")
    for raw in records:
        c = content.clean(dict(raw))
        if content.code == "r":
            label = club.status(c, content.now_value())
            date = f"{c['days']} дн. после публикации" if c["status"] == "draft" else "До " + club.end_label(c)
        else:
            label = (
                (events.phase if content.code == "c" else tracks.phase)(c, content.now_value())
                if content.code in {"c", "t"}
                else star.card_status(c, star.period("day", content.now_value()))
            )
            date = f"С {star.date_label(c['start_day'])} · {c['days']} дн."
            if c["end_day"]:
                date += " · до " + star.date_label(c["end_day"])
        lines += ["", f"<b>{e(c['title'], 80)}</b> · {label}", date]
        rows.append([btn(c["title"][:55], content_callback(content, f"manage:{encode(c['id'])}"))])
    if not records:
        lines.append("\nМатериалов со сроком нет. Бессрочные стартовые материалы доступны в каталоге.")
    nav = []
    if page:
        nav.append(btn("⬅️", content_callback(content, f"calendar:{encode(page - 1)}")))
    if (page + 1) * 6 < total:
        nav.append(btn("➡️", content_callback(content, f"calendar:{encode(page + 1)}")))
    return bounded("\n".join(lines)), ikb(*rows, nav, [btn("📚 Контент бота", content_callback(content, "home"))])


def bot_list(ctx, uid, page=0):
    if not 0 <= page <= 1000000:
        raise ContentError("Откройте список заново.")
    where = "owner_id=? AND deleted=0 AND template IN ('refbot','starbot','clicker')"
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM bots WHERE {where}", uid)
    page = min(page, max(0, (total - 1) // 6))
    records = ctx.db.all(f"SELECT id,template,username FROM bots WHERE {where} ORDER BY id DESC LIMIT 6 OFFSET ?", uid, page * 6)
    text = (
        f"📚 <b>Контент моих ботов</b>\nСтраница {page + 1} · ботов: {total}\n\n"
        "Выберите кликер, клуб или созвездия для работы с материалами."
    )
    rows = []
    for row in records:
        name = row["username"] or ""
        label = "@" + name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name) else f"бот {row['id']}"
        title = {"refbot": "🚀 Клуб", "starbot": "🌌 Созвездия", "clicker": "🎉 События"}[row["template"]] + " · " + label
        rows.append([btn(title, callback(row, "home"))])
    if not records:
        text += "\n\nПока нет ботов с контентом. Создайте бот нужного шаблона."
    nav = []
    if page:
        nav.append(btn("⬅️", f"content:all:{encode(page - 1)}"))
    if (page + 1) * 6 < total:
        nav.append(btn("➡️", f"content:all:{encode(page + 1)}"))
    return text, ikb(*rows, nav, [btn("🤖 Мои боты", "mybots")])
