"""Cross-bot admin broadcast: one explicit send to users of every non-deleted child bot.

The platform admin previews a recipient count and confirms. Nothing is sent on
startup, on a schedule, or after a restart. File IDs cannot be reused across
bots, so content is re-sent (text as-is; media is downloaded via the constructor
bot and uploaded by each child bot). Tokens are never logged or stored in FSM.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, Message

from creator.db import DB

log = logging.getLogger(__name__)

SEND_PAUSE = 0.05
MAX_RETRY_AFTER = 30
_payloads: dict[int, Message] = {}
_running = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def store_payload(admin_id: int, message: Message) -> None:
    _payloads[admin_id] = message


def pop_payload(admin_id: int) -> Message | None:
    return _payloads.pop(admin_id, None)


def clear_payload(admin_id: int) -> None:
    _payloads.pop(admin_id, None)


def can_relay(message: Message) -> bool:
    if message.text:
        return True
    return bool(
        message.photo or message.video or message.document
        or message.animation or message.audio or message.voice
    )


def child_recipient_ids(bot_dir: Path) -> list[int]:
    """Active users of a child bot: not Telegram-blocked and not owner-banned."""
    db_path = bot_dir / "bot.db"
    if not db_path.exists():
        return []
    try:
        from child.db import ChildDB
        db = ChildDB.open_existing(db_path, writable=False)
        try:
            try:
                rows = db.all(
                    "SELECT id FROM users WHERE IFNULL(blocked,0)=0 AND IFNULL(banned,0)=0"
                )
            except Exception:  # noqa: BLE001 — older child DBs may lack banned
                rows = db.all("SELECT id FROM users WHERE IFNULL(blocked,0)=0")
            return [int(r[0]) for r in rows]
        finally:
            db.conn.close()
    except Exception:  # noqa: BLE001
        log.warning("could not read child users from %s", db_path)
        return []


def collect_recipient_counts(db: DB, bot_dir_for) -> list[tuple[int, list[int]]]:
    """Return (bot_id, user_ids) for non-deleted bots that have recipients. No tokens."""
    out: list[tuple[int, list[int]]] = []
    for row in db.all("SELECT id FROM bots WHERE deleted=0 ORDER BY id"):
        ids = child_recipient_ids(bot_dir_for(row["id"]))
        if ids:
            out.append((int(row["id"]), ids))
    return out


def load_bot_token(db: DB, bot_id: int) -> str | None:
    """Return the child token for a send. Caller must not log or persist it."""
    row = db.one("SELECT token FROM bots WHERE id=? AND deleted=0", bot_id)
    if not row:
        return None
    token = str(row["token"] or "")
    return token or None


async def _download(source: Message, file_id: str, name: str) -> BufferedInputFile:
    buf = await source.bot.download(file_id)
    data = buf.read() if hasattr(buf, "read") else buf
    return BufferedInputFile(data, filename=name)


async def relay_to_child(source: Message, dest: Bot, chat_id: int) -> None:
    """Send the admin's message through a child bot. Never uses copy_message."""
    if source.text:
        await dest.send_message(chat_id, source.text, entities=source.entities or None)
        return
    caption = source.caption
    entities = source.caption_entities or None
    if source.photo:
        media = await _download(source, source.photo[-1].file_id, "photo.jpg")
        await dest.send_photo(chat_id, media, caption=caption, caption_entities=entities)
        return
    if source.video:
        media = await _download(source, source.video.file_id, "video.mp4")
        await dest.send_video(chat_id, media, caption=caption, caption_entities=entities)
        return
    if source.document:
        name = source.document.file_name or "file.bin"
        media = await _download(source, source.document.file_id, name)
        await dest.send_document(chat_id, media, caption=caption, caption_entities=entities)
        return
    if source.animation:
        media = await _download(source, source.animation.file_id, "animation.mp4")
        await dest.send_animation(chat_id, media, caption=caption, caption_entities=entities)
        return
    if source.audio:
        media = await _download(source, source.audio.file_id, "audio.mp3")
        await dest.send_audio(chat_id, media, caption=caption, caption_entities=entities)
        return
    if source.voice:
        media = await _download(source, source.voice.file_id, "voice.ogg")
        await dest.send_voice(chat_id, media, caption=caption, caption_entities=entities)
        return
    raise ValueError("unsupported message type")


async def _deliver(dest: Bot, source: Message, chat_id: int) -> bool:
    """True if delivered, False if the user blocked the bot or another hard error.

    TelegramRetryAfter is retried; the user is not skipped after a flood wait.
    """
    attempts = 0
    while True:
        try:
            await relay_to_child(source, dest, chat_id)
            return True
        except TelegramRetryAfter as exc:
            attempts += 1
            wait = min(float(exc.retry_after or 1), MAX_RETRY_AFTER)
            await asyncio.sleep(wait)
            if attempts >= 3:
                return False
        except TelegramForbiddenError:
            return False
        except Exception:  # noqa: BLE001
            log.exception("child broadcast send failed")
            return False


async def run_broadcast(
    *,
    db: DB,
    session: Any,
    source: Message,
    targets: list[tuple[int, list[int]]],
    admin_id: int,
) -> tuple[int, int]:
    job_id = db.start_bot_broadcast(admin_id, sum(len(ids) for _, ids in targets))
    delivered = failed = 0
    try:
        for bot_id, ids in targets:
            token = load_bot_token(db, bot_id)
            if not token:
                failed += len(ids)
                continue
            try:
                child = Bot(token=token, session=session)
            except Exception:  # noqa: BLE001 — skip a bot with a unusable token; do not abort the job
                log.exception("child broadcast could not open bot %s", bot_id)
                failed += len(ids)
                continue
            try:
                for uid in ids:
                    if not db.claim_bot_broadcast_recipient(job_id, bot_id, uid):
                        continue
                    if await _deliver(child, source, uid):
                        delivered += 1
                    else:
                        failed += 1
                    await asyncio.sleep(SEND_PAUSE)
            finally:
                if child.session is not session:
                    await child.session.close()
        db.finish_bot_broadcast(job_id, delivered, failed)
        db.log_admin(
            admin_id, "broadcast_bots_done",
            detail=f"job={job_id} delivered={delivered} failed={failed}",
        )
        return delivered, failed
    except Exception:
        db.finish_bot_broadcast(job_id, delivered, failed, status="interrupted")
        raise


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain() -> None:
    """Wait for in-flight broadcasts. Used by tests only."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def is_busy() -> bool:
    return _running.locked()


def lock() -> asyncio.Lock:
    return _running
