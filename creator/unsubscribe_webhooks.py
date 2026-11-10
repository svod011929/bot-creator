"""Unsubscribe-penalty webhooks for TGrass and PiarFlow (approved rules, 2026-11-10).

Factory APIs have no webhook URL parameter — URLs are pasted in the provider dashboards.
Secrets stay in the private creator DB and are never written to logs or the public tree.
"""
from __future__ import annotations

import hmac
import html
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
HUB_BOT_ID = 0
HUB_PATH = "hub"
TG_PASTE = "TGrass — Настройки бота → Webhook → отписка:"
PF_PASTE = "PiarFlow — кабинет @PiarFlowBot → Webhook / API:"
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
                 notify_child: ChildNotify | None = None,
                 notify_owner_strict: OwnerNotify | None = None):
        self.ctx = ctx
        self.notify_owner = notify_owner
        self.notify_child = notify_child
        self.notify_owner_strict = notify_owner_strict or notify_owner

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
        token = HUB_PATH if int(bot_id) == HUB_BOT_ID else str(int(bot_id))
        return f"{self.public_base()}/webhooks/{provider}/{token}/{self.ensure_secret(bot_id, provider)}"

    def hub_url(self, provider: str) -> str:
        return self.url(HUB_BOT_ID, provider)

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
        raw_bot = str(request.match_info.get("bot_id") or "")
        is_hub = raw_bot == HUB_PATH
        if is_hub:
            bot_id = HUB_BOT_ID
        else:
            try:
                bot_id = int(raw_bot)
            except (TypeError, ValueError):
                return web.json_response({"ok": False}, status=404)
        if provider not in PROVIDERS or not self._secret_ok(bot_id, provider, secret):
            return web.json_response({"ok": False}, status=404)
        if not is_hub and self.ctx.db.bot(bot_id) is None:
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
            if is_hub:
                await self.apply_hub(provider, payload)
            else:
                await self.apply(provider, bot_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("unsubscribe apply failed provider=%s bot_id=%s",
                          provider, HUB_PATH if is_hub else bot_id)
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


    def _relay_matches(self, provider: str, payload: dict) -> list:
        """Credited ops on child bots currently in relay mode, matched by user + offer link."""
        try:
            uid = int(payload.get("tg_user_id"))
        except (TypeError, ValueError):
            return []
        incoming = str(payload.get("offer_link") or "")
        ops_table = "tgrass_ops" if provider == "tgrass" else "pf_ops"
        net_table = "tgrass_bots" if provider == "tgrass" else "pf_bots"
        link_col = "offer_link" if provider == "tgrass" else "link"
        with self.ctx.db._lock:
            rows = self.ctx.db.conn.execute(
                f"SELECT o.* FROM {ops_table} o JOIN {net_table} n ON n.bot_id = o.bot_id "
                f"WHERE n.mode = 'relay' AND o.tg_user_id = ?",
                (uid,)).fetchall()
        return [r for r in rows if links_match(r[link_col], incoming)]

    async def apply_hub(self, provider: str, payload: dict) -> ApplyResult:
        """Apply a hub-URL event only when it maps to exactly one relay-mode credit.

        Several relay children can credit the same user for the same offer (shared hub key,
        TGrass events have no child bot_id). That case is left unpenalized on purpose.
        """
        if provider not in PROVIDERS:
            return ApplyResult(False, "no_op", provider=provider)
        matches = self._relay_matches(provider, payload)
        if len(matches) != 1:
            if len(matches) > 1:
                log.warning("unsubscribe hub event skipped: %s relay ops provider=%s",
                            len(matches), provider)
            return ApplyResult(False, "ambiguous" if len(matches) > 1 else "no_op",
                               provider=provider)
        body = dict(payload)
        body.pop("bot_id", None)  # hub payload bot_id is the creator, not the child
        return await self.apply(provider, int(matches[0]["bot_id"]), body)

    def _pending_url_notices(self, bot_id: int, providers: tuple[str, ...]) -> list[tuple[str, str]]:
        b = self.ctx.db.bot(bot_id)
        if not b:
            return []
        pending: list[tuple[str, str]] = []
        for provider in providers:
            if provider not in PROVIDERS:
                continue
            row = self.ctx.db.tgrass(bot_id) if provider == "tgrass" else self.ctx.db.pf(bot_id)
            if not row or row["status"] != "approved":
                continue
            secret = self.ensure_secret(bot_id, provider)
            already = self.ctx.db.one(
                "SELECT 1 FROM webhook_url_notices WHERE bot_id=? AND provider=? AND secret=?",
                bot_id, provider, secret)
            if already:
                continue
            pending.append((provider, secret))
        return pending

    async def notify_owner_urls(self, bot_id: int,
                                providers: tuple[str, ...] | None = None) -> int:
        """Send the current owner the webhook URL(s). Mark sent only after a successful send."""
        b = self.ctx.db.bot(bot_id)
        if not b:
            return 0
        wanted = providers or PROVIDERS
        pending = self._pending_url_notices(bot_id, wanted)
        if not pending:
            return 0
        sender = self.notify_owner_strict
        if sender is None:
            log.info("owner url notice skipped bot_id=%s: no sender", bot_id)
            return 0
        urls = {provider: self.url(bot_id, provider) for provider, _secret in pending}
        text = owner_url_notice_text(bot_id, b["username"] or "", urls)
        try:
            await sender(int(b["owner_id"]), text)
        except Exception as e:  # noqa: BLE001
            log.info("owner url notice failed bot_id=%s: %s", bot_id, e)
            return 0
        with self.ctx.db.tx() as c:
            for provider, secret in pending:
                c.execute(
                    "INSERT OR IGNORE INTO webhook_url_notices"
                    "(bot_id,provider,secret,owner_id,sent_at) VALUES(?,?,?,?,?)",
                    (bot_id, provider, secret, int(b["owner_id"]), now()))
        return len(pending)

    async def backfill_owner_url_notices(self) -> dict[str, int]:
        """One-shot: notify owners of currently approved integrations. Idempotent."""
        db = self.ctx.db
        bot_ids = {
            int(r["bot_id"])
            for table in ("tgrass_bots", "pf_bots")
            for r in db.conn.execute(
                f"SELECT bot_id FROM {table} WHERE status='approved'").fetchall()
        }
        sent = failed = 0
        for bot_id in sorted(bot_ids):
            pending = self._pending_url_notices(bot_id, PROVIDERS)
            if not pending:
                continue
            n = await self.notify_owner_urls(bot_id)
            if n:
                sent += 1
            else:
                failed += 1
        return {"sent": sent, "failed": failed}


def owner_url_notice_text(bot_id: int, username: str, urls: dict[str, str]) -> str:
    uname = html.escape(username or "—")
    parts = [
        f"🔗 <b>Вебхуки отписок</b> для бота ID {bot_id} @{uname}\n",
        "Вставьте эти HTTPS-адреса в кабинеты сетей. Фабрика URL сама не ставит — "
        "параметра вебхука в API нет.\n",
    ]
    if "tgrass" in urls:
        parts.append(f"{TG_PASTE}\n<code>{html.escape(urls['tgrass'])}</code>\n")
    if "piarflow" in urls:
        parts.append(f"{PF_PASTE}\n<code>{html.escape(urls['piarflow'])}</code>\n")
    parts.append("Штраф за отписку равен начисленной награде и действует после того, "
                 "как адрес установлен.\n")
    parts.append("Никому не пересылайте эти ссылки: кто их знает, сможет слать ложные отписки.\n")
    parts.append("Те же адреса всегда можно открыть кнопкой «🔗 Вебхуки отписок» в карточке бота.")
    return "\n".join(parts)


async def hook_after_approve(ctx: Any, bot_id: int, provider: str) -> None:
    unsub = getattr(ctx, "unsub", None)
    if unsub is None:
        return
    try:
        await unsub.notify_owner_urls(bot_id, providers=(provider,))
    except Exception:  # noqa: BLE001
        log.exception("owner webhook url notice failed bot_id=%s provider=%s", bot_id, provider)


def attach(ctx: Any, notify_owner: OwnerNotify | None = None,
           notify_child: ChildNotify | None = None,
           notify_owner_strict: OwnerNotify | None = None) -> UnsubscribeService:
    ctx.unsub = UnsubscribeService(ctx, notify_owner=notify_owner, notify_child=notify_child,
                                   notify_owner_strict=notify_owner_strict)
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
