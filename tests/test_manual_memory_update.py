"""
Тесты для ручного обновления памяти по кнопке ("Обновить память по проектам"):
видимость кнопки только сотруднику, форматирование итога для Telegram, и
поведение при уже выполняющемся обновлении (реальный fcntl-лок, без мока).
"""

import os
import sys
import fcntl

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bot
import daily_memory_update as dmu


# ---------------------------------------------------------------------------
# Видимость кнопки
# ---------------------------------------------------------------------------

def test_update_memory_button_visible_to_staff():
    staff_id = bot.STAFF_USERS[0]

    text, markup = bot.build_start_menu(staff_id)

    callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "menu_update_memory" in callback_datas


def test_update_memory_button_hidden_from_client():
    client_id = 1  # заведомо не в STAFF_USERS
    assert client_id not in bot.STAFF_USERS

    text, markup = bot.build_start_menu(client_id)

    callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "menu_update_memory" not in callback_datas


# ---------------------------------------------------------------------------
# format_memory_update_summary
# ---------------------------------------------------------------------------

def test_format_summary_success():
    summary = {
        "status": "ok", "processed": 8, "skipped": 13,
        "added": 12, "updated": 3, "errors": 0, "gpt_calls": 8,
    }

    text = bot.format_memory_update_summary(summary)

    assert "Обновление памяти завершено." in text
    assert "Проектов обработано: 8" in text
    assert "Пропущено без изменений: 13" in text
    assert "Добавлено записей: 12" in text
    assert "Обновлено записей: 3" in text
    assert "Ошибок: 0" in text


def test_format_summary_completed_with_errors_still_uses_success_template():
    summary = {
        "status": "completed_with_errors", "processed": 5, "skipped": 2,
        "added": 1, "updated": 0, "errors": 2, "gpt_calls": 5,
    }

    text = bot.format_memory_update_summary(summary)

    assert "Обновление памяти завершено." in text
    assert "Ошибок: 2" in text


def test_format_summary_already_running():
    text = bot.format_memory_update_summary({"status": "already_running"})

    assert text == "Обновление памяти уже выполняется. Дождитесь завершения."


def test_format_summary_fatal_error_has_no_raw_exception_text():
    text = bot.format_memory_update_summary({"status": "fatal_error"})

    assert "Traceback" not in text
    assert "Error" not in text
    assert text == "Не удалось запустить обновление памяти. Обратитесь к разработчику."


# ---------------------------------------------------------------------------
# Повторный запуск при активной блокировке (реальный fcntl-лок в tmp_path)
# ---------------------------------------------------------------------------

def test_second_run_reports_already_running_while_locked(tmp_path, monkeypatch, capsys):
    lock_path = str(tmp_path / "daily_memory_update.lock")
    monkeypatch.setattr(dmu, "LOCK_FILE", lock_path)
    monkeypatch.setattr(sys, "argv", ["daily_memory_update.py", "--json-summary"])

    holder_fd = open(lock_path, "w")
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit) as exc_info:
            dmu.main()
        assert exc_info.value.code == 2

        captured = capsys.readouterr()
        json_lines = [line for line in captured.out.splitlines() if line.strip().startswith("{")]
        assert json_lines, "ожидалась JSON-строка итога в stdout"

        import json
        summary = json.loads(json_lines[-1])
        assert summary["status"] == "already_running"
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        holder_fd.close()
        if dmu._sh not in dmu.log.handlers:
            dmu.log.addHandler(dmu._sh)
