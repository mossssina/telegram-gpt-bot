> Historical — описывает бота до рефакторинга августа 2026 (личные чаты сотрудников,
> команды /project /personal /post /stories /reels /audit /report). Актуальная архитектура — в
> CLAUDE.md и корневом ARCHITECTURE.md.

# NEXT_SESSION.md — Telegram GPT-бот Studiosuccess

## Рабочая папка

```
~/claude/telegram-gpt-bot/
```

---

## Где остановились

Сессия 2026-06-24.
Реализованы личные чаты внутри personal-папки сотрудника.

---

## Что работает

- Запуск:
  ```
  cd ~/claude/telegram-gpt-bot
  source venv/bin/activate
  python3 bot.py
  ```
- Кнопка «Личная папка» → сразу открывает личный чат
- `/personal имя_фамилия` → работает с пробелом и подчёркиванием
- В личном режиме GPT не использует клиентские материалы
- `/current` показывает personal mode корректно
- Рабочие команды в personal mode → умное сообщение
- Папки `personal_projects/` созданы для 7 сотрудников
- Синтаксис проверен: `python3 -m py_compile bot.py` — чисто

---

## Что требует живой проверки

1. **Кнопка «Личная папка»** — открывает чат, можно писать текстом, GPT отвечает
2. **`/personal anastasiia_mosina`** — открывает правильно, отказывает чужому
3. **В personal mode нажать «Пост»** — должно сказать «Сейчас открыт личный режим»
4. **`/current` в personal mode** — показывает имя папки

---

## Следующий логичный этап

**Приоритет 1 — запись истории:**
- Реализовать сохранение диалога в `personal_projects/<slug>/history.md`
- Формат: `## YYYY-MM-DD\nUser: ...\nAssistant: ...`

**Приоритет 2 — передача истории в GPT:**
- Читать последние N сообщений из `history.md`
- Передавать как `messages` в `chat.completions.create` для контекста диалога

**Приоритет 3 — интеграция Claude API:**
- Добавить ANTHROPIC_API_KEY в .env
- Создать services/claude_client.py
- Добавить /ask_claude, /ask_gpt, /think_together для сотрудников

**Приоритет 4 — подключить /brief, /task к GPT:**
- Механизм ожидания ответа после команды
