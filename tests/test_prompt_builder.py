"""
Тесты для services/prompt_builder.py — сборка system prompt из компонентов Memory Engine.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.prompt_builder import build_project_system_prompt


def test_includes_project_title_in_role_line():
    prompt = build_project_system_prompt("Базовый промпт.", "Acme", {})

    assert "Acme" in prompt
    assert prompt.startswith("Базовый промпт.")


def test_omits_project_title_line_when_empty():
    prompt = build_project_system_prompt("Базовый промпт.", "", {})

    assert "ассистент проекта" not in prompt


def test_stacks_blocks_in_documented_order():
    ctx = {
        "memory_block": "MEMORY",
        "knowledge_block": "KNOWLEDGE",
        "brief_block": "BRIEF",
        "recent_context": "RECENT",
        "new_messages": "NEW",
    }

    prompt = build_project_system_prompt("BASE", "Acme", ctx)

    order = [prompt.index(p) for p in ["BASE", "MEMORY", "KNOWLEDGE", "BRIEF", "RECENT", "NEW"]]
    assert order == sorted(order)


def test_skips_empty_blocks():
    ctx = {"memory_block": "", "knowledge_block": "", "brief_block": "", "recent_context": "", "new_messages": ""}

    prompt = build_project_system_prompt("BASE", "Acme", ctx)

    assert prompt.startswith("BASE\n\nТы ассистент проекта «Acme».")
    assert "MEMORY" not in prompt
    assert "БРИФ ПРОЕКТА:" not in prompt


def test_brief_block_gets_labeled_header():
    ctx = {"brief_block": "Отвечает на бриф-вопросы."}

    prompt = build_project_system_prompt("BASE", "", ctx)

    assert "БРИФ ПРОЕКТА:" in prompt


def test_isolation_clause_present_when_project_title_set():
    prompt = build_project_system_prompt("BASE", "Acme", {})

    assert "не используй и не упоминай сведения о других клиентских проектах" in prompt
    assert "В памяти проекта нет этой информации" in prompt


def test_isolation_clause_absent_without_project_title():
    prompt = build_project_system_prompt("BASE", "", {})

    assert "других клиентских проектах" not in prompt


def test_memory_only_context_produces_no_brief_or_chat_history_markers():
    # Форма ctx, которую реально возвращает ContextManager.prepare_memory_context —
    # только memory_block/sections_used, без brief/chat_context/staff_dialog.
    ctx = {"memory_block": "[SERVICES]\nБазовый пакет.\n", "sections_used": ["services"]}

    prompt = build_project_system_prompt("BASE", "Acme", ctx)

    assert "Базовый пакет" in prompt
    assert "БРИФ ПРОЕКТА:" not in prompt
    assert "ИСТОРИЯ ЧАТА" not in prompt
    assert "НОВЫЕ СООБЩЕНИЯ" not in prompt
