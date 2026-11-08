"""PiarFlow (piarflow.com) — доп. заработок (фабрика с авто-fallback на трансляцию).

* Владелец подаёт заявку; админ одобряет → сначала `POST /v1/factory/bots`, при любой ошибке сети/API —
  ключ хаб-бота креатора (relay). Если хаб тоже недоступен — заявка остаётся неподтверждённой.
* Хаб-ключ: `PIARFLOW_HUB_API_KEY` или `data/piarflow_hub.json` (авто-регистрация креатора).
* Дочерний бот: `POST /v1/sponsors` (см. child/piarflow.py). Награда — фикс PIARFLOW_REWARD.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from creator.db import now
from creator.tgrass import child_users_count

if TYPE_CHECKING:  # pragma: no cover
    from creator.services import Ctx

log = logging.getLogger("piarflow")

SYNC_EVERY = 30
MSK = timezone(timedelta(hours=3))
Notify = Callable[[int, str], Awaitable[None]]


class PiarFlowError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message, self.status = message, status


class PiarFlowClient:
    def __init__(self, token: str, base_url: str = "https://piarflow.com/v1", timeout: float = 20):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
                      auth: str | None = None) -> Any:
        s = await self._sess()
        headers = {"Authorization": f"Bearer {auth or self.token}", "Content-Type": "application/json"}
        try:
            async with s.request(method, self.base_url + path, json=body, params=params, headers=headers) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:  # noqa: BLE001
                    data = None
                if r.status >= 400 or (isinstance(data, dict) and data.get("status") == "error"):
                    msg = data.get("message") if isinstance(data, dict) else None
                    raise PiarFlowError(str(msg or f"HTTP {r.status}"), r.status)
                return data
        except aiohttp.ClientError as e:
            raise PiarFlowError(f"сеть: {e}") from None
        except asyncio.TimeoutError:
            raise PiarFlowError("таймаут") from None

    # --- Factory API ---
    async def add_bot(self, bot_token: str, max_sponsors: int | None = None, reset_time: int | None = None) -> dict:
        """POST /factory/bots — регистрирует (или обновляет) бота, возвращает объект бота с api_key."""
        body: dict[str, Any] = {"bot_token": bot_token}
        if max_sponsors:
            body["max_sponsors"] = max_sponsors
        if reset_time:
            body["reset_time"] = reset_time
        data = await self.request("POST", "/factory/bots", body)
        bot = (data or {}).get("bot") if isinstance(data, dict) else None
        if not isinstance(bot, dict) or not bot.get("api_key"):
            raise PiarFlowError("нет api_key в ответе PiarFlow")
        return bot

    async def bots(self, offset: int = 0, limit: int = 20) -> dict:
        return await self.request("GET", "/factory/bots", params={"offset": offset, "limit": limit}) or {}

    async def bot(self, pf_bot_id: int) -> dict:
        return ((await self.request("GET", f"/factory/bots/{int(pf_bot_id)}")) or {}).get("bot") or {}

    async def set_active(self, pf_bot_id: int, active: bool) -> dict:
        data = await self.request("PATCH", f"/factory/bots/{int(pf_bot_id)}", {"is_active": bool(active)})
        return (data or {}).get("bot") or {}

    # --- /traffic_bot ---
    async def stats(self, pf_bot_id: int, date: str | None = None) -> dict:
        """GET /traffic_bot/stats за день (по Москве): sold_subs, earned, sponsor_tasks_*."""
        date = date or datetime.now(MSK).strftime("%Y-%m-%d")
        data = await self.request("GET", "/traffic_bot/stats", params={"bot_id": int(pf_bot_id), "date": date})
        return (data or {}).get("stats") or {}

    # --- аккаунт ---
    async def balance(self) -> dict:
        return await self.request("GET", "/balance") or {}


def read_child_done(bot_dir: Path, after_id: int, limit: int = 500) -> list[tuple[int, int, str, int, int]]:
    """Новые выполненные задания из bot.db: (id, user_id, link, counted, created_at)."""
    path = bot_dir / "bot.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT id, user_id, link, counted, created_at FROM pf_done WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit))]
        finally:
            conn.close()
    except sqlite3.Error:  # таблицы ещё нет
        return []


def token_bot_id(token: str) -> int:
    head = (token or "").split(":", 1)[0]
    return int(head) if head.isdigit() else 0


class PiarFlowService:
    def __init__(self, ctx: "Ctx", client: PiarFlowClient, notify: Notify | None = None):
        self.ctx = ctx
        self.client = client
        self.notify = notify
        self._hub_key: str | None = None

    @property
    def db(self):
        return self.ctx.db

    def reward(self) -> int:
        """Награда владельцу за 1 задание, копейки."""
        return self.ctx.price("PIARFLOW_REWARD")

    def min_users(self) -> int:
        return self.ctx.price("PIARFLOW_MIN_USERS")

    def users(self, bot_id: int) -> int:
        return child_users_count(self.ctx.sup.bot_dir(bot_id))

    def hub_path(self) -> Path:
        return self.ctx.cfg.data_dir / "piarflow_hub.json"

    def _read_hub_file(self) -> dict:
        path = self.hub_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_hub_file(self, api_key: str, bot_id: int | None) -> None:
        path = self.hub_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": api_key, "bot_id": bot_id}, ensure_ascii=False), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    async def ensure_hub_key(self) -> str:
        """API key хаб-бота креатора для режима трансляции. Не логирует ключ."""
        if self._hub_key:
            return self._hub_key
        key = (self.ctx.cfg.piarflow_hub_api_key or "").strip()
        if not key:
            key = str(self._read_hub_file().get("api_key") or "").strip()
        if not key:
            token = (self.ctx.cfg.bot_token or "").strip()
            if not token:
                raise PiarFlowError("нет BOT_TOKEN для регистрации хаб-бота PiarFlow")
            try:
                bot = await self.client.add_bot(token)
            except PiarFlowError as e:
                raise PiarFlowError(f"не удалось зарегистрировать хаб-бот: {e.message}", e.status) from e
            key = str(bot["api_key"])
            pf_id = int(bot.get("bot_id") or token_bot_id(token) or 0) or None
            self._write_hub_file(key, pf_id)
            log.info("PiarFlow: хаб-бот зарегистрирован (bot_id=%s)", pf_id)
        self._hub_key = key
        return key

    async def approve(self, bot_id: int, admin_id: int) -> None:
        """Одобряет заявку: сначала factory, при любой ошибке — relay (хаб).
        Уже одобренные не трогаем. Повторное включение disabled с готовым factory-ключом —
        только set_active (без повторной регистрации). Если и хаб недоступен — ошибка, статус не approved."""
        t = self.db.pf(bot_id)
        b = self.db.bot(bot_id)
        if not t or not b:
            raise LookupError("заявка не найдена")
        if t["status"] == "approved":
            return
        prev_mode = (t["mode"] if "mode" in t.keys() else None) or "factory"
        key, pf_id = t["api_key"], t["pf_bot_id"]
        mode, fell_back, factory_err = "factory", False, None

        # Повторное включение уже зарегистрированного factory-бота — без /factory/bots заново.
        if t["status"] == "disabled" and prev_mode == "factory" and key and pf_id:
            try:
                await self.client.set_active(pf_id, True)
            except PiarFlowError as e:
                factory_err = e
                log.info("PiarFlow: set_active failed for bot %s — fallback relay (%s)", bot_id, e.message[:120])
                try:
                    mode, key, pf_id, fell_back = "relay", await self.ensure_hub_key(), None, True
                except PiarFlowError as hub_e:
                    self.db.pf_set(bot_id, error=f"factory: {e.message[:120]}; hub: {hub_e.message[:120]}"[:300])
                    raise hub_e
            else:
                self.db.pf_set(bot_id, status="approved", mode="factory", api_key=key, pf_bot_id=pf_id,
                               error=None, decided_at=now(), decided_by=admin_id)
                await self.ctx.apply_changes(bot_id)
                log.info("bot %s снова включён в PiarFlow mode=factory (админ %s)", bot_id, admin_id)
                return

        # Повторное включение relay — только хаб-ключ, без factory.
        if t["status"] == "disabled" and prev_mode == "relay":
            try:
                key = await self.ensure_hub_key()
            except PiarFlowError as e:
                self.db.pf_set(bot_id, error=e.message[:300])
                raise
            self.db.pf_set(bot_id, status="approved", mode="relay", api_key=key, pf_bot_id=None,
                           error=None, decided_at=now(), decided_by=admin_id)
            await self.ctx.apply_changes(bot_id)
            log.info("bot %s снова включён в PiarFlow mode=relay (админ %s)", bot_id, admin_id)
            return

        # Новая заявка (и любые pending): всегда factory → при ошибке relay.
        try:
            bot = await self.client.add_bot(b["token"])
            key = str(bot["api_key"])
            pf_id = int(bot.get("bot_id") or token_bot_id(b["token"]) or 0) or None
        except PiarFlowError as e:
            factory_err = e
            log.info("PiarFlow: factory failed for bot %s — fallback relay (%s)", bot_id, e.message[:120])
            try:
                mode, key, pf_id, fell_back = "relay", await self.ensure_hub_key(), None, True
            except PiarFlowError as hub_e:
                msg = f"factory: {e.message[:120]}; hub: {hub_e.message[:120]}"[:300]
                self.db.pf_set(bot_id, error=msg)
                raise hub_e

        fields = dict(status="approved", mode=mode, api_key=key, pf_bot_id=pf_id, error=None,
                      decided_at=now(), decided_by=admin_id)
        if fell_back and factory_err is not None:
            fields["error"] = f"factory→relay: {factory_err.message[:200]}"[:300]
        self.db.pf_set(bot_id, **fields)
        await self.ctx.apply_changes(bot_id)
        log.info("bot %s подключён к PiarFlow mode=%s%s (админ %s)",
                 bot_id, mode, " (fallback)" if fell_back else "", admin_id)

    async def reject(self, bot_id: int, admin_id: int) -> bool:
        t = self.db.pf(bot_id)
        if not t or t["status"] != "pending":
            return False
        self.db.pf_set(bot_id, status="rejected", decided_at=now(), decided_by=admin_id)
        return True

    async def disable(self, bot_id: int, admin_id: int) -> bool:
        t = self.db.pf(bot_id)
        if not t or t["status"] != "approved":
            return False
        mode = (t["mode"] if "mode" in t.keys() else None) or "factory"
        self.sync_bot(bot_id)
        self.db.pf_set(bot_id, status="disabled", decided_at=now(), decided_by=admin_id)
        await self.ctx.apply_changes(bot_id)
        if mode == "factory" and t["pf_bot_id"]:
            try:
                await self.client.set_active(t["pf_bot_id"], False)
            except PiarFlowError as e:
                log.warning("PiarFlow: не удалось выключить бота %s: %s", bot_id, e.message)
        return True

    def sync_bot(self, bot_id: int) -> int:
        """Забирает новые задания из bot.db дочернего бота и начисляет награды. Возвращает число начислений."""
        t = self.db.pf(bot_id)
        if not t:
            return 0
        rows = read_child_done(self.ctx.sup.bot_dir(bot_id), t["cursor"])
        if not rows:
            return 0
        reward = self.reward()
        credited = 0
        for _rid, uid, link, counted, ts in rows:
            if counted and self.db.pf_credit(bot_id, uid, link, reward, ts):
                credited += 1
        self.db.pf_set(bot_id, cursor=rows[-1][0])
        return credited

    def sync_all(self) -> int:
        n = 0
        for t in self.db.all("SELECT bot_id FROM pf_bots WHERE status IN ('approved','disabled')"):
            try:
                n += self.sync_bot(t["bot_id"])
            except Exception:  # noqa: BLE001
                log.exception("piarflow sync failed for bot %s", t["bot_id"])
        return n

    async def sync_loop(self) -> None:
        while True:
            try:
                n = self.sync_all()
                if n:
                    log.info("PiarFlow: начислено заданий: %s", n)
            except Exception:  # noqa: BLE001
                log.exception("piarflow sync loop")
            await asyncio.sleep(SYNC_EVERY)
