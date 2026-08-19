"""
Prompt Builder — сборка итогового system prompt для GPT.

Единственная точка формирования промпта из компонентов Memory Engine.

Структура итогового промпта:
  1. SYSTEM_PROMPT (базовые инструкции + роль)
  2. ПАМЯТЬ ПРОЕКТА (выбранные разделы memory.md)
  3. НОВЫЕ ЗНАНИЯ (найдены в новых сообщениях, ещё не в memory.md)
  4. БРИФ ПРОЕКТА (если запрос релевантен брифу)
  5. ИСТОРИЯ ЧАТА (хвост уже-обработанных сообщений — контекст диалога)
  6. НОВЫЕ СООБЩЕНИЯ (с момента последнего ответа GPT)
"""


def build_project_system_prompt(base_prompt: str, project_title: str, ctx: dict) -> str:
    """
    Собирает финальный system prompt из компонентов контекст-менеджера.

    Аргументы:
      base_prompt    — базовый SYSTEM_PROMPT бота
      project_title  — название проекта (вставляется в роль)
      ctx            — словарь из ContextManager.prepare_context()

    Возвращает готовую строку для передачи в {"role": "system", "content": ...}.
    """
    parts = [base_prompt.strip()]

    if project_title:
        parts[0] += (
            f"\n\nТы ассистент проекта «{project_title}». "
            "Используй только сведения, размеченными блоками переданные тебе ниже "
            "в этом системном сообщении, — это единственные разрешённые источники. "
            "Никогда не используй и не упоминай сведения о других клиентских проектах "
            "агентства. Если нужной информации нет среди переданных тебе блоков — прямо "
            "скажи «В памяти проекта нет этой информации», не придумывай и не предполагай."
        )

    if ctx.get("memory_block"):
        parts.append("ПАМЯТЬ ПРОЕКТА:\n" + ctx["memory_block"].strip())

    if ctx.get("knowledge_block"):
        parts.append(ctx["knowledge_block"].strip())

    if ctx.get("brief_block"):
        parts.append("БРИФ ПРОЕКТА:\n" + ctx["brief_block"].strip())

    if ctx.get("recent_context"):
        parts.append("ИСТОРИЯ ЧАТА (последние сообщения):\n" + ctx["recent_context"].strip())

    if ctx.get("new_messages"):
        parts.append("НОВЫЕ СООБЩЕНИЯ (ещё не обработаны):\n" + ctx["new_messages"].strip())

    return "\n\n".join(p for p in parts if p.strip())
