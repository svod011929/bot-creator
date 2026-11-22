"""Private source-key storage. Never log/format credentials or provider errors."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

PROVIDERS = ('tgrass', 'piarflow')
MAX_BYTES = 8192


class SourceError(Exception):
    """Only fixed, secret-safe messages may cross this boundary."""


@dataclass(frozen=True)
class Source:
    origin: str
    key: str = field(default='', repr=False)


def validate_key(value: str) -> str:
    if not isinstance(value, str):
        raise SourceError('Неверный формат ключа источника')
    key = value.strip()
    if not re.fullmatch(r'[A-Za-z0-9_./+=:-]{8,512}', key) or re.match(r'^\d+:', key):
        raise SourceError('Неверный формат ключа источника')
    return key


def read_object(path: Path) -> dict:
    """A missing file is distinct from an unreadable/invalid one. No symlink reads."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise SourceError('Источник недоступен: проверьте файл настройки') from None
    try:
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SourceError('Источник недоступен: проверьте файл настройки')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        obj = json.loads(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get('api_key'), str) or not obj['api_key'].strip():
            raise ValueError
        return obj
    except (OSError, ValueError, UnicodeError):
        raise SourceError('Источник недоступен: проверьте файл настройки') from None


def atomic_object(path: Path, obj: dict) -> None:
    """Commit point is os.replace; all possible reported failures precede commit."""
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='source-', suffix='.tmp', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(obj, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except (OSError, ValueError, TypeError):
        raise SourceError('Не удалось сохранить источник; прежняя настройка сохранена') from None
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass


class SourceStore:
    def __init__(self, cfg, provider: str):
        if provider not in PROVIDERS:
            raise SourceError('Неизвестная сеть')
        self.cfg, self.provider = cfg, provider
        self.directory = cfg.data_dir / 'sponsor_sources'
        self.manual_path = self.directory / f'{provider}.json'
        self.legacy_path = cfg.data_dir / f'{provider}_hub.json'

    @property
    def env_key(self) -> str:
        return (getattr(self.cfg, f'{self.provider}_hub_api_key') or '').strip()

    def _check_directory(self) -> None:
        if self.directory.is_symlink():
            raise SourceError('Источник недоступен: проверьте каталог настройки')

    def resolve(self) -> Source:
        if self.env_key:
            return Source('env', self.env_key)
        self._check_directory()
        obj = read_object(self.manual_path)
        if obj:
            if obj.get('version') != 1:
                raise SourceError('Неподдерживаемый формат источника')
            return Source('manual', validate_key(obj['api_key']))
        obj = read_object(self.legacy_path)
        return Source('auto', str(obj['api_key']).strip()) if obj else Source('unset')

    def revision(self) -> str:
        """Private optimistic revision, never placed in callbacks or UI."""
        self._check_directory()
        try:
            if self.manual_path.is_symlink():
                raise SourceError('Источник недоступен: проверьте файл настройки')
            if not self.manual_path.exists():
                return 'absent'
            # Hash is only internal CAS metadata, never a credential fingerprint in UI.
            fd = os.open(self.manual_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SourceError('Источник недоступен: проверьте файл настройки')
                raw = stream.read(MAX_BYTES + 1)
            return hashlib.sha256(raw).hexdigest()
        except OSError:
            raise SourceError('Источник недоступен: проверьте файл настройки') from None

    @contextmanager
    def _locked(self, expected: str):
        try:
            self._check_directory()
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.stat().st_mode & 0o077:
                raise SourceError('Каталог источников должен быть приватным (0700)')
            fd = os.open(self.directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.env_key:
                    raise SourceError('Ключ задан на сервере; изменение через бот недоступно')
                if expected != self.revision():
                    raise SourceError('Источник изменился; откройте настройку заново')
                yield
        except OSError:
            raise SourceError('Не удалось изменить источник; проверьте права настройки') from None

    def save(self, key: str, expected: str) -> None:
        key = validate_key(key)
        with self._locked(expected):
            atomic_object(self.manual_path, {'version': 1, 'api_key': key, 'revision': secrets.token_hex(16)})

    def remove(self, expected: str) -> None:
        with self._locked(expected):
            self.manual_path.unlink(missing_ok=True)


def configured(cfg, provider: str) -> bool:
    factory = cfg.tgrass_factory_token if provider == 'tgrass' else cfg.piarflow_token
    if factory:
        return True
    try:
        return bool(SourceStore(cfg, provider).resolve().key)
    except SourceError:
        return False
