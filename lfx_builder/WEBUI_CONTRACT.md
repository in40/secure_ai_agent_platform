# Контракт API для web-UI к langflow-развёртываниям (v1.0)

Целевой бэкенд: `aso9864/langflow-v2:1.0.29` (prod) и LFX-runtime (одна и та же
версия langflow 1.13.0). Контракт проверялся на живых стендах 2026-11-16.

## 0. Принципы
- UI НЕ знает содержимое flow заранее: определяет тип flow по его нодам (раздел 3)
  и ведёт себя соответственно. Один UI годится для любых flow.
- Единственный режим выполнения — **синхронный JSON**. Параметр `stream` в этом
  билде игнорируется (ответ всё равно `application/json`) — SSE в контракт v1 не входит.
- Ожидать ответ приходится долго (docling-инgest + LLM): клиентский таймаут
  **не менее 300 с**; серверный лимит исполнения flow — 1800 с.

## 1. Аутентификация
Заголовок `x-api-key: sk-…` (или `Authorization: Bearer sk-…`) на всех эндпоинтах
кроме `/health`, `/version`. Ключ создаёт администратор:
`POST /api/v1/api_key/` (со слэшем!) телом `{"name":"ui"}` с сессионным Bearer-токеном
(`POST /api/v1/login`, form `username=…&password=…`).

## 2. Служебные эндпоинты
| Метод/путь | Ответ |
|---|---|
| `GET /health` | 200 (без auth) |
| `GET /version` | `{"build":"1.0.29-astra4","base":"Astra Linux","langflow":"1.13.0"}` |
| `GET /api/v1/flows/` | `[{"id","name","description","endpoint_name",…}]` |
| `GET /api/v1/flows/{id}` | flow целиком, `data.nodes[]` — см. раздел 3 |

## 3. Типы flow и их детекция
По `GET /api/v1/flows/{id}`: `nodes = data.nodes`, тип ноды — `node.data.type`,
вложенный шаблон — `node.data.node.template` (поля значений: `template.<field>.value`).

| Паттерн | Признак | Поведение UI |
|---|---|---|
| **A. Upload File** | есть нода `type == "UploadedFile"` | перед каждым запросом (или по кнопке) загрузить файл (раздел 4) и передать его путь твиком `UploadedFile-<node_id>.file_path` |
| **B. ChatInput + Router** | есть `ChatInput` и кастомная нода `ChatInputRouter` | файлы (опционально) твиком `ChatInput-<node_id>.files`; сообщение с файлами = «инжест в базу», без файлов = «вопрос по базе» |
| **C. Без файлов** | ни того ни другого | просто текст в `input_value` |

`node_id` — это `node.id` в JSON flow (напр. `UploadedFile-aB3c9`, `ChatInput-fZXD3`).
UI должен резолвить их динамически по GET flow, а не хардкодить.

## 4. Загрузка файла
```
POST /api/v1/files/upload/{flow_id}     (multipart/form-data, поле "file")
→ 200 {"flowId":"…","file_path":"{flow_id}/2026-11-16_08-16-40_doc.pdf"}
```
`file_path` относительный — передаётся в твиках как есть. Поддерживаемые форматы
определяет flow (наши сборки: PDF/DOCX/TXT/MD/HTML/CSV/JSON/XLSX…).
Файлы видны только в пределах своего flow_id.

## 5. Выполнение (главный эндпоинт)
```
POST /api/v1/run/{flow_id}      (или /run/{endpoint_name}, если у flow задан alias)
```
Тело — строго в обёртке `input_request` (плоские `input`/`input_text` **молча
игнорируются** — нода получит текст из своего шаблона):
```json
{
  "input_request": {
    "input_value": "текст пользователя",
    "input_type": "chat",
    "output_type": "chat",
    "session_id": "ui-<id диалога>",
    "tweaks": {
      "ChatInput-fZXD3":   {"files": ["<file_path>", …]},        // паттерн B
      "UploadedFile-aB3c9": {"file_path": "<file_path>"}          // паттерн A (строка; несколько файлов — через \n)
    }
  }
}
```
`tweaks` для паттерна C отсутствует.

**Успех 200:**
```json
{
  "session_id": "…",
  "outputs": [
    {"inputs": {"input_value": "…"},
     "outputs": [
       {"outputs": [{"results": {"message": {"data": {
           "text": "ОТВЕТ",
           "sender": "Machine", "timestamp": "…",
           "properties": {"source": {"id":"…","source":"имя-модели"}, "usage": {"input_tokens":N,"output_tokens":M}}
       }}}}]
     ]}
  ]
}
```
Ответов может быть несколько (несколько ChatOutput) — UI показывает все непустые
`outputs[].outputs[].results.message.data.text`, дедуплицируя по тексту.
Полезные поля: `properties.usage` (токены), `properties.source` (какая нода/модель).

**Ошибка (500):** `{"detail": "<JSON-строка>"}` — внутри `{"message":"Error running graph: …"}`.
`detail` — строку надо распарсить JSON-ом для человекочитаемого текста.
403 — неверный/отсутствующий ключ; 404 — нет flow.

## 6. Сессии и история
- `session_id` — свободная строка, придумывает UI (например `uuid`). Одна сессия = один диалог.
- История: `GET /api/v1/monitor/messages?session_id=…&limit=…` → массив сообщений
  (`sender` = `User`/`Machine`, `text`, `files`, `timestamp`).
  Список сессий: `GET /api/v1/monitor/messages/sessions`.
  Очистка: `DELETE /api/v1/monitor/messages/session/{session_id}`.
- Для паттерна B: история — часть контекста flow (SessionEnricher и т.п. на стороне flow).

## 7. Размещение UI (CORS/mixed-content)
- Страница по https не может обращаться к `http://host:port` (mixed content).
  Размещать UI на том же origin, что и API, либо проксировать:
  `location /lf-api/ { proxy_pass http://127.0.0.1:7861/; }` → base URL UI = `https://site/lf-api`.
- Сервер отдаёт CORS-заголовки (`vary: Origin`), но настраиваемый whitelist — не полагайтесь.

## 8. Примеры (curl)
```bash
# C: без файлов
curl -X POST $B/api/v1/run/$FID -H "x-api-key: $K" -H "Content-Type: application/json" \
  -d '{"input_request":{"input_value":"Привет","input_type":"chat","output_type":"chat","session_id":"s1"}}'
# B: с файлом (инжест), затем без файла (вопрос по базе) — тот же session_id
# A: твик UploadedFile-<id>.file_path из ответа upload
```

## 9. Известные ограничения
- `stream` не работает (см. 0); токен-за-токеном в v1 не обещается.
- Паттерн B: flow с ChatInputRouter может быть устроен так, что запрос **без** файла
  идёт по ingest-ветке и падает на пустом документе — это свойство конкретного flow,
  не API. Рекомендация UI: для паттерна B всегда давать пользователю приложить файл
  и показывать понятную ошибку, если flow её не любит (тест по каждому flow при подключении).
- Загруженные файлы не удаляются автоматически (есть `DELETE /api/v1/files/delete/{flow_id}/{file_name}`).
- Поля `files` в истории — JSON-строка, а не массив (парсить).
