"""Файлы дочернего бота: config.json (токен, владелец) и settings.json (тексты/награды).

Используется и конструктором, и самим дочерним ботом. Запись атомарная.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_config(bot_dir: Path, cfg: dict[str, Any]) -> None:
    _atomic_write(bot_dir / "config.json", cfg, 0o600)


def read_config(bot_dir: Path) -> dict[str, Any]:
    return read_json(bot_dir / "config.json")


def read_settings(bot_dir: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    data = dict(defaults)
    data.update({k: v for k, v in read_json(bot_dir / "settings.json").items() if k in defaults})
    return data


@contextmanager
def settings_lock(bot_dir: Path):
    """Stable inode shared by every creator/child settings writer."""
    bot_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(bot_dir / ".settings.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def strict_settings(bot_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((bot_dir / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid settings object")
    return data


def settings_pair(data: dict) -> tuple[str, str]:
    return str(data.get("required_channel") or ""), str(data.get("channel_url", "-"))


def _save_settings(bot_dir: Path, data: dict) -> None:
    path = bot_dir / "settings.json"
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    _atomic_write(path, data, mode)


def write_setting(bot_dir: Path, key: str, value: Any) -> None:
    with settings_lock(bot_dir):
        data = strict_settings(bot_dir)
        data[key] = value
        _save_settings(bot_dir, data)


def write_settings_pair(bot_dir: Path, *, expected: tuple[str, str], channel: str, url: str) -> None:
    with settings_lock(bot_dir):
        data = strict_settings(bot_dir)
        if settings_pair(data) != tuple(expected):
            raise ValueError("Settings changed")
        # Invalidate even an A->B->A change unseen by a child. Failure leaves old pair intact.
        _atomic_write(bot_dir / ".subscription-cache-revision", {"revision": secrets.token_hex(16)})
        data.update(required_channel=channel, channel_url=url)
        _save_settings(bot_dir, data)


def subscription_revision(bot_dir: Path) -> str:
    try:
        return (bot_dir / ".subscription-cache-revision").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def invalidate_subscription(bot_dir: Path) -> None:
    with settings_lock(bot_dir):
        _atomic_write(bot_dir / ".subscription-cache-revision", {"revision": secrets.token_hex(16)})


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"
