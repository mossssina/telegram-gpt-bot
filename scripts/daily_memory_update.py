#!/usr/bin/env python3
"""
Daily Memory Update — ежедневная консолидация памяти клиентских проектов.

Запуск:
  python3 scripts/daily_memory_update.py             — реальный запуск
  python3 scripts/daily_memory_update.py --dry-run   — предпросмотр без изменений

Использует отдельный offset «last_daily_update_offset» в memory_state.json,
не влияет на «last_processed_offset» интерактивного бота.
"""

import os
import sys
import json
import re
import shutil
import fcntl
import logging
import argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # ContextManager использует относительные пути

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from openai import OpenAI
from services.context_manager import ContextManager
from services.file_lock import project_lock

# ─── Константы ───────────────────────────────────────────────────────────────

PROJECTS_FILE         = os.path.join(ROOT, "config", "projects.json")
LOGS_DIR              = os.path.join(ROOT, "logs")
LOG_FILE              = os.path.join(LOGS_DIR, "daily_memory_update.log")
LOCK_FILE             = os.path.join(LOGS_DIR, "daily_memory_update.lock")
MAX_BACKUPS           = 14
NEW_MSG_MAX_CHARS     = 8000   # Максимум символов новых сообщений за один запуск
SECTION_SUMMARY_CHARS = 200    # Символов каждого раздела для отправки в GPT
GPT_MODEL             = "gpt-4o-mini"

SECTION_RE = re.compile(r'^#{0,3}\s*\[(\w+)\]', re.MULTILINE)

# ─── Логирование ──────────────────────────────────────────────────────────────

os.makedirs(LOGS_DIR, exist_ok=True)
log = logging.getLogger("daily_memory_update")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

# ─── Утилиты: пути ────────────────────────────────────────────────────────────

def _abs(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _folder(slug: str, entry: dict) -> str:
    return _abs(entry.get("folder", os.path.join("client_projects", slug)))


def _memory_file(slug: str, entry: dict) -> str:
    folder = entry.get("folder", os.path.join("client_projects", slug))
    return _abs(entry.get("memory_file", os.path.join(folder, "memory.md")))


def _ctx_file(slug: str, entry: dict) -> str:
    folder = entry.get("folder", os.path.join("client_projects", slug))
    return _abs(entry.get("chat_context_file", os.path.join(folder, "chat_context.md")))


def _state_file(slug: str, entry: dict) -> str:
    return os.path.join(_folder(slug, entry), "memory_state.json")


def _pending_file(slug: str, entry: dict) -> str:
    return os.path.join(_folder(slug, entry), "pending_memory.md")


# ─── Утилиты: состояние ───────────────────────────────────────────────────────

_STATE_DEFAULTS = {
    "last_processed_offset": 0,
    "last_daily_update_offset": 0,
    "last_memory_update": None,
    "last_update_datetime": None,
    "last_daily_update_datetime": None,
}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(_STATE_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {**_STATE_DEFAULTS, **json.load(f)}
    except Exception:
        return dict(_STATE_DEFAULTS)


def save_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─── Утилиты: проекты ────────────────────────────────────────────────────────

def load_projects() -> dict:
    if not os.path.exists(PROJECTS_FILE):
        return {}
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s: p for s, p in data.items() if p.get("is_active", True)}
    except Exception as e:
        log.error(f"Ошибка чтения projects.json: {e}")
        return {}


# ─── Утилиты: новые сообщения ─────────────────────────────────────────────────

def get_new_messages(ctx_path: str, state: dict) -> tuple:
    """
    Возвращает (new_text, current_offset).
    Использует last_daily_update_offset — не трогает offset интерактивного бота.
    """
    if not os.path.exists(ctx_path):
        return "", 0
    try:
        with open(ctx_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return "", 0

    current_offset = len(content)
    prev_offset = state.get("last_daily_update_offset", 0)

    if prev_offset >= current_offset:
        return "", current_offset

    new_text = content[prev_offset:]
    if len(new_text) > NEW_MSG_MAX_CHARS:
        # Обрезаем снизу (берём самые свежие)
        new_text = new_text[-NEW_MSG_MAX_CHARS:]

    return new_text, current_offset


def has_actual_messages(text: str) -> bool:
    """Проверяет, содержит ли текст реальные сообщения (не только заголовок)."""
    return "Message:" in text or "\nUser:\n" in text


# ─── Утилиты: память ─────────────────────────────────────────────────────────

def get_sections_summary(memory_path: str) -> dict:
    """Возвращает {section_name: first_N_chars} — краткое резюме для GPT."""
    if not os.path.exists(memory_path):
        return {}
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return {}

    matches = list(SECTION_RE.finditer(content))
    if not matches:
        return {"general": content[:SECTION_SUMMARY_CHARS]}

    result = {}
    for i, m in enumerate(matches):
        name = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        result[name] = content[start:end].strip()[:SECTION_SUMMARY_CHARS]
    return result


def is_duplicate(text: str, memory_path: str, pending_path: str) -> bool:
    key = text.lower().strip()[:60]
    for fp in [memory_path, pending_path]:
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                if key in f.read().lower():
                    return True
        except Exception:
            pass
    return False


# ─── Утилиты: резервные копии ────────────────────────────────────────────────

def create_backup(memory_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(memory_path)
    backup = f"{base}.backup_{ts}{ext}"
    shutil.copy2(memory_path, backup)
    return backup


def cleanup_old_backups(folder: str):
    pattern = re.compile(r'^memory\.backup_\d{8}_\d{6}\.md$')
    backups = sorted(
        f for f in os.listdir(folder) if pattern.match(f)
    )
    for old in backups[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(folder, old))
        except Exception:
            pass


# ─── Утилиты: запись в memory.md ────────────────────────────────────────────

def add_to_section(memory_path: str, section_name: str, text: str, today: str) -> bool:
    """
    Добавляет текст в раздел section_name.
    Если раздела нет — создаёт его в конце файла.
    """
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    entry_line = f"\n— {text} (добавлено {today})"

    if not os.path.exists(memory_path):
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(f"## [{section_name}]\n{entry_line}\n")
        return True

    with open(memory_path, "r", encoding="utf-8") as f:
        content = f.read()

    pat = re.compile(r'^#{0,3}\s*\[' + re.escape(section_name) + r'\]',
                     re.MULTILINE | re.IGNORECASE)
    m = pat.search(content)

    if m:
        next_m = SECTION_RE.search(content, m.end())
        insert_pos = next_m.start() if next_m else len(content)
        content = (content[:insert_pos].rstrip()
                   + "\n" + entry_line + "\n\n"
                   + content[insert_pos:].lstrip())
    else:
        content = content.rstrip() + f"\n\n## [{section_name}]\n{entry_line}\n"

    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def update_in_section(memory_path: str, section_name: str,
                      old_hint: str, new_text: str, today: str) -> bool:
    """
    Находит строку с old_hint в разделе и заменяет на new_text.
    Если old_hint не найден — добавляет как новый элемент.
    Если файла нет — создаёт.
    """
    if not os.path.exists(memory_path):
        return add_to_section(memory_path, section_name, new_text, today)

    with open(memory_path, "r", encoding="utf-8") as f:
        content = f.read()

    pat = re.compile(r'^#{0,3}\s*\[' + re.escape(section_name) + r'\]',
                     re.MULTILINE | re.IGNORECASE)
    m = pat.search(content)

    if not m:
        return add_to_section(memory_path, section_name, new_text, today)

    next_m = SECTION_RE.search(content, m.end())
    sec_start = m.end()
    sec_end = next_m.start() if next_m else len(content)
    sec = content[sec_start:sec_end]

    hint_lower = old_hint.lower().strip()
    if hint_lower not in sec.lower():
        # Подсказка не найдена — добавляем как новый элемент
        return add_to_section(memory_path, section_name, new_text, today)

    idx = sec.lower().find(hint_lower)
    line_start = sec.rfind("\n", 0, idx) + 1
    line_end = sec.find("\n", idx)
    if line_end == -1:
        line_end = len(sec)

    old_line = sec[line_start:line_end].strip()
    new_line = f"— {new_text} (обновлено {today})"
    # Старую версию помечаем как устаревшую, новую добавляем следом
    new_sec = (
        sec[:line_start]
        + f"~~{old_line}~~ (устарело {today})\n{new_line}"
        + sec[line_end:]
    )
    content = content[:sec_start] + new_sec + content[sec_end:]

    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def add_to_pending(pending_path: str, items: list):
    os.makedirs(os.path.dirname(pending_path), exist_ok=True)
    if not os.path.exists(pending_path):
        with open(pending_path, "w", encoding="utf-8") as f:
            f.write("# Кандидаты в память\n\n")
    now = datetime.now().isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y-%m-%d")
    with open(pending_path, "a", encoding="utf-8") as f:
        for item in items:
            f.write(
                f"\n## ТРЕБУЕТ ПРОВЕРКИ — {now}\n"
                f"Раздел: {item.get('section', '?')}\n"
                f"Текст: {item.get('text', '')}\n"
                f"Причина: {item.get('reason', 'неоднозначность')}\n"
                f"Дата: {today}\n\n"
            )


# ─── GPT ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Ты менеджер памяти SMM-агентства. Твоя задача — анализировать новые сообщения "
    "из рабочего чата проекта и определять, какие устойчивые знания нужно добавить "
    "или обновить в базе знаний проекта.\n\n"
    "СОХРАНЯТЬ:\n"
    "- изменения позиционирования и особенности бренда\n"
    "- целевая аудитория, tone of voice\n"
    "- требования к текстам и визуалу, форматы контента\n"
    "- запрещённые слова и приёмы\n"
    "- принятые решения и договорённости\n"
    "- регулярные процессы, партнёры, подрядчики\n"
    "- новые услуги и направления\n"
    "- долгосрочные предпочтения клиента\n"
    "- актуальные сроки и регулярные обязательства\n\n"
    "НЕ СОХРАНЯТЬ:\n"
    "- приветствия, благодарности\n"
    "- временные обсуждения и промежуточные версии\n"
    "- разовые организационные сообщения\n"
    "- неподтверждённые предположения\n\n"
    "ПРОТИВОРЕЧИЯ: если новое знание противоречит существующему — помести в updates "
    "(новое имеет приоритет). Если неоднозначно — помести в pending.\n\n"
    "Верни ТОЛЬКО валидный JSON без пояснений:\n"
    '{"additions":[{"section":"...","text":"..."}],'
    '"updates":[{"section":"...","old_hint":"...","new_text":"..."}],'
    '"pending":[{"section":"...","text":"...","reason":"..."}]}\n\n'
    "Допустимые разделы: company, positioning, services, target_audience, "
    "tone_of_voice, content_rules, projects, history, faq, preferences, "
    "forbidden, gpt_rules.\n"
    'Если нет изменений — верни {"additions":[],"updates":[],"pending":[]}.'
)


def call_gpt(oai: OpenAI, project_title: str,
             new_messages: str, sections_summary: dict) -> dict:
    """
    Вызывает GPT для анализа новых сообщений.
    Отправляет только: new_messages + краткое резюме разделов (не весь memory.md).
    """
    summary_lines = "\n".join(
        f"[{name}]: {val[:SECTION_SUMMARY_CHARS].replace(chr(10), ' ')}"
        for name, val in sections_summary.items()
    )
    user_content = (
        f"ПРОЕКТ: {project_title}\n\n"
        f"РАЗДЕЛЫ ПАМЯТИ (краткое содержание):\n"
        f"{summary_lines if summary_lines else '(память пуста)'}\n\n"
        f"НОВЫЕ СООБЩЕНИЯ ИЗ ЧАТА:\n{new_messages}"
    )

    response = oai.chat.completions.create(
        model=GPT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=1200,
    )
    raw = response.choices[0].message.content
    result = json.loads(raw)
    return {
        "additions": result.get("additions", []),
        "updates":   result.get("updates", []),
        "pending":   result.get("pending", []),
    }


# ─── Обработка одного проекта ─────────────────────────────────────────────────

def process_project(oai: OpenAI, slug: str, entry: dict, dry_run: bool) -> dict:
    stats = {
        "slug":          slug,
        "title":         entry.get("title", slug),
        "new_msg_chars": 0,
        "found":         0,
        "added":         0,
        "updated":       0,
        "duplicates":    0,
        "pending_count": 0,
        "index_rebuilt": False,
        "status":        "ok",
        "error":         None,
        "skipped":       False,
        "skip_reason":   "",
    }

    folder       = _folder(slug, entry)
    memory_path  = _memory_file(slug, entry)
    ctx_path     = _ctx_file(slug, entry)
    state_path   = _state_file(slug, entry)
    pending_path = _pending_file(slug, entry)

    # 1. Состояние
    state = load_state(state_path)

    # 2. Новые сообщения (по last_daily_update_offset)
    new_messages, current_offset = get_new_messages(ctx_path, state)

    if not new_messages.strip():
        stats["skipped"] = True
        stats["skip_reason"] = "нет новых символов"
        stats["status"] = "skipped"
        return stats

    if not has_actual_messages(new_messages):
        # Только заголовок файла — двигаем offset и пропускаем
        if not dry_run:
            state["last_daily_update_offset"] = current_offset
            save_state(state_path, state)
        stats["skipped"] = True
        stats["skip_reason"] = "нет реальных сообщений (только заголовок)"
        stats["status"] = "skipped"
        return stats

    stats["new_msg_chars"] = len(new_messages)

    # 3. Краткое резюме памяти (без отправки всего memory.md)
    sections_summary = get_sections_summary(memory_path)

    # 4. GPT-вызов
    try:
        gpt_result = call_gpt(oai, entry.get("title", slug), new_messages, sections_summary)
    except Exception as e:
        stats["status"] = "error"
        stats["error"] = f"GPT ошибка: {e}"
        return stats

    additions = gpt_result["additions"]
    updates   = gpt_result["updates"]
    pending   = gpt_result["pending"]
    stats["found"]         = len(additions) + len(updates) + len(pending)
    stats["pending_count"] = len(pending)

    # 5. Dry-run — предпросмотр без записи
    if dry_run:
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"\n  Проект: {entry.get('title', slug)}  (slug: {slug})")
        print(f"  Новых символов: {len(new_messages)}")
        if additions:
            print(f"  Добавления ({len(additions)}):")
            for a in additions:
                print(f"    [{a.get('section','?')}]  {str(a.get('text',''))[:120]}")
        if updates:
            print(f"  Обновления ({len(updates)}):")
            for u in updates:
                print(f"    [{u.get('section','?')}]  «{str(u.get('old_hint',''))[:50]}»"
                      f" → «{str(u.get('new_text',''))[:70]}»")
        if pending:
            print(f"  Спорные ({len(pending)}):")
            for p in pending:
                print(f"    [{p.get('section','?')}]  {str(p.get('text',''))[:100]}"
                      f"  |  причина: {p.get('reason','')}")
        if not additions and not updates and not pending:
            print("  GPT не нашёл изменений для постоянной памяти")
        return stats

    # 6. Фильтрация дублей
    filtered_additions = []
    for a in additions:
        if is_duplicate(a.get("text", ""), memory_path, pending_path):
            stats["duplicates"] += 1
        else:
            filtered_additions.append(a)

    # 7. Если реальных изменений нет — только продвигаем offset, GPT уже вызван
    if not filtered_additions and not updates and not pending:
        state["last_daily_update_offset"]   = current_offset
        state["last_daily_update_datetime"] = datetime.now().isoformat(timespec="seconds")
        save_state(state_path, state)
        stats["status"] = "no_net_changes"
        return stats

    # 8. Резервная копия ПЕРЕД изменениями
    backup_path = None
    if os.path.exists(memory_path):
        try:
            backup_path = create_backup(memory_path)
        except Exception as e:
            stats["status"] = "error"
            stats["error"] = f"Ошибка бэкапа: {e}"
            return stats

    # 9. Применяем изменения (под локом — конкурирует с живым путём ContextManager)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with project_lock(folder):
            for a in filtered_additions:
                if add_to_section(memory_path, a.get("section", "general"),
                                  a.get("text", ""), today):
                    stats["added"] += 1

            for u in updates:
                if update_in_section(memory_path,
                                     u.get("section", "general"),
                                     u.get("old_hint", ""),
                                     u.get("new_text", ""),
                                     today):
                    stats["updated"] += 1

            if pending:
                add_to_pending(pending_path, pending)

        # 10. Перестраиваем индекс через существующий ContextManager
        # (собственный лок внутри _build_memory_index — вне блока выше, чтобы не блокировать самих себя)
        cm = ContextManager(slug, entry)
        cm._build_memory_index()
        stats["index_rebuilt"] = True

        # 11. Обновляем offset (только daily)
        state["last_daily_update_offset"]   = current_offset
        state["last_daily_update_datetime"] = datetime.now().isoformat(timespec="seconds")
        state["last_memory_update"]         = datetime.now().isoformat(timespec="seconds")
        save_state(state_path, state)

        # 12. Чистим старые бэкапы (оставляем MAX_BACKUPS)
        cleanup_old_backups(folder)

    except Exception as e:
        stats["status"] = "error"
        stats["error"] = str(e)
        # Откат к резервной копии
        if backup_path and os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, memory_path)
                stats["error"] += " | ОТКАТ ВЫПОЛНЕН"
            except Exception as re_err:
                stats["error"] += f" | ОТКАТ НЕ УДАЛСЯ: {re_err}"
        # offset НЕ продвигаем при ошибке

    return stats


# ─── Основной запуск ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ежедневное обновление памяти клиентских проектов"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Только предпросмотр изменений без записи")
    args = parser.parse_args()
    dry_run = args.dry_run

    log.info("=" * 55)
    log.info(f"daily_memory_update запущен {'[DRY-RUN]' if dry_run else '[PRODUCTION]'}")

    # Защита от параллельного запуска (lock-файл + fcntl)
    os.makedirs(LOGS_DIR, exist_ok=True)
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log.warning("Другой процесс уже выполняет обновление памяти. Завершение.")
        sys.exit(0)

    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.error("OPENAI_API_KEY не задан в .env")
            sys.exit(1)

        projects = load_projects()
        if not projects:
            log.info("Активных проектов не найдено.")
            return

        log.info(f"Активных проектов: {len(projects)}")
        oai = OpenAI(api_key=api_key)

        total_added   = 0
        total_updated = 0
        total_errors  = 0
        total_skipped = 0
        gpt_calls     = 0

        for slug, entry in projects.items():
            log.info(f"[{slug}] {entry.get('title', slug)}")
            try:
                stats = process_project(oai, slug, entry, dry_run)
            except Exception as e:
                log.error(f"[{slug}] Необработанная ошибка: {e}")
                total_errors += 1
                continue

            if stats["skipped"]:
                log.info(f"[{slug}] ПРОПУЩЕН — {stats['skip_reason']}")
                total_skipped += 1
                continue

            gpt_calls += 1
            log.info(
                f"[{slug}] "
                f"символов={stats['new_msg_chars']} | "
                f"найдено={stats['found']} | "
                f"добавлено={stats['added']} | "
                f"обновлено={stats['updated']} | "
                f"дублей={stats['duplicates']} | "
                f"спорных={stats['pending_count']} | "
                f"индекс={'OK' if stats['index_rebuilt'] else 'нет'} | "
                f"статус={stats['status']}"
            )
            if stats["error"]:
                log.error(f"[{slug}] ОШИБКА: {stats['error']}")
                total_errors += 1
            else:
                total_added   += stats["added"]
                total_updated += stats["updated"]

        log.info("-" * 55)
        log.info(
            f"ИТОГО: добавлено={total_added} | обновлено={total_updated} | "
            f"ошибок={total_errors} | пропущено={total_skipped} | "
            f"GPT-вызовов={gpt_calls}"
        )

    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
