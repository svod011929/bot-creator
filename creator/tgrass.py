"""TGrass (tgrass.space) — доп. заработок (фабрика с авто-fallback на трансляцию).

* Владелец подаёт заявку; админ одобряет → сначала `POST /new_bot`, при любой ошибке — ключ хаб-бота.
  Если хаб тоже недоступен — заявка остаётся неподтверждённой.
* Хаб-ключ: `TGRASS_HUB_API_KEY` или `data/tgrass_hub.json`.
* Дочерний бот: `POST /offers` (см. child/tgrass.py). Награда — фикс TGRASS_REWARD.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from creator.db import now

if TYPE_CHECKING:  # pragma: no cover
    from creator.services import Ctx

log = logging.getLogger("tgrass")

SYNC_EVERY = 30
Notify = Callable[[int, str], Awaitable[None]]


class TGrassError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message, self.status = message, status


class TGrassClient:
    def __init__(self, factory_token: str, base_url: str = "https://tgrass.space", timeout: float = 20):
        self.factory_token = factory_token
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

    async def request(self, method: str, path: str, auth: str, body: dict | None = None,
                      params: dict | None = None) -> Any:
        s = await self._sess()
        headers = {"Auth": auth, "accept": "application/json"}
        try:
            async with s.request(method, self.base_url + path, json=body, params=params, headers=headers) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:  # noqa: BLE001
                    data = None
                if r.status >= 400:
                    detail = data.get("detail") if isinstance(data, dict) else None
                    raise TGrassError(str(detail or f"HTTP {r.status}"), r.status)
                return data
        except aiohttp.ClientError as e:
            raise TGrassError(f"сеть: {e}") from None
        except asyncio.TimeoutError:
            raise TGrassError("таймаут") from None

    async def new_bot(self, username: str, user_id: int, bot_name: str) -> str:
        data = await self.request("POST", "/new_bot", self.factory_token,
                                  {"username": username, "user_id": user_id, "bot_name": bot_name})
        token = data.get("auth_token") if isinstance(data, dict) else None
        if not token:
            raise TGrassError(f"нет auth_token в ответе: {str(data)[:200]}")
        return str(token)

    async def balance(self, bot_key: str) -> float:
        data = await self.request("GET", "/balance", bot_key)
        return float((data or {}).get("balance") or 0)

    async def statistics(self, bot_key: str, date: str | None = None) -> dict:
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return await self.request("GET", "/statistics", bot_key, params={"date": date}) or {}


def child_users_for_card(bot_dir: Path) -> int | None:
    """COUNT users для карточки владельца: 0 если бота ещё нет, None если bot.db битый/нечитаем."""
    path = bot_dir / "bot.db"
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def child_users_count(bot_dir: Path) -> int:
    """Число пользователей дочернего бота (чтение bot.db только на чтение).
    При ошибке чтения — 0 (для порогов min_users / гейтов, семантика не меняется)."""
    n = child_users_for_card(bot_dir)
    return 0 if n is None else n


def read_child_done(bot_dir: Path, after_id: int, limit: int = 500) -> list[tuple[int, int, int, int, int]]:
    """Новые засчитанные ОП из bot.db: (id, user_id, offer_id, fake, created_at)."""
    path = bot_dir / "bot.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT id, user_id, offer_id, fake, created_at FROM tgrass_done WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit))]
        finally:
            conn.close()
    except sqlite3.Error:  # таблицы ещё нет (бот не перезапускался с новой версией)
        return []


class TGrassService:
    def __init__(self, ctx: "Ctx", client: TGrassClient, notify: Notify | None = None):
        self.ctx = ctx
        self.client = client
        self.notify = notify
        self._hub_key: str | None = None

    @property
    def db(self):
        return self.ctx.db

    def reward(self) -> int:
        """Награда владельцу за 1 ОП, копейки."""
        return self.ctx.price("TGRASS_REWARD")

    def min_users(self) -> int:
        return self.ctx.price("TGRASS_MIN_USERS")

    def users(self, bot_id: int) -> int:
        return child_users_count(self.ctx.sup.bot_dir(bot_id))

    def owner_login(self) -> str:
        cfg = self.ctx.cfg
        if cfg.tgrass_owner_username:
            return cfg.tgrass_owner_username
        u = self.db.user(cfg.tgrass_owner_id) if cfg.tgrass_owner_id else None
        return (u["username"] or "") if u else ""

    def hub_path(self) -> Path:
        return self.ctx.cfg.data_dir / "tgrass_hub.json"

    def _read_hub_file(self) -> dict:
        path = self.hub_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_hub_file(self, api_key: str) -> None:
        path = self.hub_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": api_key}, ensure_ascii=False), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    async def ensure_hub_key(self) -> str:
        """auth_token хаб-бота креатора для режима трансляции. Не логирует ключ."""
        if self._hub_key:
            return self._hub_key
        key = (self.ctx.cfg.tgrass_hub_api_key or "").strip()
        if not key:
            key = str(self._read_hub_file().get("api_key") or "").strip()
        if not key:
            if not self.ctx.cfg.tgrass_owner_id:
                raise TGrassError("не задан TGRASS_OWNER_ID для хаб-бота")
            name = (self.ctx.bot_username or "creator_hub").lstrip("@")
            try:
                key = await self.client.new_bot(self.owner_login(), self.ctx.cfg.tgrass_owner_id, name)
            except TGrassError as e:
                raise TGrassError(f"не удалось зарегистрировать хаб-бот: {e.message}", e.status) from e
            self._write_hub_file(key)
            log.info("TGrass: хаб-бот зарегистрирован (%s)", name)
        self._hub_key = key
        return key

    async def approve(self, bot_id: int, admin_id: int) -> None:
        """Одобряет заявку: сначала factory (/new_bot), при любой ошибке — relay (хаб).
        Уже одобренные не трогаем. Disabled с готовым ключом — повторно без /new_bot."""
        t = self.db.tgrass(bot_id)
        b = self.db.bot(bot_id)
        if not t or not b:
            raise LookupError("заявка не найдена")
        if t["status"] == "approved":
            return
        prev_mode = (t["mode"] if "mode" in t.keys() else None) or "factory"
        key = t["api_key"]
        mode, fell_back, factory_err = "factory", False, None

        # Повторное включение: ключ уже есть — не зовём /new_bot.
        if t["status"] == "disabled" and key:
            mode = "relay" if prev_mode == "relay" else "factory"
            if mode == "relay":
                try:
                    key = await self.ensure_hub_key()
                except TGrassError as e:
                    self.db.tgrass_set(bot_id, error=e.message[:300])
                    raise
            self.db.tgrass_set(bot_id, status="approved", mode=mode, api_key=key, error=None,
                               decided_at=now(), decided_by=admin_id)
            await self.ctx.apply_changes(bot_id)
            log.info("bot %s снова включён в TGrass mode=%s (админ %s)", bot_id, mode, admin_id)
            return

        if not self.ctx.cfg.tgrass_owner_id:
            # без owner_id factory невозможна — сразу relay
            factory_err = TGrassError("не задан TGRASS_OWNER_ID")
            try:
                mode, key, fell_back = "relay", await self.ensure_hub_key(), True
            except TGrassError as hub_e:
                self.db.tgrass_set(bot_id, error=f"factory: нет TGRASS_OWNER_ID; hub: {hub_e.message[:120]}"[:300])
                raise hub_e
        else:
            try:
                key = await self.client.new_bot(self.owner_login(), self.ctx.cfg.tgrass_owner_id,
                                                (b["username"] or f"bot{bot_id}").lstrip("@"))
            except TGrassError as e:
                factory_err = e
                log.info("TGrass: factory failed for bot %s — fallback relay (%s)", bot_id, e.message[:120])
                try:
                    mode, key, fell_back = "relay", await self.ensure_hub_key(), True
                except TGrassError as hub_e:
                    self.db.tgrass_set(bot_id, error=f"factory: {e.message[:120]}; hub: {hub_e.message[:120]}"[:300])
                    raise hub_e

        fields = dict(status="approved", mode=mode, api_key=key, error=None,
                      decided_at=now(), decided_by=admin_id)
        if fell_back and factory_err is not None:
            fields["error"] = f"factory→relay: {factory_err.message[:200]}"[:300]
        self.db.tgrass_set(bot_id, **fields)
        await self.ctx.apply_changes(bot_id)
        log.info("bot %s подключён к TGrass mode=%s%s (админ %s)",
                 bot_id, mode, " (fallback)" if fell_back else "", admin_id)

    async def reject(self, bot_id: int, admin_id: int) -> bool:
        t = self.db.tgrass(bot_id)
        if not t or t["status"] != "pending":
            return False
        self.db.tgrass_set(bot_id, status="rejected", decided_at=now(), decided_by=admin_id)
        return True

    async def disable(self, bot_id: int, admin_id: int) -> bool:
        t = self.db.tgrass(bot_id)
        if not t or t["status"] != "approved":
            return False
        self.sync_bot(bot_id)
        self.db.tgrass_set(bot_id, status="disabled", decided_at=now(), decided_by=admin_id)
        await self.ctx.apply_changes(bot_id)
        return True

    def sync_bot(self, bot_id: int) -> int:
        """Забирает новые ОП из bot.db дочернего бота и начисляет награды. Возвращает число начислений."""
        t = self.db.tgrass(bot_id)
        if not t:
            return 0
        rows = read_child_done(self.ctx.sup.bot_dir(bot_id), t["cursor"])
        if not rows:
            return 0
        reward = self.reward()
        credited = 0
        for _rid, uid, offer_id, fake, ts in rows:
            if not fake and self.db.tgrass_credit(bot_id, uid, offer_id, reward, ts):
                credited += 1
        self.db.tgrass_set(bot_id, cursor=rows[-1][0])
        return credited

    def sync_all(self) -> int:
        n = 0
        for t in self.db.all("SELECT bot_id FROM tgrass_bots WHERE status IN ('approved','disabled')"):
            try:
                n += self.sync_bot(t["bot_id"])
            except Exception:  # noqa: BLE001
                log.exception("tgrass sync failed for bot %s", t["bot_id"])
        return n

    async def sync_loop(self) -> None:
        while True:
            try:
                n = self.sync_all()
                if n:
                    log.info("TGrass: начислено ОП: %s", n)
            except Exception:  # noqa: BLE001
                log.exception("tgrass sync loop")
            await asyncio.sleep(SYNC_EVERY)
