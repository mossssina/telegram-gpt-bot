"""
Тесты для истории диалога и продолжения разговора внутри клиентского проекта:
dialog_history.jsonl, режимы "новый вопрос"/"продолжить диалог", постраничная
история, изоляция между сотрудниками и проектами, безопасная запись под локом.

Как и tests/test_ui_screens.py и tests/test_group_routing.py, здесь используются
моки Update/Message (AsyncMock/MagicMock) — иначе handle_button/handle_message не
проверить. Файловые операции (dialog_history.jsonl, staff_dialog.md, .memory.lock)
идут на реальный tmp_path — это не сетевой вызов, а ровно то, что тестируется.
Единственный внешний вызов, OpenAI, везде либо не достигается, либо явно
замокан статичным ответом.
"""

import os
import sys
import json
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

BOT_USERNAME = "testbot"
STAFF_A = None  # заполняется в setup из bot.STAFF_USERS
STAFF_B = None


class FakeMessage:
    _next_id = 1000

    def __init__(self, text=None, entities=None, message_id=None, message_thread_id=None):
        if message_id is None:
            message_id = FakeMessage._next_id
            FakeMessage._next_id += 1
        self.message_id = message_id
        self.text = text
        self.message_thread_id = message_thread_id
        if entities is not None:
            self.entities = entities
        self.reply_text = AsyncMock()


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot = MagicMock()
    context.bot.username = BOT_USERNAME
    context.bot.send_message = AsyncMock(side_effect=lambda **kwargs: FakeMessage())
    context.bot.delete_message = AsyncMock(return_value=True)
    return context


def make_callback_update(data, user_id, chat_id=None):
    if chat_id is None:
        chat_id = user_id
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id, username="staffuser", first_name="Имя", is_bot=False)
    update.effective_chat = MagicMock(id=chat_id, type="private")
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = FakeMessage()
    return update


def make_message_update(text, user_id, chat_id=None, update_id=5000, username="staffuser"):
    if chat_id is None:
        chat_id = user_id
    update = MagicMock()
    update.update_id = update_id
    update.message = FakeMessage(text=text)
    update.effective_message = update.message
    update.effective_user = MagicMock(id=user_id, username=username, first_name="Имя", is_bot=False)
    update.effective_chat = MagicMock(id=chat_id, type="private")
    return update


def make_gpt_response(text="GPT reply"):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def run_button(update, context):
    asyncio.run(bot.handle_button(update, context))


def run_message(update, context):
    asyncio.run(bot.handle_message(update, context))


def setup_project(tmp_path, slug="acme", title="Acme", monkeypatch=None):
    folder = tmp_path / slug
    folder.mkdir(parents=True, exist_ok=True)
    if monkeypatch is not None:
        monkeypatch.setattr(
            bot, "load_projects_registry",
            lambda: {slug: {"title": title, "folder": str(folder)}}
        )
    return str(folder)


def seed_record(folder, user_id, username, question, answer):
    bot.append_dialog_history("acme", folder, user_id, username, question, answer)


def setup_module(module):
    global STAFF_A, STAFF_B
    STAFF_A = bot.STAFF_USERS[0]
    STAFF_B = bot.STAFF_USERS[1] if len(bot.STAFF_USERS) > 1 else bot.STAFF_USERS[0] + 1


def sent_text(context):
    return context.bot.send_message.call_args_list[-1].kwargs["text"]


# ---------------------------------------------------------------------------
# 1-2. Превью последних записей в меню проекта
# ---------------------------------------------------------------------------

def test_select_project_shows_only_last_three_records(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    for i in range(5):
        seed_record(folder, STAFF_A, "staffuser", f"вопрос{i}", f"ответ{i}")
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("select_project:acme", STAFF_A)
    context = make_context()
    run_button(update, context)

    text = sent_text(context)
    assert "вопрос4" in text and "вопрос2" in text
    assert "вопрос1" not in text and "вопрос0" not in text
    bot.ACTIVE_PROJECTS.clear()


def test_select_project_reports_empty_history(monkeypatch, tmp_path):
    setup_project(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("select_project:acme", STAFF_A)
    context = make_context()
    run_button(update, context)

    assert "История по этому проекту пока пуста." in sent_text(context)
    bot.ACTIVE_PROJECTS.clear()


# ---------------------------------------------------------------------------
# 3-4. Новый вопрос vs продолжение — что уходит в GPT
# ---------------------------------------------------------------------------

def test_new_question_sends_no_history_to_gpt(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    seed_record(folder, STAFF_A, "staffuser", "старый вопрос", "старый ответ")
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "new", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return make_gpt_response("новый ответ")
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("новый вопрос", STAFF_A)
    context = make_context()
    run_message(update, context)

    assert len(captured["messages"]) == 2
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1] == {"role": "user", "content": "новый вопрос"}


def test_continue_sends_up_to_three_pairs_of_current_staff(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    seed_record(folder, STAFF_A, "staffuser", "вопрос1", "ответ1")
    seed_record(folder, STAFF_A, "staffuser", "вопрос2", "ответ2")
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "continue", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return make_gpt_response("ответ3")
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("вопрос3", STAFF_A)
    context = make_context()
    run_message(update, context)

    messages = captured["messages"]
    assert len(messages) == 1 + 4 + 1  # system + 2 пары + новый вопрос
    assert messages[1] == {"role": "user", "content": "вопрос1"}
    assert messages[2] == {"role": "assistant", "content": "ответ1"}
    assert messages[3] == {"role": "user", "content": "вопрос2"}
    assert messages[4] == {"role": "assistant", "content": "ответ2"}
    assert messages[-1] == {"role": "user", "content": "вопрос3"}


# ---------------------------------------------------------------------------
# 5-6. Изоляция между сотрудниками и между проектами
# ---------------------------------------------------------------------------

def test_continue_history_not_mixed_between_staff(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    seed_record(folder, STAFF_A, "staffA", "вопрос от A", "ответ A")
    seed_record(folder, STAFF_B, "staffB", "вопрос от B", "ответ B")
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "continue", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return make_gpt_response("новый ответ A")
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("следующий вопрос A", STAFF_A, username="staffA")
    context = make_context()
    run_message(update, context)

    contents = [m["content"] for m in captured["messages"]]
    assert "вопрос от A" in contents
    assert "вопрос от B" not in contents


def test_dialog_history_not_mixed_between_projects(tmp_path):
    folder_acme = tmp_path / "acme"
    folder_beta = tmp_path / "beta"
    folder_acme.mkdir()
    folder_beta.mkdir()
    seed_record(str(folder_acme), STAFF_A, "staffuser", "вопрос acme", "ответ acme")
    seed_record(str(folder_beta), STAFF_A, "staffuser", "вопрос beta", "ответ beta")

    acme_records = bot.load_dialog_history(str(folder_acme))
    beta_records = bot.load_dialog_history(str(folder_beta))

    assert len(acme_records) == 1 and acme_records[0]["question"] == "вопрос acme"
    assert len(beta_records) == 1 and beta_records[0]["question"] == "вопрос beta"


# ---------------------------------------------------------------------------
# 7. Не вся история — максимум MAX_CONTINUE_PAIRS
# ---------------------------------------------------------------------------

def test_continue_never_sends_more_than_max_pairs(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    for i in range(10):
        seed_record(folder, STAFF_A, "staffuser", f"в{i}", f"о{i}")
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "continue", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return make_gpt_response("итоговый ответ")
    monkeypatch.setattr(bot.client.chat.completions, "create", fake_create)

    update = make_message_update("новый вопрос", STAFF_A)
    context = make_context()
    run_message(update, context)

    history_msgs = captured["messages"][1:-1]
    assert len(history_msgs) == bot.MAX_CONTINUE_PAIRS * 2
    contents = [m["content"] for m in history_msgs]
    assert "в9" in contents  # самая свежая из старых пар присутствует
    assert "в0" not in contents  # самая старая — отброшена


# ---------------------------------------------------------------------------
# 8. Просмотр истории не вызывает OpenAI
# ---------------------------------------------------------------------------

def _button_labels(context):
    markup = context.bot.send_message.call_args_list[-1].kwargs["reply_markup"]
    return [btn.text for row in markup.inline_keyboard for btn in row]


def test_history_pagination_never_calls_openai(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    for i in range(8):
        seed_record(folder, STAFF_A, "staffuser", f"в{i}", f"о{i}")

    def boom(**kwargs):
        raise AssertionError("Просмотр истории не должен вызывать OpenAI")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)

    context = make_context()
    update1 = make_callback_update("staff_hist:acme:0", STAFF_A)
    run_button(update1, context)
    text_page1 = sent_text(context)
    assert "в7" in text_page1  # новые сначала
    assert "Старее →" in _button_labels(context)
    assert "← Новее" not in _button_labels(context)

    update2 = make_callback_update("staff_hist:acme:5", STAFF_A)
    run_button(update2, context)
    text_page2 = sent_text(context)
    assert "в0" in text_page2
    assert "← Новее" in _button_labels(context)


# ---------------------------------------------------------------------------
# 9. Переключение проекта сбрасывает режим
# ---------------------------------------------------------------------------

def test_switching_project_resets_mode_and_question_mode(monkeypatch, tmp_path):
    setup_project(tmp_path, slug="acme", monkeypatch=None)
    folder_beta = setup_project(tmp_path, slug="beta", monkeypatch=None)
    folder_acme = str(tmp_path / "acme")
    monkeypatch.setattr(
        bot, "load_projects_registry",
        lambda: {
            "acme": {"title": "Acme", "folder": folder_acme},
            "beta": {"title": "Beta", "folder": folder_beta},
        }
    )
    seed_record(folder_acme, STAFF_A, "staffuser", "в1", "о1")
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.ACTIVE_PROJECTS.clear()

    context = make_context()
    run_button(make_callback_update("select_project:acme", STAFF_A), context)
    run_button(make_callback_update("staff_cont:acme", STAFF_A), context)
    assert bot.ACTIVE_PROJECTS[STAFF_A]["question_mode"] == "continue"

    run_button(make_callback_update("select_project:beta", STAFF_A), context)
    assert bot.ACTIVE_PROJECTS[STAFF_A]["mode"] is None
    assert bot.ACTIVE_PROJECTS[STAFF_A]["question_mode"] is None
    bot.ACTIVE_PROJECTS.clear()


# ---------------------------------------------------------------------------
# 10. История переживает "перезапуск" (файл, не память)
# ---------------------------------------------------------------------------

def test_dialog_history_survives_fresh_read(tmp_path):
    folder = str(tmp_path / "acme")
    os.makedirs(folder)
    seed_record(folder, STAFF_A, "staffuser", "вопрос до рестарта", "ответ до рестарта")

    records = bot.load_dialog_history(folder)  # "свежий" вызов, как после перезапуска бота

    assert len(records) == 1
    assert records[0]["question"] == "вопрос до рестарта"


# ---------------------------------------------------------------------------
# 11. Конкурентная запись не повреждает файл
# ---------------------------------------------------------------------------

def test_concurrent_writes_do_not_corrupt_file(tmp_path):
    folder = str(tmp_path / "acme")
    os.makedirs(folder)

    def writer(username, n):
        for i in range(n):
            bot.save_dialog_turn("acme", folder, STAFF_A, username, f"{username}-{i}", f"ответ-{i}")

    t1 = threading.Thread(target=writer, args=("worker1", 20))
    t2 = threading.Thread(target=writer, args=("worker2", 20))
    t1.start(); t2.start()
    t1.join(); t2.join()

    path = os.path.join(folder, bot.DIALOG_HISTORY_FILENAME)
    with open(path, "r", encoding="utf-8") as f:
        lines = [line for line in f.read().splitlines() if line.strip()]

    assert len(lines) == 40
    for line in lines:
        json.loads(line)  # каждая строка — валидный, не слитый с соседней JSON


# ---------------------------------------------------------------------------
# 12. "Продолжить диалог" без личной истории
# ---------------------------------------------------------------------------

def test_continue_without_personal_history_shows_notice(monkeypatch, tmp_path):
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.ACTIVE_PROJECTS.clear()

    context = make_context()
    update = make_callback_update("staff_cont:acme", STAFF_A)
    run_button(update, context)

    text = sent_text(context)
    assert "У вас пока нет диалога, который можно продолжить" in text
    assert STAFF_A not in bot.ACTIVE_PROJECTS
    bot.ACTIVE_PROJECTS.clear()


# ---------------------------------------------------------------------------
# 13. Дедупликация по update_id
# ---------------------------------------------------------------------------

def test_dedup_by_update_id_prevents_double_save(tmp_path):
    folder = str(tmp_path / "acme")
    os.makedirs(folder)

    bot.save_dialog_turn("acme", folder, STAFF_A, "staffuser", "вопрос", "ответ", update_id=777001)
    bot.save_dialog_turn("acme", folder, STAFF_A, "staffuser", "вопрос", "ответ", update_id=777001)

    records = bot.load_dialog_history(folder)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# 14. Ошибка сохранения истории не теряет уже отправленный ответ
# ---------------------------------------------------------------------------

def test_history_save_error_does_not_lose_gpt_reply(monkeypatch, tmp_path):
    bot._SAVED_DIALOG_UPDATE_IDS.clear()  # изолируемся от update_id, использованных другими тестами
    folder = setup_project(tmp_path, monkeypatch=monkeypatch)
    fake_project = {
        "slug": "acme", "title": "Acme", "folder": folder, "mode": "project_chat",
        "question_mode": "new", "registry_entry": {"folder": folder},
    }
    monkeypatch.setattr(bot, "get_active_project", lambda user_id: fake_project)
    monkeypatch.setattr(bot, "get_project_chat_entry", lambda chat_id, thread_id=None: (None, None))
    monkeypatch.setattr(bot, "is_rate_limited", lambda user_id: False)
    monkeypatch.setattr(bot.client.chat.completions, "create", lambda **k: make_gpt_response("важный ответ"))
    def boom(*a, **k):
        raise OSError("диск недоступен")
    monkeypatch.setattr(bot, "append_dialog_history", boom)

    update = make_message_update("вопрос", STAFF_A)
    context = make_context()
    run_message(update, context)

    calls = update.message.reply_text.call_args_list
    assert calls[0].args == ("важный ответ",)
    assert any("Не удалось сохранить" in (c.args[0] if c.args else "") for c in calls[1:])


# ---------------------------------------------------------------------------
# 15. build_continue_messages обрезает старые пары при превышении лимита
# ---------------------------------------------------------------------------

def test_build_continue_messages_trims_oldest_when_over_char_limit():
    pairs = [
        {"question": "старый" * 100, "answer": "старый_ответ" * 100},
        {"question": "новый вопрос", "answer": "новый ответ"},
    ]
    messages = bot.build_continue_messages(pairs, max_chars=100)

    assert len(messages) == 2  # только вторая (более новая) пара уместилась
    assert messages[0]["content"] == "новый вопрос"


# ---------------------------------------------------------------------------
# Проверка существования/активности проекта в staff_new:/staff_cont:/staff_hist:/staff_brief:
# (по итогам аудита — эти кнопки раньше не проверяли реестр вовсе)
# ---------------------------------------------------------------------------

def test_staff_new_for_missing_project_does_not_call_gpt(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {})
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    def boom(**k):
        raise AssertionError("GPT не должен вызываться для отсутствующего проекта")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("staff_new:ghost", STAFF_A)
    context = make_context()
    run_button(update, context)

    text = sent_text(context)
    assert "не найден" in text.lower() or "не активен" in text.lower()
    assert STAFF_A not in bot.ACTIVE_PROJECTS
    bot.ACTIVE_PROJECTS.clear()


def test_staff_cont_for_deactivated_project_does_not_call_gpt(monkeypatch, tmp_path):
    # Проект физически существует на диске (с реальной историей), но
    # load_projects_registry() его не отдаёт — так выглядит is_active=False.
    folder = str(tmp_path / "ghost")
    os.makedirs(folder)
    seed_record(folder, STAFF_A, "staffuser", "старый вопрос", "старый ответ")
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {})
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    def boom(**k):
        raise AssertionError("GPT не должен вызываться для деактивированного проекта")
    monkeypatch.setattr(bot.client.chat.completions, "create", boom)
    bot.ACTIVE_PROJECTS.clear()

    update = make_callback_update("staff_cont:ghost", STAFF_A)
    context = make_context()
    run_button(update, context)

    text = sent_text(context)
    assert "не найден" in text.lower() or "не активен" in text.lower()
    assert STAFF_A not in bot.ACTIVE_PROJECTS
    bot.ACTIVE_PROJECTS.clear()


def test_staff_hist_for_missing_project_does_not_read_history(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {})
    calls = []
    monkeypatch.setattr(bot, "load_dialog_history", lambda *a, **k: (calls.append((a, k)), [])[1])

    update = make_callback_update("staff_hist:ghost:0", STAFF_A)
    context = make_context()
    run_button(update, context)

    assert calls == []  # load_dialog_history не вызывался вовсе — ни по какому пути
    text = sent_text(context)
    assert "не найден" in text.lower() or "не активен" in text.lower()


def test_staff_brief_shows_brief_of_requested_slug(monkeypatch, tmp_path):
    folder_a = tmp_path / "proj_a"
    folder_a.mkdir()
    brief_a = folder_a / "brief.md"
    brief_a.write_text("# Бриф проекта: Проект А\n\nСодержание А", encoding="utf-8")
    monkeypatch.setattr(
        bot, "load_projects_registry",
        lambda: {"proj_a": {"title": "Проект А", "folder": str(folder_a), "brief_file": str(brief_a)}}
    )

    update = make_callback_update("staff_brief:proj_a", STAFF_A)
    context = make_context()
    run_button(update, context)

    assert "Содержание А" in sent_text(context)


def test_staff_brief_does_not_leak_active_projects_brief(monkeypatch, tmp_path):
    """
    Прямая проверка исправленного бага: ACTIVE_PROJECTS сотрудника указывает на
    ДРУГОЙ проект (Б), а нажата кнопка «Посмотреть бриф» проекта А — должен
    вернуться бриф именно А, а не текущего активного Б.
    """
    folder_a = tmp_path / "proj_a"
    folder_b = tmp_path / "proj_b"
    folder_a.mkdir()
    folder_b.mkdir()
    brief_a = folder_a / "brief.md"
    brief_b = folder_b / "brief.md"
    brief_a.write_text("# Бриф проекта: Проект А\n\nСекретное содержание А", encoding="utf-8")
    brief_b.write_text("# Бриф проекта: Проект Б\n\nСодержание Б", encoding="utf-8")
    monkeypatch.setattr(
        bot, "load_projects_registry",
        lambda: {
            "proj_a": {"title": "Проект А", "folder": str(folder_a), "brief_file": str(brief_a)},
            "proj_b": {"title": "Проект Б", "folder": str(folder_b), "brief_file": str(brief_b)},
        }
    )
    monkeypatch.setattr(bot, "save_bot_state", lambda: None)
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[STAFF_A] = {
        "type": "client", "name": "Проект Б", "slug": "proj_b",
        "folder": str(folder_b), "memory_file": "", "brief_file": str(brief_b),
        "mode": None, "question_mode": None,
    }

    update = make_callback_update("staff_brief:proj_a", STAFF_A)
    context = make_context()
    run_button(update, context)

    text = sent_text(context)
    assert "Секретное содержание А" in text
    assert "Содержание Б" not in text
    bot.ACTIVE_PROJECTS.clear()
