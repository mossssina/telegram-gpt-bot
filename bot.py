import os
import re
import sys
import json
import time
import asyncio
import logging
from datetime import date, datetime, timezone
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from services.context_manager import ContextManager
from services.prompt_builder import build_project_system_prompt
from services.instruction_manager import load_instruction
from services.file_lock import project_lock

load_dotenv()

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
log = logging.getLogger("bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = logging.FileHandler(os.path.join(LOGS_DIR, "bot.log"), encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STAFF_USERS = [
    int(user_id.strip())
    for user_id in os.getenv("STAFF_USERS", "").split(",")
    if user_id.strip()
]
STAFF_CHAT_ID = -1004342081714


ACTIVE_PROJECTS = {}
BRIEF_STATES = {}

# ---------------------------------------------------------------------------
# Персистентное состояние бота (переживает перезапуск)
# ---------------------------------------------------------------------------

BOT_STATE_FILE = os.path.join("config", "bot_state.json")

def load_bot_state():
    """Восстанавливает ACTIVE_PROJECTS и BRIEF_STATES из файла после перезапуска бота."""
    global ACTIVE_PROJECTS, BRIEF_STATES
    if not os.path.exists(BOT_STATE_FILE):
        return
    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ACTIVE_PROJECTS = {int(k): v for k, v in data.get("active_projects", {}).items()}
        BRIEF_STATES = {int(k): v for k, v in data.get("brief_states", {}).items()}
    except Exception as e:
        log.error(f"[BOT STATE] Ошибка чтения {BOT_STATE_FILE}: {e}")

def save_bot_state():
    """Сохраняет ACTIVE_PROJECTS и BRIEF_STATES, чтобы пережить перезапуск бота."""
    os.makedirs("config", exist_ok=True)
    data = {
        "active_projects": {str(k): v for k, v in ACTIVE_PROJECTS.items()},
        "brief_states": {str(k): v for k, v in BRIEF_STATES.items()},
    }
    try:
        with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"[BOT STATE] Ошибка записи {BOT_STATE_FILE}: {e}")

# ---------------------------------------------------------------------------
# Хранилище рабочих чатов
# ---------------------------------------------------------------------------

CHATS_FILE = os.path.join("config", "chats.json")

def load_chats() -> dict:
    if not os.path.exists(CHATS_FILE):
        return {}
    try:
        with open(CHATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_chats(data: dict):
    os.makedirs("config", exist_ok=True)
    with open(CHATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def send_to_registered_chat(context, chat_slug: str, text: str) -> bool:
    chats = load_chats()
    if chat_slug not in chats:
        log.info(f"[CHAT REGISTRY] Чат '{chat_slug}' не зарегистрирован.")
        return False
    entry = chats[chat_slug]
    chat_id = entry["chat_id"]
    thread_id = entry.get("thread_id")
    try:
        kwargs = {"chat_id": int(chat_id), "text": text}
        if thread_id:
            kwargs["message_thread_id"] = int(thread_id)
        await context.bot.send_message(**kwargs)
        return True
    except Exception as e:
        log.error(f"[CHAT SEND ERROR] slug={chat_slug} thread_id={thread_id}: {e}")
        return False

# ---------------------------------------------------------------------------
# Реестр клиентских проектов
# ---------------------------------------------------------------------------

PROJECTS_FILE = os.path.join("config", "projects.json")

def load_projects_registry() -> dict:
    """Возвращает словарь активных проектов из config/projects.json."""
    if not os.path.exists(PROJECTS_FILE):
        return {}
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {slug: p for slug, p in data.items() if p.get("is_active", True)}
    except Exception as e:
        log.error(f"[PROJECTS REGISTRY] Ошибка чтения {PROJECTS_FILE}: {e}")
        return {}

def save_projects_registry(data: dict):
    os.makedirs("config", exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def register_project_in_registry(slug: str, name: str):
    """Добавляет проект в реестр, если его ещё нет."""
    os.makedirs("config", exist_ok=True)
    try:
        if os.path.exists(PROJECTS_FILE):
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
    except Exception:
        data = {}
    if slug not in data:
        folder = os.path.join("client_projects", slug)
        data[slug] = {
            "title": name,
            "folder": folder,
            "memory_file": os.path.join(folder, "memory.md"),
            "brief_file": os.path.join(folder, "brief.md"),
            "is_active": True
        }
        save_projects_registry(data)
        log.info(f"[PROJECTS REGISTRY] Добавлен проект: {slug} ({name})")

# ---------------------------------------------------------------------------
# Проектный чат: определение проекта по chat_id и сохранение контекста
# ---------------------------------------------------------------------------

def get_project_chat_entry(chat_id: int, thread_id=None) -> tuple:
    """
    Возвращает (slug, chat_entry) для чата+топика, у которого project_slug задан.

    Сверяет и chat_id, и thread_id: один и тот же супергруппа-чат может быть
    зарегистрирован под разными слагами для разных топиков (см. CHAT_REGISTRY.md).
    Без проверки thread_id сообщение из чужого топика того же чата могло бы
    попасть в память не того проекта.
    """
    chats = load_chats()
    for slug, entry in chats.items():
        if (str(entry.get("chat_id", "")) == str(chat_id)
                and entry.get("project_slug")
                and entry.get("thread_id") == thread_id):
            return entry.get("project_slug"), entry
    return None, None

def append_to_chat_context(project_slug: str, username: str, text: str):
    """Добавляет сообщение участника чата в chat_context.md проекта."""
    projects = load_projects_registry()
    project = projects.get(project_slug, {})
    ctx_file = project.get("chat_context_file",
                           os.path.join("client_projects", project_slug, "chat_context.md"))
    os.makedirs(os.path.dirname(ctx_file), exist_ok=True)
    today = date.today().isoformat()
    entry = f"\n## {today}\nUser:\n{username}\n\nMessage:\n{text}\n"
    try:
        with open(ctx_file, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        log.error(f"[CHAT CONTEXT ERROR] {e}")

def load_chat_context(project_slug: str, max_chars: int = 3000) -> str:
    """Читает последние max_chars символов из chat_context.md."""
    projects = load_projects_registry()
    project = projects.get(project_slug, {})
    ctx_file = project.get("chat_context_file",
                           os.path.join("client_projects", project_slug, "chat_context.md"))
    if not os.path.exists(ctx_file):
        return ""
    try:
        with open(ctx_file, "r", encoding="utf-8") as f:
            content = f.read()
        return content[-max_chars:] if len(content) > max_chars else content
    except Exception:
        return ""

def append_staff_dialog(project_slug: str, folder: str, staff_name: str, question: str, reply: str):
    """
    Пишет вопрос сотрудника и ответ бота в client_projects/<slug>/staff_dialog.md.

    Намеренно отдельный файл от chat_context.md: тот читает ежедневный Memory
    Engine как реальную переписку с клиентом, а вопросы сотрудника к самому боту
    не должны туда попадать и засорять то, что оттуда извлекается.
    """
    dialog_file = os.path.join(folder, "staff_dialog.md")
    os.makedirs(os.path.dirname(dialog_file), exist_ok=True)
    today = date.today().isoformat()
    entry = (
        f"\n## {today}\n"
        f"Сотрудник:\n{staff_name}\n\nВопрос:\n{question}\n\nОтвет бота:\n{reply}\n"
    )
    try:
        with open(dialog_file, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        log.error(f"[STAFF DIALOG ERROR] slug={project_slug}: {e}")

# ---------------------------------------------------------------------------
# Структурированная история диалогов проекта (dialog_history.jsonl)
# ---------------------------------------------------------------------------
#
# В отличие от staff_dialog.md (человекочитаемый общий лог, который продолжает
# вестись как раньше), этот файл пригоден для пагинации, выборки сообщений
# конкретного сотрудника и сборки короткого контекста продолжения диалога.
# Каждая строка — отдельный JSON-объект. Не используется Memory Engine.

DIALOG_HISTORY_FILENAME = "dialog_history.jsonl"
MAX_CONTINUE_PAIRS = 3
MAX_CONTINUE_CHARS = 12000
HISTORY_PAGE_SIZE = 5

_SAVED_DIALOG_UPDATE_IDS = set()


def load_dialog_history(folder: str, user_id=None) -> list:
    """
    Читает dialog_history.jsonl целиком (старые записи первыми). Если передан
    user_id — возвращает только записи этого сотрудника (для персонального
    контекста продолжения); иначе — все записи проекта (для общего просмотра).
    Битые строки пропускаются, ошибка чтения файла только логируется.
    """
    path = os.path.join(folder, DIALOG_HISTORY_FILENAME)
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if user_id is None or rec.get("user_id") == user_id:
                    records.append(rec)
    except Exception as e:
        log.error(f"[DIALOG HISTORY READ ERROR] folder={folder}: {e}")
        return []
    return records


def append_dialog_history(project_slug: str, folder: str, user_id: int, username: str,
                           question: str, answer: str) -> bool:
    """Добавляет одну запись в dialog_history.jsonl. Возвращает True/False по факту записи."""
    os.makedirs(folder, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "username": username,
        "project_slug": project_slug,
        "question": question,
        "answer": answer,
    }
    try:
        with open(os.path.join(folder, DIALOG_HISTORY_FILENAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        log.error(f"[DIALOG HISTORY ERROR] slug={project_slug}: {e}")
        return False


def save_dialog_turn(project_slug: str, folder: str, user_id: int, username: str,
                      question: str, answer: str, update_id=None) -> bool:
    """
    Сохраняет один обмен «вопрос-ответ»: и в структурированный dialog_history.jsonl,
    и (под тем же project_lock) в читаемый staff_dialog.md через уже существующий
    append_staff_dialog — так одновременная запись двух сотрудников не повредит
    ни один из файлов. Дедуплицирует по update_id: при повторной доставке одного и
    того же Telegram update повторной записи не будет. Никогда не поднимает
    исключение наружу — ошибка только логируется, вызывающий код узнаёт об этом по
    возвращаемому False и не теряет уже отправленный сотруднику ответ GPT.
    """
    if update_id is not None:
        if update_id in _SAVED_DIALOG_UPDATE_IDS:
            log.info(f"[DIALOG HISTORY] update_id={update_id} уже сохранён — пропуск повтора.")
            return True
        _SAVED_DIALOG_UPDATE_IDS.add(update_id)
        if len(_SAVED_DIALOG_UPDATE_IDS) > 2000:
            _SAVED_DIALOG_UPDATE_IDS.clear()
    try:
        with project_lock(folder):
            ok = append_dialog_history(project_slug, folder, user_id, username, question, answer)
            append_staff_dialog(project_slug, folder, username, question, answer)
        return ok
    except Exception as e:
        log.error(f"[DIALOG SAVE ERROR] slug={project_slug}: {e}")
        return False


def build_continue_messages(pairs: list, max_chars: int = MAX_CONTINUE_CHARS) -> list:
    """
    Превращает пары {question, answer} в сообщения для Chat Completions
    (user/assistant по очереди, от старых к новым). Если суммарная длина текста
    превышает max_chars, удаляет из начала (самые старые) пары, пока не уложится.
    """
    pairs = list(pairs)

    def total_len(ps):
        return sum(len(p.get("question", "")) + len(p.get("answer", "")) for p in ps)

    while pairs and total_len(pairs) > max_chars:
        pairs.pop(0)
    messages = []
    for p in pairs:
        messages.append({"role": "user", "content": p.get("question", "")})
        messages.append({"role": "assistant", "content": p.get("answer", "")})
    return messages


def format_dialog_pairs(pairs: list) -> str:
    """Человекочитаемый рендер записей истории для Telegram. Никогда не идёт в GPT."""
    blocks = []
    for rec in pairs:
        blocks.append(
            f"Сотрудник: {rec.get('username') or rec.get('user_id', '?')}\n"
            f"Вопрос: {rec.get('question', '')}\n"
            f"Ответ: {rec.get('answer', '')}"
        )
    return "\n\n---\n\n".join(blocks)

# --- Вопросы брифа (строго с сайта strategy.studiosuccess.ru/hi) ---

BRIEF_QUESTIONS = [
    {"q": "Название вашего бренда", "type": "text"},
    {"q": "Ваш номер телефона", "type": "text"},
    {"q": "Ссылка на ваш бренд (сайт, Instagram)", "type": "text"},
    {"q": "В какой индустрии работаете?", "type": "single", "options": [
        "Дизайн интерьера", "Архитектура", "Мебель",
        "Инфопродукт", "Предметный дизайн", "Производство", "Свой вариант"
    ]},
    {"q": "Расскажите подробнее о вашем продукте", "type": "text"},
    {"q": "В чём ваше преимущество?", "type": "text"},
    {"q": "Опишите вашего клиента", "type": "text"},
    {"q": "Кто вам нравится из коллег/конкурентов?", "type": "text"},
    {"q": "Пожелания к визуальному представлению", "type": "text"},
    {"q": "Пожелания к текстовому представлению", "type": "text"},
    {"q": "Как вы представлены в сети?", "type": "multi", "options": [
        "Фирменный стиль", "Сайт", "Социальные сети",
        "Профессиональные площадки", "Ничего нет", "Что-то еще"
    ]},
    {"q": "Есть ли у вас контент?", "type": "multi", "options": [
        "Рендеры", "Коллажи", "Планировки", "Скетчи",
        "Проф фото", "Проф видео", "Фото на телефон", "Видео на телефон"
    ]},
    {"q": "Нужна ли вам генерация контента?", "type": "text"},
    {"q": "Необходимо ли что-то ещё?", "type": "text"},
    {"q": "Как вы вели свой проект ранее?", "type": "text"},
    {"q": "Ваши ожидания от совместной работы", "type": "text"},
    {"q": "Любая важная информация", "type": "text"},
    {"q": "Откуда о нас узнали?", "type": "text"},
]

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def is_staff(update: Update) -> bool:
    return update.effective_user.id in STAFF_USERS

def _check_mention(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> tuple:
    """
    Определяет, упомянут ли бот в сообщении. Возвращает (is_mention, clean_text).

    Предпочитает entities сообщения (тип "mention", точное сравнение username без
    учёта регистра) — надёжнее, чем подстрока, которая может случайно совпасть
    внутри произвольного текста или URL. Резервный вариант — точное вхождение
    "@username" без учёта регистра, когда entities недоступны (например, в
    упрощённых Update в тестах).
    """
    bot_username = (context.bot.username or "").lower()
    if not bot_username:
        return False, text
    message = update.message
    if hasattr(message, "entities") and message.entities is not None:
        # entities — авторитетный источник (так их видит сам Telegram): даже
        # пустой список означает "упоминаний нет", подстрока внутри произвольного
        # текста/email/URL не должна засчитываться как обращение к боту.
        for ent in message.entities:
            if ent.type == "mention":
                entity_text = text[ent.offset: ent.offset + ent.length]
                if entity_text.lstrip("@").lower() == bot_username:
                    clean = (text[:ent.offset] + text[ent.offset + ent.length:]).strip()
                    return True, clean
        return False, text
    # entities недоступны (нестандартный/упрощённый Update) — резервный вариант.
    mention = f"@{bot_username}"
    idx = text.lower().find(mention)
    if idx == -1:
        return False, text
    clean = (text[:idx] + text[idx + len(mention):]).strip()
    return True, clean

def get_active_project(user_id: int):
    """
    Единая точка получения активного клиентского проекта пользователя.

    Каждый user_id хранит свою собственную запись в ACTIVE_PROJECTS — сотрудники
    никогда не разделяют один и тот же активный проект. Запись всегда сверяется
    с актуальным config/projects.json: если проект с тех пор пропал из реестра
    (удалён или деактивирован), возвращает None вместо того, чтобы работать со
    старыми закэшированными путями к файлам памяти.

    Возвращает dict {slug, title, folder, mode, registry_entry} или None, если
    активный проект не выбран. `registry_entry` — сырая запись из
    config/projects.json как есть, её и нужно передавать в ContextManager(...)
    (не собранный здесь dict — чтобы не потерять его собственную логику путей
    по умолчанию).
    """
    session = ACTIVE_PROJECTS.get(user_id)
    if not session or session.get("type") != "client":
        return None
    slug = session.get("slug")
    registry = load_projects_registry()
    entry = registry.get(slug)
    if not entry:
        return None
    return {
        "slug": slug,
        "title": entry.get("title", session.get("name", slug)),
        "folder": entry.get("folder", os.path.join("client_projects", slug)),
        "mode": session.get("mode"),
        "question_mode": session.get("question_mode"),
        "registry_entry": entry,
    }

def get_current_project_title(user_id: int) -> str:
    project = get_active_project(user_id)
    if project:
        return project["title"]
    return "не выбран"

def build_staff_project_header(user_id: int) -> str:
    if user_id not in STAFF_USERS:
        return ""
    return f"Проект: {get_current_project_title(user_id)}\n\n"

def project_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "_", slug)
    return slug[:45] or "project_unknown"

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
Ты Telegram-ассистент SMM-агентства Studiosuccess.
Отвечай профессионально, кратко, без эмодзи.
Помогай с текстами, контент-планами, анализом брифов, идеями для постов, сторис и Reels.
"""

# ---------------------------------------------------------------------------
# Ограничения на обращения к GPT (стоимость / защита от спама)
# ---------------------------------------------------------------------------

GPT_MAX_TOKENS = 800
GPT_COOLDOWN_SECONDS = 3
LAST_GPT_CALL = {}

def is_rate_limited(user_id: int) -> bool:
    """Не даёт одному пользователю дёргать GPT чаще, чем раз в GPT_COOLDOWN_SECONDS."""
    now = time.time()
    last = LAST_GPT_CALL.get(user_id, 0)
    if now - last < GPT_COOLDOWN_SECONDS:
        return True
    LAST_GPT_CALL[user_id] = now
    return False

# ---------------------------------------------------------------------------
# Безопасная отправка длинных ответов GPT (лимит сообщения Telegram — 4096
# символов; берём запас — 3800, как и в send_ui_screen)
# ---------------------------------------------------------------------------

GPT_REPLY_CHUNK_SIZE = 3800


def split_text_for_telegram(text: str, max_len: int = GPT_REPLY_CHUNK_SIZE) -> list:
    """
    Режет text на части не длиннее max_len для отправки в Telegram. Предпочитает
    резать по границе абзаца ("\n\n"), затем по строке ("\n"), и только если
    сам абзац/строка длиннее лимита — жёстко по max_len символов.

    "".join(chunks) == text всегда — ни один символ не теряется и не переставляется.
    """
    if len(text) <= max_len:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        cut = window.rfind("\n\n")
        if cut == -1:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_gpt_reply(update, reply: str):
    """
    Отправляет ответ GPT одним или несколькими сообщениями (см. split_text_for_telegram).
    Не вызывает GPT повторно, не участвует в ui_screens (как и весь путь
    handle_message) — это ответ на вопрос, а не UI-экран меню.
    """
    for chunk in split_text_for_telegram(reply):
        await update.message.reply_text(chunk)

# ---------------------------------------------------------------------------
# Файловые операции: брифы
# ---------------------------------------------------------------------------

def save_brief_files(slug: str, name: str, answers: list):
    folder = os.path.join("client_projects", slug)
    os.makedirs(folder, exist_ok=True)

    data = {
        "project_name": name,
        "answers": [
            {"question": BRIEF_QUESTIONS[i]["q"], "answer": answers[i]}
            for i in range(len(answers))
        ]
    }
    with open(os.path.join(folder, "brief.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lines = [f"# Бриф проекта: {name}\n"]
    for i, answer in enumerate(answers):
        lines.append(f"## {BRIEF_QUESTIONS[i]['q']}\n{answer}\n")
    with open(os.path.join(folder, "brief.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    memory_path = os.path.join(folder, "memory.md")
    if not os.path.exists(memory_path):
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\nБриф заполнен через Telegram-бота.\n")

def load_project_brief(slug: str) -> str:
    """Возвращает содержимое brief.md или пустую строку."""
    path = os.path.join("client_projects", slug, "brief.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def load_project_memory(slug: str) -> str:
    """Возвращает содержимое memory.md или пустую строку."""
    path = os.path.join("client_projects", slug, "memory.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def get_client_projects() -> list:
    """Возвращает список (slug, project_name) проектов с заполненным брифом."""
    projects_dir = "client_projects"
    if not os.path.exists(projects_dir):
        return []
    result = []
    for slug in sorted(os.listdir(projects_dir)):
        brief_path = os.path.join(projects_dir, slug, "brief.json")
        if os.path.exists(brief_path):
            try:
                with open(brief_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                result.append((slug, data.get("project_name", slug)))
            except Exception:
                result.append((slug, slug))
    return result

# ---------------------------------------------------------------------------
# Бриф: state machine
# ---------------------------------------------------------------------------

async def advance_brief(user_id: int, send_fn, context=None):
    state = BRIEF_STATES.get(user_id)
    if not state:
        return

    idx = state["question_index"]

    if idx >= len(BRIEF_QUESTIONS):
        try:
            save_brief_files(state["project_slug"], state["project_name"], state["answers"])
            register_project_in_registry(state["project_slug"], state["project_name"])
        except Exception as e:
            log.error(f"[BRIEF SAVE ERROR] {e}")
        if context:
            try:
                await send_to_registered_chat(
                    context, "team",
                    f"Новый бриф заполнен.\n\nПроект:\n{state['project_name']}\n\nБриф сохранен в памяти проекта."
                )
            except Exception as e:
                log.error(f"[BRIEF NOTIFY ERROR] {e}")
        del BRIEF_STATES[user_id]
        save_bot_state()
        await send_fn(
            "Спасибо, мы взяли в работу ваши ответы.",
            InlineKeyboardMarkup([[InlineKeyboardButton("← Главное меню", callback_data="back_to_main")]])
        )
        return

    q = BRIEF_QUESTIONS[idx]
    text = f"Вопрос {idx + 1} из {len(BRIEF_QUESTIONS)}:\n\n{q['q']}"

    if q["type"] == "text":
        await send_fn(text)
    elif q["type"] == "single":
        # question_index зашит в callback_data, чтобы обработчик мог отличить
        # ответ на ТЕКУЩИЙ вопрос от повторного/устаревшего нажатия старой кнопки.
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"brief_opt:{idx}:{opt_idx}")]
            for opt_idx, opt in enumerate(q["options"])
        ]
        await send_fn(text, InlineKeyboardMarkup(keyboard))
    elif q["type"] == "multi":
        state["current_multi_selection"] = []
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"brief_tog:{opt}")] for opt in q["options"]]
        keyboard.append([InlineKeyboardButton("Готово →", callback_data="brief_done")])
        await send_fn(text, InlineKeyboardMarkup(keyboard))

# ---------------------------------------------------------------------------
# UI-экраны: удаление предыдущего локального меню/раздела при навигации
# ---------------------------------------------------------------------------
#
# Хранилище — context.user_data["ui_screens"] = {str(chat_id): [message_id, ...]}.
# user_data уже изолирован по пользователю самим python-telegram-bot, здесь
# дополнительно разбито по chat_id — чтобы приватный чат сотрудника и групповой
# чат, где он тоже что-то нажал, не путали экраны друг друга.
#
# Только для интерфейсных сообщений (меню, инструкции, карточки, подтверждения).
# Никогда не трогает: сообщения пользователей, вопросы/ответы GPT, сообщения из
# групповых/проектных чатов, итог ручного обновления памяти, шаги брифа-визарда
# (те — часть отдельного, линейного Q&A-потока, и намеренно вне этой системы).

def _ui_screens(context) -> dict:
    return context.user_data.setdefault("ui_screens", {})


def register_ui_messages(update, context, messages, replace=True):
    """
    Регистрирует message_id, относящиеся к текущему UI-экрану этого чата.

    messages — один Message/int или список Message/int.
    replace=True (по умолчанию) — заменяет список текущего экрана целиком (новый
    экран); replace=False — добавляет к уже зарегистрированным (для составления
    одного многочастного экрана из нескольких сообщений).
    """
    chat_id = update.effective_chat.id
    screens = _ui_screens(context)
    key = str(chat_id)
    seq = messages if isinstance(messages, (list, tuple)) else [messages]
    ids = [m.message_id if hasattr(m, "message_id") else m for m in seq]
    if replace or key not in screens:
        screens[key] = ids
    else:
        screens[key].extend(ids)


async def clear_ui_screen(update, context):
    """
    Удаляет все зарегистрированные сообщения текущего UI-экрана этого чата.

    Ошибки удаления (сообщение уже удалено, слишком старое, сетевая ошибка и
    т.п.) только логируются — никогда не прерывают переход и не поднимаются
    наружу. Дополнительно всегда пытается удалить само сообщение с нажатой
    кнопкой (update.callback_query.message), даже если оно не было
    зарегистрировано — это покрывает и потерю состояния после перезапуска
    бота, и случаи, когда предыдущий экран в принципе не участвовал в этой
    системе (например, последний шаг брифа-визарда).
    """
    chat_id = update.effective_chat.id
    screens = _ui_screens(context)
    key = str(chat_id)
    message_ids = list(screens.get(key, []))

    query = getattr(update, "callback_query", None)
    if query is not None and query.message is not None:
        fallback_id = query.message.message_id
        if fallback_id not in message_ids:
            message_ids.append(fallback_id)

    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            log.info(f"[UI CLEANUP] Не удалось удалить message_id={mid} chat_id={chat_id}: {e}")

    screens[key] = []


def _prepare_instruction_markdown(text: str) -> str:
    import re
    # Убираем **жирный** и *курсив*, оставляя только содержимое — они ломают Telegram Markdown v1.
    # Бэктики (`код`) не трогаем — они нужны для копирования по тапу.
    parts = re.split(r'(`[^`]+`)', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(part)
        else:
            part = re.sub(r'\*\*(.+?)\*\*', r'\1', part, flags=re.DOTALL)
            part = re.sub(r'\*(.+?)\*', r'\1', part, flags=re.DOTALL)
            result.append(part)
    return ''.join(result)


async def send_ui_screen(update, context, text, reply_markup=None, parse_mode=None) -> list:
    """
    Отправляет экран новым сообщением (или несколькими, если text длиннее
    лимита Telegram) и регистрирует все message_id как принадлежащие текущему
    UI-экрану. Клавиатура прикрепляется только к последнему сообщению. Ничего
    не удаляет — за это отвечает clear_ui_screen, вызываемый до этого.
    """
    chat_id = update.effective_chat.id
    MAX = 3800
    if len(text) <= MAX:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        sent = [msg]
    else:
        chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)]
        sent = []
        for i, chunk in enumerate(chunks):
            kb = reply_markup if i == len(chunks) - 1 else None
            msg = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_markup=kb, parse_mode=parse_mode)
            sent.append(msg)
    register_ui_messages(update, context, sent, replace=True)
    return sent


async def _show_project_not_found(update, context):
    """
    Единая точка ответа для любой кнопки, работающей с конкретным проектом по
    slug (staff_brief:/staff_new:/staff_cont:/staff_hist:), когда этот slug
    отсутствует в реестре или деактивирован. Вызывается ДО чтения файлов
    проекта, изменения ACTIVE_PROJECTS или обращения к GPT.
    """
    await clear_ui_screen(update, context)
    await send_ui_screen(
        update, context,
        "Проект не найден или больше не активен.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К проектам", callback_data="staff_select_project")]]
        )
    )

# ---------------------------------------------------------------------------
# Меню
# ---------------------------------------------------------------------------

def build_start_menu(user_id: int):
    if user_id in STAFF_USERS:
        header = build_staff_project_header(user_id)
        text = f"{header}Вы вошли как сотрудник Studiosuccess.\n\nВыберите проект."
        keyboard = [
            [InlineKeyboardButton("Выбрать проект", callback_data="staff_select_project")],
            [InlineKeyboardButton("📚 Инструкции", callback_data="menu_instructions")],
            [InlineKeyboardButton("Обновить память по проектам", callback_data="menu_update_memory")],
        ]
        if user_id == 5247434464:
            keyboard.insert(1, [InlineKeyboardButton("✍️ Написать в чат команды", callback_data="staff_write_to_team")])
            keyboard.insert(2, [InlineKeyboardButton("✍️ Написать в чат проекта", callback_data="staff_write_to_project")])
    else:
        text = (
            "Здравствуйте. Это бот Studiosuccess.\n\n"
            "Вы можете заполнить бриф, чтобы мы взяли задачу в работу."
        )
        keyboard = [
            [InlineKeyboardButton("Заполнить бриф", callback_data="client_fill_brief")],
            [InlineKeyboardButton("Услуги", callback_data="client_services")],
            [InlineKeyboardButton("База знаний", callback_data="client_knowledge_base")],
        ]
    return text, InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        # Личное меню (выбор проекта, бриф, инструкции) не должно предлагаться в
        # групповых/рабочих чатах — это персональные сценарии.
        return
    user_id = update.effective_user.id
    if user_id in BRIEF_STATES:
        del BRIEF_STATES[user_id]
        save_bot_state()
    text, reply_markup = build_start_menu(user_id)
    await clear_ui_screen(update, context)
    await send_ui_screen(update, context, text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Обработка текстовых сообщений
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_text = update.message.text
    user_id = update.effective_user.id

    # --- Проектный чат: сохранение контекста и ответ по @упоминанию ---
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    is_group = update.effective_chat.type in ("group", "supergroup")
    proj_slug, chat_entry = get_project_chat_entry(chat_id, thread_id)
    if proj_slug and chat_entry:
        is_mention, clean_text = _check_mention(update, context, user_text)

        # Сохраняем контекст только если это не бот и не команда
        sender = update.effective_user
        sender_name = sender.username or sender.first_name or str(sender.id)
        if not sender.is_bot:
            append_to_chat_context(proj_slug, sender_name, user_text)

        # Если не упоминание — молчим
        if not is_mention:
            return

        if not clean_text:
            await update.message.reply_text("Напишите вопрос после упоминания бота.")
            return

        # Упоминание — отвечаем через Memory Engine
        projects = load_projects_registry()
        project_entry = projects.get(proj_slug, {})
        query = clean_text

        cm = ContextManager(proj_slug, project_entry)
        ctx = cm.prepare_memory_context(query)
        system_with_context = build_project_system_prompt(
            SYSTEM_PROMPT, project_entry.get("title", proj_slug), ctx
        )
        log.info(
            f"[ACTIVE PROJECT] user_id={user_id} slug={proj_slug} chat_id={chat_id} "
            f"memory_file={cm.memory_file} chat_context_file={cm.chat_context_file} "
            f"sections={ctx['sections_used']}"
        )
        if is_rate_limited(user_id):
            await update.message.reply_text("Подождите немного перед следующим запросом.")
            return
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_with_context},
                    {"role": "user", "content": query}
                ],
                max_tokens=GPT_MAX_TOKENS,
            )
            reply = response.choices[0].message.content
        except Exception as e:
            log.error(f"[GPT PROJECT CHAT ERROR] {e}")
            await update.message.reply_text("Не удалось получить ответ. Попробуйте ещё раз немного позже.")
            return

        # Сохраняем ДО отправки: сбой доставки в Telegram не должен стоить уже
        # полученного (и оплаченного) ответа — запись переживёт и отказ отправки.
        append_to_chat_context(proj_slug, "Ассистент", reply)
        try:
            await send_gpt_reply(update, reply)
        except Exception as e:
            log.error(f"[TELEGRAM SEND ERROR] {e}")
            # GPT уже отработал — повторный платный запрос не запускаем.
        return

    # --- Групповой/супергрупповой чат, не привязанный к проекту: реагируем ТОЛЬКО
    # по явному @упоминанию — иначе бот отвечал бы в любом рабочем чате всякий
    # раз, когда у написавшего туда сотрудника активен режим "Задать вопрос".
    # Приватные сценарии (бриф и т.п.) ниже относятся только к приватным чатам —
    # групповое сообщение никогда не должно попадать в BRIEF_STATES.
    if is_group:
        is_mention, clean_text = _check_mention(update, context, user_text)
        if not is_mention:
            return
        user_text = clean_text or user_text
        log.info(f"[GROUP ROUTING] user_id={user_id} chat_id={chat_id} mentioned=True")
    else:
        # --- Бриф в процессе заполнения (только приватный чат) ---
        if user_id in BRIEF_STATES:
            state = BRIEF_STATES[user_id]
            idx = state["question_index"]
            q = BRIEF_QUESTIONS[idx]

            if q["type"] != "text":
                await update.message.reply_text("Пожалуйста, выберите вариант из кнопок выше.")
                return

            if idx == 0:
                state["project_name"] = user_text.strip()
                state["project_slug"] = project_slug(user_text.strip())
                brief_path = os.path.join("client_projects", state["project_slug"], "brief.json")
                if os.path.exists(brief_path):
                    del BRIEF_STATES[user_id]
                    await update.message.reply_text(
                        "Бриф по этому проекту уже заполнен.\n\n"
                        "Если у вас есть комментарий или вопрос — обратитесь в чат проекта.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("← Главное меню", callback_data="back_to_main")]]
                        )
                    )
                    return

            state["answers"].append(user_text.strip())
            state["question_index"] += 1
            save_bot_state()

            async def send_fn(text, markup=None):
                await update.message.reply_text(text, reply_markup=markup)

            await advance_brief(user_id, send_fn, context)
            return

    # --- Обычный режим (общий код для группы-по-упоминанию и приватного чата
    # вне брифа) ---
    project = get_active_project(user_id)

    if is_staff(update):
        session = ACTIVE_PROJECTS.get(user_id, {})
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("← Главное меню", callback_data="back_to_main")]])

        if session.get("mode") == "staff_chat_compose":
            try:
                await context.bot.send_message(chat_id=STAFF_CHAT_ID, text=user_text)
                await clear_ui_screen(update, context)
                await send_ui_screen(
                    update, context,
                    "✓ Отправлено в чат команды\n\nВведите следующее сообщение:",
                    reply_markup=back_btn,
                )
            except Exception as e:
                log.error(f"[STAFF CHAT SEND ERROR] {e}")
                await update.message.reply_text("Не удалось отправить сообщение. Попробуйте ещё раз.")
            return

        if session.get("mode") == "staff_project_chat_compose":
            chat_slug = session.get("chat_slug", "")
            chat_title = session.get("chat_title", chat_slug)
            ok = await send_to_registered_chat(context, chat_slug, user_text)
            if ok:
                await clear_ui_screen(update, context)
                await send_ui_screen(
                    update, context,
                    f"✓ Отправлено в «{chat_title}»\n\nВведите следующее сообщение:",
                    reply_markup=back_btn,
                )
            else:
                await update.message.reply_text("Не удалось отправить сообщение. Попробуйте ещё раз.")
            return

        if project and project.get("mode") == "project_chat":
            # Режим вопросов по проекту — используем Memory Engine
            proj_slug = project["slug"]
            staff_name = update.effective_user.username or update.effective_user.first_name or ""

            cm = ContextManager(proj_slug, project["registry_entry"])
            ctx = cm.prepare_memory_context(user_text)
            staff_note = f"Сотрудник задаёт вопрос по клиентскому проекту «{project['title']}»."
            system_with_context = build_project_system_prompt(
                SYSTEM_PROMPT + "\n\n" + staff_note, project["title"], ctx
            )
            question_mode = project.get("question_mode") or "new"
            history_messages = []
            if question_mode == "continue":
                pairs = load_dialog_history(project["folder"], user_id=user_id)[-MAX_CONTINUE_PAIRS:]
                history_messages = build_continue_messages(pairs)
            log.info(
                f"[ACTIVE PROJECT] user_id={user_id} slug={proj_slug} question_mode={question_mode} "
                f"memory_file={cm.memory_file} chat_context_file={cm.chat_context_file} "
                f"sections={ctx['sections_used']} history_pairs={len(history_messages) // 2}"
            )
            if is_rate_limited(user_id):
                await update.message.reply_text("Подождите немного перед следующим запросом.")
                return
            try:
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "system", "content": system_with_context}]
                    + history_messages
                    + [{"role": "user", "content": user_text}],
                    max_tokens=GPT_MAX_TOKENS,
                )
                reply = response.choices[0].message.content
            except Exception as e:
                log.error(f"[GPT ERROR] {e}")
                await update.message.reply_text("Не удалось получить ответ. Попробуйте ещё раз немного позже.")
                return

            # Сохраняем ДО отправки — сбой доставки в Telegram не должен стоить
            # уже полученного (и оплаченного) ответа.
            saved_ok = save_dialog_turn(
                proj_slug, project["folder"], user_id, staff_name, user_text, reply,
                update_id=update.update_id
            )
            try:
                await send_gpt_reply(update, reply)
            except Exception as e:
                log.error(f"[TELEGRAM SEND ERROR] {e}")
                # GPT уже отработал — повторный платный запрос не запускаем.
                return
            if not saved_ok:
                await update.message.reply_text(
                    "Не удалось сохранить эту запись в историю проекта. "
                    "Ответ выше сохранён, ошибка записана в лог."
                )
            await update.message.reply_text(
                "Что дальше?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Продолжить диалог", callback_data=f"staff_cont:{proj_slug}")],
                    [InlineKeyboardButton("Новый вопрос", callback_data=f"staff_new:{proj_slug}")],
                    [InlineKeyboardButton("Показать историю", callback_data=f"staff_hist:{proj_slug}:0")],
                    [InlineKeyboardButton("← К проекту", callback_data=f"select_project:{proj_slug}")],
                ])
            )
            return

        else:
            if is_group:
                return  # молчим в группе — там это сообщение бессмысленно и шумно
            await update.message.reply_text("Нажмите /start чтобы выбрать проект.")
            return

    else:
        if is_group:
            return  # клиентские сценарии (обычный GPT-ответ) не всплывают в группах
        context_note = (
            "Пишет заказчик. Отвечай только в рамках клиентского сервиса. "
            "Не раскрывай внутренние процессы агентства, имена сотрудников и рабочие инструменты."
        )
        system_with_context = SYSTEM_PROMPT.strip() + "\n\n" + context_note
        if is_rate_limited(user_id):
            await update.message.reply_text("Подождите немного перед следующим запросом.")
            return
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_with_context},
                    {"role": "user", "content": user_text}
                ],
                max_tokens=GPT_MAX_TOKENS,
            )
            reply = response.choices[0].message.content
        except Exception as e:
            log.error(f"[GPT ERROR] {e}")
            await update.message.reply_text("Не удалось получить ответ. Попробуйте ещё раз немного позже.")
            return
        try:
            await send_gpt_reply(update, reply)
        except Exception as e:
            log.error(f"[TELEGRAM SEND ERROR] {e}")
            # GPT уже отработал — повторный платный запрос не запускаем.

# ---------------------------------------------------------------------------
# Ручное обновление памяти (кнопка "Обновить память по проектам")
# ---------------------------------------------------------------------------

def format_memory_update_summary(summary: dict) -> str:
    """
    Превращает JSON-итог daily_memory_update.py (--json-summary) в текст для
    Telegram. Никогда не показывает сырые исключения, ключи или технические
    секреты — только счётчики.
    """
    status = summary.get("status")
    if status == "already_running":
        return "Обновление памяти уже выполняется. Дождитесь завершения."
    if status == "fatal_error":
        return "Не удалось запустить обновление памяти. Обратитесь к разработчику."
    return (
        "Обновление памяти завершено.\n\n"
        f"Проектов обработано: {summary.get('processed', 0)}\n"
        f"Пропущено без изменений: {summary.get('skipped', 0)}\n"
        f"Добавлено записей: {summary.get('added', 0)}\n"
        f"Обновлено записей: {summary.get('updated', 0)}\n"
        f"Ошибок: {summary.get('errors', 0)}"
    )


async def run_memory_update_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    Запускает scripts/daily_memory_update.py как отдельный процесс с
    --json-summary — не блокирует обработку апдейтов бота — и присылает
    итог в чат по завершении. Использует ту же логику и ту же файловую
    блокировку, что и автоматический ночной запуск: второй одновременный
    запуск сам сообщит "already_running".
    """
    script_path = os.path.join("scripts", "daily_memory_update.py")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path, "--json-summary",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        log.error(f"[MEMORY UPDATE] Не удалось запустить процесс: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Не удалось запустить обновление памяти.")
        return

    summary = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                summary = json.loads(line)
            except Exception:
                continue

    if summary is None:
        log.error(
            f"[MEMORY UPDATE] Не удалось разобрать вывод скрипта. "
            f"stderr={stderr.decode('utf-8', errors='replace')[:500]}"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Обновление памяти завершилось с ошибкой. Подробности — в логах сервера."
        )
        return

    log.info(f"[MEMORY UPDATE] ручной запуск завершён: {summary}")
    await context.bot.send_message(chat_id=chat_id, text=format_memory_update_summary(summary))

# ---------------------------------------------------------------------------
# Обработка кнопок
# ---------------------------------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.type in ("group", "supergroup"):
        # Inline-кнопки этого бота — личный/сотруднический интерфейс. Старые
        # кнопки, ранее отправленные ботом в групповой чат (до перехода на
        # @-упоминания), могут оставаться активными в истории чата сколь
        # угодно долго — нажатие на них не должно запускать НИКАКУЮ логику
        # (GPT, Memory Engine, изменение состояния), только погасить "часики"
        # у пользователя. Дальше в этой функции user_id/data не читаются.
        await query.answer()
        return
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Главное меню ---

    if data == "back_to_main":
        if user_id in BRIEF_STATES:
            del BRIEF_STATES[user_id]
        if user_id in ACTIVE_PROJECTS and ACTIVE_PROJECTS[user_id].get("mode") in ("staff_chat_compose", "staff_project_chat_compose"):
            del ACTIVE_PROJECTS[user_id]
        save_bot_state()
        text, reply_markup = build_start_menu(user_id)
        await clear_ui_screen(update, context)
        await send_ui_screen(update, context, text, reply_markup=reply_markup)

    # -----------------------------------------------------------------------
    # ЗАКАЗЧИК
    # -----------------------------------------------------------------------

    elif data == "client_services":
        prices_file = os.path.join("config", "prices_bot.md")
        try:
            with open(prices_file, "r", encoding="utf-8") as f:
                text = _prepare_instruction_markdown(f.read())
        except Exception:
            text = "Информация об услугах временно недоступна."
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Назад", callback_data="back_to_main")]]
            ),
            parse_mode="Markdown",
        )

    elif data == "client_knowledge_base":
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "📚 *База знаний Studiosuccess*\n\nВыберите статью:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Опубликовать проект в журнале", url="https://telegra.ph/Mediabaza-dizajn-izdanij-2024--Studio-Success-08-24")],
                [InlineKeyboardButton("← Назад", callback_data="back_to_main")],
            ]),
            parse_mode="Markdown",
        )

    elif data == "client_fill_brief":
        BRIEF_STATES[user_id] = {
            "question_index": 0,
            "project_name": None,
            "project_slug": None,
            "answers": [],
            "current_multi_selection": []
        }
        save_bot_state()
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "Начинаем заполнять бриф.\n\nОтвечайте на вопросы — это займёт несколько минут."
        )
        async def send_fn(text, markup=None):
            # Не query.message.reply_text: то сообщение уже удалено clear_ui_screen выше.
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=markup)
        await advance_brief(user_id, send_fn, context)

    elif data.startswith("brief_opt:"):
        if user_id not in BRIEF_STATES:
            await query.edit_message_text("Сессия истекла. Нажмите /start чтобы начать снова.")
            return
        state = BRIEF_STATES[user_id]
        payload = data[len("brief_opt:"):]
        parts = payload.split(":", 1)

        # Валиден только ответ на ТЕКУЩИЙ вопрос брифа с реально существующим
        # вариантом — так отсекаются и старый формат callback_data
        # (brief_opt:<текст>, до этого исправления), и повторное/устаревшее
        # нажатие уже обработанной кнопки (question_index больше не совпадает
        # с текущим состоянием).
        current_idx = state["question_index"]
        valid = False
        if (len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()
                and int(parts[0]) == current_idx
                and 0 <= current_idx < len(BRIEF_QUESTIONS)
                and BRIEF_QUESTIONS[current_idx]["type"] == "single"):
            opt_idx = int(parts[1])
            options = BRIEF_QUESTIONS[current_idx]["options"]
            if 0 <= opt_idx < len(options):
                valid = True

        if not valid:
            if 0 <= current_idx < len(BRIEF_QUESTIONS) and BRIEF_QUESTIONS[current_idx]["type"] == "single":
                q = BRIEF_QUESTIONS[current_idx]
                text = f"Вопрос {current_idx + 1} из {len(BRIEF_QUESTIONS)}:\n\n{q['q']}"
                keyboard = [
                    [InlineKeyboardButton(opt, callback_data=f"brief_opt:{current_idx}:{opt_idx}")]
                    for opt_idx, opt in enumerate(q["options"])
                ]
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.edit_message_text("Эта кнопка устарела. Нажмите /start, чтобы продолжить.")
            return

        answer = BRIEF_QUESTIONS[current_idx]["options"][opt_idx]
        state["answers"].append(answer)
        state["question_index"] += 1
        save_bot_state()
        await query.edit_message_text(f"✓ {answer}")
        async def send_fn(text, markup=None):
            await query.message.reply_text(text, reply_markup=markup)
        await advance_brief(user_id, send_fn, context)

    elif data.startswith("brief_tog:"):
        if user_id not in BRIEF_STATES:
            await query.edit_message_text("Сессия истекла. Нажмите /start чтобы начать снова.")
            return
        state = BRIEF_STATES[user_id]
        option = data[len("brief_tog:"):]
        selected = state.setdefault("current_multi_selection", [])
        if option in selected:
            selected.remove(option)
        else:
            selected.append(option)
        idx = state["question_index"]
        q = BRIEF_QUESTIONS[idx]
        keyboard = []
        for opt in q["options"]:
            label = f"✓ {opt}" if opt in selected else opt
            keyboard.append([InlineKeyboardButton(label, callback_data=f"brief_tog:{opt}")])
        keyboard.append([InlineKeyboardButton("Готово →", callback_data="brief_done")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "brief_done":
        if user_id not in BRIEF_STATES:
            await query.edit_message_text("Сессия истекла. Нажмите /start чтобы начать снова.")
            return
        state = BRIEF_STATES[user_id]
        selected = state.get("current_multi_selection", [])
        if not selected:
            await query.answer("Выберите хотя бы один вариант.", show_alert=True)
            return
        answer = ", ".join(selected)
        state["answers"].append(answer)
        state["question_index"] += 1
        state["current_multi_selection"] = []
        save_bot_state()
        await query.edit_message_text(f"✓ {answer}")
        async def send_fn(text, markup=None):
            await query.message.reply_text(text, reply_markup=markup)
        await advance_brief(user_id, send_fn, context)

    # -----------------------------------------------------------------------
    # СОТРУДНИК
    # -----------------------------------------------------------------------

    elif data == "staff_select_project":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        projects = load_projects_registry()
        if not projects:
            await clear_ui_screen(update, context)
            await send_ui_screen(
                update, context,
                "В реестре пока нет активных проектов.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("← Назад", callback_data="back_to_main")]]
                )
            )
            return
        keyboard = [
            [InlineKeyboardButton(p["title"], callback_data=f"select_project:{slug}")]
            for slug, p in projects.items()
        ]
        keyboard.append([InlineKeyboardButton("← Назад", callback_data="back_to_main")])
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            f"{build_staff_project_header(user_id)}Выберите проект:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("select_project:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("select_project:"):]
        projects = load_projects_registry()
        project_entry = projects.get(slug)
        if not project_entry:
            await clear_ui_screen(update, context)
            await send_ui_screen(
                update, context,
                "Проект не найден в реестре.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("← К проектам", callback_data="staff_select_project")]]
                )
            )
            return
        ACTIVE_PROJECTS[user_id] = {
            "type": "client",
            "name": project_entry["title"],
            "slug": slug,
            "folder": project_entry.get("folder", ""),
            "memory_file": project_entry.get("memory_file", ""),
            "brief_file": project_entry.get("brief_file", ""),
            "mode": None,
            "question_mode": None,
        }
        save_bot_state()
        folder = project_entry.get("folder", os.path.join("client_projects", slug))
        recent = load_dialog_history(folder)[-3:]
        preview = format_dialog_pairs(recent) if recent else "История по этому проекту пока пуста."
        keyboard = [
            [InlineKeyboardButton("Задать новый вопрос", callback_data=f"staff_new:{slug}")],
            [InlineKeyboardButton("Продолжить диалог", callback_data=f"staff_cont:{slug}")],
            [InlineKeyboardButton("Показать историю", callback_data=f"staff_hist:{slug}:0")],
            [InlineKeyboardButton("Посмотреть бриф", callback_data=f"staff_brief:{slug}")],
            [InlineKeyboardButton("← К проектам", callback_data="staff_select_project")],
        ]
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            f"{build_staff_project_header(user_id)}{preview}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("staff_brief:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("staff_brief:"):]
        projects = load_projects_registry()
        project_entry = projects.get(slug)
        if not project_entry:
            await _show_project_not_found(update, context)
            return
        back_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К проекту", callback_data=f"select_project:{slug}")]]
        )
        brief_text = ""
        # Бриф читается СТРОГО из записи, найденной по slug из этой кнопки — не
        # из ACTIVE_PROJECTS сотрудника, чтобы не показать бриф другого проекта,
        # если сотрудник сейчас выбрал не тот проект, на который указывает кнопка.
        brief_file = project_entry.get(
            "brief_file", os.path.join(project_entry.get("folder", os.path.join("client_projects", slug)), "brief.md")
        )
        if brief_file and os.path.exists(brief_file):
            try:
                with open(brief_file, "r", encoding="utf-8") as f:
                    brief_text = f.read()
            except Exception:
                pass
        if not brief_text:
            brief_text = load_project_brief(slug)
        header = build_staff_project_header(user_id)
        await clear_ui_screen(update, context)
        if not brief_text:
            await send_ui_screen(update, context, f"{header}Бриф по этому проекту пока не найден.", reply_markup=back_kb)
            return
        await send_ui_screen(update, context, header + brief_text, reply_markup=back_kb)

    elif data.startswith("staff_new:") or data.startswith("staff_cont:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        is_continue = data.startswith("staff_cont:")
        slug = data[len("staff_cont:"):] if is_continue else data[len("staff_new:"):]
        projects = load_projects_registry()
        project_entry = projects.get(slug)
        if not project_entry:
            await _show_project_not_found(update, context)
            return
        folder = project_entry.get("folder", os.path.join("client_projects", slug))

        if is_continue and not load_dialog_history(folder, user_id=user_id):
            await clear_ui_screen(update, context)
            await send_ui_screen(
                update, context,
                f"{build_staff_project_header(user_id)}"
                "У вас пока нет диалога, который можно продолжить. Задайте новый вопрос.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Задать новый вопрос", callback_data=f"staff_new:{slug}")]]
                )
            )
            return

        question_mode = "continue" if is_continue else "new"
        project = ACTIVE_PROJECTS.get(user_id)
        if project and project.get("slug") == slug:
            ACTIVE_PROJECTS[user_id]["mode"] = "project_chat"
            ACTIVE_PROJECTS[user_id]["question_mode"] = question_mode
        else:
            ACTIVE_PROJECTS[user_id] = {
                "type": "client",
                "name": project_entry.get("title", slug),
                "slug": slug,
                "folder": project_entry.get("folder", ""),
                "memory_file": project_entry.get("memory_file", ""),
                "brief_file": project_entry.get("brief_file", ""),
                "mode": "project_chat",
                "question_mode": question_mode,
            }
        save_bot_state()
        prompt_text = (
            "Режим: продолжение диалога. Бот учтёт до 3 последних ваших сообщений по этому "
            "проекту. Задайте вопрос."
            if is_continue else
            "Режим: новый вопрос. Задайте вопрос по проекту."
        )
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            f"{build_staff_project_header(user_id)}{prompt_text}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← К проекту", callback_data=f"select_project:{slug}")]]
            )
        )

    elif data.startswith("staff_hist:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        _, slug, offset_str = data.split(":", 2)
        offset = int(offset_str) if offset_str.isdigit() else 0
        projects = load_projects_registry()
        project_entry = projects.get(slug)
        if not project_entry:
            await _show_project_not_found(update, context)
            return
        folder = project_entry.get("folder", os.path.join("client_projects", slug))
        records = list(reversed(load_dialog_history(folder)))
        total = len(records)
        page = records[offset: offset + HISTORY_PAGE_SIZE]
        header = build_staff_project_header(user_id)
        if not records:
            text = f"{header}История по этому проекту пока пуста."
        else:
            text = (
                f"{header}История проекта (записи {offset + 1}-{offset + len(page)} из {total}):\n\n"
                + format_dialog_pairs(page)
            )
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton(
                "← Новее", callback_data=f"staff_hist:{slug}:{max(0, offset - HISTORY_PAGE_SIZE)}"
            ))
        if offset + HISTORY_PAGE_SIZE < total:
            nav_row.append(InlineKeyboardButton(
                "Старее →", callback_data=f"staff_hist:{slug}:{offset + HISTORY_PAGE_SIZE}"
            ))
        keyboard = []
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("← К проекту", callback_data=f"select_project:{slug}")])
        await clear_ui_screen(update, context)
        await send_ui_screen(update, context, text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "staff_write_to_team":
        if user_id != 5247434464:
            await query.edit_message_text("Недостаточно прав.")
            return
        ACTIVE_PROJECTS[user_id] = {"mode": "staff_chat_compose"}
        save_bot_state()
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "Введите сообщение для отправки в чат команды:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Главное меню", callback_data="back_to_main")]]
            )
        )

    elif data == "staff_write_to_project":
        if user_id != 5247434464:
            await query.edit_message_text("Недостаточно прав.")
            return
        chats = load_chats()
        if not chats:
            await query.edit_message_text("Нет зарегистрированных чатов проектов.")
            return
        keyboard = [
            [InlineKeyboardButton(entry["title"], callback_data=f"staff_write_project_chat:{slug}")]
            for slug, entry in sorted(chats.items(), key=lambda x: x[1]["title"])
        ]
        keyboard.append([InlineKeyboardButton("← Главное меню", callback_data="back_to_main")])
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "Выберите чат проекта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("staff_write_project_chat:"):
        if user_id != 5247434464:
            await query.edit_message_text("Недостаточно прав.")
            return
        slug = data[len("staff_write_project_chat:"):]
        chats = load_chats()
        entry = chats.get(slug)
        if not entry:
            await query.edit_message_text("Чат не найден.")
            return
        ACTIVE_PROJECTS[user_id] = {
            "mode": "staff_project_chat_compose",
            "chat_slug": slug,
            "chat_title": entry["title"],
        }
        save_bot_state()
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            f"Введите сообщение для отправки в чат «{entry['title']}»:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Главное меню", callback_data="back_to_main")]]
            )
        )

    # -----------------------------------------------------------------------
    # ОБНОВЛЕНИЕ ПАМЯТИ
    # -----------------------------------------------------------------------

    elif data == "menu_update_memory":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "Обновить память всех активных проектов?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Запустить обновление", callback_data="confirm_update_memory")],
                [InlineKeyboardButton("Отмена", callback_data="back_to_main")],
            ])
        )

    elif data == "confirm_update_memory":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        await clear_ui_screen(update, context)
        await send_ui_screen(update, context, "Обновление памяти запущено…")
        # Итоговое сообщение с результатом отправляется отдельно и НЕ регистрируется как
        # UI-экран — его нельзя удалять при последующей навигации (см. run_memory_update_and_notify).
        asyncio.create_task(run_memory_update_and_notify(context, update.effective_chat.id))

    # -----------------------------------------------------------------------
    # ИНСТРУКЦИИ
    # -----------------------------------------------------------------------

    elif data == "menu_instructions":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        keyboard = [
            [InlineKeyboardButton("Коммуникация",      callback_data="instruction:communication")],
            [InlineKeyboardButton("Работа в проекте",  callback_data="instruction:project_work")],
            [InlineKeyboardButton("Контент",           callback_data="instruction:content")],
            [InlineKeyboardButton("Чаты Studiosuccess", callback_data="instruction:chats")],
            [InlineKeyboardButton("Отчеты",            callback_data="instruction:reports")],
            [InlineKeyboardButton("Соавторства",       callback_data="instruction:coauthorship")],
            [InlineKeyboardButton("База блогеров",     callback_data="instruction:ad_platforms")],
            [InlineKeyboardButton("Доступы",           callback_data="instruction:access")],
            [InlineKeyboardButton("← Назад",           callback_data="back_to_main")],
        ]
        await clear_ui_screen(update, context)
        await send_ui_screen(update, context, "Выберите инструкцию:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("instruction:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        name = data[len("instruction:"):]
        text = load_instruction(name)
        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("← Назад к инструкциям", callback_data="menu_instructions")],
            [InlineKeyboardButton("Главное меню",           callback_data="back_to_main")],
        ])
        await clear_ui_screen(update, context)
        if not text:
            await send_ui_screen(update, context, "Инструкция временно недоступна.", reply_markup=back_kb)
            return
        text = _prepare_instruction_markdown(text)
        await send_ui_screen(update, context, text, reply_markup=back_kb, parse_mode='Markdown')

    # -----------------------------------------------------------------------
    # РЕЕСТР ЧАТОВ
    # -----------------------------------------------------------------------

    elif data == "menu_chats":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        chats = load_chats()
        await clear_ui_screen(update, context)
        if not chats:
            await send_ui_screen(
                update, context,
                "Рабочие чаты пока не зарегистрированы.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("← Назад", callback_data="back_to_main")]]
                )
            )
            return
        keyboard = [
            [InlineKeyboardButton(info["title"], callback_data=f"chat_info:{slug}")]
            for slug, info in chats.items()
        ]
        keyboard.append([InlineKeyboardButton("← Назад", callback_data="back_to_main")])
        await send_ui_screen(update, context, "Зарегистрированные чаты:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("chat_info:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("chat_info:"):]
        chats = load_chats()
        if slug not in chats:
            await query.edit_message_text("Чат не найден.")
            return
        info = chats[slug]
        thread_label = str(info.get("thread_id")) if info.get("thread_id") else "общий чат"
        keyboard = [
            [InlineKeyboardButton("Отправить тест", callback_data=f"send_test_chat:{slug}")],
            [InlineKeyboardButton("← Назад", callback_data="menu_chats")],
        ]
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            f"Название:\n{info['title']}\n\nChat ID:\n{info['chat_id']}\n\nТип:\n{info['type']}\n\nThread ID:\n{thread_label}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("send_test_chat:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("send_test_chat:"):]
        chats = load_chats()
        entry = chats.get(slug, {})
        thread_label = str(entry.get("thread_id")) if entry.get("thread_id") else "общий чат"
        ok = await send_to_registered_chat(
            context, slug,
            f"Тестовое сообщение от Studiosuccess Bot.\nНазначение: {entry.get('title', slug)}\nThread ID: {thread_label}"
        )
        await clear_ui_screen(update, context)
        if ok:
            await send_ui_screen(update, context, f"Тестовое сообщение отправлено в: {entry.get('title', slug)}")
        else:
            await send_ui_screen(
                update, context,
                "Не удалось отправить сообщение. "
                "Проверьте, что бот добавлен в чат и имеет право отправлять сообщения."
            )

    else:
        # Неизвестный/устаревший callback_data (например, кнопка с чата до
        # обновления бота) — безопасный fallback: ничего не меняем и не вызываем,
        # просто предлагаем открыть меню заново. Т.к. это else после
        # исчерпывающей цепочки elif выше, действующие callback'и сюда не попадают.
        await clear_ui_screen(update, context)
        await send_ui_screen(
            update, context,
            "Эта кнопка устарела. Откройте меню заново.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Главное меню", callback_data="back_to_main")]]
            )
        )

# ---------------------------------------------------------------------------
# Команды: реестр чатов
# ---------------------------------------------------------------------------

async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update):
        return
    chat = update.effective_chat
    title = getattr(chat, "title", None) or "без названия"
    thread_id = update.effective_message.message_thread_id
    await update.message.reply_text(
        f"Chat ID:\n{chat.id}\n\n"
        f"Thread ID:\n{thread_id}\n\n"
        f"Название чата:\n{title}\n\n"
        f"Тип чата:\n{chat.type}"
    )

async def register_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update):
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(
            "Этот чат нельзя зарегистрировать как рабочую группу. "
            "Добавьте бота в нужную группу и выполните /register_chat там."
        )
        return
    if not context.args:
        await update.message.reply_text(
            "Укажите системное имя чата.\n\nПример: /register_chat team"
        )
        return
    slug = context.args[0].lower().strip()
    chat_title = getattr(chat, "title", None) or str(chat.id)
    thread_id = update.effective_message.message_thread_id
    title = f"{chat_title} / topic {thread_id}" if thread_id else chat_title
    chats = load_chats()
    chats[slug] = {
        "title": title,
        "chat_id": str(chat.id),
        "type": chat.type,
        "thread_id": thread_id
    }
    save_chats(chats)
    thread_label = str(thread_id) if thread_id else "общий чат"
    await update.message.reply_text(
        f"Зарегистрировано.\n\nСлаг: {slug}\nНазвание: {title}\nChat ID: {chat.id}\nThread ID: {thread_label}"
    )

async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update):
        return
    chats = load_chats()
    if not chats:
        await update.message.reply_text("Рабочие чаты пока не зарегистрированы.")
        return
    keyboard = [
        [InlineKeyboardButton(data["title"], callback_data=f"chat_info:{slug}")]
        for slug, data in chats.items()
    ]
    await update.message.reply_text(
        "Зарегистрированные чаты:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_test_to_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update):
        return
    if not context.args:
        await update.message.reply_text("Укажите слаг чата.\n\nПример: /send_test_to_chat team")
        return
    slug = context.args[0].lower().strip()
    chats = load_chats()
    if slug not in chats:
        await update.message.reply_text(f"Чат '{slug}' не найден. Проверьте /chats.")
        return
    entry = chats[slug]
    thread_label = str(entry.get("thread_id")) if entry.get("thread_id") else "общий чат"
    ok = await send_to_registered_chat(
        context, slug,
        f"Тестовое сообщение от Studiosuccess Bot.\nНазначение: {entry['title']}\nThread ID: {thread_label}"
    )
    if ok:
        await update.message.reply_text(f"Тестовое сообщение отправлено в: {entry['title']}")
    else:
        await update.message.reply_text(
            "Не удалось отправить сообщение. "
            "Проверьте, что бот добавлен в чат и имеет право отправлять сообщения."
        )

async def project_chat_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update):
        return
    chats = load_chats()
    projects = load_projects_registry()
    lines = []
    for slug, entry in chats.items():
        proj_slug = entry.get("project_slug")
        if not proj_slug:
            continue
        project = projects.get(proj_slug, {})
        thread_id = entry.get("thread_id")
        thread_label = str(thread_id) if thread_id else "общий чат"
        ctx_file = project.get("chat_context_file",
                               os.path.join("client_projects", proj_slug, "chat_context.md"))
        lines.append(
            f"Слаг чата: {slug}\n"
            f"Chat ID: {entry.get('chat_id')}\n"
            f"Thread ID: {thread_label}\n"
            f"Проект: {project.get('title', proj_slug)} ({proj_slug})\n"
            f"brief.md: {project.get('brief_file', '—')}\n"
            f"memory.md: {project.get('memory_file', '—')}\n"
            f"chat_context.md: {ctx_file}\n"
            f"Сохранение контекста: {'да' if entry.get('listen_for_context') else 'нет'}\n"
            f"Ответ только по @упоминанию: {'да' if entry.get('reply_only_on_mention') else 'нет'}"
        )
    if not lines:
        await update.message.reply_text("Проектных чатов не найдено. Проверьте config/chats.json.")
        return
    await update.message.reply_text("\n\n---\n\n".join(lines))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Необработанное исключение при обработке update={update}: {context.error}",
              exc_info=context.error)

def main():
    load_bot_state()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat_id", chat_id_command))
    app.add_handler(CommandHandler("register_chat", register_chat_command))
    app.add_handler(CommandHandler("chats", chats_command))
    app.add_handler(CommandHandler("send_test_to_chat", send_test_to_chat_command))
    app.add_handler(CommandHandler("project_chat_status", project_chat_status_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
