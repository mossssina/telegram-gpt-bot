"""
Тесты для Memory Engine (services/context_manager.py) — только детерминированная логика,
без обращений к GPT (ContextManager сам GPT не вызывает).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.context_manager import ContextManager


def make_cm(tmp_path, slug="acme"):
    return ContextManager(slug, {"folder": str(tmp_path)})


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Разбор разделов memory.md
# ---------------------------------------------------------------------------

def test_parse_memory_sections_splits_by_bracket_headers(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [tone_of_voice]\nПишем на Вы.\n\n## [forbidden]\nНе шутить.\n")

    sections = cm._parse_memory_sections()

    assert sections["tone_of_voice"].strip() == "Пишем на Вы."
    assert sections["forbidden"].strip() == "Не шутить."


def test_parse_memory_sections_falls_back_to_general(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "Просто текст без разделов.")

    sections = cm._parse_memory_sections()

    assert list(sections.keys()) == ["general"]


def test_parse_memory_sections_empty_when_no_file(tmp_path):
    cm = make_cm(tmp_path)
    assert cm._parse_memory_sections() == {}


# ---------------------------------------------------------------------------
# Выбор релевантных разделов
# ---------------------------------------------------------------------------

def test_select_sections_always_includes_forbidden_and_tone(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [forbidden]\nНе упоминать конкурентов.\n\n## [tone_of_voice]\nДружелюбно.\n")

    selected = cm._select_sections("любой вопрос без ключевых слов")

    assert "forbidden" in selected
    assert "tone_of_voice" in selected


def test_select_sections_scores_by_keyword(tmp_path):
    cm = make_cm(tmp_path)
    write(
        cm.memory_file,
        "## [services]\nПакет базовый 30000 руб.\n\n## [history]\nРаботаем с 2024 года.\n",
    )

    selected = cm._select_sections("Какая цена на пакет услуг?")

    assert "services" in selected
    assert selected.index("services") < len(selected)


def test_select_sections_general_when_unsectioned(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "Просто заметки о проекте.")

    assert cm._select_sections("что угодно") == ["general"]


# ---------------------------------------------------------------------------
# Дубликаты и извлечение знаний
# ---------------------------------------------------------------------------

def test_is_duplicate_true_when_text_already_in_memory(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [forbidden]\nНельзя использовать слово 'скидка'.\n")

    assert cm._is_duplicate("Нельзя использовать слово 'скидка'.") is True


def test_is_duplicate_false_for_new_text(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [forbidden]\nНельзя использовать слово 'скидка'.\n")

    assert cm._is_duplicate("Совершенно другой текст.") is False


def test_extract_knowledge_matches_forbidden_indicator(tmp_path):
    cm = make_cm(tmp_path)

    found = cm._extract_knowledge("Нельзя упоминать цены в постах.")

    assert len(found) == 1
    assert found[0]["section"] == "forbidden"


def test_extract_knowledge_ignores_short_sentences(tmp_path):
    cm = make_cm(tmp_path)
    assert cm._extract_knowledge("Нельзя.") == []


# ---------------------------------------------------------------------------
# prepare_context — сквозной сценарий (без GPT)
# ---------------------------------------------------------------------------

def test_prepare_context_picks_up_new_messages_and_advances_offset(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [tone_of_voice]\nПишем формально.\n")
    write(cm.chat_context_file, "## 2026-08-01\nUser:\nivan\n\nMessage:\nЦены выросли.\n")

    ctx = cm.prepare_context("Какая у нас цена?", sender_name="ivan")

    assert ctx["new_messages"].strip() != ""
    assert ctx["new_offset"] > 0
    assert "tone_of_voice" in ctx["memory_block"].lower() or "ПИШЕМ" in ctx["memory_block"].upper()

    # После update_state повторный вызов не должен вернуть те же сообщения как "новые"
    cm.update_state(ctx["new_offset"])
    ctx2 = cm.prepare_context("Какая у нас цена?", sender_name="ivan")
    assert ctx2["new_messages"] == ""


def test_prepare_context_builds_index_file(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [services]\nБазовый пакет.\n")

    cm.prepare_context("вопрос")

    assert os.path.exists(cm.index_file)


# ---------------------------------------------------------------------------
# prepare_memory_context — экономный вариант для обычных вопросов
# ---------------------------------------------------------------------------

def test_prepare_memory_context_returns_only_memory_and_sections(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [services]\nБазовый пакет за 30000.\n")

    ctx = cm.prepare_memory_context("Какая у нас цена на пакет?")

    assert set(ctx.keys()) == {"memory_block", "sections_used"}
    assert "Базовый пакет" in ctx["memory_block"]
    assert "services" in ctx["sections_used"]


def test_prepare_memory_context_ignores_chat_context_and_brief(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [services]\nБазовый пакет.\n")
    write(cm.chat_context_file, "## 2026-08-06\nUser:\nivan\n\nMessage:\nСекретная информация из чата.\n")
    write(cm.brief_file, "Секретная информация из брифа.")

    ctx = cm.prepare_memory_context("вопрос")

    assert "Секретная информация из чата" not in ctx["memory_block"]
    assert "Секретная информация из брифа" not in ctx["memory_block"]


def test_prepare_memory_context_does_not_touch_pending_or_state(tmp_path):
    cm = make_cm(tmp_path)
    write(cm.memory_file, "## [forbidden]\nНельзя писать про скидки.\n")
    write(cm.chat_context_file, "## 2026-08-06\nMessage:\nНельзя использовать слово скидка.\n")

    cm.prepare_memory_context("вопрос")

    assert not os.path.exists(cm.pending_file)
    assert not os.path.exists(cm.state_file)
