"""
Memory Engine v1.0 — Context Manager for Studiosuccess Bot

Единственная точка подготовки контекста для GPT.
Используется при любом обращении к GPT в рамках клиентского проекта.
"""

import os
import json
import re
import logging
from datetime import datetime

from services.file_lock import project_lock

log = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Разделы памяти и ключевые слова для матчинга
# ---------------------------------------------------------------------------

SECTION_KEYWORDS = {
    "company":         ["компани", "агентств", "студи", "назван", "основа", "команд", "сотрудник"],
    "positioning":     ["позицион", "уникальн", "ценност", "миссия", "бренд", "отличи"],
    "services":        ["услуг", "сервис", "цен", "стоимост", "пакет", "тариф"],
    "target_audience": ["аудитор", "клиент", "целев", "портрет", "покупател", "заказчик"],
    "tone_of_voice":   ["тон", "стиль", "голос", "общен", "язык", "обращен", "речь"],
    "content_rules":   ["контент", "пост", "публикац", "правил", "требован", "формат", "текст"],
    "projects":        ["проект", "кейс", "работ", "портфол", "объект"],
    "history":         ["истори", "было", "раньш", "прошл", "ранее", "договор"],
    "faq":             ["вопрос", "ответ", "часто", "почему", "зачем"],
    "preferences":     ["предпочт", "нравит", "хочет", "люб", "нужен"],
    "forbidden":       ["нельзя", "запрет", "не использ", "избегат", "запрещен", "недопустим"],
    "gpt_rules":       ["правил ответ", "инструкц", "как отвечать", "формат ответ"],
}

# Всегда включать эти разделы, если они существуют и не пусты
ALWAYS_INCLUDE_SECTIONS = ["forbidden", "gpt_rules", "tone_of_voice"]

# Ключевые слова для определения необходимости брифа
BRIEF_KEYWORDS = [
    "услуга", "цена", "стоимост", "пакет", "тариф", "бриф",
    "требован", "задача", "цель", "ожидан", "продукт", "бренд",
    "компани", "сайт", "ссылка", "индустри", "конкурент", "клиент",
    "что делает", "о компании", "расскажи о",
]

# Паттерны для обнаружения новых знаний (локальный анализ без GPT)
KNOWLEDGE_RULES = [
    {
        "section": "forbidden",
        "indicators": [
            "нельзя", "запрещено", "запрет", "не использовать",
            "избегай", "не делать", "не надо", "недопустимо",
            "не употреблять", "не писать", "не упоминать",
        ],
    },
    {
        "section": "preferences",
        "indicators": [
            "мне нравится", "хочу чтобы", "предпочитаю", "лучше делать",
            "хотел бы", "хотела бы", "мне нужно", "хочется", "нам нравится",
        ],
    },
    {
        "section": "content_rules",
        "indicators": [
            "всегда добавлять", "обязательно", "должно быть", "важно чтобы",
            "нужно всегда", "каждый пост", "в каждом посте", "обязательно включать",
        ],
    },
    {
        "section": "services",
        "indicators": [
            "новая услуга", "добавили услугу", "новый пакет", "изменили цену",
            "теперь стоит", "новое направление", "начали предоставлять",
        ],
    },
    {
        "section": "history",
        "indicators": [
            "решили", "договорились", "утвердили", "определились",
            "согласовали", "принято решение", "решение принято",
        ],
    },
    {
        "section": "tone_of_voice",
        "indicators": [
            "тон должен", "писать в стиле", "общаться как", "стиль текстов",
            "голос бренда", "язык коммуникации",
        ],
    },
]

# Сколько символов «свежего» чата брать в качестве контекста диалога
RECENT_CONTEXT_CHARS = 1500

# Максимум символов новых сообщений для анализа
NEW_MESSAGES_MAX_CHARS = 2500


class ContextManager:
    """
    Memory Engine v1.0.
    Подготавливает GPT-контекст для клиентского проекта.
    """

    def __init__(self, project_slug: str, project_entry: dict):
        self.slug = project_slug
        self.entry = project_entry

        folder = project_entry.get("folder", os.path.join("client_projects", project_slug))
        self.folder = folder
        self.memory_file      = project_entry.get("memory_file",      os.path.join(folder, "memory.md"))
        self.brief_file       = project_entry.get("brief_file",       os.path.join(folder, "brief.md"))
        self.chat_context_file = project_entry.get("chat_context_file", os.path.join(folder, "chat_context.md"))
        self.state_file   = os.path.join(folder, "memory_state.json")
        self.pending_file = os.path.join(folder, "pending_memory.md")
        self.index_file   = os.path.join(folder, "memory_index.json")

    # -----------------------------------------------------------------------
    # Состояние
    # -----------------------------------------------------------------------

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            return {
                "last_processed_offset": 0,
                "last_memory_update": None,
                "last_update_datetime": None,
            }
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "last_processed_offset": 0,
                "last_memory_update": None,
                "last_update_datetime": None,
            }

    def _save_state(self, state: dict):
        os.makedirs(self.folder, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # -----------------------------------------------------------------------
    # Разбор memory.md на разделы
    # -----------------------------------------------------------------------

    def _parse_memory_sections_raw(self) -> list:
        """
        Возвращает список:
          [{"name": str, "content": str, "start_offset": int, "end_offset": int}, ...]

        start_offset / end_offset — позиции контента раздела в файле (символы).
        Используется для построения индекса с точными смещениями.
        """
        if not os.path.exists(self.memory_file):
            return []
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return []

        if not content.strip():
            return []

        # Поддерживаемые форматы: [section], # [section], ## [section]
        SECTION_RE = re.compile(r'^#{0,3}\s*\[(\w+)\]', re.MULTILINE)
        if SECTION_RE.search(content):
            result = []
            matches = list(SECTION_RE.finditer(content))
            for i, match in enumerate(matches):
                name = match.group(1).lower()
                start = match.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                text = content[start:end].strip()
                if text:
                    result.append({
                        "name": name,
                        "content": text,
                        "start_offset": start,
                        "end_offset": end,
                    })
            return result
        else:
            return [{"name": "general", "content": content.strip(),
                     "start_offset": 0, "end_offset": len(content)}]

    def _parse_memory_sections(self) -> dict:
        """Возвращает {section_name: content}."""
        return {s["name"]: s["content"] for s in self._parse_memory_sections_raw()}

    # -----------------------------------------------------------------------
    # Индекс памяти
    # -----------------------------------------------------------------------

    def _build_memory_index(self) -> dict:
        """Строит memory_index.json из memory.md."""
        with project_lock(self.folder):
            raw = self._parse_memory_sections_raw()
            index = {
                "sections": {},
                "last_built": datetime.now().isoformat(timespec="seconds"),
            }

            priority_map = {
                "forbidden": 10, "gpt_rules": 9, "content_rules": 8,
                "tone_of_voice": 7, "preferences": 6, "services": 5,
                "target_audience": 5, "positioning": 4, "company": 3,
                "projects": 3, "faq": 2, "history": 1, "general": 4,
            }

            stop_words = {
                "это", "тоже", "быть", "иметь", "делать", "также", "можно",
                "нужно", "если", "когда", "после", "перед", "свой", "наши",
            }

            for s in raw:
                words = re.findall(r'[а-яё]{4,}', s["content"].lower())
                keywords = list({w for w in words if w not in stop_words})[:25]
                index["sections"][s["name"]] = {
                    "keywords":     keywords,
                    "priority":     priority_map.get(s["name"], 3),
                    "has_content":  bool(s["content"]),
                    "char_count":   len(s["content"]),
                    "start_offset": s["start_offset"],
                    "end_offset":   s["end_offset"],
                }

            os.makedirs(self.folder, exist_ok=True)
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

            return index

    # -----------------------------------------------------------------------
    # Новые сообщения
    # -----------------------------------------------------------------------

    def _get_new_messages(self, state: dict) -> tuple:
        """
        Возвращает (new_text, new_offset, recent_text).

        new_text    — сообщения после last_processed_offset
        new_offset  — текущий конец файла (для последующего обновления state)
        recent_text — хвост уже-обработанного текста (контекст диалога)
        """
        if not os.path.exists(self.chat_context_file):
            return "", 0, ""

        try:
            with open(self.chat_context_file, "r", encoding="utf-8") as f:
                full_content = f.read()
        except Exception:
            return "", 0, ""

        new_offset = len(full_content)
        offset = state.get("last_processed_offset", 0)

        # Нет новых сообщений
        if offset >= new_offset:
            recent = full_content[-RECENT_CONTEXT_CHARS:] if len(full_content) > RECENT_CONTEXT_CHARS else full_content
            return "", new_offset, recent

        new_text = full_content[offset:]
        if len(new_text) > NEW_MESSAGES_MAX_CHARS:
            new_text = new_text[-NEW_MESSAGES_MAX_CHARS:]

        # recent = хвост уже обработанного текста
        processed = full_content[:offset]
        recent = processed[-RECENT_CONTEXT_CHARS:] if len(processed) > RECENT_CONTEXT_CHARS else processed

        return new_text, new_offset, recent

    # -----------------------------------------------------------------------
    # Извлечение новых знаний (локально, без GPT)
    # -----------------------------------------------------------------------

    def _extract_knowledge(self, text: str) -> list:
        """
        Находит предложения-кандидаты на новые знания.
        Использует только паттерны — GPT не вызывается.
        """
        if not text.strip():
            return []

        sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
        found = []
        seen = set()

        for sentence in sentences:
            s = sentence.strip()
            if len(s) < 12:
                continue
            s_lower = s.lower()

            for rule in KNOWLEDGE_RULES:
                for indicator in rule["indicators"]:
                    if indicator in s_lower:
                        key = s_lower[:60]
                        if key not in seen:
                            seen.add(key)
                            found.append({
                                "section":   rule["section"],
                                "text":      s,
                                "indicator": indicator,
                                "datetime":  datetime.now().isoformat(timespec="seconds"),
                            })
                        break

        return found

    def _is_duplicate(self, knowledge_text: str) -> bool:
        """Проверяет, не дублирует ли кандидат существующую память."""
        key = knowledge_text.lower()[:60]
        for filepath in [self.memory_file, self.pending_file]:
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    if key in f.read().lower():
                        return True
            except Exception:
                pass
        return False

    def _add_to_pending(self, items: list, author: str = ""):
        """Записывает кандидатов в pending_memory.md."""
        if not items:
            return

        with project_lock(self.folder):
            os.makedirs(self.folder, exist_ok=True)
            if not os.path.exists(self.pending_file):
                with open(self.pending_file, "w", encoding="utf-8") as f:
                    f.write("# Кандидаты в память\n\n")

            today = datetime.now().strftime("%Y-%m-%d")
            now   = datetime.now().isoformat(timespec="seconds")

            with open(self.pending_file, "a", encoding="utf-8") as f:
                for item in items:
                    f.write(
                        f"\n## {now}\n"
                        f"Тип: {item['section']}\n"
                        f"Текст: {item['text']}\n"
                        f"Раздел: {item['section']}\n"
                        f"Источник: Telegram\n"
                        f"Дата: {today}\n"
                        + (f"Автор: {author}\n" if author else "")
                        + "\n"
                    )

    # -----------------------------------------------------------------------
    # Выбор разделов памяти (локально)
    # -----------------------------------------------------------------------

    def _select_sections(self, query: str) -> list:
        """
        Выбирает релевантные разделы памяти.
        Только локальная логика — GPT не используется.
        """
        query_lower = query.lower()
        sections = self._parse_memory_sections()
        scores = {}

        # Если нет секционирования — всегда 'general'
        if "general" in sections:
            return ["general"]

        # Обязательные разделы (если не пусты)
        for section in ALWAYS_INCLUDE_SECTIONS:
            if section in sections and sections[section]:
                scores[section] = 100

        # Скоринг по ключевым словам
        for section, keywords in SECTION_KEYWORDS.items():
            if section in scores:
                continue
            if section not in sections or not sections[section]:
                continue
            score = sum(1 for kw in keywords if kw in query_lower)
            if score > 0:
                scores[section] = score

        return [s for s, _ in sorted(scores.items(), key=lambda x: -x[1])]

    def _needs_brief(self, query: str) -> bool:
        """Определяет, нужен ли бриф для этого запроса."""
        q = query.lower()
        return any(kw in q for kw in BRIEF_KEYWORDS)

    # -----------------------------------------------------------------------
    # Главный метод
    # -----------------------------------------------------------------------

    def prepare_context(self, query: str, sender_name: str = "") -> dict:
        """
        Подготавливает полный контекст для GPT-вызова.

        Возвращает dict:
          memory_block    — выбранные разделы memory.md
          knowledge_block — новые знания (из chat_context, ещё не обработанные)
          brief_block     — бриф (если релевантен)
          recent_context  — хвост уже-обработанного чата (контекст диалога)
          new_messages    — текст новых сообщений
          new_offset      — позиция конца файла после обработки
          sections_used   — список использованных разделов
          new_knowledge   — список извлечённых знаний
        """
        state = self._load_state()

        # Шаг 1-4: Получить новые сообщения
        new_messages, new_offset, recent_context = self._get_new_messages(state)

        # Шаг 5: Извлечь новые знания (локально)
        new_knowledge = []
        if new_messages:
            candidates = self._extract_knowledge(new_messages)
            for item in candidates:
                if not self._is_duplicate(item["text"]):
                    new_knowledge.append(item)
            if new_knowledge:
                self._add_to_pending(new_knowledge, author=sender_name)

        # Обновить индекс памяти
        try:
            self._build_memory_index()
        except Exception as e:
            log.error(f"[MEMORY INDEX ERROR] slug={self.slug}: {e}")

        # Выбрать разделы памяти
        selected_sections = self._select_sections(query)
        sections = self._parse_memory_sections()

        memory_block = ""
        for section in selected_sections:
            if section in sections and sections[section]:
                memory_block += f"[{section.upper()}]\n{sections[section]}\n\n"

        # Блок новых знаний
        knowledge_block = ""
        if new_knowledge:
            lines = [f"- [{item['section'].upper()}] {item['text']}" for item in new_knowledge]
            knowledge_block = "НОВЫЕ ЗНАНИЯ (учитывать уже в этом ответе):\n" + "\n".join(lines)

        # Бриф (только если релевантен)
        brief_block = ""
        if self._needs_brief(query) and os.path.exists(self.brief_file):
            try:
                with open(self.brief_file, "r", encoding="utf-8") as f:
                    brief_block = f.read()
            except Exception:
                pass

        # Лог
        log.info(
            f"[MEMORY ENGINE] project={self.slug} | "
            f"sections={selected_sections} | "
            f"new_msgs={len(new_messages)} chars | "
            f"new_knowledge={len(new_knowledge)} | "
            f"brief={'yes' if brief_block else 'no'}"
        )

        return {
            "memory_block":   memory_block,
            "knowledge_block": knowledge_block,
            "brief_block":    brief_block,
            "recent_context": recent_context,
            "new_messages":   new_messages,
            "new_offset":     new_offset,
            "sections_used":  selected_sections,
            "new_knowledge":  new_knowledge,
        }

    def update_state(self, new_offset: int):
        """
        Вызывается после успешного ответа GPT.
        Сдвигает last_processed_offset на новый конец файла.
        """
        state = self._load_state()
        state["last_processed_offset"]  = new_offset
        state["last_update_datetime"]   = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

