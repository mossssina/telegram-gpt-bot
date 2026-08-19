"""
Регрессионные тесты по итогам технического аудита:
- fallback handle_button() для неизвестного/устаревшего callback_data;
- сообщения об ошибке GPT никогда не содержат сырой текст исключения;
- длинные ответы GPT режутся на несколько сообщений (безопасно для лимита
  Telegram), в истории при этом сохраняется один полный ответ, меню
  "Что дальше?" уходит после всех частей, чанкинг не дёргает OpenAI повторно.

Как и в остальных файлах этого набора — моки Update/Message/CallbackQuery
(AsyncMock/MagicMock), реальных сетевых запросов нет.
"""

import os
import sys
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

BOT_USERNAME = "testbot"
STAFF_ID = None


def setup_module(module):
    global STAFF_ID
    STAFF_ID = bot.STAFF_USERS[0]


class FakeMessage:
    _next_id = 1000

    def __init__(self, text=None, message_id=None, message_thread_id=None):
        if message_id is None:
            message_id = FakeMessage._next_id
            FakeMessage._next_id += 1
        self.message_id = message_id
        self.text = text
        self.message_thread_id = message_thread_id
        self.reply_text = AsyncMock()


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot = MagicMock()
    context.bot.username = BOT_USERNAME
    context.bot.send_message = AsyncMock(side_effect=lambda **kwargs: FakeMessage())
    context.bot.delete_message = AsyncMock(return_value=True)
    return context


def make_callback_update(data, user_id):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = FakeMessage()
    return update


def make_message_update(text, user_id, update_id=9000, username="staffuser"):
    update = MagicMock()
    update.update_id = update_id
    update.message = FakeMessage(text=text)
    update.effective_message = update.message
    update.effective_user = MagicMock(id=user_id, username=username, first_name="Имя", is_bot=False)
    update.effective_chat = MagicMock(id=user_id, type="private")
    return update


def make_gpt_response(text):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def run_button(update, context):
    asyncio.run(bot.handle_button(update, context))


def run_message(update, context):
    asyncio.run(bot.handle_message(update, context))


# ---------------------------------------------------------------------------
# Fallback для неизвестного callback_data
# ---------------------------------------------------------------------------

def test_unknown_callback_shows_fallback_without_side_effects(monkeypatch):
    def boom(**k):
        raise AssertionError("Неизвестная кнопка не должна вызывать OpenAI")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)
    bot.BRIEF_STATES.clear()
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("совершенно_неизвестный_callback_42", STAFF_ID)
    context = make_context()
    run_button(update, context)

    text = context.bot.send_message.call_args_list[-1].kwargs["text"]
    assert "устарела" in text.lower()
    markup = context.bot.send_message.call_args_list[-1].kwargs["reply_markup"]
    labels = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "back_to_main" in labels
    assert STAFF_ID not in bot.ACTIVE_PROJECTS
    assert STAFF_ID not in bot.BRIEF_STATES


def test_known_callback_not_intercepted_by_fallback(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES.clear()
    update = make_callback_update("back_to_main", STAFF_ID)
    context = make_context()
    run_button(update, context)

    text = context.bot.send_message.call_args_list[-1].kwargs["text"]
    assert "устарела" not in text.lower()


# ---------------------------------------------------------------------------
# Сообщения об ошибке GPT не содержат текст исключения
# ---------------------------------------------------------------------------

def test_gpt_exception_text_never_reaches_user(monkeypatch, tmp_path):
    secret = "sk-super-secret-key-should-not-leak-12345"

    def boom(**k):
        raise RuntimeError(secret)
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)

    client_id = 111222333  # не входит в STAFF_USERS
    update = make_message_update("вопрос от клиента", client_id)
    context = make_context()
    run_message(update, context)

    for call in update.message.reply_text.call_args_list:
        for arg in list(call.args) + list(call.kwargs.values()):
            assert secret not in str(arg)
    update.message.reply_text.assert_any_call("Не удалось получить ответ. Попробуйте ещё раз немного позже.")


# ---------------------------------------------------------------------------
# Чанкинг длинных ответов GPT
# ---------------------------------------------------------------------------

def test_split_text_for_telegram_preserves_full_text():
    text = ("Первый абзац с некоторым содержанием.\n\n" * 50) + ("x" * 5000)
    chunks = bot.split_text_for_telegram(text, max_len=500)
    assert "".join(chunks) == text
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) > 1


def test_split_text_for_telegram_short_text_is_single_chunk():
    text = "Короткий ответ."
    assert bot.split_text_for_telegram(text, max_len=3800) == [text]


def test_split_text_for_telegram_hard_splits_line_without_breaks():
    text = "a" * 9000  # одна строка без единого переноса
    chunks = bot.split_text_for_telegram(text, max_len=3800)
    assert "".join(chunks) == text
    assert all(len(c) <= 3800 for c in chunks)


def test_long_gpt_reply_sent_as_multiple_messages_and_full_text_preserved(monkeypatch, tmp_path):
    long_reply = "Абзац номер один.\n\n" * 400  # заведомо больше 3800 символов
    folder = str(tmp_path / "acme")
    os.makedirs(folder)
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "new", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)

    call_count = {"n": 0}
    def fake_create(**kwargs):
        call_count["n"] += 1
        return make_gpt_response(long_reply)
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("длинный вопрос", STAFF_ID)
    context = make_context()
    run_message(update, context)

    calls = update.message.reply_text.call_args_list
    texts = [c.args[0] for c in calls if c.args]

    # Последний вызов — меню "Что дальше?" (по ключевому слову в тексте),
    # всё до него (кроме опциональной строки про ошибку сохранения) — чанки ответа.
    assert texts[-1] == "Что дальше?"
    reply_chunks = [t for t in texts[:-1] if "Не удалось сохранить" not in t]
    assert len(reply_chunks) > 1  # реально разбито на несколько сообщений
    assert "".join(reply_chunks) == long_reply

    # OpenAI вызван ровно один раз, несмотря на чанкинг на отправке.
    assert call_count["n"] == 1

    # В истории — один ПОЛНЫЙ ответ, не фрагмент.
    records = bot.load_dialog_history(folder)
    assert len(records) == 1
    assert records[0]["answer"] == long_reply


def test_send_failure_does_not_retry_gpt_call(monkeypatch, tmp_path):
    folder = str(tmp_path / "acme")
    os.makedirs(folder)
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "new", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)

    call_count = {"n": 0}
    def fake_create(**kwargs):
        call_count["n"] += 1
        return make_gpt_response("нормальный ответ")
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("вопрос", STAFF_ID)
    update.message.reply_text = AsyncMock(side_effect=RuntimeError("Telegram недоступен"))
    context = make_context()

    run_message(update, context)  # не должно бросить исключение наружу

    assert call_count["n"] == 1  # GPT вызван один раз, повторной попытки не было
