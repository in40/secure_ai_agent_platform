#!/bin/bash
# build_lfx.sh — собрать LFX-образ из flow JSON. ПОЛНОСТЬЮ ОФЛАЙН
# (нужны только: docker, python3, базовый образ, каталог сервисов).
#
# usage: build_lfx.sh <flow.json> <имя> [-- без --local-services, даже если каталог есть]
# env:   LFX_BASE   базовый образ (default aso9864/langflow-v2:1.0.29-astra)
#        LFX_SVC    каталог bin/ lib/ models/ (default ./lfx-svc рядом со скриптом)
#        LFX_NO_SVC=1 — не запекать сервисы
#
# Результат: образ lfx-<имя>:latest. Запуск — см. README_REMOTE.md шаг 3.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FLOW=${1:?"нужен путь к flow JSON (экспорт flow из langflow)"}
NAME=${2:?"нужно короткое имя образа"}
shift 2
BASE=${LFX_BASE:-aso9864/langflow-v2:1.0.29-astra}
SVC=${LFX_SVC:-$HERE/lfx-svc}

echo "=== 1. профиль flow (что вычитать, какие сервисы нужны) ==="
python3 "$HERE/lfx_profile.py" --file "$FLOW" --manifest "$HERE/manifest.json" \
    --json "/tmp/lfx-$NAME.profile.json" --strict

echo "=== 2. сборка образа ==="
ARGS=(--profile "/tmp/lfx-$NAME.profile.json" --base "$BASE" --tag "lfx-$NAME:latest"
      --flow-json "$FLOW" --llm-env --no-ui)
if [ "${LFX_NO_SVC:-0}" != 1 ] && [ -d "$SVC/bin" ]; then
    ARGS+=(--local-services "$SVC")
else
    echo "ВНИМАНИЕ: каталог сервисов $SVC не найден — образ будет без локальных сервисов"
fi
python3 "$HERE/lfx_subtract.py" "${ARGS[@]}"

echo "=== готово: lfx-$NAME:latest ==="
