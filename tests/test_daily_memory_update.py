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


def test_load_state_migrates_old_offset_field(tmp_path):
    """Проект со старой схемой (только last_daily_update_offset) не должен переобрабатываться с нуля."""
    state_path = str(tmp_path / "memory_state.json")
    dmu.save_state(state_path, {"last_daily_update_offset": 777, "last_processed_offset": 10})

    state = dmu.load_state(state_path)

    assert state["last_daily_client_chat_offset"] == 777
    assert state["last_daily_update_offset"] == 777


def test_load_state_does_not_remigrate_once_client_chat_offset_present(tmp_path):
    state_path = str(tmp_path / "memory_state.json")
    dmu.save_state(state_path, {
        "last_daily_update_offset": 999,
        "last_daily_client_chat_offset": 5,
    })

    state = dmu.load_state(state_path)

    assert state["last_daily_client_chat_offset"] == 5


def test_load_state_fresh_project_starts_client_chat_offset_at_zero(tmp_path):
    state = dmu.load_state(str(tmp_path / "memory_state.json"))

    assert state["last_daily_client_chat_offset"] == 0
    assert state["last_daily_staff_dialog_offset"] == 0
    assert state["brief_hash"] is None


# ---------------------------------------------------------------------------
# compute_brief_hash
# ---------------------------------------------------------------------------

def test_compute_brief_hash_changes_when_content_changes(tmp_path):
    brief_path = tmp_path / "brief.md"
    brief_path.write_text("Версия 1", encoding="utf-8")
    h1 = dmu.compute_brief_hash(str(brief_path))

    brief_path.write_text("Версия 2", encoding="utf-8")
    h2 = dmu.compute_brief_hash(str(brief_path))

    assert h1 != h2
    assert dmu.compute_brief_hash(str(tmp_path / "missing.md")) is None


def test_compute_brief_hash_stable_for_same_content(tmp_path):
    brief_path = tmp_path / "brief.md"
    brief_path.write_text("Одно и то же", encoding="utf-8")

    assert dmu.compute_brief_hash(str(brief_path)) == dmu.compute_brief_hash(str(brief_path))


# ---------------------------------------------------------------------------
# _read_new_slice — независимые offset'ы клиентского чата и диалога сотрудника
# ---------------------------------------------------------------------------

def test_client_chat_and_staff_dialog_offsets_are_independent(tmp_path):
    ctx_path = tmp_path / "chat_context.md"
    staff_path = tmp_path / "staff_dialog.md"
    ctx_path.write_text("## header\nMessage:\nold client msg\n", encoding="utf-8")
    staff_path.write_text(
        "## header\nСотрудник:\nivan\n\nВопрос:\nq\n\nОтвет бота:\na\n", encoding="utf-8"
    )

    # Продвигаем офсет только клиентского чата — offset диалога сотрудника остаётся на нуле
    client_offset_advanced = len(ctx_path.read_text(encoding="utf-8"))

    new_client, _ = dmu._read_new_slice(str(ctx_path), client_offset_advanced)
    new_staff, _ = dmu._read_new_slice(str(staff_path), 0)

    assert new_client == ""
    assert "Вопрос" in new_staff


# ---------------------------------------------------------------------------
# build_consolidation_user_content — маркировка источников
# ---------------------------------------------------------------------------

def test_build_consolidation_user_content_labels_each_source():
    content = dmu.build_consolidation_user_content(
        "Acme",
        {
            "client_chat": "клиент сказал А",
            "staff_dialog": "сотрудник спросил Б",
            "brief": "бриф текст В",
        },
        {},
    )

    assert "ИСТОЧНИК: клиентский чат" in content
    assert "клиент сказал А" in content
    assert "ИСТОЧНИК: диалог сотрудника с ботом" in content
    assert "сотрудник спросил Б" in content
    assert "ИСТОЧНИК: бриф проекта" in content
    assert "бриф текст В" in content


def test_build_consolidation_user_content_omits_unchanged_sources():
    content = dmu.build_consolidation_user_content("Acme", {"client_chat": "только чат"}, {})

    assert "только чат" in content
    assert "диалог сотрудника" not in content
    assert "бриф проекта" not in content


# ---------------------------------------------------------------------------
# process_project — экономия GPT-вызовов
# ---------------------------------------------------------------------------

def test_process_project_skips_gpt_call_when_nothing_changed(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("call_gpt не должен вызываться, если ничего не изменилось")

    monkeypatch.setattr(dmu, "call_gpt", boom)

    entry = {"folder": str(tmp_path), "title": "Acme"}
    stats = dmu.process_project(oai=None, slug="acme", entry=entry, dry_run=False)

    assert stats["skipped"] is True
    assert stats["status"] == "skipped"


def test_process_project_calls_gpt_once_when_client_chat_changed(tmp_path, monkeypatch):
    ctx_path = tmp_path / "chat_context.md"
    ctx_path.write_text("## 2026-08-06\nUser:\nivan\n\nMessage:\nЦены выросли.\n", encoding="utf-8")

    calls = []

    def fake_call_gpt(oai, title, source_blocks, sections_summary):
        calls.append(source_blocks)
        return {"additions": [], "updates": [], "pending": []}

    monkeypatch.setattr(dmu, "call_gpt", fake_call_gpt)

    entry = {"folder": str(tmp_path), "title": "Acme"}
    stats = dmu.process_project(oai=None, slug="acme", entry=entry, dry_run=False)

    assert len(calls) == 1
    assert "client_chat" in calls[0]
    assert "staff_dialog" not in calls[0]
    assert stats["status"] == "no_net_changes"
