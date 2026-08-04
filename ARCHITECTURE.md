# ARCHITECTURE.md — Telegram GPT-бот Studiosuccess

> Актуально на август 2026. Более ранняя версия этого документа описывала бота с командами
> `/project`, `/personal`, `/post`, `/stories`, `/reels` и т.д. — тот дизайн заменён текущим (брифы
> + проектные групповые чаты + Memory Engine). Старые версии документов — в `archive/docs_v1/`.

## Рабочая папка проекта

```
~/claude/telegram-gpt-bot/
```

## Стек

- Python 3.9.6 локально (`venv/`), Python 3.12.3 на продакшн-сервере (`.venv/`)
- python-telegram-bot 22.5
- openai 2.43.0
- python-dotenv 1.2.1

## Структура проекта

```
telegram-gpt-bot/
├── bot.py                          # основной файл бота
├── .env                            # токены и Telegram ID (не в git)
├── services/
│   ├── context_manager.py          # Memory Engine — сборка контекста для GPT
│   ├── prompt_builder.py           # сборка итогового system prompt
│   └── instruction_manager.py      # чтение instructions/*.md
├── scripts/
│   ├── daily_memory_update.py      # офлайн-консолидация памяти (cron/systemd timer)
│   └── import_project_memories.py  # разовый импорт готовых файлов памяти
├── config/
│   ├── projects.json               # реестр клиентских проектов (единственный источник)
│   ├── chats.json                  # реестр рабочих Telegram-групп/топиков
│   └── bot_state.json              # персистентные ACTIVE_PROJECTS / BRIEF_STATES
├── client_projects/<slug>/         # данные проекта: brief, memory, chat_context, state
├── instructions/0N_*.md            # внутренние регламенты для сотрудников
├── deployment/                     # deploy.sh + systemd unit-файлы
├── venv/                           # виртуальное окружение (локально)
└── backups/                        # резервные копии bot.py (не в git)
```

---

## Роли

### 1. Заказчик (клиент)
Любой пользователь, чей Telegram ID не входит в `STAFF_USERS`.
Авторизация не требуется — доступ по умолчанию.
Видит только кнопку «Заполнить бриф».

### 2. Сотрудник
Пользователь, чей Telegram ID входит в `STAFF_USERS` из `.env`.
Проверка: `is_staff(update)` → `update.effective_user.id in STAFF_USERS`.

Личные папки/личные чаты сотрудников (`personal_projects/`), описанные в старой версии этого
документа, в текущем `bot.py` не используются — папки остались на диске, но код их не читает.

---

## Команды

### Общедоступные

| Команда | Функция | Описание |
|---|---|---|
| /start | `start()` | Показывает меню в зависимости от роли |

### Только для сотрудников (STAFF_USERS)

| Команда | Функция | Описание |
|---|---|---|
| /chat_id | `chat_id_command()` | Показать chat_id/thread_id текущего чата |
| /register_chat <slug> | `register_chat_command()` | Зарегистрировать групповой чат/топик |
| /chats | `chats_command()` | Список зарегистрированных чатов |
| /send_test_to_chat <slug> | `send_test_to_chat_command()` | Тестовое сообщение в чат |
| /project_chat_status | `project_chat_status_command()` | Диагностика привязки чатов к проектам |

Остальные действия сотрудника (выбор проекта, просмотр брифа, режим вопросов, инструкции) идут
только через inline-кнопки `/start` → `handle_button()`, текстовых команд для них нет.

---

## Клиентский бриф — state machine

`BRIEF_QUESTIONS` — фиксированный список вопросов (текст / одиночный выбор / множественный выбор).
`BRIEF_STATES[user_id]` хранит прогресс заполнения и теперь **сохраняется на диск**
(`config/bot_state.json`, см. ниже) — прогресс переживает перезапуск бота.

По завершении брифа (`advance_brief`):
1. `save_brief_files()` — пишет `brief.json` + `brief.md` в `client_projects/<slug>/`, создаёт
   пустой `memory.md`, если его ещё нет.
2. `register_project_in_registry()` — добавляет проект в `config/projects.json`, если его там нет.
3. Уведомление в зарегистрированный чат `team` (если он есть в `config/chats.json`).

---

## Персистентное состояние бота

`config/bot_state.json` хранит `ACTIVE_PROJECTS` и `BRIEF_STATES` между перезапусками бота:

```json
{ "active_projects": { "<user_id>": {...} }, "brief_states": { "<user_id>": {...} } }
```

`load_bot_state()` вызывается один раз при старте (`main()`), `save_bot_state()` — после каждой
точки мутации этих двух словарей (выбор проекта, каждый отвеченный вопрос брифа, завершение брифа,
переход в режим вопросов по проекту).

`LAST_BOT_MESSAGE` (message_id последнего меню, для `send_clean_message`-подобной логики) остаётся
только в памяти — это чисто косметика, не критично при перезапуске.

---

## Работа сотрудника с клиентским проектом

`staff_select_project` → список активных проектов из `config/projects.json` → `select_project:<slug>`
устанавливает `ACTIVE_PROJECTS[user_id]`. Дальше:

- **«Посмотреть бриф»** (`staff_brief:<slug>`) — показывает `brief.md` проекта, разбивая на части
  по 3800 символов при необходимости.
- **«Задать вопрос»** (`staff_ask:<slug>`) — переводит проект в `mode: "project_chat"`; следующее
  текстовое сообщение сотрудника уходит через Memory Engine (`ContextManager` + `prompt_builder`)
  в GPT (`gpt-4o`) с контекстом проекта.

---

## Проектные групповые чаты

`config/chats.json` может связывать Telegram-группу с проектом через `project_slug`. Если сообщение
пришло из такого чата (`get_project_chat_entry`):
- Оно всегда дописывается в `chat_context.md` проекта (`append_to_chat_context`), кроме сообщений
  от ботов.
- Бот отвечает только если его @упомянули — тогда запрос идёт через тот же Memory Engine путь.

---

## Инструкции для сотрудников

`menu_instructions` → `instruction:<name>` читает `instructions/0N_*.md` через
`services/instruction_manager.py` (без обработки, без GPT) и показывает как есть, с разбивкой на
части при превышении лимита Telegram.

---

## Memory Engine

См. подробное описание в `CLAUDE.md` (раздел «Memory Engine») — там задокументированы оба пути:
живой (`ContextManager.prepare_context`, вызывается на каждый вопрос сотрудника/упоминание в
проектном чате) и офлайн (`scripts/daily_memory_update.py`, по расписанию через systemd timer).

---

## Список клиентских проектов

Актуальный список — в `config/projects.json` (единственный источник; см. `PROJECTS_REGISTRY.md`).
На август 2026: 18 активных проектов, 3 неактивных (в т.ч. `studiodelight` — проект закрыт,
работа с ним не ведётся).
