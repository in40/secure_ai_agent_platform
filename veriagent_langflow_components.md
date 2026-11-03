# VeriAgent × Langflow-v2 — сводка компонентов

Состояние на 2026-11-03. 3 кастомных компонента + стор расписок в postgres.
Файлы компонентов (host, bind-mounted): `/home/sorokin/crawler/langflow-v2/custom_components/`
Локальные копии: `/root/hybrid/lf_components/`

## Принцип
Guard'ы **advisory**: выдают `Verdict` (Data) и опционально вбрасывают баннер в ответ
(`inject_warning`). **Жёсткого block нет намеренно** — решение «блокировать или нет»
остаётся на **вызывающем** (система или человек, HITL). Не добавлять Conditional/block в компоненты.

## Деплой / где крутить
- Langflow-v2 управляется `docker-compose`: `/home/sorokin/crawler/langflow-v2/docker-compose.yml`
  (бинарь — standalone `docker-compose`, не `docker compose`).
- Компоненты подключены bind-mount: `./custom_components -> /app/custom_components`
  (`LANGFLOW_COMPONENTS_PATH=/app/custom_components`).
- Правка компонента = отредактировать файл в `custom_components/` + `docker restart langflow-v2`
  (пересборка образа НЕ нужна).
- env в compose: `LANGFLOW_CUSTOM_COMPONENT_ADMIN_ONLY=true`, `VERIAGENT_JUDGE_URL`,
  `VERIAGENT_JUDGE_MODEL`, `VERIAGENT_JUDGE_KEY`, `VERIAGENT_SIGNING_KEY`.
- Судья (122B) доступен из контейнера по `https://chat.sorokinonline.com/v1/chat/completions`.
- Стор: postgres-сервис `langflow-v2-postgres`, DSN в env `LANGFLOW_DATABASE_URL`.
- Тестовая страница с инъекцией: `https://langflow2.sorokinonline.com/verai-test/`
  (nginx `location /verai-test/` → `/home/sorokin/verai-test/`; конфиг langflow2 забэкаплен).

## 1. VeriAgent Input Guard (`veriagent_input_guard.py`)
Ставится на входе flow (перед Agent/LLM). Детект prompt-injection во входном тексте.
- Входы: `text` (Message, обяз.), `use_regex` (bool, default True), `use_judge` (bool, default False).
- Выходы:
  - `Guarded Text` (Message, синий) — текст насквозь → в `Agent → Input`.
  - `Verdict` (Data, красный) — `text`, `verdict` (CLEAN/SUSPECT), `flags`, `blocked`.
- Regex-флаги: `special_token`, `role_forgery`, `zero_width`, `override_phrase`.
- `use_judge`: 122B-фильтр (CLEAN/INJECTION); verdict=SUSPECT если regex OR judge=INJECTION.
- env: `VERIAGENT_JUDGE_URL/KEY/MODEL` (только для `use_judge`).

## 2. VeriAgent Grounding Guard (`veriagent_grounding_guard.py`)
Ставится ПОСЛЕ Agent/LLM (перед Chat Output). Сверяет ответ с источниками; в receipts-режиме
берёт подписанные расписки этого прогона и проверяет их.
- Входы: `answer` (Message, обяз.), `context` (Message list), `use_judge` (bool True),
  `use_overlap` (bool True), `use_receipts` (bool False), `inject_warning` (bool True).
- Выходы:
  - `Grounded Answer` (Message, синий) — ответ + (опц.) баннеры предупреждений → в Chat Output.
  - `Verdict` (Data, красный) — `verdict`, `overlap`, `judge`, `grounded`, `verified`, `tampered`,
    `handles`, `run_id`, `tool_injection`, `warnings`, `store_error`.
- `verdict`: `grounded` / `weak` / `unsupported` / `contradicted` / `tampered` / `no_context`.
  judge (bound/inferred→grounded; unsupported/contradicted→соотв.) приоритетнее overlap.
- `use_receipts`: читает `self.graph.run_id`, тянет `verai_receipts` этого рана, верифицирует
  Ed25519 (pubkey из строки) → `verified`/`tampered`; `src` = конкатенация `raw`.
- Баннеры (`inject_warning`):
  - unsupported/contradicted → «⚠️ ОПАСНОСТЬ: выявлены галлюцинации — ответ не подтверждён источником.»
  - tampered → «⚠️ ОПАСНОСТЬ: целостность данных инструмента нарушена (подпись не сходится).»
  - tool_injection (флаги в расписках рана) → «⚠️ ОПАСНОСТЬ: атака через инструмент — в выводе инструмента признаки prompt-injection.»
- env: `VERIAGENT_JUDGE_*` (для `use_judge`), `LANGFLOW_DATABASE_URL` (для `use_receipts`).

## 3. VeriAgent Tool Guard (`veriagent_tool_guard.py`)
Обёртка инструмента для Agent. Выход — тип `Tool`, цепляется в `Agent → Tools`.
При каждом вызове агентом: прогоняет вывод вложенного инструмента через scan + Ed25519-sign + store.
- Входы: `inner_tool` (HandleInput, `input_types=["Tool"]`, опц.), `use_scan` (bool True),
  `use_sign` (bool True).
- Выход: `Tool` (method `build_tool`, тип Tool) — обёрнутый инструмент (`<имя>_guarded`,
  сохраняет name/description/args_schema) или фолбэк `veriagent_fetch` (если инструмент не подключён).
- Нюанс: подключённый tool может прийти списком — нормализуется `isinstance(inner,(list,tuple))→[0]`;
  далее проверка `hasattr(inner,"invoke")`.
- На вызове: `raw = inner.invoke(**kw)` (или fetch url) → scan → `handle=sha256(raw)[:12]` →
  Ed25519-sign(`handle+sha256`) → INSERT в `verai_receipts` (ключ `self.graph.run_id`) →
  вернуть `raw[:4000] + [verai:HANDLE] + [verai-flag:...]`.
- env: `VERIAGENT_SIGNING_KEY` (base64 32-байтный seed Ed25519), `LANGFLOW_DATABASE_URL`.

## Стор расписок (postgres)
Таблица `verai_receipts`: `id, run_id, session_id, flow_id, handle, sha256, algo('ed25519'),
sig, pubkey, raw, flags, ts`. Пишет Tool Guard, читает/верифицирует Grounding Guard (receipts).
Pubkey в строке → каждая расписка самопроверяема; постоянный signer-ключ в `VERIAGENT_SIGNING_KEY`.

## Типовая сборка (Simple Agent)
- `Chat Input → Input Guard(text) → Guarded Text → Agent Input`
- `[инструмент] → Tool Guard(inner_tool) → Tool → Agent Tools`
- `Agent Response → Grounding Guard(answer, use_receipts=ON, inject_warning=ON) → Grounded Answer → Chat Output`

## Проверено end-to-end
- Input Guard: clean→CLEAN, inj→SUSPECT (regex + judge).
- Tool Guard: обёртка URL/даты/калькулятора; на вызове — расписка с `run_id`, подпись валидна.
- Grounding Guard (receipts): `verified=1`, баннеры «галлюцинации» / «атака через инструмент»
  (тест: `https://langflow2.sorokinonline.com/verai-test/` → flags `override_phrase,special_token`).
