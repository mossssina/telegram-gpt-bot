import os
import re
import sys
import json
import time
import logging
from datetime import date
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from services.context_manager import ContextManager
from services.prompt_builder import build_project_system_prompt
from services.instruction_manager import load_instruction

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


ACTIVE_PROJECTS = {}
LAST_BOT_MESSAGE = {}
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
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"brief_opt:{opt}")] for opt in q["options"]]
        await send_fn(text, InlineKeyboardMarkup(keyboard))
    elif q["type"] == "multi":
        state["current_multi_selection"] = []
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"brief_tog:{opt}")] for opt in q["options"]]
        keyboard.append([InlineKeyboardButton("Готово →", callback_data="brief_done")])
        await send_fn(text, InlineKeyboardMarkup(keyboard))

# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------

async def send_clean_message(update, text, reply_markup=None):
    user_id = update.effective_user.id
    try:
        old_message_id = LAST_BOT_MESSAGE.get(user_id)
        if old_message_id:
            await update.effective_chat.delete_message(old_message_id)
    except Exception:
        pass
    sent_message = await update.message.reply_text(text, reply_markup=reply_markup)
    LAST_BOT_MESSAGE[user_id] = sent_message.message_id

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
        ]
    else:
        text = (
            "Здравствуйте. Это бот Studiosuccess.\n\n"
            "Вы можете заполнить бриф, чтобы мы взяли задачу в работу."
        )
        keyboard = [[InlineKeyboardButton("Заполнить бриф", callback_data="client_fill_brief")]]
    return text, InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in BRIEF_STATES:
        del BRIEF_STATES[user_id]
        save_bot_state()
    text, reply_markup = build_start_menu(user_id)
    await send_clean_message(update, text, reply_markup=reply_markup)


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
    proj_slug, chat_entry = get_project_chat_entry(chat_id, thread_id)
    if proj_slug and chat_entry:
        bot_username = context.bot.username  # например StudiosuccBot
        mention = f"@{bot_username}"
        is_mention = mention.lower() in user_text.lower()

        # Сохраняем контекст только если это не бот и не команда
        sender = update.effective_user
        sender_name = sender.username or sender.first_name or str(sender.id)
        if not sender.is_bot:
            append_to_chat_context(proj_slug, sender_name, user_text)

        # Если не упоминание — молчим
        if not is_mention:
            return

        # Упоминание — отвечаем через Memory Engine
        projects = load_projects_registry()
        project_entry = projects.get(proj_slug, {})
        clean_text = user_text.replace(mention, "").replace(mention.lower(), "").strip()
        query = clean_text or "Что обсуждалось?"

        cm = ContextManager(proj_slug, project_entry)
        ctx = cm.prepare_context(query, sender_name=sender_name)
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
            cm.update_state(ctx["new_offset"])
            append_to_chat_context(proj_slug, "Ассистент", reply)
            await update.message.reply_text(reply)
        except Exception as e:
            log.error(f"[GPT PROJECT CHAT ERROR] {e}")
            await update.message.reply_text(f"Ошибка при обращении к GPT: {e}")
        return

    # --- Бриф в процессе заполнения ---
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

    # --- Обычный режим ---
    # В группах/супергруппах (рабочие чаты) бот молчит, если его явно не позвали
    # через @упоминание — иначе он бы отвечал в любом рабочем чате всякий раз,
    # когда у сотрудника, написавшего туда, активен режим "Задать вопрос".
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        mention = f"@{bot_username}"
        if mention.lower() not in user_text.lower():
            return
        user_text = user_text.replace(mention, "").replace(mention.lower(), "").strip() or user_text

    project = get_active_project(user_id)

    if is_staff(update):
        if project and project.get("mode") == "project_chat":
            # Режим вопросов по проекту — используем Memory Engine
            proj_slug = project["slug"]
            staff_name = update.effective_user.username or update.effective_user.first_name or ""

            cm = ContextManager(proj_slug, project["registry_entry"])
            ctx = cm.prepare_context(user_text, sender_name=staff_name)
            staff_note = f"Сотрудник задаёт вопрос по клиентскому проекту «{project['title']}»."
            system_with_context = build_project_system_prompt(
                SYSTEM_PROMPT + "\n\n" + staff_note, project["title"], ctx
            )
            log.info(
                f"[ACTIVE PROJECT] user_id={user_id} slug={proj_slug} "
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
                        {"role": "user", "content": user_text}
                    ],
                    max_tokens=GPT_MAX_TOKENS,
                )
                reply = response.choices[0].message.content
                cm.update_state(ctx["new_offset"])
                append_staff_dialog(proj_slug, project["folder"], staff_name, user_text, reply)
                await update.message.reply_text(reply)
            except Exception as e:
                log.error(f"[GPT ERROR] {e}")
                await update.message.reply_text(f"Ошибка при обращении к GPT: {e}")
            return

        else:
            await update.message.reply_text("Нажмите /start чтобы выбрать проект.")
            return

    else:
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
            await update.message.reply_text(response.choices[0].message.content)
        except Exception as e:
            log.error(f"[GPT ERROR] {e}")
            await update.message.reply_text(f"Ошибка при обращении к GPT: {e}")

# ---------------------------------------------------------------------------
# Обработка кнопок
# ---------------------------------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Главное меню ---

    if data == "back_to_main":
        if user_id in BRIEF_STATES:
            del BRIEF_STATES[user_id]
            save_bot_state()
        text, reply_markup = build_start_menu(user_id)
        await query.edit_message_text(text, reply_markup=reply_markup)

    # -----------------------------------------------------------------------
    # ЗАКАЗЧИК
    # -----------------------------------------------------------------------

    elif data == "client_fill_brief":
        BRIEF_STATES[user_id] = {
            "question_index": 0,
            "project_name": None,
            "project_slug": None,
            "answers": [],
            "current_multi_selection": []
        }
        save_bot_state()
        await query.edit_message_text(
            "Начинаем заполнять бриф.\n\nОтвечайте на вопросы — это займёт несколько минут."
        )
        async def send_fn(text, markup=None):
            await query.message.reply_text(text, reply_markup=markup)
        await advance_brief(user_id, send_fn, context)

    elif data.startswith("brief_opt:"):
        if user_id not in BRIEF_STATES:
            await query.edit_message_text("Сессия истекла. Нажмите /start чтобы начать снова.")
            return
        state = BRIEF_STATES[user_id]
        answer = data[len("brief_opt:"):]
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
            await query.edit_message_text(
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
        await query.edit_message_text(
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
            await query.edit_message_text(
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
            "mode": None
        }
        save_bot_state()
        keyboard = [
            [InlineKeyboardButton("Посмотреть бриф", callback_data=f"staff_brief:{slug}")],
            [InlineKeyboardButton("Задать вопрос", callback_data=f"staff_ask:{slug}")],
            [InlineKeyboardButton("← К проектам", callback_data="staff_select_project")],
        ]
        await query.edit_message_text(
            f"{build_staff_project_header(user_id)}Что хотите сделать?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("staff_brief:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("staff_brief:"):]
        back_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К проекту", callback_data=f"select_project:{slug}")]]
        )
        project = get_active_project(user_id)
        brief_text = ""
        brief_file = project["registry_entry"].get("brief_file", "") if project else ""
        if brief_file and os.path.exists(brief_file):
            try:
                with open(brief_file, "r", encoding="utf-8") as f:
                    brief_text = f.read()
            except Exception:
                pass
        if not brief_text:
            brief_text = load_project_brief(slug)
        header = build_staff_project_header(user_id)
        if not brief_text:
            await query.edit_message_text(f"{header}Бриф по этому проекту пока не найден.", reply_markup=back_kb)
            return
        MAX = 3800
        if len(brief_text) <= MAX - len(header):
            await query.edit_message_text(header + brief_text, reply_markup=back_kb)
        else:
            chunks = [brief_text[i:i+MAX] for i in range(0, len(brief_text), MAX)]
            await query.edit_message_text(f"{header}Бриф (часть 1 из {len(chunks)}):")
            for i, chunk in enumerate(chunks):
                kb = back_kb if i == len(chunks) - 1 else None
                await query.message.reply_text(chunk, reply_markup=kb)

    elif data.startswith("staff_ask:"):
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        slug = data[len("staff_ask:"):]
        project = ACTIVE_PROJECTS.get(user_id)
        if project and project.get("slug") == slug:
            ACTIVE_PROJECTS[user_id]["mode"] = "project_chat"
        else:
            projects = load_projects_registry()
            project_entry = projects.get(slug, {})
            ACTIVE_PROJECTS[user_id] = {
                "type": "client",
                "name": project_entry.get("title", slug),
                "slug": slug,
                "folder": project_entry.get("folder", ""),
                "memory_file": project_entry.get("memory_file", ""),
                "brief_file": project_entry.get("brief_file", ""),
                "mode": "project_chat"
            }
        save_bot_state()
        await query.edit_message_text(
            f"{build_staff_project_header(user_id)}Режим вопросов активен. Задайте вопрос по проекту.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← К проекту", callback_data=f"select_project:{slug}")]]
            )
        )

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
            [InlineKeyboardButton("Отчеты",            callback_data="instruction:reports")],
            [InlineKeyboardButton("← Назад",           callback_data="back_to_main")],
        ]
        await query.edit_message_text(
            "Выберите инструкцию:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

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
        if not text:
            await query.edit_message_text("Инструкция временно недоступна.", reply_markup=back_kb)
            return
        MAX = 3800
        if len(text) <= MAX:
            await query.edit_message_text(text, reply_markup=back_kb)
        else:
            chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)]
            await query.edit_message_text(chunks[0])
            for i, chunk in enumerate(chunks[1:], 1):
                kb = back_kb if i == len(chunks) - 1 else None
                await query.message.reply_text(chunk, reply_markup=kb)

    # -----------------------------------------------------------------------
    # РЕЕСТР ЧАТОВ
    # -----------------------------------------------------------------------

    elif data == "menu_chats":
        if user_id not in STAFF_USERS:
            await query.edit_message_text("Эта функция доступна только сотрудникам.")
            return
        chats = load_chats()
        if not chats:
            await query.edit_message_text(
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
        await query.edit_message_text(
            "Зарегистрированные чаты:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

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
        await query.edit_message_text(
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
        if ok:
            await query.edit_message_text(f"Тестовое сообщение отправлено в: {entry.get('title', slug)}")
        else:
            await query.edit_message_text(
                "Не удалось отправить сообщение. "
                "Проверьте, что бот добавлен в чат и имеет право отправлять сообщения."
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
