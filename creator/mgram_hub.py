"""Автосохранение ключа хаб-бота MGRAM конструктора.

Ключ пишется только в защищённое хранилище админки и никогда не попадает
в дочерний процесс, чат (кроме уже существующего показа ключа подключаемого
дочернего бота) или логи.
"""
from __future__ import annotations

import logging

from creator.integration_keys import KeyErrorSafe, revision, save, spec, value

log = logging.getLogger("mgram.hub")


def _extract_api_key(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(data, dict):
        return ""
    return str(data.get("api_key") or "").strip()


def persist_hub_key(cfg, key: str) -> str:
    """Save the constructor hub key if none is already resolved. Never logs the value."""
    if value(cfg, "mgram_hub"):
        return "present"
    item = spec("mgram_hub")
    try:
        save(cfg, "mgram_hub", key, revision(cfg, item))
    except KeyErrorSafe:
        log.warning("MGRAM hub key not saved: storage unavailable")
        return "failed"
    if value(cfg, "mgram_hub"):
        log.info("MGRAM hub key saved for task relay")
        return "saved"
    log.warning("MGRAM hub key not saved: storage unavailable")
    return "failed"


async def ensure_hub_key(cfg, *, bot_token: str = "", owner_id: int = 0, factory=None) -> str:
    """Register the constructor as a MGRAM hub bot once, if the key is missing.

    Returns a status word only: present, saved, skipped_no_partner, skipped_no_token,
    failed_no_key, failed. Never returns or logs the key.
    """
    if value(cfg, "mgram_hub"):
        log.info("MGRAM hub key already present")
        return "present"
    partner = value(cfg, "mgram_partner")
    if not partner:
        log.info("MGRAM hub key skipped: partner key is not set")
        return "skipped_no_partner"
    token = (bot_token or getattr(cfg, "bot_token", "") or "").strip()
    if not token:
        log.info("MGRAM hub key skipped: constructor token is not set")
        return "skipped_no_token"

    from creator.mgram_factory import MgramError, MgramFactory

    owned = factory is None
    client = factory or MgramFactory(partner)
    try:
        result = await client.register_bot(token, owner_id=int(owner_id or 0))
        key = _extract_api_key(result)
        if not key:
            log.warning(
                "MGRAM hub register returned no key; set it in admin integration keys"
            )
            return "failed_no_key"
        return persist_hub_key(cfg, key)
    except MgramError as exc:
        log.warning(
            "MGRAM hub register failed code=%s status=%s",
            exc.code or "-",
            exc.status or "-",
        )
        return "failed"
    except Exception:  # noqa: BLE001 — no token/key in logs
        log.warning("MGRAM hub register failed: unexpected error")
        return "failed"
    finally:
        if owned:
            await client.close()
