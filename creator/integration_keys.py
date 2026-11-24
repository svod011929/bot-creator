"""Каталог и хранение ключей интеграций. Полные значения не логируются и не попадают в UI."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from creator.secretbox import KEY_FILE_NAME
from creator.sponsor_sources import SourceError, SourceStore, atomic_object, read_object, validate_key

MAX_BYTES = 8192
DIR_NAME = "integration_keys"


class KeyErrorSafe(Exception):
    """Only fixed, secret-safe messages may cross this boundary."""


@dataclass(frozen=True)
class KeySpec:
    id: str
    title: str
    env_name: str
    cfg_attr: str
    editable: bool = True
    readonly_reason: str = ""
    kind: str = "json"  # json | sponsor | payout | env
    sponsor_provider: str = ""
    legacy_name: str = ""
    probe: str = ""  # lavepay | mgram_partner | none


# Editable first, then read-only status rows.
SPECS: tuple[KeySpec, ...] = (
    KeySpec("lavepay", "LavePay API", "LAVEPAY_API_KEY", "lavepay_api_key", probe="lavepay"),
    KeySpec("mgram_partner", "MGRAM Partner API", "MGRAM_PARTNER_API_KEY", "mgram_partner_api_key",
            probe="mgram_partner"),
    KeySpec("mgram_hub", "MGRAM хаб-бот (задания)", "MGRAM_HUB_API_KEY", "mgram_hub_api_key",
            legacy_name="mgram_hub.json"),
    KeySpec("tgrass_hub", "TGrass хаб (трансляция ОП)", "TGRASS_HUB_API_KEY", "tgrass_hub_api_key",
            kind="sponsor", sponsor_provider="tgrass"),
    KeySpec("piarflow_hub", "PiarFlow хаб (трансляция ОП)", "PIARFLOW_HUB_API_KEY", "piarflow_hub_api_key",
            kind="sponsor", sponsor_provider="piarflow"),
    KeySpec("tgrass_factory", "TGrass фабрика", "TGRASS_FACTORY_TOKEN", "tgrass_factory_token"),
    KeySpec("piarflow", "PiarFlow аккаунт", "PIARFLOW_TOKEN", "piarflow_token"),
    KeySpec("payout", "Ключ шифрования выплат владельцев", "OWNER_PAYOUT_KEY", "owner_payout_key",
            editable=False, kind="payout",
            readonly_reason="Смена этого ключа сделает сохранённые магазины владельцев нечитаемыми. "
                            "Он задаётся на сервере или создаётся автоматически."),
    KeySpec("bot_token", "Токен конструктора", "BOT_TOKEN", "bot_token",
            editable=False, kind="env",
            readonly_reason="Смена токена остановит бота. Меняется только на сервере."),
    KeySpec("lavepay_wh", "Секрет вебхука LavePay", "LAVEPAY_WEBHOOK_SECRET", "lavepay_webhook_secret",
            editable=False, kind="env",
            readonly_reason="Смена секрета сломает приём вебхуков. Меняется только на сервере."),
)

SPECS_BY_ID = {s.id: s for s in SPECS}


@dataclass(frozen=True)
class KeyStatus:
    spec: KeySpec
    origin: str  # env | admin | file | unset | error
    present: bool
    mask: str = ""
    key: str = field(default="", repr=False)


def spec(key_id: str) -> KeySpec:
    found = SPECS_BY_ID.get(key_id)
    if found is None:
        raise KeyErrorSafe("Неизвестный ключ")
    return found


def mask_tail(value: str) -> str:
    """At most the last 4 characters, and only when the secret is long enough."""
    text = (value or "").strip()
    if len(text) < 8:
        return ""
    return "…" + text[-4:]


def _env_value(cfg, item: KeySpec) -> str:
    return (getattr(cfg, item.cfg_attr, None) or "").strip()


def _directory(cfg) -> Path:
    return Path(cfg.data_dir) / DIR_NAME


def _manual_path(cfg, item: KeySpec) -> Path:
    return _directory(cfg) / f"{item.id}.json"


def _legacy_path(cfg, item: KeySpec) -> Path | None:
    if not item.legacy_name:
        return None
    return Path(cfg.data_dir) / item.legacy_name


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read_legacy(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        obj = read_object(path)
    except SourceError:
        # Tolerate {"key": "..."} in older hub files; never raise the raw body.
        if not path.is_file() or path.is_symlink():
            return ""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return ""
            obj = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            return ""
        if not isinstance(obj, dict):
            return ""
    return str(obj.get("api_key") or obj.get("key") or "").strip()


def _check_dir(path: Path) -> None:
    if path.is_symlink():
        raise KeyErrorSafe("Хранилище ключей недоступно: проверьте каталог")


def _backup_file(path: Path) -> None:
    """Copy the current file to *.bak (0600) before replace or remove. Missing is fine."""
    if not path.exists() or path.is_symlink() or not path.is_file():
        return
    dest = _backup_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл")
    tmp = None
    try:
        tfd, tmp = tempfile.mkstemp(prefix="key-bak-", suffix=".tmp", dir=path.parent)
        with os.fdopen(tfd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except OSError:
        raise KeyErrorSafe("Не удалось сохранить резервную копию ключа") from None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _origin_label(origin: str) -> str:
    return {
        "env": "сервер (env)",
        "admin": "админка",
        "file": "прежний файл",
        "unset": "не задан",
        "error": "не удалось прочитать",
    }.get(origin, origin)


def status_line(st: KeyStatus) -> str:
    if not st.present:
        body = "не задан" if st.origin != "error" else "не удалось прочитать"
        return f"{st.spec.title}: {body}"
    bits = ["задан", _origin_label(st.origin)]
    if st.mask:
        bits.append(st.mask)
    return f"{st.spec.title}: " + " · ".join(bits)


def resolve(cfg, key_id: str) -> KeyStatus:
    item = spec(key_id)
    try:
        if item.kind == "payout":
            env = _env_value(cfg, item) or (os.environ.get(item.env_name) or "").strip()
            path = Path(cfg.data_dir) / KEY_FILE_NAME
            if env:
                return KeyStatus(item, "env", True, mask_tail(env), env)
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
                # Read only to mask; never log. Do not create a key by viewing.
                try:
                    raw = path.read_text(encoding="utf-8").strip()
                except OSError:
                    return KeyStatus(item, "error", False)
                if raw:
                    return KeyStatus(item, "file", True, mask_tail(raw), raw)
            return KeyStatus(item, "unset", False)
        if item.kind == "sponsor":
            env = _env_value(cfg, item)
            if env:
                return KeyStatus(item, "env", True, mask_tail(env), env)
            try:
                source = SourceStore(cfg, item.sponsor_provider).resolve()
            except SourceError:
                return KeyStatus(item, "error", False)
            if not source.key:
                return KeyStatus(item, "unset", False)
            origin = "admin" if source.origin == "manual" else ("file" if source.origin == "auto" else source.origin)
            return KeyStatus(item, origin, True, mask_tail(source.key), source.key)
        env = _env_value(cfg, item)
        if env:
            return KeyStatus(item, "env", True, mask_tail(env), env)
        if item.kind == "env":
            return KeyStatus(item, "unset", False)
        try:
            obj = read_object(_manual_path(cfg, item))
        except SourceError:
            raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл") from None
        if obj:
            try:
                key = validate_key(obj["api_key"])
            except SourceError:
                raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл") from None
            return KeyStatus(item, "admin", True, mask_tail(key), key)
        legacy = _read_legacy(_legacy_path(cfg, item))
        if legacy:
            return KeyStatus(item, "file", True, mask_tail(legacy), legacy)
        return KeyStatus(item, "unset", False)
    except KeyErrorSafe:
        raise
    except SourceError:
        return KeyStatus(item, "error", False)


def value(cfg, key_id: str) -> str:
    try:
        return resolve(cfg, key_id).key
    except KeyErrorSafe:
        return ""


def revision(cfg, item: KeySpec) -> str:
    """Private optimistic revision, never shown in UI or callbacks."""
    if item.kind == "sponsor":
        try:
            return SourceStore(cfg, item.sponsor_provider).revision()
        except SourceError as exc:
            raise KeyErrorSafe(str(exc)) from None
    path = _manual_path(cfg, item)
    _check_dir(_directory(cfg))
    try:
        if path.is_symlink():
            raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл")
        if not path.exists():
            return "absent"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл")
            raw = stream.read(MAX_BYTES + 1)
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        raise KeyErrorSafe("Хранилище ключей недоступно: проверьте файл") from None


@contextmanager
def _locked(cfg, item: KeySpec, expected: str):
    if not item.editable:
        raise KeyErrorSafe("Этот ключ нельзя изменить через бота")
    if _env_value(cfg, item):
        raise KeyErrorSafe("Ключ задан на сервере; изменение через бот недоступно")
    directory = _directory(cfg)
    try:
        _check_dir(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        if directory.stat().st_mode & 0o077:
            raise KeyErrorSafe("Каталог ключей должен быть приватным (0700)")
        fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _env_value(cfg, item):
                raise KeyErrorSafe("Ключ задан на сервере; изменение через бот недоступно")
            if expected != revision(cfg, item):
                raise KeyErrorSafe("Ключ изменился; откройте настройку заново")
            yield None
    except KeyErrorSafe:
        raise
    except OSError:
        raise KeyErrorSafe("Не удалось изменить ключ; проверьте права") from None


def env_blocked(cfg, item: KeySpec) -> bool:
    return bool(_env_value(cfg, item))


def has_admin_file(cfg, item: KeySpec) -> bool:
    if not item.editable:
        return False
    if item.kind == "sponsor":
        return SourceStore(cfg, item.sponsor_provider).manual_path.exists()
    return _manual_path(cfg, item).is_file() and not _manual_path(cfg, item).is_symlink()


def save(cfg, key_id: str, key: str, expected: str) -> None:
    item = spec(key_id)
    try:
        key = validate_key(key)
    except SourceError:
        raise KeyErrorSafe("Неверный формат ключа") from None
    if item.kind == "sponsor":
        if _env_value(cfg, item):
            raise KeyErrorSafe("Ключ задан на сервере; изменение через бот недоступно")
        store = SourceStore(cfg, item.sponsor_provider)
        _backup_file(store.manual_path)
        try:
            store.save(key, expected)
        except SourceError as exc:
            raise KeyErrorSafe(str(exc)) from None
        return
    with _locked(cfg, item, expected):
        path = _manual_path(cfg, item)
        _backup_file(path)
        atomic_object(path, {"version": 1, "api_key": key, "revision": secrets.token_hex(16)})


def remove(cfg, key_id: str, expected: str) -> None:
    item = spec(key_id)
    if item.kind == "sponsor":
        if _env_value(cfg, item):
            raise KeyErrorSafe("Ключ задан на сервере; изменение через бот недоступно")
        store = SourceStore(cfg, item.sponsor_provider)
        _backup_file(store.manual_path)
        try:
            store.remove(expected)
        except SourceError as exc:
            raise KeyErrorSafe(str(exc)) from None
        return
    with _locked(cfg, item, expected):
        path = _manual_path(cfg, item)
        _backup_file(path)
        path.unlink(missing_ok=True)


async def probe(cfg, key_id: str, key: str) -> str | None:
    """Read-only connectivity check. Returns a short status or None if not applicable.
    Never includes provider error text (it may contain the secret)."""
    item = spec(key_id)
    if not item.probe:
        return None
    try:
        if item.probe == "lavepay":
            from creator.lavepay import LavePayClient
            client = LavePayClient(key, getattr(cfg, "lavepay_base_url", "") or "https://laveshka.io/merchant/v1")
            try:
                await client.me()
                return "проверка: успешно"
            finally:
                await client.close()
        if item.probe == "mgram_partner":
            from creator.mgram_factory import MgramFactory
            client = MgramFactory(key, timeout=10.0)
            try:
                await client.list_bots(limit=1)
                return "проверка: успешно"
            finally:
                await client.close()
    except Exception:  # noqa: BLE001 — no provider/key details
        return "проверка: не удалась (ключ в ответ не попадает)"
    return None


def all_statuses(cfg) -> list[KeyStatus]:
    rows = []
    for item in SPECS:
        try:
            rows.append(resolve(cfg, item.id))
        except KeyErrorSafe:
            rows.append(KeyStatus(item, "error", False))
    return rows


async def apply_runtime(ctx, key_id: str) -> str:
    """Hot-reload clients after a file-backed change. Never logs values.
    Returns a short extra note; empty if nothing else is required."""
    item = spec(key_id)
    if not item.editable:
        return ""
    try:
        if key_id == "lavepay":
            return await _reload_lavepay(ctx)
        if key_id in {"tgrass_factory", "tgrass_hub", "piarflow", "piarflow_hub"}:
            from creator.integrations import ensure_services
            from creator.piarflow import PiarFlowClient
            from creator.tgrass import TGrassClient
            ensure_services(ctx)
            if key_id.startswith("tgrass") and getattr(ctx, "tgrass", None) is not None:
                token = value(ctx.cfg, "tgrass_factory")
                old = ctx.tgrass.client
                ctx.tgrass.client = TGrassClient(token, ctx.cfg.tgrass_base_url)
                if old is not None:
                    await old.close()
            if key_id.startswith("piarflow") and getattr(ctx, "piarflow", None) is not None:
                token = value(ctx.cfg, "piarflow")
                old = ctx.piarflow.client
                ctx.piarflow.client = PiarFlowClient(token, ctx.cfg.piarflow_base_url)
                if old is not None:
                    await old.close()
        # MGRAM readers resolve the key on each use.
    except Exception:  # noqa: BLE001 — no provider/key details
        return ("Ключ сохранён, но живой сервис не переподключился. "
                "При необходимости перезапустите конструктор.")
    return ""


async def _reload_lavepay(ctx) -> str:
    from creator.lavepay import LavePayClient
    key = value(ctx.cfg, "lavepay")
    service = getattr(ctx, "lave", None)
    if service is None:
        if key:
            return ("Ключ сохранён. Чтобы заработал приём платежей LavePay "
                    "(опрос счетов и вебхук), перезапустите конструктор.")
        return ""
    old = service.client
    new = LavePayClient(key, ctx.cfg.lavepay_base_url) if key else None
    service.client = new
    payouts = getattr(ctx, "payouts", None) or getattr(service, "payouts", None)
    if payouts is not None and getattr(payouts, "client", None) is old:
        payouts.client = new
    if old is not None:
        await old.close()
    return ""
