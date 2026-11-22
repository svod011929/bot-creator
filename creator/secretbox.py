"""Encrypt short owner secrets at rest. The key never belongs in the public repo or child env."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("secretbox")

KEY_FILE_NAME = ".owner_payout_key"
_ENV = "OWNER_PAYOUT_KEY"


class SecretBoxError(Exception):
    pass


def _as_fernet_key(raw: str) -> bytes:
    value = (raw or "").strip().encode()
    if not value:
        raise SecretBoxError("missing key")
    try:
        decoded = base64.urlsafe_b64decode(value)
        if len(decoded) == 32:
            return value
    except Exception:  # noqa: BLE001
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest())


def _read_env_or_file(data_dir: Path, explicit: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    env = os.environ.get(_ENV, "").strip()
    if env:
        return env
    path = data_dir / KEY_FILE_NAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def ensure_key(data_dir: Path, explicit: str = "") -> str:
    """Return the key material, creating a file-backed key if none exists."""
    found = _read_env_or_file(data_dir, explicit)
    if found:
        return found
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / KEY_FILE_NAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        generated = Fernet.generate_key().decode()
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)
        log.info("created owner payout key file")
        return generated
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def box_for(data_dir: Path, explicit: str = "") -> Fernet:
    return Fernet(_as_fernet_key(ensure_key(data_dir, explicit)))


def encrypt(fernet: Fernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode())


def decrypt(fernet: Fernet, blob: bytes) -> str:
    try:
        return fernet.decrypt(blob).decode()
    except InvalidToken as exc:
        raise SecretBoxError("undecipherable") from exc
