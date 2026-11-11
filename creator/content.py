"""Owner-bound access to child content and independent persistent Creator forms."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from child.clicker_events import FIELDS as EVENT_FIELDS
from child.clicker_events import SCHEMA as EVENT_SCHEMA
from child.clicker_events import ClickerEvents
from child.db import ChildDB
from child.referral_club import SCHEMA as CLUB_SCHEMA
from child.referral_club import WIZARD_FIELDS, ReferralClub, validate_field
from child.referral_teams import SCHEMA as TEAM_SCHEMA
from child.referral_teams import ReferralTeams
from child.star_journey import EDITOR_FIELDS, StarJourney
from child.star_journey import SCHEMA as STAR_SCHEMA
from child.star_tracks import FIELDS as TRACK_FIELDS
from child.star_tracks import SCHEMA as TRACK_SCHEMA
from child.star_tracks import StarTracks
from creator.bot_setup import MAX_JSON, TOKEN_LIKE, _read_object

KINDS = {"r": "refbot", "s": "starbot", "c": "clicker", "t": "starbot"}
CODES = {"refbot": "r", "starbot": "s", "clicker": "c"}
SCHEMAS = {"r": CLUB_SCHEMA, "s": STAR_SCHEMA, "c": EVENT_SCHEMA, "t": TRACK_SCHEMA}
ENGINES = {"r": ReferralClub, "s": StarJourney, "c": ClickerEvents, "t": StarTracks}
FORM_FIELDS = {"r": WIZARD_FIELDS, "s": EDITOR_FIELDS, "c": EVENT_FIELDS, "t": TRACK_FIELDS}
FORM_SCHEMA = """CREATE TABLE IF NOT EXISTS creator_content_forms (
    owner_id INTEGER PRIMARY KEY,
    bot_id INTEGER NOT NULL,
    template TEXT NOT NULL,
    target_id INTEGER,
    target_revision INTEGER,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    edit_field INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)"""
UNAVAILABLE = "Контент недоступен. Проверьте владение ботом и его конфигурацию."


class ContentError(Exception):
    """Safe user-facing failure without files, tokens or provider errors."""


def encode(number):
    if type(number) is not int or not 0 <= number <= 2**63 - 1:
        raise ContentError("Кнопка устарела. Откройте контент заново.")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while number:
        number, digit = divmod(number, 36)
        result = digits[digit] + result
    return result or "0"


def decode(value):
    if not re.fullmatch(r"(?:0|[1-9a-z][0-9a-z]{0,12})", value):
        raise ContentError("Кнопка устарела. Откройте контент заново.")
    result = int(value, 36)
    if result > 2**63 - 1:
        raise ContentError("Кнопка устарела. Откройте контент заново.")
    return result


def callback(row, action, *, code=None):
    code = code or CODES[row["template"]]
    if KINDS.get(code) != row["template"]:
        raise ContentError("Откройте каталог заново.")
    value = f"cnt:{encode(row['id'])}:{code}:{action}"
    if len(value.encode()) > 64:
        raise ContentError("Откройте список контента заново.")
    return value


def _identity(row):
    return row["id"], row["owner_id"], row["template"], row["token"], row["username"]


def _checked_path(ctx, row):
    base = ctx.cfg.bots_dir.resolve()
    path = ctx.sup.bot_dir(row["id"])
    if path.is_symlink() or path.resolve() != base / str(row["id"]) or not path.is_dir():
        raise ContentError(UNAVAILABLE)
    for name in ("config.json", "bot.db", "bot.db-wal", "bot.db-shm", "bot.db-journal"):
        if (path / name).is_symlink():
            raise ContentError(UNAVAILABLE)
    config = _read_object(path / "config.json")
    if (
        not config
        or type(config.get("owner_id")) is not int
        or any(config.get(k) != row[k] for k in ("owner_id", "template", "token", "username"))
        or ("bot_id" in config and (type(config["bot_id"]) is not int or config["bot_id"] != row["id"]))
    ):
        raise ContentError(UNAVAILABLE)
    return path


def _guard(ctx, row, uid):
    latest = ctx.db.bot(row["id"])
    if not latest or latest["owner_id"] != uid or _identity(latest) != _identity(row):
        raise ContentError(UNAVAILABLE)
    return _checked_path(ctx, latest)


@contextmanager
def open_content(ctx, uid, bot_id, code, *, write=False, prepare=False):
    """No awaits inside: creator ownership is locked until child operation finishes."""
    db = None
    try:
        with ctx.db.tx() if write else ctx.db.owned_bot(bot_id, uid):
            row = ctx.db.bot(bot_id)
            if not row or row["owner_id"] != uid or KINDS.get(code) != row["template"]:
                raise ContentError(UNAVAILABLE)
            row = dict(row)
            path = _guard(ctx, row, uid)
            file = path / "bot.db"
            ready = False
            if file.exists():
                if not file.is_file():
                    raise ContentError(UNAVAILABLE)
                db = ChildDB.open_existing(file, writable=write)
                schema = SCHEMAS[code]
                required = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
                present = {r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
                ready = required <= present
                if not ready and required & present:
                    raise ContentError("Версия каталога не поддерживается. Нужна сверка установленного пакета.")
            if prepare and not ready:
                if not write:
                    raise ContentError(UNAVAILABLE)
                if code == "t" and (not db or not db.one("SELECT 1 FROM sqlite_master WHERE name='star_cards'")):
                    raise ContentError("Сначала подготовьте каталог карточек в «Созвездиях».")
                if db:
                    db.conn.close()
                db = ChildDB(file)

            def owner_check(actor):
                if actor != uid:
                    return False
                _guard(ctx, row, uid)
                return True

            engine = None
            if ready or prepare:
                cls = ENGINES[code]
                engine = cls(db, owner_id=uid, owner_check=owner_check, initialize=not ready)
            yield Content(ctx, row, uid, db, engine, code)
            _guard(ctx, row, uid)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        raise ContentError("Данные контента временно недоступны. Попробуйте открыть каталог снова.") from None
    finally:
        if db:
            db.conn.close()


class Content:
    def __init__(self, ctx, row, uid, db, engine, code):
        self.ctx, self.row, self.uid, self.db, self.engine, self.code = ctx, row, uid, db, engine, code

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def team_catalog(self, *, prepare=False):
        if self.code != "r" or not self.engine:
            raise ContentError("Команды доступны в подготовленном реферальном клубе.")
        self.engine._owner(self.uid)
        required = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", TEAM_SCHEMA))
        present = {row[0] for row in self.db.all("SELECT name FROM sqlite_master WHERE type='table'")}
        ready = required <= present
        if not ready and required & present:
            raise ContentError("Версия команд не поддерживается. Нужна сверка установленного пакета.")
        if not ready and not prepare:
            return None

        def owner_check(actor):
            self.engine._owner(actor)
            return True

        return ReferralTeams(self.db, owner_id=self.uid, owner_check=owner_check, clock=self.now_value, initialize=not ready)

    @property
    def fields(self):
        return FORM_FIELDS[self.code]

    @property
    def proxy(self):
        return SimpleNamespace(club=self, sky=self, events=self, tracks=self)

    @property
    def form_table(self):
        return "creator_track_forms" if self.code == "t" else "creator_content_forms"

    def cb(self, action):
        return callback(self.row, action, code=self.code)

    def clean(self, value):
        if isinstance(value, str):
            if self.row["token"]:
                value = value.replace(self.row["token"], "[скрыто]")
            return TOKEN_LIKE.sub("[скрыто]", value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        return value

    def owner_data(self, cid):
        method = {"r": "owner_campaign", "s": "owner_card", "c": "owner_event", "t": "owner_track"}[self.code]
        return getattr(self.engine, method)(self.uid, cid)

    def owner_track(self, actor, tid):
        return self.clean(self.engine.owner_track(actor, tid))

    def list_tracks(self, actor, page=0, **kwargs):
        return self.clean(self.engine.list_tracks(actor, page, **kwargs))

    def sources(self, actor, page=0):
        return self.clean(self.engine.sources(actor, page))

    def selected(self, actor, cards):
        return self.clean(self.engine.selected(actor, cards))

    def track_pick(self, cid, version):
        with self.db.tx():
            form = self._form_version(version)
            if self.code != "t" or form["step"] != "cards":
                raise ContentError("Откройте шаг выбора этапов.")
            data = form["data"]
            chosen = data.get("cards", [])
            if cid in chosen:
                chosen.remove(cid)
            else:
                self.engine.source(self.uid, cid)
                if len(chosen) >= 12:
                    raise ContentError("В маршруте может быть не больше 12 этапов.")
                chosen.append(cid)
            data["cards"] = chosen
            self.db.exec(
                f"UPDATE {self.form_table} SET payload_json=?,version=version+1,updated_at=? WHERE owner_id=?",
                json.dumps(data, ensure_ascii=False),
                self.now_value(),
                self.uid,
            )

    def owner_event(self, actor, cid):
        return self.clean(self.engine.owner_event(actor, cid))

    def list_events(self, actor, page=0, **kwargs):
        return self.clean(self.engine.list_events(actor, page, **kwargs))

    def owner_campaign(self, actor, cid):
        return self.clean(self.engine.owner_campaign(actor, cid))

    def owner_card(self, actor, cid):
        return self.clean(self.engine.owner_card(actor, cid))

    def list_campaigns(self, actor, **kwargs):
        return self.clean(self.engine.list_campaigns(actor, **kwargs))

    def list_cards(self, actor, page=0):
        return self.clean(self.engine.list_cards(actor, page))

    def form_get(self):
        if not self.db or not self.db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", self.form_table):
            return None
        form = self.db.one(f"SELECT * FROM {self.form_table} WHERE owner_id=?", self.uid)
        if not form:
            return None
        if form["bot_id"] != self.row["id"] or form["template"] != self.row["template"]:
            raise ContentError("Сохранённая форма относится к другой версии бота.")
        result = dict(form)
        result["data"] = json.loads(result.pop("payload_json"))
        return result

    def wizard_get(self, actor):
        self.engine._owner(actor)
        return self.clean(self.form_get())

    editor_get = wizard_get

    def form_begin(self, cid=None, revision=None):
        with self.db.tx():
            self.engine._owner(self.uid)
            self.db.exec(FORM_SCHEMA.replace("creator_content_forms", self.form_table))
            old = self.form_get()
            if old:
                if cid is not None and old["target_id"] != cid:
                    raise ContentError("Сначала завершите или удалите сохранённую форму через «Продолжить».")
                return old
            data, step = {}, self.fields[0]
            if cid is not None:
                c = self.owner_data(cid)
                if c["status"] != "draft" or c["revision"] != revision:
                    raise ContentError("Редактировать можно только актуальный неопубликованный черновик.")
                data, step = {field: self.clean(c[field]) for field in self.fields}, "confirm"
            self.db.exec(
                f"INSERT INTO {self.form_table}(owner_id,bot_id,template,target_id,target_revision,step,"
                "payload_json,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                self.uid,
                self.row["id"],
                self.row["template"],
                cid,
                revision,
                step,
                json.dumps(data, ensure_ascii=False),
                secrets.randbelow(2**52) + 1,
                self.now_value(),
            )
            return self.form_get()

    def now_value(self):
        return self.engine.now() if self.code == "s" else self.engine._now()

    def _form_version(self, version):
        self.engine._owner(self.uid)
        form = self.form_get()
        if not form or form["version"] != version:
            raise ContentError("Форма изменилась. Нажмите «Продолжить» в каталоге.")
        return form

    def check_text(self, value):
        text = json.dumps(value, ensure_ascii=False)
        if (self.row["token"] and self.row["token"] in text) or TOKEN_LIKE.search(text):
            raise ContentError("Удалите ключ доступа из текста перед сохранением или публикацией.")

    def form_field(self, field, version):
        with self.db.tx():
            form = self._form_version(version)
            if form["step"] != "confirm" or field not in self.fields:
                raise ContentError("Откройте предпросмотр формы заново.")
            self.db.exec(
                f"UPDATE {self.form_table} SET step=?,edit_field=1,version=version+1,updated_at=? WHERE owner_id=?",
                field,
                self.now_value(),
                self.uid,
            )

    def form_input(self, value, version):
        with self.db.tx():
            form = self._form_version(version)
            field, data = form["step"], form["data"]
            if field not in self.fields:
                raise ContentError("Выберите действие в предпросмотре.")
            self.check_text(value)
            data[field] = validate_field(field, value) if self.code == "r" else self.engine.validate_field(field, value)
            if self.code == "r" and "goal" in data and "quota" in data and data["goal"] > data["quota"]:
                raise ContentError("Лимит подтверждений должен быть не меньше цели.")
            index = self.fields.index(field) + 1
            step = self.fields[index] if index < len(self.fields) and not form["edit_field"] else "confirm"
            payload = json.dumps(data, ensure_ascii=False)
            if len(payload.encode()) > MAX_JSON:
                raise ContentError("Текст слишком большой.")
            self.db.exec(
                f"UPDATE {self.form_table} SET step=?,payload_json=?,edit_field=0,version=version+1,updated_at=? "
                "WHERE owner_id=?",
                step,
                payload,
                self.now_value(),
                self.uid,
            )

    def form_save(self, version):
        with self.db.tx():
            form = self._form_version(version)
            if form["step"] != "confirm":
                raise ContentError("Сначала завершите все шаги формы.")
            self.check_text(form["data"])
            cid = form["target_id"]
            if cid is None:
                cid = self.engine.create_draft(self.uid, form["data"])
            else:
                self.engine.update_draft(self.uid, cid, form["target_revision"], form["data"])
            self.db.exec(f"DELETE FROM {self.form_table} WHERE owner_id=?", self.uid)
            return cid

    def form_discard(self, version):
        with self.db.tx():
            self._form_version(version)
            self.db.exec(f"DELETE FROM {self.form_table} WHERE owner_id=?", self.uid)
