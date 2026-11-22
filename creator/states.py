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
    user_lookup = State()
    user_balance = State()
    bot_lookup = State()


class BotList(StatesGroup):
    search = State()


class Withdraw(StatesGroup):
    details = State()
    amount = State()
    confirm = State()
    reason = State()  # approver: optional reject reason
