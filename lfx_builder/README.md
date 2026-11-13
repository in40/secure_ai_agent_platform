# lfx-builder — per-flow slim images by subtraction

Инструмент для **офлайн-хостов**: на remote уже стоит полный образ
`aso9864/langflow-v2:1.0.28`; из него вычитанием собираются минимальные
образы `lfx-<flow>:latest` — только то, что нужно конкретному flow.

Ничего не скачивается: ни wheelhouse, ни базовые слои — только сам инструмент
(3 файла) + полный образ на хосте.

## Состав
- `lfx_profile.py` — профайлер flow (тип ноды + значения полей → что нужно/лишнее)
- `manifest.json` — карта «фича → pip-пакеты + пути моделей»; правится по мере появления новых нод
- `lfx_subtract.py` — исполнитель: create → exec-uninstall → export|import (Config сохраняется через `gen_changes.py`)
- `gen_changes.py` — генератор `--change` аргументов для docker import (он же используется в flatten.sh)

## Использование
```bash
# 1. профиль (по API или по экспортированному JSON)
python3 lfx_profile.py --flow <flow_id> --url http://localhost:7860 \
    --username <u> --password <p> --json prof.json
python3 lfx_profile.py --file flow_export.json --json prof.json

# 2. dry-run: что будет вычтено
python3 lfx_subtract.py --profile prof.json --dry-run

# 3. сборка + смоук-тест (старт образа, /health)
python3 lfx_subtract.py --profile prof.json --smoke
# → lfx-<flow-name>:latest

# 4. запуск образа как сервиса flow
docker run -d --name lfx-agent --memory 4g -p 7900:7860 \
  --env-file secrets.env lfx-simple-agent:latest
```

## Что вычитается (примеры экономии на базе 7.8GB)
| Flow | KEEP | Итоговый размер (ориентир) |
|---|---|---|
| chat-only (LLM+RAG) | база | ~1.5–2GB |
| + PDFPlumber | pdfplumber | +~15MB |
| + Docling Inline easyocr | docling_core, easyocr, LO | ~5–6GB |
| DoclingRemote вместо Inline | база (OCR в docling-serve-cpu) | ~1.5–2GB |

## Безопасность вычитания
- Удаляются только пакеты/пути из манифеста; общие (numpy, pillow, pandas) не трогаются
- Неизвестный тип ноды → предупреждение; с `--strict` — отказ сборки
- `--dry-run` показывает точный список до изменений
- Смоук проверяет, что урезанный образ стартует и `/health` = 200
- Полная пересборка в любой момент: `docker rmi lfx-*` — база не тронута

## Кастомные ноды, встроенные в flow (flow-only custom components)
Если нода не из базы и не из наших baked-компонентов, её код хранится внутри
flow JSON. Профайлер such ноды:
1. **экспортирует** код в `<рядом с --json>/lfx_components/<тип>.py` (или `--export-dir`);
2. **анализирует импорты** (AST) — по `import_map` в манифесте определяет, какие
   тяжёлые пакеты ноде нужны, и **запрещает их вычитать** (`forced keep`);
3. `lfx_subtract.py` **запекает** экспортированные .py в `/app/custom_components`
   итогового LFX-образа — ноды работают в runtime без внешнего каталога.

Неизвестный импорт (нет в `import_map`) → предупреждение: пакет может быть
случайно удалён, проверьте вручную или дополните `import_map`.
Типы встроенных нод langflow добавляйте в `known_base_types`, наших компонентов —
в `base_custom_types`.

## Кастомные компоненты из flow → в образ
`export_flow_components.py` обходит все flow инстанса по API и экспортирует код
flow-only кастомных нод (тех, что встроены в flow JSON) в `custom_components/`
контекста сборки. Запускать **перед** `docker build`:
```bash
python3 lfx_builder/export_flow_components.py \
    --url http://127.0.0.1:7861 --username <u> --password <p> \
    --out-dir custom_components/          # без --out-dir покажет --list-only
docker build --build-arg APP_VERSION=... -t ... .
```
Существующие типы не перезаписываются (`--no-skip-existing` — перезаписывать);
при разном коде одного типа в разных flow побеждает более свежий flow (CONFLICT печатается).
Внимание ИБ: код из flow становится частью образа.

## Добавление новой фичи в манифест
```json
"feature_name": {
  "pip": ["package-name"],
  "paths": ["/app/.../models/X"],
  "match_types": ["componenttypename"],
  "field_rule": {"type": "componenttypename", "field": "field_name", "values": ["value"]}
}
```
Типы нод смотрятся нормализацией (lowercase, без `_ - пробелов, `ext:<bundle>:` и `@official` срезаются).
