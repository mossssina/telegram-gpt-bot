"""
Тесты для подключения инструкции "Чаты Studiosuccess" (instructions/05_chats.md)
через существующий общий механизм — services/instruction_manager.py + bot.py's
generic "instruction:<name>" callback. Файл 05_chats.md не редактируется здесь,
только читается для проверки.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.instruction_manager import load_instruction, INSTRUCTIONS


def test_load_instruction_chats_returns_content():
    text = load_instruction("chats")

    assert text != ""


def test_load_instruction_chats_starts_with_expected_heading():
    text = load_instruction("chats")

    assert text.startswith("# Чаты Studiosuccess")


def test_chats_key_registered_with_correct_path():
    assert "chats" in INSTRUCTIONS
    assert INSTRUCTIONS["chats"].endswith(os.path.join("instructions", "05_chats.md"))


def test_existing_four_instructions_still_load():
    for key in ["communication", "project_work", "content", "reports"]:
        assert load_instruction(key) != "", f"{key} должен по-прежнему загружаться"


def test_unknown_instruction_key_returns_empty_string():
    assert load_instruction("does_not_exist") == ""


def test_bot_py_has_chats_button_wired_to_generic_handler():
    """
    handle_button не оборачивается в тесты (нужен мок Update/CallbackQuery —
    вне границ этого набора тестов, см. CLAUDE.md). Проверяем на уровне
    исходного текста, что кнопка подключена к существующему общему
    обработчику instruction:<name>, а не к отдельному новому.
    """
    bot_py_path = os.path.join(ROOT, "bot.py")
    with open(bot_py_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert 'callback_data="instruction:chats"' in source
    assert '"Чаты Studiosuccess"' in source
    # Нет отдельного обработчика именно под чаты — только общий "instruction:"
    assert 'data == "instruction:chats"' not in source
    assert 'data.startswith("instruction:")' in source


# ---------------------------------------------------------------------------
# 06/07/08 — Соавторства, База блогеров (рекламных площадок), Доступы
# ---------------------------------------------------------------------------

def test_load_instruction_coauthorship_returns_expected_heading():
    text = load_instruction("coauthorship")

    assert text != ""
    assert text.startswith("# Соавторства")


def test_load_instruction_ad_platforms_returns_expected_heading():
    text = load_instruction("ad_platforms")

    assert text != ""
    assert text.startswith("# База рекламных площадок")


def test_load_instruction_access_returns_expected_heading():
    text = load_instruction("access")

    assert text != ""
    assert text.startswith("# Доступы")


def test_new_instruction_keys_registered_with_correct_paths():
    assert INSTRUCTIONS["coauthorship"].endswith(os.path.join("instructions", "06_soavtorstva.md"))
    assert INSTRUCTIONS["ad_platforms"].endswith(
        os.path.join("instructions", "07_advertising-platforms-knowledge-base.md")
    )
    assert INSTRUCTIONS["access"].endswith(os.path.join("instructions", "08_dostupy.md"))


def test_bot_py_has_new_buttons_wired_to_generic_handler():
    bot_py_path = os.path.join(ROOT, "bot.py")
    with open(bot_py_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert 'callback_data="instruction:coauthorship"' in source
    assert 'callback_data="instruction:ad_platforms"' in source
    assert 'callback_data="instruction:access"' in source
    assert '"Соавторства"' in source
    assert '"База блогеров"' in source
    assert '"Доступы"' in source
    # Никаких отдельных обработчиков — всё через общий instruction:<name>
    assert 'data == "instruction:coauthorship"' not in source
    assert 'data == "instruction:ad_platforms"' not in source
    assert 'data == "instruction:access"' not in source
