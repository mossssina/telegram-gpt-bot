"""
Тесты для утечки интерфейсных действий бота в групповые чаты: старые
inline-кнопки, ранее отправленные ботом в группу/супергруппу (до перехода на
@-упоминания), не должны запускать никакую логику при нажатии — ни GPT, ни
Memory Engine, ни изменение состояния, ни отправку/удаление сообщений. Кнопка
должна лишь погасить "часики" у пользователя через query.answer().

Как и в остальных файлах этого набора — моки Update/CallbackQuery
(AsyncMock/MagicMock), реальных сетевых запросов нет.
"""

import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

GROUP_CHAT_ID = -1004342081714  # тот самый chat_id из инцидента в задаче
STAFF_ID = None


def setup_module(module):
    global STAFF_ID
    STAFF_ID = bot.STAFF_USERS[0]


class FakeMessage:
    _next_id = 1000

    def __init__(self, message_id=None):
        if message_id is None:
            message_id = FakeMessage._next_id
            FakeMessage._next_id += 1
        self.message_id = message_id


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(side_effect=lambda **kwargs: FakeMessage())
    context.bot.delete_message = AsyncMock(return_value=True)
    return context


def make_callback_update(data, user_id, chat_type="private", chat_id=None):
    if chat_id is None:
        chat_id = user_id
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id, username="staffuser", first_name="Имя", is_bot=False)
    update.effective_chat = MagicMock(id=chat_id, type=chat_type)
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message = FakeMessage()
    return update


def run_button(update, context):
    asyncio.run(bot.handle_button(update, context))


def assert_no_side_effects(update, context):
    update.callback_query.answer.assert_called_once()
    context.bot.send_message.assert_not_called()
    context.bot.delete_message.assert_not_called()
    update.callback_query.edit_message_text.assert_not_called()
    update.callback_query.edit_message_reply_markup.assert_not_called()


# ---------------------------------------------------------------------------
# Групповые/супергрупповые callback — безопасный отказ без побочных эффектов
# ---------------------------------------------------------------------------

def test_back_to_main_in_group_rejected_without_side_effects(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: (_ for _ in ()).throw(
        AssertionError("save_bot_state не должен вызываться из группового callback")
    ))
    bot.BRIEF_STATES.clear()
    bot.BRIEF_STATES[STAFF_ID] = {
        "question_index": 2, "project_name": "X", "project_slug": "x",
        "answers": ["a", "b"], "current_multi_selection": [],
    }
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("back_to_main", STAFF_ID, chat_type="group", chat_id=GROUP_CHAT_ID)
    context = make_context()
    run_button(update, context)

    assert_no_side_effects(update, context)
    # Состояние сотрудника не тронуто — не удалено, не изменено.
    assert STAFF_ID in bot.BRIEF_STATES
    assert bot.BRIEF_STATES[STAFF_ID]["question_index"] == 2
    assert STAFF_ID not in bot.ACTIVE_PROJECTS
    bot.BRIEF_STATES.clear()


def test_menu_instructions_in_supergroup_rejected_without_side_effects(monkeypatch):
    update = make_callback_update("menu_instructions", STAFF_ID, chat_type="supergroup", chat_id=GROUP_CHAT_ID)
    context = make_context()
    run_button(update, context)

    assert_no_side_effects(update, context)


def test_confirm_update_memory_in_supergroup_does_not_start_memory_update(monkeypatch):
    """
    Прямая проверка инцидента из задачи: нажатие "Запустить обновление" в группе
    не должно ни создавать asyncio.create_task, ни вызывать
    run_memory_update_and_notify (которая шлёт итог обновления в update.effective_chat.id
    — то есть в тот же групповой чат).
    """
    create_task_calls = []
    monkeypatch.setattr(bot.asyncio, "create_task", lambda coro, **k: create_task_calls.append(coro))

    def boom(*a, **k):
        raise AssertionError("run_memory_update_and_notify не должен вызываться из группового callback")
    monkeypatch.setattr(bot, "run_memory_update_and_notify", boom)

    update = make_callback_update("confirm_update_memory", STAFF_ID, chat_type="supergroup", chat_id=GROUP_CHAT_ID)
    context = make_context()
    run_button(update, context)  # не должно бросить исключение — boom не должен сработать

    assert create_task_calls == []
    assert_no_side_effects(update, context)


def test_unknown_callback_in_group_rejected_without_side_effects():
    """
    Устаревший/неизвестный callback_data (например, кнопка из очень старой версии
    бота) в группе тоже должен молча гаситься на самом первом чек-пойнте — не
    доходя даже до общего fallback-сообщения "Эта кнопка устарела".
    """
    update = make_callback_update("совершенно_неизвестный_callback_777", STAFF_ID,
                                   chat_type="group", chat_id=GROUP_CHAT_ID)
    context = make_context()
    run_button(update, context)

    assert_no_side_effects(update, context)


# ---------------------------------------------------------------------------
# Регресс: в приватных чатах поведение кнопок не меняется
# ---------------------------------------------------------------------------

def test_back_to_main_in_private_chat_still_works(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES.clear()

    update = make_callback_update("back_to_main", STAFF_ID, chat_type="private")
    context = make_context()
    run_button(update, context)

    update.callback_query.answer.assert_called_once()
    context.bot.send_message.assert_called_once()
