"""
Проверка расписания systemd-таймера ежедневного обновления памяти —
09:00 по Москве (UTC+3, без перехода на летнее время) = 06:00 UTC.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMER_PATH = os.path.join(ROOT, "deployment", "studiosuccess-memory-update.timer")


def test_timer_scheduled_for_09_00_moscow():
    with open(TIMER_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert "OnCalendar=*-*-* 06:00:00 UTC" in content
    assert "09:00" in content
