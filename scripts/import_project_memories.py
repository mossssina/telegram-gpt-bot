"""
Разовый импорт файлов памяти клиентских проектов.

Использование:
  python3 scripts/import_project_memories.py            — реальный импорт
  python3 scripts/import_project_memories.py --dry-run  — только проверка

Файлы читаются из:  imports/pending/*_memory.md
После импорта:      imports/processed/*_memory.md
"""

import os
import sys
import json
import re
import shutil
import argparse
from datetime import datetime

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PENDING_DIR    = os.path.join(ROOT, "imports", "pending")
PROCESSED_DIR  = os.path.join(ROOT, "imports", "processed")
PROJECTS_FILE  = os.path.join(ROOT, "config", "projects.json")
CLIENT_PROJECTS_DIR = os.path.join(ROOT, "client_projects")

from services.context_manager import ContextManager

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Приводит строку к виду для сравнения: нижний регистр, только буквы/цифры."""
    s = s.lower().strip()
    s = re.sub(r'[\s\-\.\_]+', '', s)
    return s


def slug_from_filename(filename: str) -> str:
    """chehova4_memory.md  →  chehova4"""
    name = filename
    if name.endswith("_memory.md"):
        name = name[: -len("_memory.md")]
    # нормализуем: нижний регистр, двойные _ убираем
    name = name.lower().strip()
    name = re.sub(r'[\s\-\.]+', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name


def extract_title_from_memory(content: str, fallback: str) -> str:
    """Извлекает название проекта из первой строки H1 файла памяти."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('#') and not line.startswith('##'):
            title = re.sub(r'^#+\s*', '', line)
            # Пропускаем строки, похожие на имя файла
            if title.lower().endswith('.md') or title.lower().endswith('.txt'):
                continue
            title = re.split(r'[—–-]', title)[0].strip()
            title = re.sub(r'\[.*?\]', '', title).strip()
            if title:
                return title
    return fallback


def load_projects() -> dict:
    if not os.path.exists(PROJECTS_FILE):
        return {}
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_projects(data: dict):
    os.makedirs(os.path.dirname(PROJECTS_FILE), exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_existing_project(projects: dict, slug: str) -> tuple:
    """
    Ищет совпадение среди существующих проектов.
    Возвращает (existing_slug, entry) или (None, None).
    Если неоднозначно — возвращает (AMBIGUOUS, [список вариантов]).
    """
    slug_norm = normalize(slug)
    matches = []

    for ex_slug, entry in projects.items():
        # 1. Точное совпадение slug
        if ex_slug == slug:
            return ex_slug, entry
        # 2. Нормализованное совпадение slug
        if normalize(ex_slug) == slug_norm:
            matches.append((ex_slug, entry))
        # 3. Нормализованное совпадение по title
        elif normalize(entry.get("title", "")) == slug_norm:
            matches.append((ex_slug, entry))

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        return "AMBIGUOUS", [m[0] for m in matches]
    return None, None


def validate_memory_file(path: str) -> tuple:
    """
    Проверяет файл памяти.
    Возвращает (ok: bool, error: str, content: str, section_count: int).
    """
    if not os.path.exists(path):
        return False, "файл не найден", "", 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError as e:
        return False, f"ошибка кодировки UTF-8: {e}", "", 0

    if not content.strip():
        return False, "файл пустой", "", 0

    # Проверяем наличие разделов (поддерживаем # [section] и ## [section])
    SECTION_RE = re.compile(r'^#{0,3}\s*\[(\w+)\]', re.MULTILINE)
    sections = SECTION_RE.findall(content)

    if not sections:
        return False, "не найдено ни одного раздела вида [section_name]", content, 0

    return True, "", content, len(sections)


def make_backup_path(filepath: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(filepath)
    return f"{base}.backup_{ts}{ext}"


def build_project_entry(slug: str, title: str) -> dict:
    """Строит запись проекта по текущей схеме projects.json."""
    folder = os.path.join("client_projects", slug)
    return {
        "title": title,
        "folder": folder,
        "memory_file": os.path.join(folder, "memory.md"),
        "brief_file": os.path.join(folder, "brief.md"),
        "chat_context_file": os.path.join(folder, "chat_context.md"),
        "is_active": True,
    }


def ensure_project_files(folder: str, slug: str, title: str, memory_content: str,
                          dry_run: bool) -> dict:
    """
    Создаёт/обновляет все файлы проекта.
    Возвращает статус каждого файла.
    """
    status = {}

    if not dry_run:
        os.makedirs(folder, exist_ok=True)

    # --- memory.md ---
    memory_path = os.path.join(folder, "memory.md")
    if not dry_run:
        if os.path.exists(memory_path):
            backup = make_backup_path(memory_path)
            shutil.copy2(memory_path, backup)
            status["memory.md"] = f"заменён (бэкап: {os.path.basename(backup)})"
        else:
            status["memory.md"] = "создан"
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(memory_content)
    else:
        status["memory.md"] = "будет создан" if not os.path.exists(memory_path) else "будет заменён (с бэкапом)"

    # --- brief.md ---
    brief_path = os.path.join(folder, "brief.md")
    if not os.path.exists(brief_path):
        if not dry_run:
            with open(brief_path, "w", encoding="utf-8") as f:
                f.write(f"# Бриф проекта: {title}\n\n_Бриф ещё не заполнен._\n")
        status["brief.md"] = "создан (пустой)"
    else:
        status["brief.md"] = "не изменён (существует)"

    # --- chat_context.md ---
    ctx_path = os.path.join(folder, "chat_context.md")
    if not os.path.exists(ctx_path):
        if not dry_run:
            with open(ctx_path, "w", encoding="utf-8") as f:
                f.write(f"# Контекст чата проекта {title}\n")
        status["chat_context.md"] = "создан (пустой)"
    else:
        status["chat_context.md"] = "не изменён (существует)"

    # --- pending_memory.md ---
    pending_path = os.path.join(folder, "pending_memory.md")
    if not os.path.exists(pending_path):
        if not dry_run:
            with open(pending_path, "w", encoding="utf-8") as f:
                f.write("# Кандидаты в память\n\n")
        status["pending_memory.md"] = "создан (пустой)"
    else:
        status["pending_memory.md"] = "не изменён (существует)"

    # --- memory_state.json ---
    state_path = os.path.join(folder, "memory_state.json")
    if not os.path.exists(state_path):
        if not dry_run:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "last_processed_offset": 0,
                    "last_memory_update": None,
                    "last_update_datetime": None,
                }, f, ensure_ascii=False, indent=2)
        status["memory_state.json"] = "создан"
    else:
        status["memory_state.json"] = "не изменён (существует)"

    return status


SECTION_RE_IDX = re.compile(r'^#{0,3}\s*\[(\w+)\]', re.MULTILINE)


def count_sections_in_content(content: str) -> int:
    return len(SECTION_RE_IDX.findall(content))


def build_index(slug: str, project_entry: dict, dry_run: bool, content: str = "") -> tuple:
    """
    Строит memory_index.json через существующий ContextManager.
    Возвращает (ok, section_count, error).
    dry_run=True: считаем разделы прямо из content, без записи.
    """
    if dry_run:
        count = count_sections_in_content(content)
        return True, count, ""
    try:
        cm = ContextManager(slug, project_entry)
        index = cm._build_memory_index()
        count = len(index.get("sections", {}))
        if count == 0:
            return False, 0, "индекс построен, но разделов 0"
        return True, count, ""
    except Exception as e:
        return False, 0, str(e)


def move_to_processed(src: str, dry_run: bool) -> str:
    """Перемещает файл в imports/processed/. Если там уже есть — добавляет метку времени."""
    if dry_run:
        return "(dry-run, не перемещается)"
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    dst = os.path.join(PROCESSED_DIR, os.path.basename(src))
    if os.path.exists(dst):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(os.path.basename(src))
        dst = os.path.join(PROCESSED_DIR, f"{base}_{ts}{ext}")
    shutil.move(src, dst)
    return os.path.basename(dst)


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def process_file(filename: str, projects: dict, dry_run: bool) -> dict:
    """
    Обрабатывает один файл памяти.
    Возвращает результат: {ok, slug, title, is_new, file_statuses, sections, error, moved_to}
    """
    result = {
        "filename": filename,
        "ok": False,
        "slug": None,
        "title": None,
        "is_new": None,
        "file_statuses": {},
        "sections": 0,
        "error": None,
        "moved_to": None,
        "skipped": False,
    }

    src_path = os.path.join(PENDING_DIR, filename)

    # 1. Валидация файла
    valid, err, content, section_count = validate_memory_file(src_path)
    if not valid:
        result["error"] = f"ОШИБКА ВАЛИДАЦИИ: {err}"
        return result

    # 2. Определяем slug из имени файла
    slug = slug_from_filename(filename)
    result["slug"] = slug

    # 3. Извлекаем название проекта из содержимого
    title = extract_title_from_memory(content, slug)
    result["title"] = title

    # 4. Ищем существующий проект
    ex_slug, ex_entry = find_existing_project(projects, slug)

    if ex_slug == "AMBIGUOUS":
        result["error"] = f"НЕОДНОЗНАЧНОЕ СОВПАДЕНИЕ: возможные варианты — {ex_entry}"
        result["skipped"] = True
        return result

    if ex_slug is not None:
        # Проект найден
        result["is_new"] = False
        slug = ex_slug
        result["slug"] = slug
        result["title"] = ex_entry.get("title", title)
        project_entry = ex_entry.copy()
        folder = os.path.join(ROOT, project_entry.get("folder", os.path.join("client_projects", slug)))
    else:
        # Новый проект
        result["is_new"] = True
        project_entry = build_project_entry(slug, title)
        folder = os.path.join(ROOT, "client_projects", slug)

    # Абсолютные пути для работы с файлами
    project_entry_abs = {
        "title": project_entry["title"] if not result["is_new"] else title,
        "folder": folder,
        "memory_file": os.path.join(folder, "memory.md"),
        "brief_file": os.path.join(folder, "brief.md"),
        "chat_context_file": os.path.join(folder, "chat_context.md"),
        "is_active": True,
    }

    # 5. Создаём/обновляем файлы проекта
    file_statuses = ensure_project_files(
        folder, slug, project_entry_abs["title"], content, dry_run
    )
    result["file_statuses"] = file_statuses

    # 6. Строим индекс через существующий ContextManager
    idx_ok, idx_count, idx_err = build_index(slug, project_entry_abs, dry_run, content=content)
    result["sections"] = idx_count
    if not idx_ok:
        result["error"] = f"ОШИБКА ИНДЕКСА: {idx_err}"
        return result

    # 7. Обновляем реестр
    if not dry_run:
        if result["is_new"]:
            # Добавляем новый проект — используем относительные пути как в существующих записях
            new_entry = build_project_entry(slug, project_entry_abs["title"])
            projects[slug] = new_entry
        else:
            # Убеждаемся, что chat_context_file прописан в существующей записи
            if "chat_context_file" not in projects[slug]:
                rel_folder = projects[slug].get("folder", os.path.join("client_projects", slug))
                projects[slug]["chat_context_file"] = os.path.join(rel_folder, "chat_context.md")
        save_projects(projects)

    # 8. Перемещаем файл в processed
    moved = move_to_processed(src_path, dry_run)
    result["moved_to"] = moved
    result["ok"] = True
    return result


def run(dry_run: bool):
    print()
    print("=" * 60)
    mode = "DRY-RUN (ничего не записывается)" if dry_run else "ИМПОРТ"
    print(f"  Memory Engine — {mode}")
    print("=" * 60)
    print()

    # Проверяем папки
    if not os.path.exists(PENDING_DIR):
        print(f"ОШИБКА: папка imports/pending не найдена: {PENDING_DIR}")
        sys.exit(1)

    # Ищем файлы
    files = sorted([
        f for f in os.listdir(PENDING_DIR)
        if f.endswith("_memory.md") and os.path.isfile(os.path.join(PENDING_DIR, f))
    ])

    if not files:
        print("Файлов *_memory.md в imports/pending/ не найдено.")
        return

    print(f"Найдено файлов: {len(files)}\n")

    projects = load_projects()

    results = []
    for filename in files:
        print(f"{'─' * 55}")
        print(f"Файл: {filename}")
        r = process_file(filename, projects, dry_run)
        results.append(r)
        # Перезагружаем проекты после каждого изменения
        if not dry_run:
            projects = load_projects()

        # Вывод результата
        if r["error"]:
            print(f"  Статус:  ОШИБКА")
            print(f"  Причина: {r['error']}")
        elif r["skipped"]:
            print(f"  Статус:  ПРОПУЩЕН")
        else:
            status = "новый" if r["is_new"] else "найден"
            print(f"  Проект:  {r['title']}  (slug: {r['slug']}, {status})")
            print(f"  Разделы: {r['sections']}")
            for fname, fstatus in r["file_statuses"].items():
                print(f"  {fname:25s}  {fstatus}")
            if r["moved_to"] and not dry_run:
                print(f"  Перемещён в processed: {r['moved_to']}")
            print(f"  Итог:    {'OK (dry-run)' if dry_run else 'УСПЕШНО'}")

    # Сводка
    print()
    print("=" * 60)
    print("  СВОДКА")
    print("=" * 60)
    total     = len(results)
    ok        = sum(1 for r in results if r["ok"])
    new       = sum(1 for r in results if r["ok"] and r["is_new"])
    updated   = sum(1 for r in results if r["ok"] and r["is_new"] is False)
    skipped   = sum(1 for r in results if r["skipped"])
    errors    = sum(1 for r in results if r["error"] and not r["skipped"])

    print(f"  Найдено файлов:           {total}")
    print(f"  Успешно импортировано:    {ok}")
    print(f"  Создано новых проектов:   {new}")
    print(f"  Обновлено существующих:   {updated}")
    print(f"  Пропущено (неоднозначно): {skipped}")
    print(f"  Ошибок:                   {errors}")

    if errors:
        print()
        print("  Файлы с ошибками:")
        for r in results:
            if r["error"] and not r["skipped"]:
                print(f"    {r['filename']:45s}  {r['error']}")

    if skipped:
        print()
        print("  Пропущенные файлы (требуется ручное сопоставление):")
        for r in results:
            if r["skipped"]:
                print(f"    {r['filename']}: {r['error']}")

    print()
    if not dry_run and ok > 0:
        print("  Проекты добавлены в config/projects.json.")
        print("  Проверьте меню сотрудника после запуска бота.")
    print()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Импорт файлов памяти клиентских проектов")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только проверка без записи файлов")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
