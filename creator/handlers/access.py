"""Server-side admin access check shared by admin routers."""
from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from creator.services import Ctx


def private_admin(event, ctx: Ctx) -> bool:
    """Current ADMIN_IDS member acting in their own private chat. Button visibility is never trusted."""
    user = getattr(event, "from_user", None)
    message = event.message if isinstance(event, CallbackQuery) else event
    return (user is not None and isinstance(message, Message) and message.chat.type == "private"
            and message.chat.id == user.id and ctx.is_admin(user.id))
