"""LavePay (laveshka.io): счета в $, оплата по ссылке, опрос статуса и подписанный вебхук.

Документация: https://laveshka.io/docs/lave-pay
- POST /invoices {amount (USD строкой), description, order_id, payload, ttl} -> {id, pay_url, status, ...}
- GET /invoices/{id} -> статус active | paid | expired | cancelled | refunded
- вебхук: X-LavePay-Signature = "v1=" + hex(HMAC_SHA256(secret, f"{ts}." + raw_body)), ts не старше 5 минут
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from decimal import ROUND_UP, Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from creator.money import rub

if TYPE_CHECKING:  # pragma: no cover
    from creator.services import Ctx

log = logging.getLogger("lavepay")

INVOICE_TTL = 3600           # срок жизни счёта, с
POLL_EVERY = 20              # опрос открытых счетов, с
MIN_USD = Decimal("0.10")    # минимум счёта по документации
MAX_USD = Decimal("10000")
SIGNATURE_TOLERANCE = 300    # вебхук старше 5 минут отбрасываем
MIN_TOPUP_RUB = 50
MAX_TOPUP_RUB = 100000
MIN_TOPUP_KOP = MIN_TOPUP_RUB * 100
MAX_TOPUP_KOP = MAX_TOPUP_RUB * 100

Notify = Callable[[int, str], Awaitable[None]]


class LavePayError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, details: Any = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.details = code, message, status, details


class LavePayClient:
    """Минимальный REST-клиент Lave Pay."""

    def __init__(self, api_key: str, base_url: str = "https://laveshka.io/merchant/v1", timeout: float = 20):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers={"X-Api-Key": self.api_key})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        s = await self._sess()
        async with s.request(method, self.base_url + path, json=body) as r:
            try:
                data = await r.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                raise LavePayError("bad_response", f"HTTP {r.status}", r.status) from None
        if not isinstance(data, dict) or not data.get("ok"):
            err = (data or {}).get("error") or {} if isinstance(data, dict) else {}
            raise LavePayError(err.get("code", "error"), err.get("message", f"HTTP {r.status}"), r.status,
                               err.get("details"))
        return data.get("data")

    async def me(self) -> dict:
        return await self.request("GET", "/me")

    async def create_invoice(self, amount: str, *, description: str, order_id: str, payload: str = "",
                             ttl: int = INVOICE_TTL) -> dict:
        body = {"amount": amount, "description": description[:256], "order_id": order_id[:128], "ttl": ttl}
        if payload:
            body["payload"] = payload[:1024]
        return await self.request("POST", "/invoices", body)

    async def get_invoice(self, invoice_id: str) -> dict:
        return await self.request("GET", f"/invoices/{invoice_id}")

    async def cancel_invoice(self, invoice_id: str) -> dict:
        return await self.request("POST", f"/invoices/{invoice_id}/cancel")


def verify_signature(secret: str, timestamp: str, raw_body: bytes, signature: str, *, now: float | None = None,
                     tolerance: int = SIGNATURE_TOLERANCE) -> bool:
    if not secret or not timestamp.isdigit() or not signature:
        return False
    if abs((now if now is not None else time.time()) - int(timestamp)) > tolerance:
        return False
    expected = "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()


def rub_to_usd(kop: int, usd_rub: int) -> str:
    """Копейки -> строка USD с 2 знаками, вверх до цента, не меньше 0.10."""
    rate = Decimal(max(usd_rub, 1))
    usd = (Decimal(kop) / 100 / rate).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return str(max(usd, MIN_USD))


def _ts(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return 0


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


class LavePayService:
    """Счета LavePay для payload'ов конструктора (topup/create/host/vip). Суммы — копейки."""

    def __init__(self, ctx: Ctx, client: LavePayClient | None, notify: Notify | None = None,
                 webhook_secret: str = ""):
        self.ctx = ctx
        self.client = client
        self.notify = notify
        self.webhook_secret = webhook_secret
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ---------- цены ----------
    @staticmethod
    def topup_payload(rub: int) -> str | None:
        """payload пополнения баланса на rub целых рублей."""
        if not MIN_TOPUP_RUB <= rub <= MAX_TOPUP_RUB:
            return None
        return f"topup:{rub * 100}"

    def quote(self, uid: int, payload: str) -> int | None:
        """Сумма в копейках за payload или None, если заказ недействителен (считается на сервере)."""
        amount, _ = self.ctx.expected_amount(uid, payload)
        return amount if amount and amount > 0 else None

    # ---------- счета ----------
    async def create(self, uid: int, payload: str, description: str) -> dict:
        """Создаёт счёт. Возвращает строку lave_invoices. Бросает LavePayError/ValueError."""
        if not self.client:
            raise LavePayError("disabled", "LavePay не настроен")
        amount = self.quote(uid, payload)
        if amount is None:
            raise ValueError("Заказ устарел или неверная сумма")
        usd = rub_to_usd(amount, self.ctx.price("LAVE_USD_RUB"))
        if Decimal(usd) > MAX_USD:
            raise ValueError("Слишком большая сумма")
        local_id = self.ctx.db.lave_new(uid, payload, amount, usd)
        order_id = f"bc-{local_id}"
        inv = await self.client.create_invoice(
            usd, description=f"{description} ({rub(amount)})", order_id=order_id,
            payload=json.dumps({"uid": uid, "p": payload}, separators=(",", ":")),
        )
        self.ctx.db.lave_set_created(local_id, inv["id"], inv.get("pay_url") or "", _ts(inv.get("expires_at"))
                                     or int(time.time()) + INVOICE_TTL)
        return self.ctx.db.lave_get(local_id)

    def _matches(self, row, inv: dict) -> bool:
        try:
            # amount — оценка в USD (у реального API бывает на цент больше), сверяем названную сумму
            face = inv.get("face_amount") or inv.get("amount")
            same_amount = Decimal(str(face)) == Decimal(row["usd"]) and inv.get("currency", "USD") == "USD"
        except Exception:  # noqa: BLE001
            same_amount = False
        return inv.get("id") == row["invoice_id"] and inv.get("order_id") == f"bc-{row['id']}" and same_amount

    async def apply(self, row, inv: dict) -> str | None:
        """Применяет статус счёта из API/вебхука. Возвращает текст пользователю при оплате."""
        status = inv.get("status")
        if status != "paid":
            if status in ("expired", "cancelled", "refunded"):
                self.ctx.db.lave_set_status(row["id"], status)
            return None
        if not self._matches(row, inv):
            log.error("invoice %s mismatch: amount=%s order=%s", row["invoice_id"], inv.get("amount"),
                      inv.get("order_id"))
            self.ctx.db.lave_set_status(row["id"], "mismatch")
            return None
        async with self._lock:
            if not self.ctx.db.lave_mark_paid(row["id"]):
                return None  # уже зачислен
            uid, amount, payload = row["user_id"], row["amount"], row["payload"]
            self.ctx.db.register_payment(f"lave:{row['invoice_id']}", uid, amount, payload)
            text = await self.ctx.fulfil_payload(uid, payload, amount, via="LavePay")
        log.info("LavePay invoice %s paid: uid=%s payload=%s kop=%s", row["invoice_id"], uid, payload, amount)
        await self._send(uid, text)
        ref = self.ctx.db.credit_referrer(uid, amount, self.ctx.price("REF_PERCENT"))
        if ref:
            await self._send(ref[0], f"💰 Партнёрское начисление: +{rub(ref[1])} от оплаты вашего реферала.")
        for admin in self.ctx.cfg.admin_ids:
            await self._send(admin, f"💳 LavePay: {rub(amount)} (${row['usd']}, {inv.get('paid_via') or '?'}) "
                                    f"от <code>{uid}</code>: <code>{payload}</code>")
        return text

    async def _send(self, uid: int, text: str) -> None:
        if self.notify:
            try:
                await self.notify(uid, text)
            except Exception:  # noqa: BLE001
                log.info("notify %s failed", uid)

    async def check(self, local_id: int) -> tuple[str, str | None]:
        """Ручная проверка («Проверить оплату»). Возвращает (статус, текст при оплате)."""
        row = self.ctx.db.lave_get(local_id)
        if not row or not row["invoice_id"]:
            return "missing", None
        if row["status"] == "paid":
            return "paid", None
        inv = await self.client.get_invoice(row["invoice_id"])
        text = await self.apply(row, inv)
        return (self.ctx.db.lave_get(local_id)["status"], text)

    async def poll_once(self) -> None:
        now = int(time.time())
        for row in self.ctx.db.lave_active():
            try:
                inv = await self.client.get_invoice(row["invoice_id"])
                await self.apply(row, inv)
            except LavePayError as e:
                log.warning("poll %s: %s", row["invoice_id"], e)
                if e.code == "not_found":
                    self.ctx.db.lave_set_status(row["id"], "missing")
            except Exception:  # noqa: BLE001
                log.exception("poll %s failed", row["invoice_id"])
            else:
                # подстраховка: счёт давно истёк, а статус всё ещё active
                if row["expires_at"] and now > row["expires_at"] + 86400:
                    self.ctx.db.lave_set_status(row["id"], "expired")

    async def poll_loop(self, every: int = POLL_EVERY) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("poll loop failed")
            await asyncio.sleep(every)

    # ---------- вебхук ----------
    async def handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        ts = request.headers.get("X-LavePay-Timestamp", "")
        sig = request.headers.get("X-LavePay-Signature", "")
        if not verify_signature(self.webhook_secret, ts, raw, sig):
            log.warning("webhook: bad signature or stale timestamp")
            return web.Response(status=401, text="bad signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return web.Response(status=400, text="bad json")
        etype, data = event.get("type"), event.get("data") or {}
        log.info("webhook %s %s", event.get("id"), etype)
        if etype and etype.startswith("invoice.") and isinstance(data, dict) and data.get("id"):
            row = self.ctx.db.lave_by_invoice(data["id"])
            if row:
                asyncio.get_running_loop().create_task(self._apply_safe(row, data))
        return web.json_response({"ok": True})

    async def _apply_safe(self, row, data: dict) -> None:
        try:
            await self.apply(row, data)
        except Exception:  # noqa: BLE001
            log.exception("webhook apply failed")

    def webhook_app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post("/lavepay", self.handle_webhook)
        app.router.add_get("/health", _health)
        return app

    async def start_webhook(self, host: str, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.webhook_app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("LavePay webhook слушает http://%s:%s/lavepay", host, port)
        return runner
