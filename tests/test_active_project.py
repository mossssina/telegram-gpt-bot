"""
Тесты для bot.get_active_project — единая точка чтения активного проекта
пользователя. Проверяют изоляцию между разными user_id и то, что запись
всегда сверяется с актуальным реестром, а не с закэшированными путями.

Импортируется как обычный модуль (bot.py — не пакет), так же как остальные
тесты подключают services/scripts. Импорт не строит Telegram Application и
не делает сетевых вызовов — ApplicationBuilder().build() вызывается только
внутри main(), под `if __name__ == "__main__":`.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot


FAKE_REGISTRY = {
    "4room": {
        "title": "4ROOM STUDIO",
        "folder": "client_projects/4room",
        "memory_file": "client_projects/4room/memory.md",
        "brief_file": "client_projects/4room/brief.md",
        "chat_context_file": "client_projects/4room/chat_context.md",
        "is_active": True,
    },
    "taiga_architects": {
        "title": "TAIGA.ARCHITECTS",
        "folder": "client_projects/taiga_architects",
        "memory_file": "client_projects/taiga_architects/memory.md",
        "brief_file": "client_projects/taiga_architects/brief.md",
        "chat_context_file": "client_projects/taiga_architects/chat_context.md",
        "is_active": True,
    },
}


def fake_registry_loader():
    return dict(FAKE_REGISTRY)


def test_two_users_have_independent_active_projects(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", fake_registry_loader)
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[111] = {"type": "client", "name": "4ROOM STUDIO", "slug": "4room", "mode": "project_chat"}
    bot.ACTIVE_PROJECTS[222] = {"type": "client", "name": "TAIGA.ARCHITECTS", "slug": "taiga_architects", "mode": "project_chat"}

    p111 = bot.get_active_project(111)
    p222 = bot.get_active_project(222)

    assert p111["slug"] == "4room"
    assert p222["slug"] == "taiga_architects"
    assert p111["folder"] != p222["folder"]


def test_no_active_project_returns_none(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", fake_registry_loader)
    bot.ACTIVE_PROJECTS.clear()

    assert bot.get_active_project(999) is None


def test_non_client_session_returns_none(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", fake_registry_loader)
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[333] = {"type": "personal", "name": "кто-то"}

    assert bot.get_active_project(333) is None


def test_project_removed_from_registry_returns_none(monkeypatch):
    """Проект удалили/деактивировали в реестре после выбора — не работаем со старым кэшем."""
    monkeypatch.setattr(bot, "load_projects_registry", lambda: {})
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[444] = {"type": "client", "name": "Old", "slug": "gone", "mode": "project_chat"}

    assert bot.get_active_project(444) is None


def test_returns_registry_entry_unmodified_for_context_manager(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", fake_registry_loader)
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[555] = {"type": "client", "name": "4ROOM STUDIO", "slug": "4room", "mode": "project_chat"}

    project = bot.get_active_project(555)

    assert project["registry_entry"] == FAKE_REGISTRY["4room"]
    assert project["mode"] == "project_chat"


def test_mode_none_when_project_selected_but_not_in_ask_mode(monkeypatch):
    monkeypatch.setattr(bot, "load_projects_registry", fake_registry_loader)
    bot.ACTIVE_PROJECTS.clear()
    bot.ACTIVE_PROJECTS[666] = {"type": "client", "name": "4ROOM STUDIO", "slug": "4room", "mode": None}

    project = bot.get_active_project(666)

    assert project is not None
    assert project["mode"] is None
