"""Unsubscribe-penalty webhooks for TGrass and PiarFlow (approved rules, 2026-11-10).

Factory APIs have no webhook URL parameter — URLs are pasted in the provider dashboards.
Secrets stay in the private creator DB and are never written to logs or the public tree.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from aiohttp import web

from creator.db import now
from creator.money import rub
from creator.withdrawals import mask

log = logging.getLogger("unsubscribe")

REJECT_REASON = "автоотклонена: штраф за отписку"
PROVIDERS = ("tgrass", "piarflow")
PENALTY_KIND = {"tgrass": "tgrass_penalty", "piarflow": "piarflow_penalty"}
OwnerNotify = Callable[[int, str], Awaitable[None]]
ChildNotify = Callable[[int, int, str], Awaitable[None]]


def normalize_offer_link(link: str) -> str:
    """Canonical form for matching a display link with a provider tracking URL."""
    raw = (link or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query)
    if host in {"piarflow.com"} and path.endswith("/track"):
        for key in ("offer", "link", "url", "target", "channel"):
            if query.get(key):
                return normalize_offer_link(query[key][0])
    if host in {"t.me", "telegram.me"}:
        parts = [p for p in path.split("/") if p and p.lower() != "s"]
        if parts and parts[0].lower() not in {"piarflowbot", "share", "proxy"}:
            return f"https://t.me/{parts[0].lower()}"
        if parts and parts[0].lower() == "piarflowbot":
            for key in ("offer", "link", "url", "target", "channel"):
                if query.get(key):
                    return normalize_offer_link(query[key][0])
            return "https://t.me/piarflowbot"
    if host:
        return f"https://{host}{path}".lower()
    return raw.lower().rstrip("/")


def links_match(left: str | None, right: str | None) -> bool:
    a, b = normalize_offer_link(left or ""), normalize_offer_link(right or "")
    return bool(a and b and a == b)


@dataclass
class ApplyResult:
    applied: bool
    reason: str = ""
    amount: int = 0
    owner_id: int | None = None
    tg_user_id: int | None = None
    bot_id: int | None = None
    provider: str = ""
    withdrawal_id: int | None = None
    withdraw_amount: int = 0
    penalty_id: int | None = None
    username: str = ""


class UnsubscribeService:
    def __init__(self, ctx: Any, notify_owner: OwnerNotify | None = None,
                 notify_child: ChildNotify | None = None):
        self.ctx = ctx
        self.notify_owner = notify_owner
        self.notify_child = notify_child

    def public_base(self) -> str:
        return (getattr(self.ctx.cfg, "webhook_public_base", "") or "https://bnbb.tech").rstrip("/")

    def ensure_secret(self, bot_id: int, provider: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError(provider)
        db = self.ctx.db
        with db.tx() as c:
            row = c.execute("SELECT secret FROM webhook_secrets WHERE bot_id=? AND provider=?",
                            (bot_id, provider)).fetchone()
            if row and row["secret"]:
                return str(row["secret"])
            secret = secrets.token_urlsafe(24)
            c.execute("INSERT INTO webhook_secrets(bot_id,provider,secret,created_at) VALUES(?,?,?,?)",
                      (bot_id, provider, secret, now()))
            return secret

    def url(self, bot_id: int, provider: str) -> str:
        return f"{self.public_base()}/webhooks/{provider}/{int(bot_id)}/{self.ensure_secret(bot_id, provider)}"

    def _secret_ok(self, bot_id: int, provider: str, given: str) -> bool:
        row = self.ctx.db.one("SELECT secret FROM webhook_secrets WHERE bot_id=? AND provider=?",
                              bot_id, provider)
        if not row or not row["secret"] or not given:
            return False
        return hmac.compare_digest(str(row["secret"]), str(given))

    def app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post("/webhooks/{provider}/{bot_id}/{secret}", self.handle)
        return app

    async def start(self, host: str, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("unsubscribe webhooks listening on http://%s:%s/webhooks/", host, port)
        return runner

    async def handle(self, request: web.Request) -> web.Response:
        provider = str(request.match_info.get("provider") or "")
        secret = str(request.match_info.get("secret") or "")
        try:
            bot_id = int(request.match_info.get("bot_id") or "")
        except (TypeError, ValueError):
            return web.json_response({"ok": False}, status=404)
        if provider not in PROVIDERS or not self._secret_ok(bot_id, provider, secret):
            return web.json_response({"ok": False}, status=404)
        if self.ctx.db.bot(bot_id) is None:
            return web.json_response({"ok": False}, status=404)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False}, status=400)
        if payload.get("test") is True:
            return web.json_response({"ok": True})
        try:
            await self.apply(provider, bot_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("unsubscribe apply failed provider=%s bot_id=%s", provider, bot_id)
            return web.json_response({"ok": False}, status=500)
        return web.json_response({"ok": True})

    def _telegram_id(self, bot_id: int) -> int | None:
        b = self.ctx.db.bot(bot_id)
        if not b:
            return None
        from creator.piarflow import token_bot_id
        pf = self.ctx.db.pf(bot_id)
        if pf and pf["pf_bot_id"]:
            return int(pf["pf_bot_id"])
        ident = token_bot_id(b["token"])
        return ident or None

    def _find_op(self, c: sqlite3.Connection, provider: str, bot_id: int, payload: dict) -> sqlite3.Row | None:
        try:
            uid = int(payload.get("tg_user_id"))
        except (TypeError, ValueError):
            return None
        incoming = str(payload.get("offer_link") or "")
        if provider == "tgrass":
            rows = c.execute("SELECT * FROM tgrass_ops WHERE bot_id=? AND tg_user_id=?",
                             (bot_id, uid)).fetchall()
            matches = [r for r in rows if links_match(r["offer_link"], incoming)]
        else:
            body_bot = payload.get("bot_id")
            if body_bot is not None:
                try:
                    expected = self._telegram_id(bot_id)
                    if expected and int(body_bot) != expected:
                        return None
                except (TypeError, ValueError):
                    return None
            rows = c.execute("SELECT * FROM pf_ops WHERE bot_id=? AND tg_user_id=?",
                             (bot_id, uid)).fetchall()
            matches = [r for r in rows if links_match(r["link"], incoming)]
        return matches[0] if len(matches) == 1 else None

    def apply_once(self, provider: str, bot_id: int, payload: dict) -> ApplyResult:
        if provider not in PROVIDERS:
            return ApplyResult(False, "no_op", provider=provider, bot_id=bot_id)
        db = self.ctx.db
        b = db.bot(bot_id)
        username = (b["username"] if b else "") or ""
        already = None
        need_revert = False
        op = None
        with db.tx() as c:
            op = self._find_op(c, provider, bot_id, payload)
            if op is None:
                return ApplyResult(False, "no_op", provider=provider, bot_id=bot_id)
            existing = c.execute(
                "SELECT * FROM unsubscribe_penalties WHERE provider=? AND bot_id=? AND op_id=?",
                (provider, bot_id, op["id"])).fetchone()
            if existing:
                already = ApplyResult(False, "already", amount=int(existing["amount"]),
                                   owner_id=int(existing["owner_id"]), tg_user_id=int(existing["tg_user_id"]),
                                   bot_id=bot_id, provider=provider, penalty_id=int(existing["id"]),
                                   username=username)
                need_revert = not existing["child_reverted"]
            else:
                owner_id = int(op["owner_id"])
                amount = max(0, int(op["reward_kop"]))
                pending = c.execute(
                    "SELECT * FROM withdrawal_requests WHERE user_id=? AND status='pending'",
                    (owner_id,)).fetchone()
                withdrawal_id = None
                withdraw_amount = 0
                if pending:
                    cur = c.execute(
                        "UPDATE withdrawal_requests SET status='rejected', reason=?, details=?, "
                        "resolved_at=?, resolved_by=? WHERE id=? AND status='pending'",
                        (REJECT_REASON, mask(pending["details"]), now(), 0, pending["id"]))
                    if cur.rowcount == 1:
                        db._change_balance(c, owner_id, int(pending["amount"]), "withdraw_refund",
                                           f"Возврат по заявке на вывод №{pending['id']}")
                        withdrawal_id = int(pending["id"])
                        withdraw_amount = int(pending["amount"])
                if amount:
                    db._change_balance(c, owner_id, -amount, PENALTY_KIND[provider],
                                       f"Штраф за отписку в боте ID {bot_id}", allow_negative=True)
                offer_key = str(op["offer_link"] if provider == "tgrass" else op["link"] or "")
                cur = c.execute(
                    "INSERT INTO unsubscribe_penalties(provider,bot_id,op_id,tg_user_id,offer_key,amount,"
                    "owner_id,withdrawal_request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (provider, bot_id, op["id"], int(op["tg_user_id"]), offer_key, amount,
                     owner_id, withdrawal_id, now()))
                penalty_id = int(cur.lastrowid)
                need_revert = True
                already = ApplyResult(True, "applied", amount=amount, owner_id=owner_id,
                                      tg_user_id=int(op["tg_user_id"]), bot_id=bot_id, provider=provider,
                                      withdrawal_id=withdrawal_id, withdraw_amount=withdraw_amount,
                                      penalty_id=penalty_id, username=username)
        if already and need_revert and op is not None:
            self._try_revert(already, provider, bot_id, op)
        return already or ApplyResult(False, "no_op", provider=provider, bot_id=bot_id)

    def _try_revert(self, result: ApplyResult, provider: str, bot_id: int, op: sqlite3.Row) -> None:
        try:
            self._revert_child(provider, bot_id, int(op["tg_user_id"]),
                               offer_id=int(op["offer_id"]) if provider == "tgrass" else None,
                               link=str(op["link"]) if provider == "piarflow" else (op["offer_link"] or ""))
            if result.penalty_id:
                with self.ctx.db.tx() as c:
                    c.execute("UPDATE unsubscribe_penalties SET child_reverted=1 WHERE id=?",
                              (result.penalty_id,))
        except Exception:  # noqa: BLE001
            log.exception("child revert failed provider=%s bot_id=%s", provider, bot_id)

    async def apply(self, provider: str, bot_id: int, payload: dict) -> ApplyResult:
        result = self.apply_once(provider, bot_id, payload)
        if result.reason == "applied":
            await self._notify(result)
        return result

    async def _notify(self, result: ApplyResult) -> None:
        if result.withdrawal_id and result.owner_id and self.notify_owner:
            text = (f"Заявка на вывод №{result.withdrawal_id} отклонена: {REJECT_REASON}. "
                    f"{rub(result.withdraw_amount)} возвращены на баланс.")
            try:
                await self.notify_owner(result.owner_id, text)
            except Exception as e:  # noqa: BLE001
                log.info("withdraw reject notify failed penalty=%s: %s", result.penalty_id, e)
        title = "TGrass" if result.provider == "tgrass" else "PiarFlow"
        bot_ref = f"@{result.username}" if result.username else f"ID {result.bot_id}"
        if result.applied and result.owner_id and self.notify_owner:
            text = (f"С вашего баланса списана награда {rub(result.amount)}: пользователь отписался "
                    f"от задания {title} в боте {bot_ref} (ID {result.bot_id}).")
            try:
                await self.notify_owner(result.owner_id, text)
                if result.penalty_id:
                    self.ctx.db.conn.execute(
                        "UPDATE unsubscribe_penalties SET owner_notified=1 WHERE id=?", (result.penalty_id,))
            except Exception as e:  # noqa: BLE001
                log.info("owner penalty notify failed penalty=%s: %s", result.penalty_id, e)
        if result.applied and result.tg_user_id and result.bot_id and self.notify_child:
            text = ("Подписка на спонсора была снята, поэтому задание снова нужно выполнить, "
                    "чтобы пользоваться ботом.")
            try:
                await self.notify_child(result.bot_id, result.tg_user_id, text)
                if result.penalty_id:
                    self.ctx.db.conn.execute(
                        "UPDATE unsubscribe_penalties SET user_notified=1 WHERE id=?", (result.penalty_id,))
            except Exception as e:  # noqa: BLE001
                log.info("child penalty notify failed penalty=%s: %s", result.penalty_id, e)

    def _revert_child(self, provider: str, bot_id: int, uid: int, *,
                      offer_id: int | None, link: str) -> None:
        bot_dir = self.ctx.sup.bot_dir(bot_id)
        if provider == "tgrass":
            from child.tgrass import revert_completion
            revert_completion(bot_dir, uid, int(offer_id or 0))
        else:
            from child.piarflow import revert_completion
            revert_completion(bot_dir, uid, link)


def attach(ctx: Any, notify_owner: OwnerNotify | None = None,
           notify_child: ChildNotify | None = None) -> UnsubscribeService:
    ctx.unsub = UnsubscribeService(ctx, notify_owner=notify_owner, notify_child=notify_child)
    return ctx.unsub


async def send_as_child(ctx: Any, bot_id: int, user_id: int, text: str) -> None:
    """Deliver a single penalty notice through the child bot token. Never log the token."""
    from aiogram import Bot
    b = ctx.db.bot(bot_id)
    if not b or not b["token"]:
        return
    bot = Bot(b["token"])
    try:
        await bot.send_message(user_id, text)
    except Exception as e:  # noqa: BLE001
        log.info("child notify failed bot_id=%s: %s", bot_id, e)
    finally:
        await bot.session.close()
