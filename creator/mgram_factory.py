"""Client for the broadcast monetization Partner API (internal).

Base: https://api.mgram.biz
Auth: Authorization: Bearer <Partner API key>
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger("mgram.factory")


class MgramError(Exception):
    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class MgramFactory:
    BASE = "https://api.mgram.biz"

    def __init__(self, api_key: str, timeout: float = 15.0):
        self.api_key = api_key.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        if not self.enabled:
            raise MgramError("MGRAM Partner API key не настроен")
        url = self.BASE + path
        sess = await self._sess()
        try:
            async with sess.request(method, url, json=json, params=params) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"message": await resp.text()}
                if resp.status >= 400:
                    code = data.get("code") or data.get("error") or "http_error"
                    msg = data.get("message") or data.get("error") or f"HTTP {resp.status}"
                    raise MgramError(str(msg), code=str(code), status=resp.status)
                return data if isinstance(data, dict) else {"data": data}
        except aiohttp.ClientError as exc:
            raise MgramError(f"Сеть: {exc}") from exc

    async def register_bot(self, token: str, owner_id: int, referrer_id: int | None = None) -> dict:
        """POST /register/bot. Returns bot_id, username, status, api_key (once), etc."""
        body: dict[str, Any] = {"token": token, "owner_id": owner_id}
        if referrer_id:
            body["referrer_id"] = referrer_id
        return await self._request("POST", "/register/bot", json=body)

    async def list_bots(self, *, limit: int = 100, cursor: int | None = None,
                        status: str | None = None, mailings: str | None = None) -> dict:
        """GET /register/bots. Returns {items: [...], next_cursor}."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if mailings:
            params["mailings"] = mailings
        return await self._request("GET", "/register/bots", params=params)

    async def set_mailings(self, bot_ids: list[int], enabled: bool) -> dict:
        """POST /register/bots/settings."""
        return await self._request("POST", "/register/bots/settings",
                                   json={"bot_ids": bot_ids, "mailings_enabled": enabled})

    async def test_webhook(self, url: str) -> dict:
        """POST /register/webhook/test (no auth)."""
        # This one does not require the key according to docs.
        sess = await self._sess()
        try:
            async with sess.post(self.BASE + "/register/webhook/test", json={"url": url},
                                 headers={"Content-Type": "application/json"}) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise MgramError(data.get("message", f"HTTP {resp.status}"), status=resp.status)
                return data
        except aiohttp.ClientError as exc:
            raise MgramError(f"Сеть: {exc}") from exc
