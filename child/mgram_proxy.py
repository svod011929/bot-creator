"""Клиент локального прокси заданий MGRAM. Ключ хаба здесь не хранится и не запрашивается."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger("child.mgram_proxy")

REQUEST_TIMEOUT = 12.0


@dataclass(frozen=True)
class ProxyCheck:
    confirmed: bool
    reward: float | None
    reason: str


def _proxy_conf(cfg: dict[str, Any]) -> tuple[str, str] | None:
    sock = str(cfg.get("mgram_proxy") or "").strip()
    token = str(cfg.get("mgram_proxy_token") or "").strip()
    if not sock or not token:
        return None
    if any(key in cfg for key in ("mgram_key", "mgram_hub_api_key", "mgram_partner_api_key")):
        # Defensive: a leaked creator key in config must never be used by the child.
        log.error("refusing mgram proxy: unexpected creator key field in child config")
        return None
    return sock, token


async def _post(cfg: dict[str, Any], path: str, body: dict) -> dict | None:
    conf = _proxy_conf(cfg)
    if conf is None:
        return None
    sock, token = conf
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        connector = aiohttp.UnixConnector(path=sock)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.post(
                f"http://localhost{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
    except TimeoutError:
        return {"ok": False, "reason": "timeout", "confirmed": False, "tasks": []}
    except Exception:  # noqa: BLE001 — no token/path in logs
        log.exception("mgram proxy request failed")
        return None


async def fetch_pending(cfg: dict[str, Any], bot_id: int, user_id: int) -> list[dict]:
    data = await _post(cfg, "/tasks", {"bot_id": bot_id, "user_id": user_id})
    if not data or not data.get("ok"):
        return []
    rows = data.get("tasks")
    if not isinstance(rows, list):
        return []
    return [t for t in rows if isinstance(t, dict) and t.get("id")]


async def check_completion(cfg: dict[str, Any], bot_id: int, user_id: int, task_id: int) -> ProxyCheck:
    data = await _post(cfg, "/check", {"bot_id": bot_id, "user_id": user_id, "task_id": task_id})
    if data is None:
        return ProxyCheck(False, None, "error")
    reason = str(data.get("reason") or ("ok" if data.get("ok") else "error"))
    if data.get("reason") == "timeout":
        return ProxyCheck(False, None, "timeout")
    if not data.get("ok") or not data.get("confirmed"):
        return ProxyCheck(False, None, reason if reason != "ok" else "pending")
    reward = data.get("reward")
    try:
        amount = float(reward) if reward is not None and reward != "" else None
    except (TypeError, ValueError):
        amount = None
    return ProxyCheck(True, amount, "completed")
