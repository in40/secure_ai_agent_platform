#!/bin/bash
# fetch_local_services.sh — подготовка каталога локальных сервисов для LFX.
# ЗАПУСКАТЬ НА МАШИНЕ С ИНТЕРНЕТОМ (сборочный сервер). На офлайн-remote — только
# скопировать результат (см. README_REMOTE.md, способ Б: docker cp из aso9864/lfx-svc).
#
# Результат: OUTDIR/{bin,llama-server,qdrant; lib/*.so; models/*.gguf}
#   bin/  — исполняемые (qdrant статический musl, llama-server CPU-only)
#   lib/  — .so для llama-server (libllama*, libggml*, libmtmd*)
#   models/ — GGUF (эмбеддер ОБЯЗАТЕЛЬНО тот, что индексировал коллекции!)
#
# env: OUTDIR (default /tmp/lfx-svc), BASE_IMAGE (default aso9864/langflow-v2:1.0.29-astra),
#      EMBED_GGUF (путь к локальному gguf эмбеддера — копируется, не качается),
#      SKIP_LLAMA=1 / SKIP_QDRANT=1 / SKIP_MODELS=1
set -euo pipefail
OUTDIR=${OUTDIR:-/tmp/lfx-svc}
BASE_IMAGE=${BASE_IMAGE:-aso9864/langflow-v2:1.0.29}
QDRANT_VER=${QDRANT_VER:-1.20.1}
RERANK_URL=${RERANK_URL:-https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q4_K_M.gguf}
mkdir -p "$OUTDIR/bin" "$OUTDIR/lib" "$OUTDIR/models"

# 1) qdrant — статический musl-бинарник (~36MB tar)
if [ "${SKIP_QDRANT:-0}" != 1 ] && [ ! -x "$OUTDIR/bin/qdrant" ]; then
    cd "$OUTDIR"
    curl -sL -o qdrant.tar.gz "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VER}/qdrant-x86_64-unknown-linux-musl.tar.gz"
    tar xzf qdrant.tar.gz -C bin/ && rm qdrant.tar.gz
fi

# 2) модели. Эмбеддер НЕ качаем по умолчанию — он должен совпадать с индексом
#    коллекций заказчика: положите свой GGUF и укажите EMBED_GGUF=/path/to.gguf
if [ "${SKIP_MODELS:-0}" != 1 ]; then
    [ -n "${EMBED_GGUF:-}" ] && cp -n "$EMBED_GGUF" "$OUTDIR/models/" || true
    if [ ! -f "$OUTDIR/models/bge-reranker-v2-m3-Q4_K_M.gguf" ]; then
        curl -sL --retry 3 -o "$OUTDIR/models/bge-reranker-v2-m3-Q4_K_M.gguf" "$RERANK_URL"
    fi
fi

# 3) llama-server — СБОРКА CPU-only внутри базового контейнера.
#    Хостовый бинарник не подходит: обычно CUDA-сборка, в Astra без CUDA не грузится.
if [ "${SKIP_LLAMA:-0}" != 1 ] && [ ! -x "$OUTDIR/bin/llama-server" ]; then
    docker rm -f llama-build >/dev/null 2>&1 || true
    docker run -d --name llama-build --entrypoint sleep "$BASE_IMAGE" 7200
    docker exec --user root llama-build sh -c 'apt-get update -qq && apt-get install -y -qq git cmake build-essential libssl-dev ca-certificates >/dev/null 2>&1'
    docker exec --user root llama-build git clone --depth 1 https://github.com/ggml-org/llama.cpp /src
    docker exec --user root llama-build sh -c 'cd /src && cmake -B build -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF >/dev/null && cmake --build build --target llama-server -j "$(nproc)"'
    docker cp llama-build:/src/build/bin/llama-server "$OUTDIR/bin/"
    for so in $(docker exec --user root llama-build sh -c 'ls /src/build/bin/*.so* 2>/dev/null | xargs -n1 basename'); do
        docker cp "llama-build:/src/build/bin/$so" "$OUTDIR/lib/" || true
    done
    docker rm -f llama-build >/dev/null 2>&1 || true
fi

ls -la "$OUTDIR/bin" "$OUTDIR/lib" "$OUTDIR/models"
echo "FETCH_DONE -> $OUTDIR"
