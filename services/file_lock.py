"""
Общая файловая блокировка для записи в memory.md / pending_memory.md одного проекта.

Используется и живым путём (services/context_manager.py), и офлайн-скриптом
(scripts/daily_memory_update.py), чтобы они не могли одновременно писать в файлы одного
и того же проекта и повредить их гонкой записи.
"""

import os
import fcntl
from contextlib import contextmanager

LOCK_FILENAME = ".memory.lock"


@contextmanager
def project_lock(folder: str):
    """
    Блокирующий advisory-лок на файл `<folder>/.memory.lock`.

    В отличие от run-level лока daily_memory_update.py (не блокирующий — пропускает
    весь запуск при конфликте), здесь короткое ожидание безопаснее пропуска записи:
    оба вызывающих кода держат лок лишь на время самой записи в файлы.
    """
    os.makedirs(folder, exist_ok=True)
    lock_path = os.path.join(folder, LOCK_FILENAME)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
