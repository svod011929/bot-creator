"""Задания Boostly: хаб-ключ остаётся в конструкторе.

Дочерний бот ходит только на локальный UNIX-сокет с одноразовым прокси-токеном.
Ключ BOOSTLY_HUB_API_KEY в дочерний процесс не передаётся.

Награда владельцу подтверждается только статусом задания confirmed
(ответ POST /v1/tasks/{id}/check или событие task.confirmed из GET /v1/events).
Ошибки, таймауты, пустые ответы, исчезнувшие задания, paid=false и op=true
начислением не считаются. Повтор той же пары бот+пользователь+задание
(проверка и событие) повторно не начисляет.
"""
from __future__ import annotations

import asyncio
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
EVENT_POLL_SECONDS = 30


@dataclass(frozen=True)
class CheckResult:
    confirmed: bool
    reason: str
    status: str = ""
    rewardable: bool = False


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


def _flag(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def is_rewardable(*, status: str, paid: Any = None, op: Any = None) -> bool:
    """Owner reward only on confirmed, paid!=false and op!=true."""
    if _status_of(status) != CONFIRMED:
        return False
    if _flag(op) is True:
        return False
    if _flag(paid) is False:
        return False
    return True


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
        "paid": raw.get("paid"),
        "op": raw.get("op"),
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
        public = {k: v for k, v in task.items() if k not in {"status", "paid", "op"}}
        if raw.get("kind"):
            public["kind"] = str(raw.get("kind"))[:64]
        out.append(public)
    return pending_flags(payload, out)


def pending_flags(payload: Any, tasks: list[dict]) -> list[dict]:
    return tasks


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


def _paid_op(source: dict) -> tuple[Any, Any]:
    return source.get("paid"), source.get("op")


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

    def _result(status: str, source: dict, reason: str | None = None) -> CheckResult:
        confirmed = _status_of(status) == CONFIRMED
        paid, op = _paid_op(source)
        return CheckResult(
            confirmed,
            reason or (CONFIRMED if confirmed else (status or "pending")),
            status=_status_of(status) or "",
            rewardable=is_rewardable(status=status, paid=paid, op=op),
        )

    rows = _task_rows(payload)
    if rows:
        for raw in rows:
            task = sanitize_task(raw)
            if not task or task["id"] != wanted:
                continue
            status = _status_of(task["status"]) or "pending"
            return _result(status, raw, CONFIRMED if status == CONFIRMED else status)
        # The checked task is absent from a non-empty list: not a confirmation.
        return CheckResult(False, "missing")

    status = _status_of(inner.get("status") or (payload or {}).get("status"))
    listed_id = _task_id(inner.get("id") or inner.get("task_id") or (payload or {}).get("id"))
    has_tasks_field = "tasks" in inner or "tasks" in (payload or {})
    if status == CONFIRMED and listed_id == wanted:
        return _result(status, inner)
    if status == CONFIRMED and listed_id is None and not has_tasks_field:
        # Check URL is already scoped to this task_id; a bare confirmed is enough.
        return _result(status, inner)
    if has_tasks_field:
        return CheckResult(False, "missing")
    if status:
        return _result(status, inner, status)
    return CheckResult(False, "empty")


def parse_event_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("events", "items", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def parse_event(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    event_id = str(raw.get("id") or payload.get("id") or "").strip()[:200]
    event_type = str(payload.get("event") or payload.get("type") or raw.get("event") or "").strip()
    task_id = _task_id(payload.get("task_id") or payload.get("id") or raw.get("task_id"))
    user_raw = payload.get("telegram_user")
    if user_raw is None:
        user_raw = payload.get("telegram_user_id") or raw.get("telegram_user") or raw.get("telegram_user_id")
    try:
        tg_user_id = int(user_raw) if user_raw is not None else None
    except (TypeError, ValueError):
        tg_user_id = None
    sequence = raw.get("sequence")
    try:
        sequence = int(sequence) if sequence is not None else None
    except (TypeError, ValueError):
        sequence = None
    if not event_id:
        if task_id and sequence is not None:
            event_id = f"{event_type or 'event'}:{task_id}:{sequence}"
        elif task_id and tg_user_id:
            event_id = f"{event_type or 'event'}:{task_id}:{tg_user_id}"
        else:
            return None
    paid, op = _paid_op(payload)
    status = CONFIRMED if event_type == "task.confirmed" else _status_of(payload.get("status"))
    return {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "task_id": task_id,
        "tg_user_id": tg_user_id,
        "paid": paid,
        "op": op,
        "rewardable": event_type == "task.confirmed" and is_rewardable(
            status=CONFIRMED, paid=paid, op=op,
        ),
        "status": status,
    }


def reward_kop(db, cfg) -> int:
    raw = db.kv_get("price:BOOSTLY_REWARD")
    if raw is not None and str(raw).lstrip("-").isdigit():
        return int(raw)
    return int((getattr(cfg, "prices", None) or {}).get("BOOSTLY_REWARD") or 50)


def credit_owner(db, cfg, bot_id: int, user_id: int, task_id: str) -> bool:
    """Insert a new owner credit. Returns True only when a new row was written."""
    return bool(db.boostly_credit(bot_id, user_id, task_id, reward_kop(db, cfg)))


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
                       body: dict | None = None) -> tuple[Any, str]:
        if not self.enabled:
            return None, "not_configured"
        sess = await self._sess()
        try:
            async with sess.request(method, self.api_base + path, params=params, json=body) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None, "empty"
                if resp.status == 429:
                    return None, "rate_limited"
                if resp.status >= 400:
                    return None, "error"
                if isinstance(data, (dict, list)):
                    return data, "ok"
                return None, "empty"
        except TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError:
            return None, "error"

    async def fetch_pending(self, user_id: int, meta: dict | None = None) -> tuple[list[dict], str]:
        params: dict[str, Any] = {"telegram_user_id": int(user_id)}
        if meta:
            for key in ("language_code", "first_name", "username"):
                value = meta.get(key)
                if value:
                    params[key] = str(value)[:64]
            if meta.get("is_premium") is not None:
                params["is_premium"] = bool(meta.get("is_premium"))
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

    async def fetch_events(self, after_sequence: int = 0) -> tuple[list[dict], str]:
        data, reason = await self._request(
            "GET", "/v1/events", params={"after_sequence": max(0, int(after_sequence))},
        )
        if reason != "ok":
            return [], reason
        return parse_event_list(data), "ok"


STATE = web.AppKey("boostly_state", object)


class ProxyState:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.client: BoostlyTasksClient | None = None


def _client(state: ProxyState | None, cfg=None) -> BoostlyTasksClient:
    if state is not None:
        cfg = state.cfg
        current = state.client
    else:
        current = None
    key = load_hub_key(cfg)
    api_base = base_url(cfg)
    if current is not None and current.api_key == key and current.api_base == api_base:
        return current
    client = BoostlyTasksClient(key, api_base)
    if state is not None:
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
    state.db.boostly_remember_shown(bot_id, user_id, [str(t.get("id")) for t in tasks if t.get("id")])
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
        state.db.boostly_remember_shown(bot_id, user_id, [task_id])
        if result.rewardable:
            credit_owner(state.db, state.cfg, bot_id, user_id, task_id)
            credited = state.db.boostly_already_credited(bot_id, user_id, task_id)
    payload = {
        "ok": True,
        "confirmed": result.confirmed,
        "reason": result.reason,
        "task_id": task_id,
        "credited": bool(credited) if result.confirmed and result.rewardable else False,
        "rewardable": result.rewardable,
    }
    return web.json_response(payload)


def process_events(db, cfg, rows: list[dict]) -> dict:
    """Apply a batch of Boostly events. Idempotent on event id and on (bot, user, task)."""
    processed = 0
    credited = 0
    cursor = db.boostly_event_cursor()
    max_seq = cursor
    for raw in rows:
        event = parse_event(raw)
        if event is None:
            continue
        if event["sequence"] is not None:
            max_seq = max(max_seq, event["sequence"])
        if not db.boostly_record_event(
            event["event_id"], event["sequence"], event["event_type"],
            event["task_id"], event["tg_user_id"],
        ):
            processed += 1
            continue
        processed += 1
        if not event["rewardable"] or event["tg_user_id"] is None or not event["task_id"]:
            continue
        bot_ids = db.boostly_shown_bots(event["tg_user_id"], event["task_id"])
        for bot_id in bot_ids:
            if not db.boostly_approved(bot_id):
                continue
            if credit_owner(db, cfg, bot_id, event["tg_user_id"], event["task_id"]):
                credited += 1
    if max_seq > cursor:
        db.boostly_set_event_cursor(max_seq)
    return {"processed": processed, "credited": credited, "cursor": max_seq}


async def poll_events_once(cfg, db) -> dict:
    client = BoostlyTasksClient(load_hub_key(cfg), base_url(cfg))
    try:
        if not client.enabled:
            return {"ok": False, "reason": "not_configured"}
        rows, reason = await client.fetch_events(db.boostly_event_cursor())
        if reason != "ok":
            return {"ok": False, "reason": reason}
        result = process_events(db, cfg, rows)
        result["ok"] = True
        result["fetched"] = len(rows)
        return result
    finally:
        await client.close()


async def events_loop(cfg, db) -> None:
    """Poll GET /v1/events every 30s so a user who never presses «Проверить» can still be credited."""
    while True:
        try:
            result = await poll_events_once(cfg, db)
            if result.get("fetched") or result.get("credited"):
                log.info(
                    "boostly events poll fetched=%s processed=%s credited=%s cursor=%s",
                    result.get("fetched") or 0,
                    result.get("processed") or 0,
                    result.get("credited") or 0,
                    result.get("cursor"),
                )
            elif not result.get("ok") and result.get("reason") not in {"not_configured"}:
                log.warning("boostly events poll failed: %s", result.get("reason"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — no key material in logs
            log.exception("boostly events poll failed")
        await asyncio.sleep(EVENT_POLL_SECONDS)


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
