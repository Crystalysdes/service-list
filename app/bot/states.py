from aiogram.fsm.state import State, StatesGroup


class ChannelConnect(StatesGroup):
    waiting = State()


class StaffAdd(StatesGroup):
    waiting_user = State()


class AdminInput(StatesGroup):
    """Generic single-value input requested by an admin screen (see routers.admin.inputs)."""

    waiting = State()


class AddService(StatesGroup):
    category = State()
    name = State()
    description = State()
    link = State()
    confirm = State()


class EditService(StatesGroup):
    value = State()


class ReportFlow(StatesGroup):
    search = State()
    text = State()
    photos = State()
    confirm = State()


class OwnerReply(StatesGroup):
    text = State()
    photos = State()


class ClaimFlow(StatesGroup):
    search = State()


class DealWizard(StatesGroup):
    title = State()
    terms = State()
    amount = State()
    counterparty = State()


class DealDispute(StatesGroup):
    reason = State()
