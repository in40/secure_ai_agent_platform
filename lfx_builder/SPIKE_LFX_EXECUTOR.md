# Спайк: официальный lfx-executor (PyPI) вместо урезанного langflow-образа

**Статус: зелёный (2026-11-23).** Flow BasicRAG (query-часть) полностью исполняется
официальным `lfx` 1.13.3 (`lfx serve`), E2E-запрос прошёл: embed (bge-m3) → qdrant →
rerank → qwen3.5-122b (Tyk) → ответ.

## Сравнение

| | lfx-basicrag (текущий) | spike-lfx (executor) |
|---|---|---|
| размер образа | 9.29 GB | **3.19 GB** (с docling; query-only без docling ≈ 1.1 GB) |
| рантайм | полный langflow + вычитание pip | только `lfx` + зависимости flow |
| UI | вырезан физически | отсутствует by design |
| API | /api/v1/run + input_request | /flows/{id}/run `{input_value, session_id, global_vars}` |
| старт | ~60 c | ~5–10 c |

## Конвертер

`lf2lfx.py` — компоненты langflow→lfx. Основание: в langflow≥1.6 `langflow.*` —
ре-экспорты `lfx.*` (проверено `is`-сравнениями). Режимы: `component.py` (префикс-замена
импортов), `--check` (импорт с заблокированным langflow + сборка шаблона), `--flow` (код
нод внутри экспорта flow JSON).

## Грабли executor'а (все победены, учтены в lf2lfx.py / prepare_spike_flow.py)

1. **trace_context_var** есть только в langflow-пакете. Валидатор lfx
   (`custom/validate.py`) исполняет из модульного кода только Import/ImportFrom/
   Assign/Def/Class — **Try на уровне модуля выбрасывается**, try/except-заглушка не
   работает. Заглушка: unconditional `import contextvars; _tc_var = ContextVar(...)`.
2. **Мёртвые ноды**: executor билдит ВСЕ ноды графа. Незапитанные ChatOutput и
   ingest-ветка (Docling без файла) падают с None/TypeError → flow для executor'а должен
   быть **query-only**, ingest — отдельный flow.
3. **Поле api_key резолвится как ИМЯ переменной** из env (не литерал). Ловушка
   двойного резолва: значение `EMPTY` (uppercase) само выглядит как имя переменной →
   резолвится повторно → None. Значения env-кредов писать строчными (`empty`).
4. **Провайдеры**: `vLLM`/`OpenAI Compatible` — расширения langflow, в standalone lfx
   не зарегистрированы. Чат маппить на встроенный `OpenAI` (base_url берётся ТОЛЬКО из
   env `OPENAI_BASE_URL`, компонентный api_base для чата игнорируется). Для эмбеддингов
   `OpenAI Compatible` ОСТАВЛЯТЬ: lfx сам ставит `check_embedding_ctx_length=False`
   (иначе langchain шлёт llama-server cl100k-токены → «Prompt contains invalid tokens»).
5. **Гарды ИБ в lfx 1.13.3**: env-креды на нестандартные хосты требуют
   `LANGFLOW_PROVIDER_CREDENTIAL_ALLOWED_HOSTS` (с явной схемой `http://` для cleartext)
   и `LANGFLOW_SSRF_ALLOWED_HOSTS`.
6. `lfx serve` требует `LANGFLOW_API_KEY` (обязательно).
7. Пакет `lfx` (executor) и библиотека `lfx` внутри langflow — разные версии: flow
   экспортирован из langflow 1.13.0, executor 1.13.3 — совместимость по компонентам
   проверять `lfx validate`.

## Рабочая команда запуска (спайк)

```
docker run -d --network host \
  -e LANGFLOW_API_KEY=... -e OPENAI_API_KEY=empty -e OPENAI_COMPATIBLE_API_KEY=empty \
  -e TYK_LLM_KEY=sk-... -e OPENAI_BASE_URL=http://192.168.100.9:8000/chat122b/v1 \
  -e LANGFLOW_PROVIDER_CREDENTIAL_ALLOWED_HOSTS=http://192.168.100.9,http://127.0.0.1 \
  -e LANGFLOW_SSRF_ALLOWED_HOSTS=192.168.100.9,127.0.0.1 \
  spike-lfx:0.1 sh -c 'lfx serve /flows/flow.json --host 127.0.0.1 --port 7898'
```

Сервисы на хосте: qdrant:6333, llama-server embed :8081 (`--embedding --pooling mean`),
rerank :8082 (`--reranking`) — бинарники из `/app/lfx/bin` образа basicrag.

## Что дальше (если переезжаем)

1. Ingest-flow отдельно (upload → docling → qdrant) — тот же executor, образ с docling.
2. База Astra Linux (требование ИБ) вместо python:3.12-slim + flatten до 1 слоя.
3. TykVLLMModel в executor'е не нужен: EPA/Tyk закрывается env `OPENAI_BASE_URL` + ключом
   (как в спайке) — но если нужен reasoning on/off, проверять проброс chat_template_kwargs
   через extra_body стандартной ноды.
4. Контракт WEBUI_CONTRACT переработать под `/flows/{id}/run` (проще: без input_request).

## Раунд 2: Astra-образ + docling + наши компоненты (без qdrant) — ЗЕЛЁНЫЙ

`exec/Dockerfile.lfxexec`: alse-python:1.8.1-3.13.5 + venv(lfx 1.13.3, langchain-openai,
docling, CPU torch) + конвертированные компоненты (tyk_vllm_model, document_page_splitter,
patched_docling_inline) + `_vllm_tyk_lib` в /app. flatten → **astra-exec:0.1 = 4.34 GB**.

Демо-flow docqa (`exec/assemble_docqa.py`): ChatInput("путь---вопрос") → Router(фолбэк:
путь до `---`) → DoclingInline → Chunk → answer_synthesizer → **TykVLLMModel** → ChatOutput.
Нода TykGенерируется из компонента в контейнере (`exec/gen_tyk_node.py`, формат ноды —
копия структуры существующей custom-ноды, tpl['template'] без обёртки 'value').

Грабли раунда 2:
1. flatten (docker export/import) **съедает ENV** — PATH/PYTHONPATH/HF_HOME передавать
   через `docker run -e` (или `docker import --change`).
2. Handle-валидация executor'а строгая: `targetHandle.type` обязан совпадать со схемой
   исходного ребра (`str`, не `other`), иначе «invalid handles».
3. DataFrame-выходы нод приходят как pandas **Table**; `lfx.DataFrame.text` — pandas-аксессор,
   который ломает unwrap → str(df) с обрезкой ячеек. В synthesizer добавлена ветка
   `to_dict("records")` до unwrap (см. assemble_docqa.py).
4. TykVLLMModel без Tyk/EPA: `VLLM_EPA_AUTH=false` + `VLLM_API_BASE` на OpenAI-совместимый
   эндпоинт (в спайке — локальный llama-server-прокси 122b) + `VLLM_API_KEY`, `VLLM_MODEL_CHOICES`.

E2E: `POST /flows/{id}/run {"input_value":"/data/contract.md---Кто ответственный...?"}` →
«Иванов И.И., до 15 декабря 2026» (43 с, холодный docling).
