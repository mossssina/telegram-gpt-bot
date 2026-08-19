"""
Тесты для очистки UI-экранов при навигации: register_ui_messages/clear_ui_screen/
send_ui_screen, и того, что конвертированные ветки handle_button используют этот
механизм вместо голого query.edit_message_text.

Единственный файл в наборе, где по прямому указанию используются моки Telegram API
(AsyncMock для context.bot.send_message/delete_message) — остальные тесты проекта
сознательно избегают этого (см. CLAUDE.md), но эта функциональность и есть отправка/
удаление сообщений Telegram, так что иначе её не проверить. Реальных сетевых запросов
нигде не выполняется; asyncio.run() используется напрямую (в проекте нет pytest-asyncio).
"""

import os
import sys
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot


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


def make_update(chat_id=111, with_callback=True, callback_message_id=None):
    update = MagicMock()
    update.effective_chat.id = chat_id
    if with_callback:
        update.callback_query = MagicMock()
        update.callback_query.message = FakeMessage(callback_message_id)
    else:
        update.callback_query = None
    return update


def make_callback_update(data, user_id, chat_id=111, callback_message_id=None):
    update = make_update(chat_id=chat_id, with_callback=True, callback_message_id=callback_message_id)
    update.effective_user.id = user_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


def deleted_ids_of(context):
    return [call.kwargs["message_id"] for call in context.bot.delete_message.call_args_list]


def screen_ids(context, update):
    return context.user_data.get("ui_screens", {}).get(str(update.effective_chat.id), [])


# ---------------------------------------------------------------------------
# Прямые тесты хелперов (1, 2, 9, 10)
# ---------------------------------------------------------------------------

def test_clear_ui_screen_deletes_all_registered_parts():
    context = make_context()
    update = make_update()
    bot.register_ui_messages(update, context, [FakeMessage(10), FakeMessage(11), FakeMessage(12)])

    asyncio.run(bot.clear_ui_screen(update, context))

    assert set(deleted_ids_of(context)) >= {10, 11, 12}
    assert screen_ids(context, update) == []


def test_clear_ui_screen_tolerates_delete_failures():
    context = make_context()
    update = make_update()
    bot.register_ui_messages(update, context, [FakeMessage(20), FakeMessage(21)])

    async def flaky_delete(**kwargs):
        if kwargs["message_id"] == 20:
            raise Exception("Message to delete not found")
        return True
    context.bot.delete_message = AsyncMock(side_effect=flaky_delete)

    # Не должно кидать исключение наружу, даже если одно из удалений упало.
    asyncio.run(bot.clear_ui_screen(update, context))

    # Следующий экран всё равно можно отправить.
    messages = asyncio.run(bot.send_ui_screen(update, context, "следующий экран"))
    assert len(messages) == 1


def test_clear_ui_screen_falls_back_to_callback_message_when_unregistered():
    """Имитирует потерю состояния после перезапуска бота — ui_screens пуст."""
    context = make_context()
    update = make_update(callback_message_id=777)

    asyncio.run(bot.clear_ui_screen(update, context))

    assert 777 in deleted_ids_of(context)


def test_register_ui_messages_replace_vs_append():
    context = make_context()
    update = make_update()

    bot.register_ui_messages(update, context, [FakeMessage(1), FakeMessage(2)], replace=True)
    bot.register_ui_messages(update, context, FakeMessage(3), replace=False)
    assert screen_ids(context, update) == [1, 2, 3]

    bot.register_ui_messages(update, context, FakeMessage(99), replace=True)
    assert screen_ids(context, update) == [99]


def test_send_ui_screen_splits_long_text_and_attaches_keyboard_to_last_chunk():
    context = make_context()
    update = make_update()
    long_text = "x" * 9000
    markup = object()

    messages = asyncio.run(bot.send_ui_screen(update, context, long_text, reply_markup=markup))

    assert len(messages) == 3
    calls = context.bot.send_message.call_args_list
    assert calls[0].kwargs.get("reply_markup") is None
    assert calls[1].kwargs.get("reply_markup") is None
    assert calls[2].kwargs.get("reply_markup") is markup
    assert len(screen_ids(context, update)) == 3


# ---------------------------------------------------------------------------
# Интеграционные тесты handle_button (3, 4, 5, 6, 8)
# ---------------------------------------------------------------------------

def test_instructions_back_clears_instruction_screen():
    context = make_context()
    staff_id = bot.STAFF_USERS[0]

    u1 = make_callback_update("menu_instructions", staff_id)
    asyncio.run(bot.handle_button(u1, context))
    list_ids = list(screen_ids(context, u1))
    assert list_ids

    u2 = make_callback_update("instruction:communication", staff_id)
    asyncio.run(bot.handle_button(u2, context))
    instr_ids = list(screen_ids(context, u2))
    assert instr_ids
    assert set(list_ids).issubset(set(deleted_ids_of(context)))

    u3 = make_callback_update("menu_instructions", staff_id)
    asyncio.run(bot.handle_button(u3, context))
    assert set(instr_ids).issubset(set(deleted_ids_of(context)))


def test_staff_brief_back_to_project_clears_all_brief_chunks(tmp_path, monkeypatch):
    bot.ACTIVE_PROJECTS.clear()
    context = make_context()
    staff_id = bot.STAFF_USERS[0]

    brief_path = tmp_path / "brief.md"
    brief_path.write_text("x" * 9000, encoding="utf-8")
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": str(tmp_path), "mode": None,
        "registry_entry": {"brief_file": str(brief_path)},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(
        bot, "load_projects_registry",
        lambda: {"acme": {"title": "Acme", "folder": str(tmp_path)}}
    )

    u1 = make_callback_update("staff_brief:acme", staff_id)
    asyncio.run(bot.handle_button(u1, context))
    brief_ids = list(screen_ids(context, u1))
    assert len(brief_ids) >= 3

    u2 = make_callback_update("select_project:acme", staff_id)
    asyncio.run(bot.handle_button(u2, context))

    assert set(brief_ids).issubset(set(deleted_ids_of(context)))
    bot.ACTIVE_PROJECTS.clear()


def test_staff_select_project_clears_previous_screen():
    context = make_context()
    staff_id = bot.STAFF_USERS[0]

    u1 = make_callback_update("menu_instructions", staff_id)
    asyncio.run(bot.handle_button(u1, context))
    prev_ids = list(screen_ids(context, u1))

    u2 = make_callback_update("staff_select_project", staff_id)
    asyncio.run(bot.handle_button(u2, context))

    assert set(prev_ids).issubset(set(deleted_ids_of(context)))
    assert screen_ids(context, u2) != prev_ids


def test_cancel_memory_update_clears_confirmation_prompt():
    context = make_context()
    staff_id = bot.STAFF_USERS[0]

    u1 = make_callback_update("menu_update_memory", staff_id)
    asyncio.run(bot.handle_button(u1, context))
    confirm_ids = list(screen_ids(context, u1))
    assert confirm_ids

    # "Отмена" -> callback_data="back_to_main"; НЕ "confirm_update_memory" —
    # тот запускает реальный subprocess/OpenAI и намеренно нигде в этом файле не вызывается.
    u2 = make_callback_update("back_to_main", staff_id)
    asyncio.run(bot.handle_button(u2, context))

    assert set(confirm_ids).issubset(set(deleted_ids_of(context)))


def test_double_click_leaves_exactly_one_current_screen():
    context = make_context()
    staff_id = bot.STAFF_USERS[0]

    u1 = make_callback_update("menu_instructions", staff_id)
    asyncio.run(bot.handle_button(u1, context))
    first_ids = list(screen_ids(context, u1))

    u2 = make_callback_update("menu_instructions", staff_id)
    asyncio.run(bot.handle_button(u2, context))
    second_ids = screen_ids(context, u2)

    assert set(first_ids).issubset(set(deleted_ids_of(context)))
    assert len(second_ids) == len(first_ids)
    assert set(second_ids).isdisjoint(set(first_ids))


# ---------------------------------------------------------------------------
# GPT/память не затрагиваются навигацией (7, 13, 14)
# ---------------------------------------------------------------------------

def test_handle_message_gpt_path_never_touches_ui_screens():
    source = inspect.getsource(bot.handle_message)
    assert "ui_screens" not in source
    assert "register_ui_messages" not in source
    assert "clear_ui_screen" not in source
    assert "send_ui_screen" not in source


NAV_CALLBACKS = ["back_to_main", "menu_instructions", "staff_select_project", "menu_chats", "menu_update_memory"]
# Намеренно исключено: "confirm_update_memory" — запускает реальный subprocess/OpenAI.


def test_navigation_never_calls_openai(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("Навигация не должна вызывать OpenAI")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)

    context = make_context()
    staff_id = bot.STAFF_USERS[0]
    for data in NAV_CALLBACKS:
        asyncio.run(bot.handle_button(make_callback_update(data, staff_id), context))


def test_navigation_never_touches_memory_engine(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("Навигация не должна трогать Memory Engine")
    monkeypatch.setattr(bot, "ContextManager", boom)

    context = make_context()
    staff_id = bot.STAFF_USERS[0]
    for data in NAV_CALLBACKS:
        asyncio.run(bot.handle_button(make_callback_update(data, staff_id), context))


# ---------------------------------------------------------------------------
# ReplyKeyboardMarkup (12)
# ---------------------------------------------------------------------------

def test_no_reply_keyboard_markup_in_use_yet():
    """
    Постоянная ReplyKeyboardMarkup ещё не реализована в проекте (проверено явно) —
    значит, очистке UI-экранов сейчас нечего ломать. Если её добавят в будущем,
    этот тест начнёт падать и напомнит пересмотреть эту логику.
    """
    with open(os.path.join(ROOT, "bot.py"), "r", encoding="utf-8") as f:
        source = f.read()
    assert "ReplyKeyboardMarkup" not in source
