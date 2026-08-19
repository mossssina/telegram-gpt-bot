"""
Тесты для исключения приватного сценария брифа (и другой приватной логики)
из групповых/супергрупповых чатов, и для нового entity-based определения
@упоминания бота (_check_mention).

Как и tests/test_ui_screens.py, здесь используются моки Update/Message
(AsyncMock/MagicMock) — иначе маршрутизацию по chat_type и entities не
проверить. Реальных сетевых запросов нигде не выполняется: OpenAI-клиент
и ContextManager либо не достигаются (сообщение отфильтровано раньше),
либо явно замоканы/используют tmp_path без файлов.
"""

import os
import sys
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

BOT_USERNAME = "testbot"
CLIENT_ID = 999111222  # заведомо не входит в bot.STAFF_USERS


class FakeEntity:
    def __init__(self, type_, offset, length):
        self.type = type_
        self.offset = offset
        self.length = length


class FakeMessage:
    def __init__(self, text, entities=None, message_thread_id=None):
        self.text = text
        # entities=None means the attribute is genuinely absent (simplified Update,
        # exercises _check_mention's substring fallback); pass entities=[] explicitly
        # to simulate a real Telegram message with no mentions (authoritative empty).
        if entities is not None:
            self.entities = entities
        self.message_thread_id = message_thread_id
        self.reply_text = AsyncMock()


def make_update(text, user_id, chat_id=555, chat_type="private", entities=None,
                 is_bot=False, username="clientuser", thread_id=None):
    update = MagicMock()
    update.message = FakeMessage(text, entities=entities, message_thread_id=thread_id)
    update.effective_message = update.message
    update.effective_user = MagicMock(id=user_id, username=username, first_name="Имя", is_bot=is_bot)
    update.effective_chat = MagicMock(id=chat_id, type=chat_type)
    return update


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot = MagicMock()
    context.bot.username = BOT_USERNAME
    return context


def make_gpt_response(text="GPT reply"):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def run(update, context):
    asyncio.run(bot.handle_message(update, context))


# ---------------------------------------------------------------------------
# 1-2. Клиент с активным брифом, сообщение в чужой группе
# ---------------------------------------------------------------------------

def test_brief_state_untouched_by_unrelated_group_message_without_mention(monkeypatch):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    bot.BRIEF_STATES.clear()
    bot.BRIEF_STATES[CLIENT_ID] = {"question_index": 0, "project_name": None,
                                    "project_slug": None, "answers": [], "current_multi_selection": []}
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)

    update = make_update("привет всем", CLIENT_ID, chat_id=777, chat_type="group")
    context = make_context()
    run(update, context)

    assert bot.BRIEF_STATES[CLIENT_ID]["question_index"] == 0
    assert bot.BRIEF_STATES[CLIENT_ID]["answers"] == []
    update.message.reply_text.assert_not_called()
    bot.BRIEF_STATES.clear()


def test_brief_state_untouched_by_unrelated_group_message_with_mention(monkeypatch):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    bot.BRIEF_STATES.clear()
    bot.BRIEF_STATES[CLIENT_ID] = {"question_index": 0, "project_name": None,
                                    "project_slug": None, "answers": [], "current_multi_selection": []}
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)

    update = make_update(f"@{BOT_USERNAME} привет", CLIENT_ID, chat_id=777, chat_type="group")
    context = make_context()
    run(update, context)

    assert bot.BRIEF_STATES[CLIENT_ID]["question_index"] == 0
    assert bot.BRIEF_STATES[CLIENT_ID]["answers"] == []
    update.message.reply_text.assert_not_called()
    bot.BRIEF_STATES.clear()


# ---------------------------------------------------------------------------
# 3-5. Сотрудник в незарегистрированной группе
# ---------------------------------------------------------------------------

def test_staff_without_project_chat_mode_ignored_in_group(monkeypatch):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: None)
    staff_id = bot.STAFF_USERS[0]

    update = make_update(f"@{BOT_USERNAME} привет", staff_id, chat_id=777, chat_type="supergroup")
    context = make_context()
    run(update, context)

    update.message.reply_text.assert_not_called()


def test_staff_with_project_chat_mode_replies_in_group(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": str(tmp_path), "mode": "project_chat",
        "registry_entry": {"folder": str(tmp_path)},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    calls = []
    monkeypatch.setattr(bot, "append_staff_dialog", lambda *a, **k: calls.append(("staff_dialog", a, k)))
    monkeypatch.setattr(bot, "append_to_chat_context", lambda *a, **k: calls.append(("chat_context", a, k)))
    monkeypatch.setattr(bot.client.chat.completions, "create", lambda **k: make_gpt_response("ответ по проекту"))
    staff_id = bot.STAFF_USERS[0]

    update = make_update(f"@{BOT_USERNAME} как дела у проекта?", staff_id, chat_id=777, chat_type="supergroup")
    context = make_context()
    run(update, context)

    first_call_args = update.message.reply_text.call_args_list[0]
    assert first_call_args.args == ("ответ по проекту",)
    assert any(c[0] == "staff_dialog" for c in calls)
    assert not any(c[0] == "chat_context" for c in calls)


def test_staff_with_project_chat_mode_ignored_without_mention_in_group(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": str(tmp_path), "mode": "project_chat",
        "registry_entry": {"folder": str(tmp_path)},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    def boom(**k):
        raise AssertionError("GPT не должен вызываться без упоминания")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)
    staff_id = bot.STAFF_USERS[0]

    update = make_update("как дела у проекта?", staff_id, chat_id=777, chat_type="supergroup")
    context = make_context()
    run(update, context)

    update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Приватный чат: бриф по-прежнему нормально продвигается
# ---------------------------------------------------------------------------

def test_brief_flow_still_advances_in_private_chat(monkeypatch):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES.clear()
    bot.BRIEF_STATES[CLIENT_ID] = {"question_index": 4, "project_name": "Тест",
                                    "project_slug": "test", "answers": ["a", "b", "c", "d"],
                                    "current_multi_selection": []}

    update = make_update("Мой продукт — мебель ручной работы", CLIENT_ID, chat_id=CLIENT_ID, chat_type="private")
    context = make_context()
    run(update, context)

    assert bot.BRIEF_STATES[CLIENT_ID]["question_index"] == 5
    assert bot.BRIEF_STATES[CLIENT_ID]["answers"][-1] == "Мой продукт — мебель ручной работы"
    update.message.reply_text.assert_called_once()
    bot.BRIEF_STATES.clear()


# ---------------------------------------------------------------------------
# 7-9. Зарегистрированный проектный чат
# ---------------------------------------------------------------------------

def test_registered_project_chat_saves_context_without_mention(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bot, "get_project_chat_entry",
        lambda chat_id, thread_id=None: ("acme", {"chat_id": str(chat_id), "project_slug": "acme"})
    )
    calls = []
    monkeypatch.setattr(bot, "append_to_chat_context", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {"acme": {"title": "Acme", "folder": str(tmp_path)}})
    def boom(**k):
        raise AssertionError("GPT не должен вызываться без упоминания")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)

    update = make_update("просто сообщение в чате проекта", CLIENT_ID, chat_id=888, chat_type="supergroup")
    context = make_context()
    run(update, context)

    assert len(calls) == 1
    update.message.reply_text.assert_not_called()


def test_registered_project_chat_replies_via_memory_engine_on_mention(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bot, "get_project_chat_entry",
        lambda chat_id, thread_id=None: ("acme", {"chat_id": str(chat_id), "project_slug": "acme"})
    )
    monkeypatch.setattr(bot, "append_to_chat_context", lambda *a, **k: None)
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {"acme": {"title": "Acme", "folder": str(tmp_path)}})
    monkeypatch.setattr(bot.client.chat.completions, "create", lambda **k: make_gpt_response("вот что обсуждали"))

    entities = [FakeEntity("mention", 0, len(BOT_USERNAME) + 1)]
    update = make_update(f"@{BOT_USERNAME} что нового?", CLIENT_ID, chat_id=888, chat_type="supergroup",
                          entities=entities)
    context = make_context()
    run(update, context)

    update.message.reply_text.assert_called_once_with("вот что обсуждали")


def test_registered_project_chat_mention_with_no_text_asks_for_question(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bot, "get_project_chat_entry",
        lambda chat_id, thread_id=None: ("acme", {"chat_id": str(chat_id), "project_slug": "acme"})
    )
    monkeypatch.setattr(bot, "append_to_chat_context", lambda *a, **k: None)
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {"acme": {"title": "Acme", "folder": str(tmp_path)}})
    def boom(**k):
        raise AssertionError("GPT не должен вызываться без текста вопроса")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)

    update = make_update(f"@{BOT_USERNAME}", CLIENT_ID, chat_id=888, chat_type="supergroup")
    context = make_context()
    run(update, context)

    update.message.reply_text.assert_called_once_with("Напишите вопрос после упоминания бота.")


# ---------------------------------------------------------------------------
# 10-13. _check_mention
# ---------------------------------------------------------------------------

def test_check_mention_ignores_substring_without_entity():
    """
    entities=[] (реальный Telegram сказал "упоминаний нет") — авторитетно,
    подстрока "@testbot" внутри email не должна засчитываться.
    """
    update = make_update(f"почта админа admin@{BOT_USERNAME}.ru", CLIENT_ID, chat_type="group", entities=[])
    context = make_context()
    is_mention, clean = bot._check_mention(update, context, update.message.text)
    assert is_mention is False


def test_check_mention_detects_entity_mid_text():
    text = f"привет @{BOT_USERNAME} как дела"
    offset = text.index("@")
    entities = [FakeEntity("mention", offset, len(BOT_USERNAME) + 1)]
    update = make_update(text, CLIENT_ID, chat_type="group", entities=entities)
    context = make_context()
    is_mention, clean = bot._check_mention(update, context, text)
    assert is_mention is True
    assert f"@{BOT_USERNAME}" not in clean
    assert "привет" in clean and "как дела" in clean


def test_check_mention_fallback_substring_without_entities():
    """entities отсутствует как атрибут (упрощённый Update) — включается резервный вариант."""
    text = f"@{BOT_USERNAME.upper()} привет"
    update = make_update(text, CLIENT_ID, chat_type="group")
    context = make_context()
    is_mention, clean = bot._check_mention(update, context, text)
    assert is_mention is True
    assert clean.strip() == "привет"


def test_check_mention_no_bot_username_never_raises():
    update = make_update("любой текст", CLIENT_ID, chat_type="group")
    context = make_context()
    context.bot.username = None
    is_mention, clean = bot._check_mention(update, context, "любой текст")
    assert is_mention is False
    assert clean == "любой текст"


# ---------------------------------------------------------------------------
# 14-16. /start в группе и приватно
# ---------------------------------------------------------------------------

def test_start_silent_in_group_for_client(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.BRIEF_STATES.clear()
    update = make_update("/start", CLIENT_ID, chat_type="group")
    context = make_context()
    asyncio.run(bot.start(update, context))

    context.bot.send_message.assert_not_called()
    assert CLIENT_ID not in bot.BRIEF_STATES


def test_start_silent_in_group_for_staff(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    staff_id = bot.STAFF_USERS[0]
    update = make_update("/start", staff_id, chat_type="supergroup")
    context = make_context()
    asyncio.run(bot.start(update, context))

    context.bot.send_message.assert_not_called()


def test_start_still_works_in_private_chat(monkeypatch):
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    context = make_context()
    context.bot.send_message = AsyncMock(side_effect=lambda **kwargs: MagicMock(message_id=1))
    context.bot.delete_message = AsyncMock(return_value=True)
    bot.BRIEF_STATES.clear()
    update = make_update("/start", CLIENT_ID, chat_type="private")

    asyncio.run(bot.start(update, context))

    context.bot.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# 17. Безопасное логирование маршрутизации групп
# ---------------------------------------------------------------------------

def test_group_routing_log_has_no_raw_text(monkeypatch, caplog):
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: None)
    staff_id = bot.STAFF_USERS[0]

    update = make_update(f"@{BOT_USERNAME} секретный текст сообщения", staff_id, chat_id=777, chat_type="group")
    context = make_context()
    with caplog.at_level(logging.INFO, logger="bot"):
        run(update, context)

    routing_lines = [r.message for r in caplog.records if "[GROUP ROUTING]" in r.message]
    assert routing_lines
    for line in routing_lines:
        assert "секретный текст сообщения" not in line
        assert str(staff_id) in line or "user_id=" in line


# ---------------------------------------------------------------------------
# 18. Полный набор тестов проекта по-прежнему проходит — проверяется отдельным
# запуском `python3 -m pytest tests/ -q`, а не отсюда.
# ---------------------------------------------------------------------------
