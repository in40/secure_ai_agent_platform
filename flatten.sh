#!/bin/bash
set -e
SRC=langflow-v2-docling:1.0.27
DST=langflow-v2-docling:1.0.28

mapfile -t ARGS < <(docker inspect -f '{{json .Config}}' "$SRC" | python3 /tmp/gen_changes.py)
echo "changes: ${#ARGS[@]} args"
docker rm -f flat_tmp 2>/dev/null || true
docker create --name flat_tmp "$SRC" >/dev/null
echo "exporting (this takes a few minutes)..."
docker export flat_tmp | docker import "${ARGS[@]}" - "$DST"
docker rm flat_tmp
echo "layers:"; docker history "$DST" | wc -l
docker images "$DST" --format 'SIZE={{.Size}}'
