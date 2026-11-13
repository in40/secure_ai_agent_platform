# LFX runtime services — что подключать по составу flow

Правило: LFX-образ автономен. Если в flow обнаружен узел, требующий внешний
сервис, соответствующий сервис поднимается **внутри** контейнера (entrypoint),
а `lfx_init` прописывает URL нод на 127.0.0.1. Ничего заранее наполнять не
нужно, если flow не переиспользует записи между запусками.

| Обнаружено в flow (тип ноды) | Сервис | Как | Порт | Ключевые правила |
|---|---|---|---|---|
| `QdrantVectorStoreComponent`, `ext:qdrant:*` | Qdrant | статический musl-бинарник (GitHub release, ~36MB tar), `QDRANT__STORAGE__ON_DISK=false` | 6333 | in-memory, если flow не переиспользует записи (паттерн «файл→инgest→поиск→забыли»); персист — только если коллекции живут между запусками; проверять сессионную изоляцию коллекции |
| `EmbeddingModel` с OpenAI-совместимым `api_base` (llama.cpp GGUF) | эмбеддер | `llama-server -m <model> --embedding --pooling mean -ngl 0` | 8081 | **модель обязана совпадать с той, что индексировала коллекции** (stateful!). Для наших flow: `user-bge-m3-q8_0.gguf` (Content-AI/USER-bge-m3-Q8_0-GGUF, файн-тюн; Q4 у автора нет и быть не должно — съедет векторное пространство). Источники GGUF: gpustack/bge-m3-GGUF, gpustack/bge-reranker-v2-m3-GGUF |
| `RerankComponent` (`rerank_url` /v1/rerank) | реранкер | `llama-server -m <model> --reranking -ngl 0` | 8082 | stateless — квант свободный: bge-reranker-v2-m3 **Q4_K_M** (~390MB) вместо Q8 без переиндексации |
| `LanguageModelComponent` / кастомные LLM-ноды | LLM | **не** запекать: из env `LFX_LLM_BASE_URL/API_KEY/MODEL` (Tyk/vLLM remote) | — | init создаёт Credential-переменные OPENAI_COMPATIBLE_BASE_URL/_API_KEY (payload: name,type=Credential,value,default_fields=[],category=credentials; PATCH требует id в body) и патчит provider/model_name нод |
| `DoclingInline`, `ChunkDoclingDocument` | — | всё в образе (torch, docling, tesserocr, LibreOffice) | — | вычитать нельзя; OCR-движок по field_rule |
| flow-only кастомные ноды | — | экспорт кода из flow JSON + AST-анализ импортов (см. README) | — | forced keep по import_map |

## Сборка локальных сервисов
- `llama-server` — **CPU-only сборка из исходников внутри Astra-контейнера**
  (хостовый бинарник — CUDA-сборка, в Astra без CUDA не грузится; проверено):
  git clone llama.cpp → cmake -DGGML_CUDA=OFF → извлечь `llama-server` + `libllama*/libggml*` в /app/lfx/bin|lib
- `qdrant` — https://github.com/qdrant/qdrant/releases (x86_64-unknown-linux-musl), бинарник в /app/lfx/bin
- модели — /app/lfx/models/*.gguf (запекаются)
- entrypoint порядок старта: qdrant → llama-embed → llama-rerank → langflow → lfx_init
- lfx_init патчит URL нод: env `LFX_EMBED_URL` (дефолт http://127.0.0.1:8081/v1),
  `LFX_RERANK_URL` (http://127.0.0.1:8082/v1/rerank), `LFX_QDRANT_URL` (http://127.0.0.1:6333)

## Проверенные замеры (Astra-контейнер, CPU)
- llama.cpp GGUF q8: embed ~0.4s, RAM пары ~1.6GB — выбранный вариант
- sentence-transformers fp16 (альтернатива): embed 1.0s, rerank8 5.4s, RAM 5.4GB — отклонено
- qdrant in-memory: RAM ~0.3GB на малые коллекции

## Грабли EmbeddingModel + «OpenAI Compatible» (проверено на lfx 1.0.29)
- **ДВА template в ноде**: `data.template` (верхний уровень, его отдаёт GET /flows) и
  `data.node.template` (внутри него запечённый код ноды; ЕГО читает загрузчик графа).
  Патчить поля только по верхнему пути — рантайм продолжит видеть старые значения
  (симптом: «Connection error» на URL из исходного экспорта, хотя API показывает новые).
  lfx_init._templates() правит оба.
- Скалярные поля `provider`/`model_name`/`api_base` НЕ достаточны: если поле `model`
  (ModelInput selection) пустое — компонент уходит в глобальную модель по умолчанию
  (OpenAI → api.openai.com → 403 region). Нужно писать selection-лист в `model`.
- «OpenAI Compatible» не зарегистрирован в EMBEDDING_PROVIDER_CLASS_MAPPING →
  selection обязан нести `metadata.embedding_class="OpenAIEmbeddings"` +
  `metadata.param_mapping` (копия маппинга OpenAI: api_base→base_url и т.д.),
  иначе ValueError "No embedding class defined in metadata".
- `api_base` ноды применяется только когда provider selection == provider ноды
  (use_component_overrides). Иначе берётся глобальная OPENAI_COMPATIBLE_BASE_URL
  (а она у нас — chat-сервер!).
- transform_localhost_url переписывает localhost/127.0.0.1 → host.docker.internal
  только для ГЛОБАЛЬНЫХ переменных провайдера; явный api_base ноды не трогает.
- Готовая структура selection — в lfx_init.patch_service_urls.

## Формат /api/v1/run/{flow_id} (langflow 1.0.29, x-api-key)
- Плоские `{"input": ...}` / `{"input_text": ...}` **игнорируются молча** (нода получает
  текст из template-дефолта ChatInput — симптом: LLM отвечает на «Hello»).
- Правильно: `{"input_request": {"input_value": "...", "input_type": "chat",
  "output_type": "chat", "session_id": "...", "tweaks": {...}}}`.
- Файл в flow передаётся твиком `ChatInput-<id>.files: ["<flow_id>/<имя_файла>"]`
  (путь из ответа /api/v1/files/upload).
- Тестер langflow-rag-tester.html исправлен под этот формат (2026-11-13).


