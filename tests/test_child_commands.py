"""Child bot command menu and /profile /withdraw aliases."""
from types import SimpleNamespace

from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeChat

from child.core import (
    ADMIN_BOT_COMMAND,
    DEFAULT_BOT_SETTINGS,
    USER_BOT_COMMANDS,
    SetMySettings,
    apply_bot_settings,
    build_core_router,
    install_bot_commands,
)


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_my_commands(self, commands, scope=None):
        self.calls.append((list(commands), scope))


async def test_install_bot_commands_scopes_admin_to_owner():
    bot = _FakeBot()
    app = SimpleNamespace(owner_id=99)
    await install_bot_commands(bot, app)
    assert len(bot.calls) == 2
    default_cmds, default_scope = bot.calls[0]
    assert [c.command for c in default_cmds] == ["start", "profile", "withdraw"]
    assert all(isinstance(c, BotCommand) for c in default_cmds)
    assert default_cmds == list(USER_BOT_COMMANDS)
    assert default_scope is None
    owner_cmds, owner_scope = bot.calls[1]
    assert [c.command for c in owner_cmds] == ["start", "profile", "withdraw", "admin"]
    assert owner_cmds[-1] == ADMIN_BOT_COMMAND
    assert isinstance(owner_scope, BotCommandScopeChat)
    assert owner_scope.chat_id == 99


async def test_install_bot_commands_skips_owner_scope_without_owner():
    bot = _FakeBot()
    await install_bot_commands(bot, SimpleNamespace(owner_id=0))
    assert len(bot.calls) == 1
    assert [c.command for c in bot.calls[0][0]] == ["start", "profile", "withdraw"]


async def test_install_bot_commands_survives_api_error():
    class Boom(_FakeBot):
        async def set_my_commands(self, commands, scope=None):
            raise RuntimeError("telegram down")

    await install_bot_commands(Boom(), SimpleNamespace(owner_id=7))


def test_core_router_accepts_profile_and_withdraw_commands():
    class FakeDB:
        def user(self, uid):
            return None

        def is_banned(self, uid):
            return False

    class FakeTemplate:
        key = "clicker"

        def pause_forms(self, app, uid):
            return None

    class FakeApp:
        db = FakeDB()
        s = {"min_withdraw": 0}
        currency = "₽"
        owner_id = 1
        template = FakeTemplate()

    router = build_core_router(FakeApp())
    commands = set()
    for handler in router.message.handlers:
        for flt in handler.filters:
            callback = getattr(flt, "callback", flt)
            if isinstance(callback, Command):
                commands.update(callback.commands)
    assert "profile" in commands
    assert "withdraw" in commands



class _CallBot:
    def __init__(self) -> None:
        self.methods: list = []

    async def __call__(self, method):
        self.methods.append(method)
        return True


async def test_apply_bot_settings_sends_conservative_defaults():
    bot = _CallBot()
    await apply_bot_settings(bot)
    assert len(bot.methods) == 1
    method = bot.methods[0]
    assert isinstance(method, SetMySettings)
    assert method.__api_method__ == "setMySettings"
    payload = method.model_dump(exclude_none=True)
    assert payload == DEFAULT_BOT_SETTINGS
    assert payload["can_join_groups"] is True
    assert payload["can_read_all_group_messages"] is False
    assert payload["callback_query_mode"] == "default"


async def test_apply_bot_settings_survives_api_error():
    class Boom(_CallBot):
        async def __call__(self, method):
            raise RuntimeError("https://api.telegram.org/botSECRET/setMySettings")

    await apply_bot_settings(Boom())
