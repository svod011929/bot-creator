"""Задания MGRAM для шаблона «Микрозадачи»: ключ хаба остаётся в конструкторе.

Дочерний бот ходит только на локальный UNIX-сокет с одноразовым прокси-токеном.
Ключи MGRAM_HUB_API_KEY и MGRAM_PARTNER_API_KEY в дочерний процесс не передаются.

Награда подтверждается только статусом задания completed в ответе POST /tasks/check.
Исчезновение задания, ошибки, таймауты и пустые ответы начислением не считаются.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

log = logging.getLogger("mgram.tasks")

TASKS_PATH = "/tasks"
CHECK_PATH = "/tasks/check"
BASE = "https://api.mgram.biz"
SOCK_NAME = "mgram_tasks.sock"
COMPLETED = "completed"
REQUEST_TIMEOUT = 10.0


@dataclass(frozen=True)
class CheckResult:
    confirmed: bool
    reward: float | None
    reason: str
    status: str = ""


def sock_path(cfg) -> Path:
    return Path(cfg.data_dir) / SOCK_NAME


def load_hub_key(cfg) -> str:
    """Bot API key of the constructor hub bot. Never falls back to the partner key."""
    key = (getattr(cfg, "mgram_hub_api_key", None) or "").strip()
    if key:
        return key
    path = Path(cfg.data_dir) / "mgram_hub.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("api_key") or data.get("key") or "").strip()


def parse_reward(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount


def _unwrap(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return None
    inner = payload.get("data")
    if isinstance(inner, dict):
        return inner
    if payload.get("ok") is True:
        # {ok: true} without data is empty, not a confirmation.
        return payload if "tasks" in payload or "status" in payload else None
    if "tasks" in payload or "status" in payload:
        return payload
    return None


def _task_id(value: Any) -> int | None:
    try:
        tid = int(value)
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def sanitize_task(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    tid = _task_id(raw.get("id"))
    if tid is None:
        return None
    reward = parse_reward(raw.get("reward"))
    if reward is None:
        reward = 0.0
    title = str(raw.get("title") or raw.get("button_text") or "Задание").strip() or "Задание"
    button = str(raw.get("button_text") or title).strip() or title
    link = str(raw.get("link") or "").strip()
    return {
        "id": tid,
        "title": title[:200],
        "button_text": button[:200],
        "link": link[:500],
        "reward": reward,
        "currency": str(raw.get("currency") or "").strip()[:8],
        "type": str(raw.get("type") or "").strip()[:32],
        "status": str(raw.get("status") or "").strip()[:32],
    }


def pending_tasks(payload: Any) -> list[dict]:
    inner = _unwrap(payload)
    if inner is None:
        return []
    rows = inner.get("tasks")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for raw in rows:
        task = sanitize_task(raw)
        if not task or task["status"] != "pending" or task["id"] in seen:
            continue
        seen.add(task["id"])
        public = {k: v for k, v in task.items() if k != "status"}
        out.append(public)
    return out


def completion_of(payload: Any, task_id: int) -> CheckResult:
    """Positive confirmation only when this task_id has status=completed in a valid payload."""
    inner = _unwrap(payload)
    if inner is None:
        return CheckResult(False, None, "empty")
    rows = inner.get("tasks")
    if not isinstance(rows, list) or not rows:
        reason = str(inner.get("reason") or "").strip() or "empty"
        if reason in {"busy", "user_not_seen", "no_list", "no_offers", "all_done"}:
            return CheckResult(False, None, reason)
        return CheckResult(False, None, "empty")
    wanted = _task_id(task_id)
    if wanted is None:
        return CheckResult(False, None, "empty")
    for raw in rows:
        task = sanitize_task(raw)
        if not task or task["id"] != wanted:
            continue
        if task["status"] == COMPLETED:
            return CheckResult(True, parse_reward(raw.get("reward")) if isinstance(raw, dict) else task["reward"],
                               "completed", status=COMPLETED)
        status = task["status"] or "pending"
        return CheckResult(False, None, status, status=status)
    return CheckResult(False, None, "missing")


class MgramTasksClient:
    def __init__(self, api_key: str, timeout: float = REQUEST_TIMEOUT):
        self.api_key = (api_key or "").strip()
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

    async def _post(self, path: str, body: dict) -> tuple[dict | None, str]:
        if not self.enabled:
            return None, "not_configured"
        sess = await self._sess()
        try:
            async with sess.post(BASE + path, json=body) as resp:
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

    async def fetch_pending(self, user_id: int) -> tuple[list[dict], str]:
        data, reason = await self._post(TASKS_PATH, {"user_id": int(user_id), "language_code": "ru"})
        if reason != "ok":
            return [], reason
        return pending_tasks(data), "ok"

    async def check(self, user_id: int, task_id: int) -> CheckResult:
        body = {"user_id": int(user_id), "task_ids": [int(task_id)], "language_code": "ru"}
        data, reason = await self._post(CHECK_PATH, body)
        if reason != "ok":
            return CheckResult(False, None, reason)
        return completion_of(data, task_id)


STATE = web.AppKey('mgram_state', object)


class ProxyState:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.client: MgramTasksClient | None = None


def _client(state: ProxyState) -> MgramTasksClient:
    key = load_hub_key(state.cfg)
    if state.client is not None and state.client.api_key == key:
        return state.client
    client = MgramTasksClient(key)
    state.client = client
    return client


def _auth_bot_id(request: web.Request, state: ProxyState) -> int | None:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return None
    return state.db.mgram_proxy_bot(token)


async def _read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest() from None
    return data if isinstance(data, dict) else {}


async def handle_tasks(request: web.Request) -> web.Response:
    state: ProxyState = request.app[STATE]
    bot_id = _auth_bot_id(request, state)
    if bot_id is None:
        raise web.HTTPNotFound()
    body = await _read_json(request)
    if body.get("bot_id") not in (None, bot_id) and _task_id(body.get("bot_id")) != bot_id:
        raise web.HTTPNotFound()
    user_id = _task_id(body.get("user_id"))
    if user_id is None:
        return web.json_response({"ok": False, "reason": "empty", "tasks": []})
    client = _client(state)
    if not client.enabled:
        return web.json_response({"ok": False, "reason": "not_configured", "tasks": []})
    tasks, reason = await client.fetch_pending(user_id)
    if reason != "ok":
        return web.json_response({"ok": False, "reason": reason, "tasks": []})
    return web.json_response({"ok": True, "tasks": tasks})


async def handle_check(request: web.Request) -> web.Response:
    state: ProxyState = request.app[STATE]
    bot_id = _auth_bot_id(request, state)
    if bot_id is None:
        raise web.HTTPNotFound()
    body = await _read_json(request)
    if body.get("bot_id") not in (None, bot_id) and _task_id(body.get("bot_id")) != bot_id:
        raise web.HTTPNotFound()
    user_id = _task_id(body.get("user_id"))
    task_id = _task_id(body.get("task_id"))
    if user_id is None or task_id is None:
        return web.json_response({"ok": False, "confirmed": False, "reason": "empty"})
    client = _client(state)
    if not client.enabled:
        return web.json_response({"ok": False, "confirmed": False, "reason": "not_configured"})
    result = await client.check(user_id, task_id)
    payload = {
        "ok": True,
        "confirmed": result.confirmed,
        "reason": result.reason,
        "task_id": task_id,
    }
    if result.confirmed and result.reward is not None:
        payload["reward"] = result.reward
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
    log.info("mgram tasks proxy listening on unix socket")
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
