# Удалённая сборка LFX (офлайн-remote) — инструкция

LFX = самодостаточный runtime одного flow: базовый образ langflow, из которого
вычтено всё лишнее, flow запечён, UI удалён, нужные внешние сервисы (эмбеддер,
реранкер, qdrant) подняты внутри контейнера. Интернет на remote **не нужен** —
используются только наши образы с Docker Hub и принесённые файлы.

## Что понадобится на remote
Поставка — **только образы с Docker Hub** (git remote недоступен):
- `aso9864/langflow-v2:1.0.29` (~8.4GB) — база + **LFX-тулкит уже внутри** (`/opt/lfx_builder`);
- `aso9864/lfx-svc:1.0.0` (~250MB) — бинарники llama-server(CPU)+qdrant, без моделей;
- docker + python3 на хосте remote;
- экспорт flow JSON (Settings → Export, из их langflow версии 1.13.x/нашей сборки);
- GGUF-модели заказчика (см. «Правила зависимостей»);
- LLM-эндпоинт: OpenAI-compatible URL+ключ+модель (не запекается, из env).

## Шаг 0 — достать тулкит и бинарники из образов
```bash
docker create --name kf aso9864/langflow-v2:1.0.29 && docker cp kf:/opt/lfx_builder ./lfx_builder && docker rm kf
docker create --name svc aso9864/lfx-svc:1.0.0
mkdir -p lfx-svc && docker cp svc:/bin lfx-svc/bin && docker cp svc:/lib lfx-svc/lib && docker rm svc
mkdir -p lfx-svc/models && cp /носитель/с-моделями/*.gguf lfx-svc/models/
```
(Образы `docker pull` делаются заранее, пока доступ к Hub есть; дальше — полный офлайн.)

## Шаг 1 — каталог локальных сервисов `lfx-svc/`
Структура: `lfx-svc/bin/{qdrant,llama-server}`, `lfx-svc/lib/*.so`, `lfx-svc/models/*.gguf`.

**Способ А (на машине с интернетом, потом перенести каталог):**
```bash
OUTDIR=./lfx-svc EMBED_GGUF=/path/к-вашему-эмбеддеру.gguf bash fetch_local_services.sh
```
**Способ Б (полный офлайн, только наши образы с Hub):**
```bash
docker create --name svc aso9864/lfx-svc:1.0.0
mkdir -p lfx-svc && docker cp svc:/bin lfx-svc/bin && docker cp svc:/lib lfx-svc/lib
docker rm svc
mkdir -p lfx-svc/models && cp /носитель/с-моделями/*.gguf lfx-svc/models/
```

## Шаг 2 — сборка
```bash
bash build_lfx.sh /path/flow.json имя-образа
# env: LFX_BASE (базовый образ), LFX_SVC (каталог сервисов), LFX_NO_SVC=1 (без сервисов)
```
Профайлер на этом шаге печатает `REMOVE:` (что вырезано), `SERVICES:` (какие сервисы
нужны flow) и `UNKNOWN TYPES` — если есть unknown, flow использует ноды, которых нет
в базовом образе: либо добавить их код, либо отказаться (сборка с `--strict` падает).

## Шаг 3 — запуск
```bash
docker network create lfx-net 2>/dev/null || true
docker run -d --name lfx-имя --network lfx-net -p 7867:7860 --memory 12g \
  -v /path/data:/data \
  -e LANGFLOW_DATABASE_URL="sqlite:////data/lfx.db" \
  -e LANGFLOW_SUPERUSER=admin -e LANGFLOW_SUPERUSER_PASSWORD='***' \
  -e LANGFLOW_AUTO_LOGIN=False \
  -e LANGFLOW_SSRF_ALLOWED_HOSTS=127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 \
  -e LFX_LLM_BASE_URL=http://llm-host:8000/v1 \
  -e LFX_LLM_API_KEY='***' \
  -e LFX_LLM_MODEL=имя-модели \
  lfx-имя:latest
```
Важно: `--memory 12g` минимум (на пике docling+llama при 8g — OOM-137);
`127.0.0.1` в SSRF-allowlist обязателен, иначе локальные сервисы режутся policy;
том `/data` — персистентный sqlite (переиспользовать между запусками).
Модели сервисов переопределяются: `LFX_EMBED_MODEL`, `LFX_RERANK_MODEL` (пути внутри
образа `/app/lfx/models/...`), имя эмбеддера для ноды — `LFX_EMBED_MODEL_NAME`
(по умолчанию `user-bge-m3-q8_0` — имя файла без расширения, как в их индексах).

Первый старт ~1–2 мин: qdrant → llama-embed → llama-rerank → langflow → lfx_init
(лог `docker logs -f`: строки `[lfx_init] ... готово`).

## Шаг 4 — проверка
```bash
K=$(curl -s -X POST -H "Authorization: Bearer $(curl -s -X POST http://127.0.0.1:7867/api/v1/login \
   -d 'username=admin&password=…' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
   -H "Content-Type: application/json" http://127.0.0.1:7867/api/v1/api_key/ -d '{"name":"t"}' \
   | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
bash test_lfx.sh http://127.0.0.1:7867 "$K" <flow-id>   # flow id — из JSON или GET /api/v1/flows/
```
Ожидаемый ANSWER — факт из загруженного тест-документа.

## Правила внешних зависимостей («затаскиватель»)
Полная таблица «нода flow → сервис» — в `RUNTIME_SERVICES.md`. Кратко:
- **qdrant** (`QdrantVectorStoreComponent`): статический бинарник, in-memory
  (`QDRANT__STORAGE__ON_DISK=false`), если flow не переиспользует коллекции между
  запусками; персист — только при необходимости.
- **эмбеддер** (`EmbeddingModel`): `llama-server --embedding --pooling mean`.
  Модель = **та же, что индексировала коллекции** (stateful!). Q8 предпочтителен;
  смена кванта/модели = переиндексация.
- **реранкер** (`RerankComponent`): `llama-server --reranking`, stateless — квант свободный.
- **LLM**: никогда не запекать — только env `LFX_LLM_*`.
- **свои сервисы заказчика**: 1) положить бинарник в `lfx-svc/bin`; 2) добавить старт
  в `lfx_entrypoint.sh` (копируется в образ при сборке — правьте в репо до сборки);
  3) добавить сопоставление «тип ноды → поле URL → env» в `lfx_init.py`
  (`patch_service_urls`); 4) завести запись в `manifest.json` → `services`
  (тогда профайлер сам детектит потребность); 5) URL сервиса — на 127.0.0.1 и
  добавить подсеть в SSRF-allowlist запуска.
- **python-зависимости flow**: должны быть в базовом образе (он полный — вычитаем,
  не добавляем). Офлайн `pip install` невозможен: если flow требует пакетов вне базы —
  переносить сборку LFX на машину с интернетом или расширить базовый образ заранее.

## Известные грабли (детали в RUNTIME_SERVICES.md)
- В ноде ДВА template (`data.template` и `data.node.template`); загрузчик читает
  второй — lfx_init патчит оба.
- `/api/v1/run` принимает только `{"input_request":{"input_value":…}}`; плоский
  `input`/`input_text` игнорируется молча.
- «OpenAI Compatible» для эмбеддингов требует selection с
  `metadata.embedding_class`+`param_mapping` (lfx_init делает это сам).
- llama-server — только CPU-сборка из исходников (хостовый CUDA-бинарник не грузится).
- Тестер UI: страница по https не может fetchить http://IP:port (mixed content) —
  проксировать через тот же origin (nginx `location /lfx/` → контейнер).

## Ограничения
- 8GB RAM remote: один LFX с эмбеддером+реранкером+qdrant ≈ 2–3GB в простое,
  пик с docling до ~6–8GB — больше одного LFX-контейнера на машину не селить.
- Версия экспорта flow должна соответствовать версии базового образа (1.0.29),
  иначе миграция типов нод даст warnings/ошибки.
