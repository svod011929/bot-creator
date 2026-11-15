"""Constructor-side worker for owner-shop USDT TRC-20 payouts.

Sends money only from the bot owner's LavePay shop, and only after the owner
confirms a live quote. The platform merchant is not charged. Children never
receive API keys. Insufficient merchant funds leave the request pending.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from child.db import ChildDB, fmt
from child.payouts import (
    JOB_AWAITING,
    JOB_QUEUED,
    JOB_SUBMITTED,
    PAYOUT_TEMPLATES,
    loads_destination,
    parse_idempotency_key,
)
from creator.lavepay import LavePayError
from creator.owner_payouts import (
    child_flags,
    convert_amount,
    owner_client,
    quote_for_job,
    shop_plain_key,
)

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
    "withdrawal.completed": True,
    "withdrawal.failed": False,
}
BACKOFF = (60, 120, 300, 600, 1800, 3600)
Notify = Callable[[int, str], Awaitable[None]]


def _delay(attempts: int) -> int:
    return BACKOFF[min(max(attempts, 0), len(BACKOFF) - 1)]


def _safe_result(payload: dict) -> dict[str, Any]:
    keep = ("id", "status", "amount", "fee", "debit", "currency", "asset", "network",
            "tx_hash", "tx_url", "error", "replayed", "idempotency_key")
    out = {k: payload.get(k) for k in keep if payload.get(k) is not None}
    addr = payload.get("address")
    if isinstance(addr, str) and len(addr) > 10:
        out["address"] = f"{addr[:4]}…{addr[-4:]}"
    return out


class ChildPayoutService:
    def __init__(self, ctx: Ctx, client=None, notify: Notify | None = None):
        self.ctx = ctx
        self.client = client  # platform client, unused for owner payouts
        self.notify = notify
        self._lock = asyncio.Lock()
        self._open: dict[int, ChildDB] = {}

    def enabled(self) -> bool:
        return bool(getattr(self.ctx.cfg, "child_lavepay_payouts", False))

    def bot_dir(self, bot_id: int) -> Path:
        return self.ctx.cfg.bots_dir / str(bot_id)

    def _db(self, bot_id: int) -> ChildDB:
        db = self._open.get(bot_id)
        if db is not None:
            return db
        db = ChildDB(self.bot_dir(bot_id) / "bot.db")
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

    async def _send(self, uid: int, text: str) -> None:
        if self.notify and uid:
            try:
                await self.notify(uid, text)
            except Exception:  # noqa: BLE001
                log.info("notify %s failed", uid)

    def record_event(self, etype: str, data: dict) -> dict[str, Any] | None:
        """Owner shops do not share the platform webhook secret; polling is the source of truth."""
        if etype not in PAYOUT_EVENTS or not isinstance(data, dict):
            return None
        parsed = parse_idempotency_key(str(data.get("idempotency_key") or ""))
        if not parsed:
            return None
        bot_id, wid = parsed
        if self.ctx.db.owner_lavepay_shop(bot_id) is None:
            return None
        ok = PAYOUT_EVENTS[etype]
        status = str(data.get("status") or ("completed" if ok else "failed"))
        if status not in ("completed", "failed"):
            return None
        applied = self._apply(bot_id, wid, ok=(status == "completed"), payload=data)
        if not applied:
            return None
        return {"bot_id": bot_id, "withdrawal_id": wid, "ok": applied == "completed"}

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
            await self._send(
                w["user_id"],
                f"❌ Выплата по заявке №{wid} ({amount}) не прошла ({reason}). Средства возвращены на баланс.",
            )
            if row:
                await self._send(
                    row["owner_id"],
                    f"❌ LavePay не выплатил заявку №{wid} бота #{bot_id}: {reason}. Баланс пользователя возвращён.",
                )

    async def poll_once(self) -> None:
        if not self.enabled():
            return
        async with self._lock:
            for bot in self.ctx.db.all("SELECT id, template FROM bots"):
                if bot["template"] not in PAYOUT_TEMPLATES:
                    continue
                if not (self.bot_dir(bot["id"]) / "bot.db").exists():
                    continue
                if shop_plain_key(self.ctx.cfg, self.ctx.db, bot["id"]) is None:
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

    async def _owner_client(self, bot_id: int):
        return await owner_client(self.ctx.cfg, self.ctx.db, bot_id)

    async def _reconcile(self, bot_id: int, job) -> None:
        client = await self._owner_client(bot_id)
        try:
            ident = job["provider_id"] or job["idempotency_key"]
            payload = await client.get_chain_withdrawal(ident)
        finally:
            await client.close()
        status = str(payload.get("status") or "")
        if status == "completed":
            applied = self._apply(bot_id, job["withdrawal_id"], ok=True, payload=payload)
            if applied == "completed":
                await self._notify_outcome(bot_id, job["withdrawal_id"], True)
        elif status == "failed":
            applied = self._apply(bot_id, job["withdrawal_id"], ok=False, payload=payload)
            if applied == "failed":
                await self._notify_outcome(bot_id, job["withdrawal_id"], False)

    def job_amounts(self, bot_id: int, wid: int) -> tuple[str | None, Decimal | None]:
        flags = child_flags(self.ctx.cfg, self.ctx.db, bot_id)
        w = self._db(bot_id).withdrawal(wid)
        if not w or not flags.get("lavepay_unit_kind") or not flags.get("lavepay_unit_rate"):
            raise ValueError("unit")
        return convert_amount(float(w["amount"]), flags["lavepay_unit_kind"], flags["lavepay_unit_rate"])

    async def quote_withdrawal(self, bot_id: int, wid: int) -> dict[str, Any]:
        db = self._db(bot_id)
        w = db.withdrawal(wid)
        job = db.payout_job(wid)
        if not w or w["status"] != "pending" or not job or job["status"] not in (JOB_AWAITING, JOB_QUEUED):
            raise ValueError("unavailable")
        amount_usdt, amount_rub = self.job_amounts(bot_id, wid)
        client = await self._owner_client(bot_id)
        try:
            quote = await quote_for_job(client, amount_usdt=amount_usdt, amount_rub=amount_rub)
        finally:
            await client.close()
        return quote

    async def confirm_and_queue(self, bot_id: int, wid: int, quote: dict[str, Any]) -> str:
        """Freeze the shown quote, queue the job, and try to send. Returns a status word."""
        db = self._db(bot_id)
        if db.queue_payout_job(wid) is None:
            job = db.payout_job(wid)
            if not job or job["status"] not in (JOB_QUEUED, JOB_SUBMITTED, "completed"):
                raise ValueError("unavailable")
        db.exec(
            "UPDATE payout_jobs SET amount_asset=?, debit=?, result_json=?, updated_at=? WHERE withdrawal_id=?",
            quote["amount"], quote.get("debit"),
            json.dumps({"quoted": quote}, ensure_ascii=False, separators=(",", ":")),
            int(time.time()), wid,
        )
        job = db.payout_job(wid)
        if job["status"] == JOB_QUEUED:
            await self._submit(bot_id, job, max_debit=str(quote.get("debit") or ""))
        job = db.payout_job(wid)
        w = db.withdrawal(wid)
        if job and job["status"] == "completed":
            return "completed"
        if job and job["status"] == "failed":
            return "failed"
        if job and job["status"] == JOB_SUBMITTED:
            return "submitted"
        if job and job["status"] == JOB_QUEUED:
            return "queued"
        if w and w["status"] == "pending":
            return "pending"
        return job["status"] if job else "missing"

    async def _submit(self, bot_id: int, job, max_debit: str | None = None) -> None:
        db = self._db(bot_id)
        wid = job["withdrawal_id"]
        w = db.withdrawal(wid)
        if not w or w["status"] != "pending":
            return
        dest = loads_destination(job["destination"])
        address = str(dest.get("address") or "")
        if not address:
            applied = db.apply_payout_outcome(wid, ok=False, error="validation_error: нет адреса")
            if applied == "failed":
                await self._notify_outcome(bot_id, wid, False)
            return
        amount = str(job["amount_asset"] or dest.get("amount") or "")
        if not amount:
            try:
                amount, _rub = self.job_amounts(bot_id, wid)
                if not amount:
                    raise ValueError("need quote")
            except ValueError:
                db.defer_payout_job(
                    wid, error="Нужно подтверждение курса в конструкторе",
                    delay=_delay(int(job["attempts"] or 0)),
                )
                return
        client = await self._owner_client(bot_id)
        try:
            try:
                if not max_debit:
                    quote = await quote_for_job(client, amount_usdt=amount)
                    max_debit = quote.get("debit")
                payload = await client.create_chain_withdrawal(
                    "USDT", "TRX", address, amount, job["idempotency_key"], max_debit=max_debit,
                )
            except LavePayError as exc:
                await self._handle_error(bot_id, job, exc)
                return
        finally:
            await client.close()
        status = str(payload.get("status") or "")
        provider_id = str(payload.get("id") or "")
        debit = str(payload["debit"]) if payload.get("debit") is not None else None
        if status == "failed":
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

    async def _handle_error(self, bot_id: int, job, exc: LavePayError) -> None:
        db = self._db(bot_id)
        wid = job["withdrawal_id"]
        # Insufficient funds and temporary provider issues: keep the user reservation, tell the owner.
        if exc.code == "insufficient_funds" or exc.code in RETRYABLE - {"rate_changed"}:
            # Roll back to awaiting_approval so the owner can retry after funding.
            db.exec(
                "UPDATE payout_jobs SET status='awaiting_approval',error=?,updated_at=?,next_attempt_at=0 "
                "WHERE withdrawal_id=? AND status IN ('queued','awaiting_approval')",
                f"{exc.code}: {exc.message}"[:500], int(time.time()), wid,
            )
            row = self._bot_row(bot_id)
            if row:
                if exc.code == "insufficient_funds":
                    await self._send(
                        row["owner_id"],
                        f"⏳ На магазине LavePay не хватает USDT для заявки №{wid} бота #{bot_id}. "
                        "Заявка остаётся в ожидании, баланс пользователя не возвращён. "
                        "Пополните магазин и подтвердите выплату снова.",
                    )
                else:
                    await self._send(
                        row["owner_id"],
                        f"⏳ Выплата заявки №{wid} бота #{bot_id} временно недоступна ({exc.message}). "
                        "Заявка остаётся в ожидании.",
                    )
            return
        if exc.code in IMMEDIATE_FAIL:
            applied = db.apply_payout_outcome(wid, ok=False, error=f"{exc.code}: {exc.message}")
            if applied == "failed":
                await self._notify_outcome(bot_id, wid, False)
            return
        delay = _delay(int(job["attempts"] or 0))
        db.defer_payout_job(wid, error=f"{exc.code}: {exc.message}", delay=delay)
