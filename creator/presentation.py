"""Owner-bound presentation drafts and a value-checked atomic settings bundle."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager

from child import templates
from child.files import _save_settings, settings_lock
from child.presentation import LABELS, LIMITS, PRESETS, TEXT_FIELDS, preset_values
from creator.bot_setup import MAX_JSON, TOKEN_LIKE
from creator.content import ContentError, _guard, _identity, encode

SCHEMA = """CREATE TABLE IF NOT EXISTS presentation_drafts (
    owner_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    base_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    version INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id,bot_id)
)"""


class PresentationError(Exception):
    """Safe owner message without source paths, config payloads or tokens."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def callback(bid, action):
    value = f"brand:{encode(bid)}:{action}"
    if len(value.encode()) > 64:
        raise PresentationError("Откройте оформление заново.")
    return value


@contextmanager
def open_presentation(ctx, uid, bid):
    try:
        with ctx.db.owned_bot(bid, uid):
            row = ctx.db.bot(bid)
            if not row or row["owner_id"] != uid or row["template"] not in TEXT_FIELDS:
                raise PresentationError("Оформление недоступно. Проверьте владение ботом.")
            studio = Presentation(ctx, uid, dict(row))
            studio.guard()
            yield studio
            studio.guard()
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ContentError):
        raise PresentationError("Оформление временно недоступно. Проверьте конфигурацию и откройте раздел снова.") from None


class Presentation:
    def __init__(self, ctx, uid, row):
        self.ctx, self.uid, self.row = ctx, uid, row
        self.template = row["template"]
        self.keys = (*TEXT_FIELDS[self.template], "launch_text", "launch_scene")
        self.identity = digest(_identity(row))

    def cb(self, action):
        return callback(self.row["id"], action)

    def _exec(self, sql, *args):
        return self.ctx.db.conn.execute(sql, args)

    def guard(self):
        path = _guard(self.ctx, self.row, self.uid)
        for name in ("settings.json", ".settings.lock"):
            if (path / name).is_symlink():
                raise PresentationError("Оформление недоступно. Нужна проверка файлов бота.")
        return path

    def raw_settings(self):
        path = self.guard() / "settings.json"
        try:
            if path.stat().st_size > MAX_JSON:
                raise PresentationError("Настройки слишком велики. Нужна сверка конфигурации.")
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}

        def invalid(_):
            raise ValueError("Non-finite JSON")

        def finite_float(raw):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("Non-finite JSON number")
            return value

        data = json.loads(text, parse_constant=invalid, parse_float=finite_float)
        if not isinstance(data, dict):
            raise PresentationError("Сохранённые настройки повреждены. Их нужно восстановить перед редактированием.")
        return data

    def base_hash(self, raw):
        return digest({key: raw[key] for key in self.keys if key in raw})

    def effective(self, raw):
        defaults = templates.info(self.template).defaults()
        return {key: raw.get(key, defaults.get(key, "")) for key in self.keys}

    def clean(self, value):
        if isinstance(value, str):
            if self.row["token"]:
                value = value.replace(self.row["token"], "[скрыто]")
            return TOKEN_LIKE.sub("[скрыто]", value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return {self.clean(k): self.clean(v) for k, v in value.items()} if isinstance(value, dict) else value

    def validate(self, key, value):
        if key == "launch_scene":
            selected = value.removeprefix("off:") if isinstance(value, str) else None
            if value != "" and (selected is None or selected not in PRESETS[self.template]):
                raise PresentationError("Выберите готовый сценарий для этого шаблона.")
            return value
        if key not in self.keys or key not in LABELS or not isinstance(value, str):
            raise PresentationError("Поле оформления недоступно.")
        value = value.strip()
        if not (0 if key == "launch_text" else 1) <= len(value) <= LIMITS[key]:
            raise PresentationError(f"Поле «{LABELS[key]}»: до {LIMITS[key]} символов, текст одной записи.")
        if (self.row["token"] and self.row["token"] in value) or TOKEN_LIKE.search(value):
            raise PresentationError("Удалите ключ доступа из текста перед сохранением.")
        return value

    def validated(self, values):
        if set(values) != set(self.keys):
            raise PresentationError("Черновик относится к другой версии оформления.")
        return {key: self.validate(key, values[key]) for key in self.keys}

    def draft(self):
        if not self.ctx.db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='presentation_drafts'"):
            return None
        row = self.ctx.db.one("SELECT * FROM presentation_drafts WHERE owner_id=? AND bot_id=?", self.uid, self.row["id"])
        if not row:
            return None
        d = dict(row)
        d["stale"] = d["identity_hash"] != self.identity
        d["values"] = json.loads(d.pop("values_json")) if not d["stale"] else {}
        return d

    def _version(self, version):
        self.guard()
        d = self.draft()
        if not d or d["stale"] or d["version"] != version:
            raise PresentationError("Черновик изменился. Откройте оформление заново.")
        return d

    def stamp(self, draft):
        return digest([draft["nonce"], draft["version"], draft["values"]])

    def status(self, draft, raw):
        if draft["stale"]:
            return "stale"
        if raw.get("_launch_applied") == self.stamp(draft):
            return "applied" if self.base_hash(raw) == digest(draft["values"]) else "changed_after_apply"
        return "draft" if self.base_hash(raw) == draft["base_hash"] else "conflict"

    def begin(self, preset, version):
        with self.ctx.db.tx():
            self.guard()
            old = self.draft()
            if (old["version"] if old else 0) != version:
                raise PresentationError("Черновик уже изменился. Откройте выбор сценария заново.")
            raw = self.raw_settings()
            if preset == "current":
                values = self.validated(self.effective(raw))
            else:
                if preset not in PRESETS[self.template]:
                    raise PresentationError("Выберите сценарий для этого шаблона.")
                values = self.validated(preset_values(self.template, preset))
            self._exec(SCHEMA)
            self._exec(
                "INSERT INTO presentation_drafts(owner_id,bot_id,identity_hash,base_hash,nonce,version,values_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,bot_id) DO UPDATE SET identity_hash=excluded.identity_hash,"
                "base_hash=excluded.base_hash,nonce=excluded.nonce,version=excluded.version,"
                "values_json=excluded.values_json,updated_at=excluded.updated_at",
                self.uid,
                self.row["id"],
                self.identity,
                self.base_hash(raw),
                secrets.token_hex(16),
                secrets.randbelow(2**52) + 1,
                json.dumps(values, ensure_ascii=False),
                int(time.time()),
            )
            return self.draft()

    def editable(self, version):
        d = self._version(version)
        if self.status(d, self.raw_settings()) in {"applied", "changed_after_apply"}:
            raise PresentationError("Этот вариант уже применялся. Начните новый черновик от текущего оформления.")
        return d

    def edit(self, key, value, version):
        with self.ctx.db.tx():
            d = self.editable(version)
            values = d["values"]
            values[key] = self.validate(key, value)
            self._exec(
                "UPDATE presentation_drafts SET values_json=?,version=version+1,updated_at=? WHERE owner_id=? AND bot_id=?",
                json.dumps(values, ensure_ascii=False),
                int(time.time()),
                self.uid,
                self.row["id"],
            )
            return self.draft()

    def refresh(self, version):
        with self.ctx.db.tx():
            self.editable(version)
            self._exec(
                "UPDATE presentation_drafts SET base_hash=?,version=version+1,updated_at=? WHERE owner_id=? AND bot_id=?",
                self.base_hash(self.raw_settings()),
                int(time.time()),
                self.uid,
                self.row["id"],
            )
            return self.draft()

    def discard(self, version):
        with self.ctx.db.tx():
            d = self.draft()
            if not d or d["version"] != version:
                raise PresentationError("Черновик изменился. Откройте оформление заново.")
            self._exec("DELETE FROM presentation_drafts WHERE owner_id=? AND bot_id=?", self.uid, self.row["id"])

    def apply(self, version):
        # No SQLite acknowledgement is needed after replace: the persisted draft and file stamp make retries idempotent.
        with self.ctx.db.tx():
            d = self._version(version)
            values = self.validated(d["values"])
            path = self.guard()
            with settings_lock(path):
                raw = self.raw_settings()
                status = self.status(d, raw)
                if status == "applied":
                    return False
                if status != "draft":
                    raise PresentationError(
                        "Действующее оформление изменилось. Сверьте предпросмотр и обновите основу черновика."
                    )
                self.guard()
                merged = {**raw, **values, "_launch_applied": self.stamp(d)}
                if len(json.dumps(merged, ensure_ascii=False, indent=2).encode()) > MAX_JSON:
                    raise PresentationError("Обновлённые настройки слишком велики. Нужна сверка конфигурации.")
                _save_settings(path, merged)
            return True
