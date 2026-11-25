"""Удалить политические тексты-примеры из prompt-шаблонов langchain_classic (найдены сканером ИБ).

Заменяем пример «president/Michael Jackson» (речь о России/Украине) целиком на нейтральный,
плюс точечная чистка строк с ключевыми словами. Файлы остаются валидными Python и
импортируемыми (константы PROMPT/MAP_PROMPT и т.п. сохраняются).
"""
import pathlib
import re

BASE = pathlib.Path("/app/.venv/lib/python3.13/site-packages/langchain_classic/chains")
EXAMPLE_RE = re.compile(
    r"QUESTION: What did the president say about Michael Jackson\?.*?FINAL ANSWER: The president did not mention Michael Jackson\.\nSOURCES:",
    re.S,
)
NEUTRAL = (
    "QUESTION: What did the report say about the budget?\n"
    "=========\n"
    "Content: Example content about a business contract.\n"
    "Source: 1-pl\n"
    "=========\n"
    "FINAL ANSWER: The report did not mention the budget.\n"
    "SOURCES:"
)
KEYWORD_RE = re.compile(
    r"Russian dictator|Vladimir Putin|Zelenskyy|Ukrainian|roll into Ukraine|Russia's|"
    r"sanctions|free world|totalitarianism|blocking tanks",
    re.I,
)

total = 0
for f in sorted(BASE.rglob("*.py")):
    txt = f.read_text()
    new = EXAMPLE_RE.sub(NEUTRAL, txt)
    if new != txt:
        total += 1
    lines = new.splitlines(keepends=True)
    out = []
    changed = False
    for line in lines:
        # правим только строки внутри шаблонных строк (Content-примеры), не код
        if KEYWORD_RE.search(line) and not re.match(r"\s*(import|from|def|class|[A-Z_]+\s*=)", line):
            out.append("Content: Example content about a business contract.\n")
            changed = True
        else:
            out.append(line)
    if changed:
        new = "".join(out)
        total += 1
    if new != txt:
        f.write_text(new)
        print("sanitized:", f)
print("files changed:", total)
