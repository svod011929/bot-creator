"""Platform-side LavePay payout worker for clicker and refbot child withdrawals.

Runs only in the constructor process, which holds the merchant key. Child
processes never see the key. The worker does nothing unless the global
CHILD_LAVEPAY_PAYOUTS flag is on; even then each bot must also have its own
flag on, and both default to off. POST endpoints that move money are called
only for queued jobs that already reserved the user's in-bot balance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from child import templates
from child.db import ChildDB, fmt
from child.files import read_settings
from child.payouts import (
    JOB_QUEUED,
    JOB_SUBMITTED,
    PAYOUT_TEMPLATES,
    loads_destination,
    match_sbp_bank,
    parse_idempotency_key,
    payouts_auto_bot_enabled,
    payouts_bot_enabled,
)
from creator.lavepay import LavePayError

if TYPE_CHECKING:  # pragma: no cover
    from creator.services import Ctx

log = logging.getLogger("child_payouts")

POLL_EVERY = 20
RETRYABLE = frozenset({
    "insufficient_funds", "payout_unavailable", "rate_limited", "rate_changed",
    "provider_error", "provider_not_configured", "forbidden",
})
IMMEDIATE_FAIL = frozenset({"validation_error", "not_found", "idempotency_conflict"})
PAYOUT_EVENTS = {
    "card_payout.completed": True,
    "card_payout.failed": False,
    "sbp_payout.completed": True,
    "sbp_payout.failed": False,
    "withdrawal.completed": True,
    "withdrawal.failed": False,
}
BACKOFF = (60, 120, 300, 600, 1800, 3600)
Notify = Callable[[int, str], Awaitable[None]]


def _delay(attempts: int) -> int:
    return BACKOFF[min(max(attempts, 0), len(BACKOFF) - 1)]


def _day_start(now_ts: int) -> int:
    local = datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _safe_result(payload: dict) -> dict[str, Any]:
    """Persist provider fields that are not payment credentials."""
    keep = ("id", "status", "amount_rub", "debit", "currency", "rate", "amount",
            "fee", "asset", "network", "tx_hash", "tx_url", "error", "replayed",
            "idempotency_key")
    out = {k: payload.get(k) for k in keep if payload.get(k) is not None}
    card = payload.get("card")
    if isinstance(card, dict):
        out["card_last4"] = card.get("last4")
        out["card_masked"] = card.get("masked")
    rec = payload.get("recipient")
    if isinstance(rec, dict):
        out["recipient_last4"] = rec.get("last4")
        out["bank_name"] = rec.get("bank_name")
    addr = payload.get("address")
    if isinstance(addr, str) and len(addr) > 10:
        out["address"] = f"{addr[:4]}…{addr[-4:]}"
    return out


class ChildPayoutService:
    def __init__(self, ctx: Ctx, client, notify: Notify | None = None):
        self.ctx = ctx
        self.client = client
        self.notify = notify
        self._lock = asyncio.Lock()
        self._banks: list[dict[str, Any]] = []
        self._banks_at = 0.0
        self._open: dict[int, ChildDB] = {}

    def enabled(self) -> bool:
        return bool(getattr(self.ctx.cfg, "child_lavepay_payouts", False)) and self.client is not None

    def bot_dir(self, bot_id: int) -> Path:
        return self.ctx.cfg.bots_dir / str(bot_id)

    def _db(self, bot_id: int) -> ChildDB:
        db = self._open.get(bot_id)
        if db is not None:
            return db
        path = self.bot_dir(bot_id) / "bot.db"
        db = ChildDB(path)
        self._open[bot_id] = db
        return db

    def close(self) -> None:
        for db in self._open.values():
            try:
                db.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._open.clear()

    def _bot_row(self, bot_id: int):
        return self.ctx.db.bot(bot_id)

    def _bot_flags(self, bot_id: int) -> tuple[bool, bool, dict[str, Any]]:
        row = self._bot_row(bot_id)
        if not row or row["template"] not in PAYOUT_TEMPLATES:
            return False, False, {}
        defaults = templates.info(row["template"]).defaults()
        settings = read_settings(self.bot_dir(bot_id), defaults)
        return payouts_bot_enabled(settings), payouts_auto_bot_enabled(settings), settings

    async def _banks_list(self) -> list[dict[str, Any]]:
        if self._banks and time.monotonic() - self._banks_at < 3600:
            return self._banks
        data = await self.client.sbp_banks()
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            self._banks = [i for i in items if isinstance(i, dict)]
            self._banks_at = time.monotonic()
        return self._banks

    async def _send(self, uid: int, text: str) -> None:
        if self.notify and uid:
            try:
                await self.notify(uid, text)
            except Exception:  # noqa: BLE001
                log.info("notify %s failed", uid)

    def record_event(self, etype: str, data: dict) -> dict[str, Any] | None:
        """Apply a signed payout webhook to the child job, before returning 200."""
        if etype not in PAYOUT_EVENTS or not isinstance(data, dict):
            return None
        key = str(data.get("idempotency_key") or "")
        parsed = parse_idempotency_key(key)
        if not parsed:
            return None
        bot_id, wid = parsed
        if not self._bot_flags(bot_id)[0] and not self._job_exists(bot_id, wid):
            return None
        ok = PAYOUT_EVENTS[etype]
        status = str(data.get("status") or ("completed" if ok else "failed"))
        if status not in ("completed", "failed"):
            return None
        applied = self._apply(bot_id, wid, ok=(status == "completed"), payload=data)
        if not applied:
            return None
        return {"bot_id": bot_id, "withdrawal_id": wid, "ok": applied == "completed"}

    def _job_exists(self, bot_id: int, wid: int) -> bool:
        try:
            return self._db(bot_id).payout_job(wid) is not None
        except Exception:  # noqa: BLE001
            return False

    def _apply(self, bot_id: int, wid: int, *, ok: bool, payload: dict) -> str | None:
        db = self._db(bot_id)
        return db.apply_payout_outcome(
            wid,
            ok=ok,
            provider_id=str(payload.get("id") or "") or None,
            debit=str(payload["debit"]) if payload.get("debit") is not None else None,
            error=str(payload.get("error") or "") or None,
            result=_safe_result(payload),
        )

    async def notify_recorded(self, recorded: dict[str, Any]) -> None:
        try:
            await self._notify_outcome(recorded["bot_id"], recorded["withdrawal_id"], recorded["ok"])
        except Exception:  # noqa: BLE001
            log.exception("payout notify failed bot=%s wid=%s", recorded.get("bot_id"), recorded.get("withdrawal_id"))

    async def _notify_outcome(self, bot_id: int, wid: int, ok: bool) -> None:
        db = self._db(bot_id)
        w = db.withdrawal(wid)
        job = db.payout_job(wid)
        row = self._bot_row(bot_id)
        if not w:
            return
        unit = w["currency_at_request"] or ""
        amount = f"{fmt(w['amount'])} {unit}".strip()
        if ok:
            extra = ""
            if job and job["result_json"]:
                try:
                    data = json.loads(job["result_json"])
                    if data.get("tx_url"):
                        extra = f"\nТранзакция: {data['tx_url']}"
                    elif data.get("tx_hash"):
                        extra = f"\nХеш: {data['tx_hash']}"
                except json.JSONDecodeError:
                    extra = ""
            await self._send(w["user_id"], f"✅ Выплата по заявке №{wid} ({amount}) прошла через LavePay.{extra}")
            if row:
                await self._send(row["owner_id"], f"✅ LavePay выплатил заявку №{wid} бота #{bot_id}: {amount}.{extra}")
        else:
            reason = (job["error"] if job and job["error"] else "выплата не прошла")
            await self._send(w["user_id"],
                             f"❌ Выплата по заявке №{wid} ({amount}) не прошла ({reason}). Средства возвращены на баланс.")
            if row:
                await self._send(row["owner_id"],
                                 f"❌ LavePay не выплатил заявку №{wid} бота #{bot_id}: {reason}. Баланс пользователя возвращён.")

    async def poll_once(self) -> None:
        if not self.enabled():
            return
        async with self._lock:
            for bot in self.ctx.db.all("SELECT id, template FROM bots"):
                if bot["template"] not in PAYOUT_TEMPLATES:
                    continue
                if not (self.bot_dir(bot["id"]) / "bot.db").exists():
                    continue
                bot_on, _, _ = self._bot_flags(bot["id"])
                if not bot_on:
                    continue
                try:
                    await self._process_bot(bot["id"])
                except Exception:  # noqa: BLE001
                    log.exception("payout poll failed bot=%s", bot["id"])

    async def poll_loop(self, every: int = POLL_EVERY) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("payout poll loop failed")
            await asyncio.sleep(every)

    async def _process_bot(self, bot_id: int) -> None:
        db = self._db(bot_id)
        now_ts = int(time.time())
        for job in db.payout_jobs_by_status(JOB_SUBMITTED):
            try:
                await self._reconcile(bot_id, job)
            except Exception:  # noqa: BLE001
                log.exception("reconcile failed bot=%s wid=%s", bot_id, job["withdrawal_id"])
        for job in db.payout_jobs_by_status(JOB_QUEUED, now_ts=now_ts):
            try:
                await self._submit(bot_id, job)
            except Exception:  # noqa: BLE001
                log.exception("submit failed bot=%s wid=%s", bot_id, job["withdrawal_id"])

    async def _reconcile(self, bot_id: int, job) -> None:
        ident = job["provider_id"] or job["idempotency_key"]
        if job["method"] == "card":
            payload = await self.client.get_card_payout(ident)
        elif job["method"] == "sbp":
            payload = await self.client.get_sbp_payout(ident)
        elif job["method"] == "usdt_trc20":
            payload = await self.client.get_chain_withdrawal(ident)
        else:
            return
        status = str(payload.get("status") or "")
        if status == "completed":
            applied = self._apply(bot_id, job["withdrawal_id"], ok=True, payload=payload)
            if applied == "completed":
                await self._notify_outcome(bot_id, job["withdrawal_id"], True)
        elif status == "failed":
            applied = self._apply(bot_id, job["withdrawal_id"], ok=False, payload=payload)
            if applied == "failed":
                await self._notify_outcome(bot_id, job["withdrawal_id"], False)

    async def _submit(self, bot_id: int, job) -> None:
        db = self._db(bot_id)
        wid = job["withdrawal_id"]
        w = db.withdrawal(wid)
        if not w or w["status"] != "pending":
            return
        dest = loads_destination(job["destination"])
        if job["amount_rub"]:
            blocked = self.check_daily_limit(bot_id, int(job["amount_rub"]))
            if blocked:
                db.defer_payout_job(wid, error=blocked, delay=_delay(int(job["attempts"] or 0)))
                return
        try:
            payload = await self._send_payout(job, dest)
        except LavePayError as exc:
            await self._handle_error(bot_id, job, exc)
            return
        if not payload:
            return
        status = str(payload.get("status") or "")
        provider_id = str(payload.get("id") or "")
        debit = str(payload["debit"]) if payload.get("debit") is not None else None
        if status == "failed":
            # 200 + failed: money already returned to the merchant; refund the user once.
            applied = db.apply_payout_outcome(
                wid, ok=False, provider_id=provider_id or None, debit=debit,
                error=str(payload.get("error") or "failed"), result=_safe_result(payload),
            )
            if applied == "failed":
                await self._notify_outcome(bot_id, wid, False)
            return
        if status in ("pending", "processing", "completed") and provider_id:
            if status == "completed":
                applied = db.apply_payout_outcome(
                    wid, ok=True, provider_id=provider_id, debit=debit, result=_safe_result(payload),
                )
                if applied == "completed":
                    await self._notify_outcome(bot_id, wid, True)
                return
            if db.mark_payout_submitted(wid, provider_id, debit, _safe_result(payload)) is None:
                log.warning("could not mark submitted bot=%s wid=%s", bot_id, wid)

    async def _send_payout(self, job, dest: dict[str, Any]) -> dict | None:
        method = job["method"]
        key = job["idempotency_key"]
        if method == "card":
            amount = int(job["amount_rub"])
            quote = await self.client.card_rate(amount)
            if quote.get("available") is False:
                raise LavePayError("payout_unavailable", str(quote.get("reason") or "unavailable"), 409)
            price = quote.get("price") or {}
            max_debit = str(price["debit"]) if price.get("debit") is not None else None
            return await self.client.create_card_payout(
                str(dest["card_number"]), amount, key, max_debit=max_debit,
            )
        if method == "sbp":
            amount = int(job["amount_rub"])
            banks = await self._banks_list()
            bank_id = str(dest.get("bank_id") or "")
            if not bank_id:
                matched = match_sbp_bank(banks, str(dest.get("bank") or dest.get("bank_name") or ""))
                if not matched:
                    raise LavePayError("validation_error", "Банк СБП не распознан", 422)
                bank_id = matched["bank_id"]
            quote = await self.client.sbp_rate(amount)
            if quote.get("available") is False:
                raise LavePayError("payout_unavailable", str(quote.get("reason") or "unavailable"), 409)
            price = quote.get("price") or {}
            max_debit = str(price["debit"]) if price.get("debit") is not None else None
            return await self.client.create_sbp_payout(
                str(dest["phone"]), bank_id, str(dest["recipient_name"]), amount, key, max_debit=max_debit,
            )
        if method == "usdt_trc20":
            amount = str(job["amount_asset"] or dest.get("amount") or "")
            quote = await self.client.withdrawals_quote("USDT", "TRX", amount)
            max_debit = str(quote["debit"]) if quote.get("debit") is not None else None
            return await self.client.create_chain_withdrawal(
                "USDT", "TRX", str(dest["address"]), amount, key, max_debit=max_debit,
            )
        raise LavePayError("validation_error", f"unknown method {method}", 422)

    async def _handle_error(self, bot_id: int, job, exc: LavePayError) -> None:
        db = self._db(bot_id)
        wid = job["withdrawal_id"]
        if exc.code in IMMEDIATE_FAIL:
            applied = db.apply_payout_outcome(wid, ok=False, error=f"{exc.code}: {exc.message}")
            if applied == "failed":
                await self._notify_outcome(bot_id, wid, False)
            return
        # Retryable (including insufficient funds and payouts temporarily disabled): keep reserved, try later.
        delay = _delay(int(job["attempts"] or 0))
        db.defer_payout_job(wid, error=f"{exc.code}: {exc.message}", delay=delay)
        if exc.code == "insufficient_funds":
            row = self._bot_row(bot_id)
            if row:
                await self._send(
                    row["owner_id"],
                    f"⏳ Заявка на вывод №{wid} бота #{bot_id} ждёт пополнения баланса LavePay. "
                    "Сумма по-прежнему зарезервирована у пользователя.",
                )

    def check_daily_limit(self, bot_id: int, amount_rub: int | None) -> str | None:
        cap = int(getattr(self.ctx.cfg, "child_lavepay_daily_limit_rub", 0) or 0)
        settings = self._bot_flags(bot_id)[2]
        try:
            bot_cap = int(settings.get("lavepay_daily_limit") or 0)
        except (TypeError, ValueError):
            bot_cap = 0
        limit = 0
        if cap > 0 and bot_cap > 0:
            limit = min(cap, bot_cap)
        elif cap > 0:
            limit = cap
        elif bot_cap > 0:
            limit = bot_cap
        if not limit or not amount_rub:
            return None
        used = self._db(bot_id).paid_or_inflight_rub_since(_day_start(int(time.time())))
        if used + int(amount_rub) > limit:
            return f"Дневной лимит выплат ({limit} ₽) будет превышен"
        return None
