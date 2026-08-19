"""
Статическая проверка deployment/deploy.sh: .env не должен уходить на сервер при
деплое. Файл не выполняется — только читается как текст.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SCRIPT = os.path.join(ROOT, "deployment", "deploy.sh")


def _read_deploy_script() -> str:
    with open(DEPLOY_SCRIPT, "r", encoding="utf-8") as f:
        return f.read()


def test_deploy_script_excludes_env_from_rsync():
    content = _read_deploy_script()
    assert '--exclude=".env"' in content


def test_deploy_script_env_exclude_is_inside_rsync_command():
    """
    Не просто наличие строки где-то в файле — она должна быть частью самой
    команды rsync (между "rsync" и переменной REMOTE_DIR), иначе исключение
    может не относиться к реальному вызову копирования файлов.
    """
    content = _read_deploy_script()
    rsync_start = content.index("rsync -az")
    remote_dir_use = content.index("${REMOTE}:${REMOTE_DIR}/", rsync_start)
    rsync_block = content[rsync_start:remote_dir_use]
    assert '--exclude=".env"' in rsync_block
