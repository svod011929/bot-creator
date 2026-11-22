from aiogram.fsm.state import State, StatesGroup


class CreateBot(StatesGroup):
    token = State()


class BotEdit(StatesGroup):
    transfer = State()
    setting = State()
    sell_price = State()


class Cabinet(StatesGroup):
    lave_amount = State()


class Info(StatesGroup):
    report = State()


class Admin(StatesGroup):
    price = State()
    broadcast = State()
    broadcast_bots = State()
    news = State()
    user_lookup = State()
    user_balance = State()
    bot_lookup = State()
    settings_input = State()


class BotList(StatesGroup):
    search = State()


class ContentInput(StatesGroup):
    active = State()


class PresentationInput(StatesGroup):
    value = State()


class Withdraw(StatesGroup):
    details = State()
    amount = State()
    confirm = State()
    reason = State()  # approver: optional reject reason


class OwnerPayout(StatesGroup):
    api_key = State()
    coin_rate = State()
