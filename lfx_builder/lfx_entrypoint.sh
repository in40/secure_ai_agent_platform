#!/bin/sh
# LFX runtime entrypoint: langflow + стартовая инициализация (креды/flow/LLM из env)
langflow run &
LF=$!
python3 /app/lfx/lfx_init.py &
INIT=$!
# init не должен вешать контейнер, если упадёт — langflow продолжит жить
wait $INIT 2>/dev/null || echo "[entrypoint] lfx_init завершился с ошибкой"
wait $LF
