#!/bin/sh
# LFX runtime entrypoint: локальные сервисы (если запечены) + langflow + инициализация
if [ -x /app/lfx/bin/qdrant ]; then
    export LD_LIBRARY_PATH=/app/lfx/lib:$LD_LIBRARY_PATH
    mkdir -p /tmp/qdrant
    QDRANT__STORAGE__ON_DISK=false \
    QDRANT__STORAGE__STORAGE_PATH=/tmp/qdrant \
    QDRANT__SERVICE__HTTP_PORT=6333 \
    /app/lfx/bin/qdrant &
    /app/lfx/bin/llama-server -m "${LFX_EMBED_MODEL:-/app/lfx/models/user-bge-m3-q8_0.gguf}" \
        --embedding --pooling mean -ngl 0 --host 127.0.0.1 --port 8081 -t 4 &
    /app/lfx/bin/llama-server -m "${LFX_RERANK_MODEL:-/app/lfx/models/bge-reranker-v2-m3-Q4_K_M.gguf}" \
        --reranking -ngl 0 --host 127.0.0.1 --port 8082 -t 4 &
    echo "[entrypoint] локальные сервисы запущены (qdrant:6333, embed:8081, rerank:8082)"
fi
langflow run &
LF=$!
python3 /app/lfx/lfx_init.py &
INIT=$!
# init не должен вешать контейнер, если упадёт — langflow продолжит жить
wait $INIT 2>/dev/null || echo "[entrypoint] lfx_init завершился с ошибкой"
wait $LF
