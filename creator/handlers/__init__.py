from aiogram import Router

from creator.handlers import (
    admin,
    bot_list,
    bot_setup,
    cabinet,
    common,
    create,
    info,
    lavepay,
    mybots,
    piarflow,
    resale,
    sponsor_sources,
    subscription,
    tgrass,
)


def build_routers() -> list[Router]:
    # common первым: /start, отмена и сброс состояния по кнопкам меню
    routers = [common.router, lavepay.router, sponsor_sources.router, admin.router, tgrass.router, piarflow.router, create.router,
               subscription.router, bot_setup.router, bot_list.router, mybots.router,
               cabinet.router, resale.router, info.router]
    for r in routers:  # позволяет собирать диспетчер повторно (тесты, --dry-run)
        r._parent_router = None
    return routers
