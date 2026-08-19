"""
Тесты идемпотентности одиночного выбора в брифе (brief_opt:<question_index>:
<option_index>) — защита от двойного нажатия/повторной доставки Telegram-update
и от старого формата callback_data (голый текст ответа).

Как и в других файлах этого набора, здесь используются моки Update/CallbackQuery
(AsyncMock/MagicMock) — обработчик кнопок иначе не проверить. Реальных сетевых
запросов не выполняется.
"""

import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

CLIENT_ID = 555444333  # не входит в bot.STAFF_USERS
INDUSTRY_QUESTION_INDEX = 3  # BRIEF_QUESTIONS[3] — "В какой индустрии работаете?", type "single"


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


def make_callback_update(data, user_id, callback_message_id=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = FakeMessage(callback_message_id)
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def fresh_brief_state():
    return {
        "question_index": INDUSTRY_QUESTION_INDEX,
        "project_name": "Тестовый бренд",
        "project_slug": "testovyi_brend",
        "answers": ["Тестовый бренд", "+79990000000", "https://example.com"],
        "current_multi_selection": [],
    }


def run_button(update, context):
    asyncio.run(bot.handle_button(update, context))


def setup_function(_):
    bot.BRIEF_STATES.clear()


def teardown_function(_):
    bot.BRIEF_STATES.clear()


def test_valid_single_press_advances_normally(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES[CLIENT_ID] = fresh_brief_state()

    option_idx = 0
    update = make_callback_update(f"brief_opt:{INDUSTRY_QUESTION_INDEX}:{option_idx}", CLIENT_ID)
    run_button(update, make_context())

    state = bot.BRIEF_STATES[CLIENT_ID]
    expected_answer = bot.BRIEF_QUESTIONS[INDUSTRY_QUESTION_INDEX]["options"][option_idx]
    assert state["answers"][-1] == expected_answer
    assert state["question_index"] == INDUSTRY_QUESTION_INDEX + 1


def test_repeated_press_does_not_duplicate_or_skip(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES[CLIENT_ID] = fresh_brief_state()

    option_idx = 1
    data = f"brief_opt:{INDUSTRY_QUESTION_INDEX}:{option_idx}"
    context = make_context()
    # Первое (настоящее) нажатие.
    run_button(make_callback_update(data, CLIENT_ID), context)
    # Повторная доставка / двойной тап того же callback_data.
    run_button(make_callback_update(data, CLIENT_ID), context)

    state = bot.BRIEF_STATES[CLIENT_ID]
    answers_added = len(state["answers"]) - 3  # было 3 текстовых ответа до этого вопроса
    assert answers_added == 1
    assert state["question_index"] == INDUSTRY_QUESTION_INDEX + 1


def test_mismatched_question_index_does_not_corrupt_brief(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES[CLIENT_ID] = fresh_brief_state()

    # question_index в кнопке (2) не совпадает с текущим состоянием (3) —
    # имитирует нажатие устаревшей/уже обработанной кнопки.
    stale_data = f"brief_opt:{INDUSTRY_QUESTION_INDEX - 1}:0"
    update = make_callback_update(stale_data, CLIENT_ID)
    run_button(update, make_context())

    state = bot.BRIEF_STATES[CLIENT_ID]
    assert len(state["answers"]) == 3  # ничего не добавилось
    assert state["question_index"] == INDUSTRY_QUESTION_INDEX  # не сдвинулся
    update.callback_query.edit_message_text.assert_called_once()


def test_out_of_range_option_index_does_not_crash_or_corrupt(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES[CLIENT_ID] = fresh_brief_state()

    options_count = len(bot.BRIEF_QUESTIONS[INDUSTRY_QUESTION_INDEX]["options"])
    bad_data = f"brief_opt:{INDUSTRY_QUESTION_INDEX}:{options_count + 5}"
    update = make_callback_update(bad_data, CLIENT_ID)
    run_button(update, make_context())  # не должно бросить исключение

    state = bot.BRIEF_STATES[CLIENT_ID]
    assert len(state["answers"]) == 3
    assert state["question_index"] == INDUSTRY_QUESTION_INDEX


def test_old_format_callback_handled_safely(monkeypatch):
    """
    Кнопка старого формата brief_opt:<текст ответа> (до этого исправления) не
    должна слепо записываться как ответ — бриф не повреждается, показывается
    актуальный вопрос заново.
    """
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES[CLIENT_ID] = fresh_brief_state()

    old_style_data = "brief_opt:Дизайн интерьера"
    update = make_callback_update(old_style_data, CLIENT_ID)
    run_button(update, make_context())

    state = bot.BRIEF_STATES[CLIENT_ID]
    assert len(state["answers"]) == 3  # ничего не записано вслепую
    assert state["question_index"] == INDUSTRY_QUESTION_INDEX
    update.callback_query.edit_message_text.assert_called_once()


def test_expired_session_message_unchanged():
    """Регресс: сообщение об истёкшей сессии для отсутствующего BRIEF_STATES не менялось."""
    update = make_callback_update(f"brief_opt:{INDUSTRY_QUESTION_INDEX}:0", CLIENT_ID)
    run_button(update, make_context())
    update.callback_query.edit_message_text.assert_called_once_with(
        "Сессия истекла. Нажмите /start чтобы начать снова."
    )
