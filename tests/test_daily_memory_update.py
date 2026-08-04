"""
Тесты для scripts/daily_memory_update.py — только файловые хелперы, без вызовов GPT
(call_gpt/main не тестируются здесь — это потребовало бы мокать OpenAI).

Импортируется как обычный скрипт (не пакет), как он и задуман для запуска через
`python3 scripts/daily_memory_update.py`. Note: импорт скрипта выполняет `os.chdir(ROOT)`
на верхнем уровне — безобидно, если pytest запущен из корня репозитория (как описано
в QUICKSTART.md).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import daily_memory_update as dmu


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# add_to_section / update_in_section
# ---------------------------------------------------------------------------

def test_add_to_section_creates_file_with_section(tmp_path):
    memory_path = str(tmp_path / "memory.md")

    dmu.add_to_section(memory_path, "services", "Новый пакет за 30000.", "2026-08-04")

    content = read(memory_path)
    assert "[services]" in content
    assert "Новый пакет за 30000." in content


def test_add_to_section_appends_to_existing_section(tmp_path):
    memory_path = str(tmp_path / "memory.md")
    write(memory_path, "## [services]\n— Базовый пакет (добавлено 2026-08-01)\n\n## [history]\nСтарая запись.\n")

    dmu.add_to_section(memory_path, "services", "Премиум пакет.", "2026-08-04")

    content = read(memory_path)
    assert "Базовый пакет" in content
    assert "Премиум пакет." in content
    # запись в history не задета
    assert content.index("[services]") < content.index("[history]")


def test_update_in_section_strikes_through_old_and_adds_new(tmp_path):
    memory_path = str(tmp_path / "memory.md")
    dmu.add_to_section(memory_path, "services", "Пакет за 30000.", "2026-08-01")

    dmu.update_in_section(memory_path, "services", "30000", "Пакет за 35000.", "2026-08-04")

    content = read(memory_path)
    assert "~~" in content
    assert "Пакет за 35000." in content


def test_update_in_section_falls_back_to_add_when_hint_missing(tmp_path):
    memory_path = str(tmp_path / "memory.md")
    dmu.add_to_section(memory_path, "services", "Пакет за 30000.", "2026-08-01")

    dmu.update_in_section(memory_path, "services", "не найдётся в тексте", "Новая запись.", "2026-08-04")

    content = read(memory_path)
    assert "Новая запись." in content
    assert "~~" not in content


# ---------------------------------------------------------------------------
# get_new_messages / has_actual_messages
# ---------------------------------------------------------------------------

def test_get_new_messages_returns_text_after_offset(tmp_path):
    ctx_path = str(tmp_path / "chat_context.md")
    write(ctx_path, "## 2026-08-01\nUser:\nivan\n\nMessage:\nПривет\n")
    state = {"last_daily_update_offset": 0}

    new_text, offset = dmu.get_new_messages(ctx_path, state)

    assert "Привет" in new_text
    assert offset == len(read(ctx_path))


def test_get_new_messages_empty_when_offset_at_end(tmp_path):
    ctx_path = str(tmp_path / "chat_context.md")
    content = "## 2026-08-01\nUser:\nivan\n\nMessage:\nПривет\n"
    write(ctx_path, content)
    state = {"last_daily_update_offset": len(content)}

    new_text, offset = dmu.get_new_messages(ctx_path, state)

    assert new_text == ""


def test_has_actual_messages_detects_message_marker():
    assert dmu.has_actual_messages("Message:\nПривет") is True
    assert dmu.has_actual_messages("# Контекст чата проекта Acme\n") is False


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------

def test_is_duplicate_checks_both_memory_and_pending(tmp_path):
    memory_path = str(tmp_path / "memory.md")
    pending_path = str(tmp_path / "pending_memory.md")
    write(memory_path, "## [forbidden]\nНельзя писать про скидки.\n")

    assert dmu.is_duplicate("Нельзя писать про скидки.", memory_path, pending_path) is True
    assert dmu.is_duplicate("Совсем другой текст.", memory_path, pending_path) is False


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------

def test_load_state_returns_defaults_when_missing(tmp_path):
    state = dmu.load_state(str(tmp_path / "memory_state.json"))

    assert state["last_daily_update_offset"] == 0
    assert state["last_processed_offset"] == 0


def test_save_state_then_load_state_roundtrips(tmp_path):
    state_path = str(tmp_path / "memory_state.json")
    dmu.save_state(state_path, {**dmu._STATE_DEFAULTS, "last_daily_update_offset": 42})

    loaded = dmu.load_state(state_path)

    assert loaded["last_daily_update_offset"] == 42
