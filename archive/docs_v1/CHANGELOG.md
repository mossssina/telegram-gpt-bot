> Historical — описывает бота до рефакторинга августа 2026 (личные чаты сотрудников,
> команды /project /personal /post /stories /reels /audit /report). Актуальная архитектура — в
> CLAUDE.md и корневом ARCHITECTURE.md.

# CHANGELOG.md — Telegram GPT-бот Studiosuccess

## [0.3.0] — 2026-06-24

### Добавлено
- `LAST_BOT_MESSAGE = {}` — хранит message_id последнего меню каждого пользователя
- `send_clean_message(update, text, reply_markup)` — удаляет старое меню, отправляет новое
- `handle_button(update, context)` — маршрутизация 14 callback-кнопок
- Inline-кнопки для сотрудника в /start: Клиентские проекты, Текущий режим, Пост, Сторис, Reels, Аудит, Отчет, Личная папка (если есть доступ)
- Inline-кнопки для заказчика в /start: Заполнить бриф, Получить разбор брифа, Поставить задачу, Статус задач, Статус оплаты подписки
- Импорты: `InlineKeyboardButton`, `InlineKeyboardMarkup`, `CallbackQueryHandler`
- Elizaveta Barankovskaya добавлена в STAFF_USERS

### Изменено
- `start()` — теперь показывает inline-кнопки вместо текстового списка команд
- `projects()`, `current()`, `post()`, `stories()`, `reels()`, `audit()`, `report()`,
  `client_brief()`, `client_brief_review()`, `client_task()`, `client_task_status()`,
  `client_subscription_status()`, `help_command()` — используют `send_clean_message`
  вместо `reply_text`
- `projects()` — добавлена подсказка «Для выбора напишите: /project название»
- Кнопки при нажатии редактируют сообщение (`query.edit_message_text`), не создают новое

### Не изменено
- `handle_message()` — GPT-ответы не удаляются
- `select_project()`, `select_personal()` — логика без изменений
- `staff_only()`, `require_client_project()` — логика без изменений
- Все текстовые команды работают параллельно с кнопками

---

## [0.2.0] — 2026-06-24

### Добавлено
- Три уровня доступа: заказчик, сотрудник, личная папка
- Гард `require_client_project()` — блокирует рабочие команды без выбранного проекта
- Гард `staff_only()` — блокирует сотрудниковые команды для заказчиков
- Динамический системный промпт в `handle_message()` с учётом активного проекта
- Elizaveta Barankovskaya добавлена в PERSONAL_ACCESS

### Исправлено
- Опечатка `PPERSONAL_ANASTASIIAMOSINA` → `PERSONAL_ANASTASIIAMOSINA`
- `select_personal()` читал только первое слово имени (`args[0]`) — исправлено на полное имя
- Кривой отступ внутри `projects()`

---

## [0.1.0] — до 2026-06-24

- Базовая структура бота без разделения ролей
- Единый SYSTEM_PROMPT для всех пользователей
- Команды без проверок доступа
