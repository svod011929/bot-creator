"""Задания Boostly: хаб-ключ остаётся в конструкторе.

Дочерний бот ходит только на локальный UNIX-сокет с одноразовым прокси-токеном.
Ключ BOOSTLY_HUB_API_KEY в дочерний процесс не передаётся.

Награда владельцу подтверждается только статусом задания confirmed в ответе
POST /v1/tasks/{id}/check. Ошибки, таймауты, пустые ответы и исчезнувшие
задания начислением не считаются. Повторная проверка того же задания
повторно не начисляет.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web

from creator.boostly import load_hub_key

log = logging.getLogger("boostly.tasks")

DEFAULT_BASE = "https://www.boostlyofficial.online"
SOCK_NAME = "boostly_tasks.sock"
CONFIRMED = "confirmed"
REQUEST_TIMEOUT = 10.0
@dataclass(frozen=True)
class CheckResult:
    confirmed: bool
    reason: str
    status: str = ""


def sock_path(cfg) -> Path:
    return Path(cfg.data_dir) / SOCK_NAME


def base_url(cfg) -> str:
    return (getattr(cfg, "boostly_base_url", "") or DEFAULT_BASE).rstrip("/")


def _task_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text[:128]


def _status_of(value: Any) -> str:
    return str(value or "").strip().lower()


def sanitize_task(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    tid = _task_id(raw.get("id") or raw.get("task_id"))
    if tid is None:
        return None
    url = str(raw.get("url") or raw.get("link") or "").strip()
    title = str(raw.get("title") or raw.get("button_text") or "Задание").strip() or "Задание"
    return {
        "id": tid,
        "title": title[:200],
        "url": url[:500],
        "status": str(raw.get("status") or "").strip()[:32],
    }


def pending_tasks(payload: Any) -> list[dict]:
    rows = _task_rows(payload)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in rows:
        task = sanitize_task(raw)
        if not task or task["id"] in seen:
            continue
        if _status_of(task["status"]) == CONFIRMED or raw.get("op"):
            continue
        url = task["url"]
        if url and not url.startswith("http"):
            continue
        seen.add(task["id"])
        public = {k: v for k, v in task.items() if k != "status"}
        out.append(public)
    return out


def _unwrap(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return None
    inner = payload.get("data")
    if isinstance(inner, dict):
        return inner
    return payload


def _task_rows(payload: Any) -> list:
    inner = _unwrap(payload)
    if inner is None:
        return []
    for key in ("tasks", "items", "data"):
        rows = inner.get(key) if key != "data" or isinstance(inner.get("data"), list) else None
        if isinstance(rows, list):
            return rows
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return payload["tasks"]
    return []


def completion_of(payload: Any, task_id: str) -> CheckResult:
    """Positive confirmation only when this task has status=confirmed in a valid payload."""
    wanted = _task_id(task_id)
    if wanted is None:
        return CheckResult(False, "empty")
    if payload is None:
        return CheckResult(False, "empty")
    inner = _unwrap(payload)
    if inner is None:
        return CheckResult(False, "empty")

    rows = _task_rows(payload)
    if rows:
        for raw in rows:
            task = sanitize_task(raw)
            if not task or task["id"] != wanted:
                continue
            if _status_of(task["status"]) == CONFIRMED:
                return CheckResult(True, "confirmed", status=CONFIRMED)
            status = _status_of(task["status"]) or "pending"
            return CheckResult(False, status, status=status)
        # The checked task is absent from a non-empty list: not a confirmation.
        return CheckResult(False, "missing")

    status = _status_of(inner.get("status") or (payload or {}).get("status"))
    listed_id = _task_id(inner.get("id") or inner.get("task_id") or (payload or {}).get("id"))
    has_tasks_field = "tasks" in inner or "tasks" in (payload or {})
    if status == CONFIRMED and listed_id == wanted:
        return CheckResult(True, "confirmed", status=CONFIRMED)
    if status == CONFIRMED and listed_id is None and not has_tasks_field:
        # Check URL is already scoped to this task_id; a bare confirmed is enough.
        return CheckResult(True, "confirmed", status=CONFIRMED)
    if has_tasks_field:
        return CheckResult(False, "missing")
    if status:
        return CheckResult(False, status, status=status)
    return CheckResult(False, "empty")


class BoostlyTasksClient:
    def __init__(self, api_key: str, api_base: str = DEFAULT_BASE, timeout: float = REQUEST_TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.api_base = (api_base or DEFAULT_BASE).rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None) -> tuple[dict | None, str]:
        if not self.enabled:
            return None, "not_configured"
        sess = await self._sess()
        try:
            async with sess.request(method, self.api_base + path, params=params, json=body) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None, "empty"
                if resp.status >= 400:
                    return None, "error"
                return data if isinstance(data, dict) else None, "ok" if isinstance(data, dict) else "empty"
        except TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError:
            return None, "error"

    async def fetch_pending(self, user_id: int, meta: dict | None = None) -> tuple[list[dict], str]:
        params = {"telegram_user_id": int(user_id)}
        if meta:
            for key in ("language_code", "first_name", "username"):
                value = meta.get(key)
                if value:
                    params[key] = str(value)[:64]
        data, reason = await self._request("GET", "/v1/tasks", params=params)
        if reason != "ok":
            return [], reason
        return pending_tasks(data), "ok"

    async def check(self, user_id: int, task_id: str) -> CheckResult:
        tid = _task_id(task_id)
        if tid is None:
            return CheckResult(False, "empty")
        path = "/v1/tasks/" + quote(tid, safe="") + "/check"
        data, reason = await self._request("POST", path, body={"telegram_user_id": int(user_id)})
        if reason != "ok":
            return CheckResult(False, reason)
        return completion_of(data, tid)


STATE = web.AppKey("boostly_state", object)


class ProxyState:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.client: BoostlyTasksClient | None = None


def _client(state: ProxyState) -> BoostlyTasksClient:
    key = load_hub_key(state.cfg)
    api_base = base_url(state.cfg)
    if state.client is not None and state.client.api_key == key and state.client.api_base == api_base:
        return state.client
    client = BoostlyTasksClient(key, api_base)
    state.client = client
    return client


def _auth_bot_id(request: web.Request, state: ProxyState) -> int | None:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return None
    return state.db.boostly_proxy_bot(token)


async def _read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest() from None
    return data if isinstance(data, dict) else {}


def _body_bot_id(body: dict, bot_id: int) -> bool:
    raw = body.get("bot_id")
    if raw in (None, bot_id):
        return True
    try:
        return int(raw) == bot_id
    except (TypeError, ValueError):
        return False


async def handle_tasks(request: web.Request) -> web.Response:
    state: ProxyState = request.app[STATE]
    bot_id = _auth_bot_id(request, state)
    if bot_id is None:
        raise web.HTTPNotFound()
    body = await _read_json(request)
    if not _body_bot_id(body, bot_id):
        raise web.HTTPNotFound()
    if not state.db.boostly_approved(bot_id):
        return web.json_response({"ok": False, "reason": "not_approved", "tasks": []})
    try:
        user_id = int(body.get("user_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "reason": "empty", "tasks": []})
    if user_id <= 0:
        return web.json_response({"ok": False, "reason": "empty", "tasks": []})
    client = _client(state)
    if not client.enabled:
        return web.json_response({"ok": False, "reason": "not_configured", "tasks": []})
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else None
    tasks, reason = await client.fetch_pending(user_id, meta)
    if reason != "ok":
        return web.json_response({"ok": False, "reason": reason, "tasks": []})
    return web.json_response({"ok": True, "tasks": tasks})


async def handle_check(request: web.Request) -> web.Response:
    state: ProxyState = request.app[STATE]
    bot_id = _auth_bot_id(request, state)
    if bot_id is None:
        raise web.HTTPNotFound()
    body = await _read_json(request)
    if not _body_bot_id(body, bot_id):
        raise web.HTTPNotFound()
    if not state.db.boostly_approved(bot_id):
        return web.json_response({"ok": False, "confirmed": False, "reason": "not_approved"})
    try:
        user_id = int(body.get("user_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "confirmed": False, "reason": "empty"})
    task_id = _task_id(body.get("task_id"))
    if user_id <= 0 or task_id is None:
        return web.json_response({"ok": False, "confirmed": False, "reason": "empty"})
    client = _client(state)
    if not client.enabled:
        return web.json_response({"ok": False, "confirmed": False, "reason": "not_configured"})
    result = await client.check(user_id, task_id)
    credited = False
    if result.confirmed:
        raw = state.db.kv_get("price:BOOSTLY_REWARD")
        if raw is not None and str(raw).lstrip("-").isdigit():
            reward = int(raw)
        else:
            reward = int((state.cfg.prices or {}).get("BOOSTLY_REWARD") or 50)
        credited = bool(state.db.boostly_credit(bot_id, user_id, task_id, reward))
        # Already credited is still a confirmation for the child gate.
        credited = credited or state.db.boostly_already_credited(bot_id, user_id, task_id)
    payload = {
        "ok": True,
        "confirmed": result.confirmed,
        "reason": result.reason,
        "task_id": task_id,
        "credited": bool(credited) if result.confirmed else False,
    }
    return web.json_response(payload)


def build_app(cfg, db) -> web.Application:
    app = web.Application()
    app[STATE] = ProxyState(cfg, db)
    app.router.add_post("/tasks", handle_tasks)
    app.router.add_post("/check", handle_check)
    return app


async def start_proxy(cfg, db) -> web.AppRunner:
    path = sock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    runner = web.AppRunner(build_app(cfg, db), access_log=None)
    await runner.setup()
    await web.UnixSite(runner, str(path)).start()
    os.chmod(path, 0o600)
    log.info("boostly tasks proxy listening on unix socket")
    return runner


async def stop_proxy(runner: web.AppRunner | None, cfg=None) -> None:
    if runner is None:
        return
    state = runner.app[STATE] if runner.app is not None else None
    if state is not None and state.client is not None:
        await state.client.close()
    await runner.cleanup()
    if cfg is not None:
        path = sock_path(cfg)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
