"""Фейковая сессия Telegram: бот работает без сети и без настоящего токена."""
from __future__ import annotations

import itertools
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    CopyMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    ForwardMessage,
    GetChat,
    GetChatMember,
    GetMe,
    SendInvoice,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, Message, MessageId, Update, User

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ids = itertools.count(1000)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.member_status = "member"

    def of(self, cls) -> list[Any]:
        return [r for r in self.requests if isinstance(r, cls)]

    def texts(self) -> list[str]:
        return [r.text for r in self.requests if isinstance(r, (SendMessage, EditMessageText))]

    def clear(self) -> None:
        self.requests.clear()

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.requests.append(method)
        now = datetime.now()
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Test", username="test_bot")
        if isinstance(method, (SendMessage, SendInvoice, ForwardMessage)):
            return Message(message_id=next(_ids), date=now, chat=Chat(id=int(method.chat_id), type="private"),
                           text=getattr(method, "text", None)).as_(bot)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return True
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(_ids))
        if isinstance(method, GetChat):
            from types import SimpleNamespace
            return SimpleNamespace(id=-100123, type="channel", username="chan", title="Channel")
        if isinstance(method, GetChatMember):
            if method.user_id == bot.id:
                from types import SimpleNamespace
                return SimpleNamespace(status="administrator")
            user = User(id=int(method.user_id), is_bot=False, first_name="U")
            if self.member_status == "member":
                return ChatMemberMember(user=user)
            return ChatMemberLeft(user=user)
        return True

    async def stream_content(self, *a: Any, **kw: Any):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


_upd = itertools.count(1)


def user(uid: int, name: str = "User") -> dict:
    return {"id": uid, "is_bot": False, "first_name": f"{name}{uid}", "username": f"u{uid}"}


def msg_update(uid: int, text: str | None = None, **extra: Any) -> Update:
    m = {"message_id": next(_ids), "date": int(datetime.now().timestamp()),
         "chat": {"id": uid, "type": "private"}, "from": user(uid)}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return Update.model_validate({"update_id": next(_upd), "message": m})


def cb_update(uid: int, data: str) -> Update:
    return Update.model_validate({
        "update_id": next(_upd),
        "callback_query": {
            "id": str(next(_ids)), "from": user(uid), "chat_instance": "x", "data": data,
            "message": {"message_id": next(_ids), "date": int(datetime.now().timestamp()),
                        "chat": {"id": uid, "type": "private"}, "text": "old"},
        },
    })


@pytest.fixture
def session() -> MockSession:
    return MockSession()


@pytest.fixture
def bot(session: MockSession) -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    return Bot("42:TEST-TOKEN", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
