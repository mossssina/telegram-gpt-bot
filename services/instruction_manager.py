"""
Instruction Manager — чтение инструкций из Markdown-файлов.

Не использует GPT, Memory Engine или Prompt Builder.
Файлы читаются непосредственно при каждом запросе.
"""

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INSTRUCTIONS = {
    "communication": os.path.join(_ROOT, "instructions", "01_communication.md"),
    "project_work":  os.path.join(_ROOT, "instructions", "02_project_work.md"),
    "content":       os.path.join(_ROOT, "instructions", "03_content_standards.md"),
    "reports":       os.path.join(_ROOT, "instructions", "04_reports.md"),
    "chats":         os.path.join(_ROOT, "instructions", "05_chats.md"),
    "coauthorship":  os.path.join(_ROOT, "instructions", "06_soavtorstva.md"),
    "ad_platforms":  os.path.join(_ROOT, "instructions", "07_advertising_platforms_knowledge_base.md"),
    "access":        os.path.join(_ROOT, "instructions", "08_dostupy.md"),
    "bot_reports":   os.path.join(_ROOT, "instructions", "09_reports_from_screenshots.md"),
}


def load_instruction(name: str) -> str:
    """
    Читает Markdown-файл инструкции по имени ключа.
    Возвращает текст или пустую строку если файл недоступен.
    """
    path = INSTRUCTIONS.get(name)
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""
