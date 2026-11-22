"""Сборка приложения конструктора."""
from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from creator.handlers import build_routers
from creator.handlers.common import UserMiddleware
from creator.handlers.gate import SubscribeMiddleware
from creator.services import Ctx


def build_dispatcher(ctx: Ctx) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    mw = UserMiddleware()
    sub = SubscribeMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.message.outer_middleware(sub)
    dp.callback_query.outer_middleware(sub)
    for r in build_routers():
        dp.include_router(r)
    return dp
