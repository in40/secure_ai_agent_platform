#!/bin/bash
# Верификация зачистки ОС-стека в astra-exec-slim:0.2
IMG=astra-exec-slim:0.2
echo "=== 1. dpkg/apt БЫТЬ НЕ ДОЛЖНО:"
docker run --rm --entrypoint sh $IMG -c 'ls /usr/bin/dpkg /usr/bin/apt /usr/bin/perl /usr/bin/curl /usr/bin/wget /usr/bin/gzip /usr/bin/tar /var/lib/dpkg 2>&1 | grep -v "No such" || echo CLEAN'
echo "=== 2. импорты venv:"
docker run --rm --entrypoint sh $IMG -c '/app/.venv/bin/python -c "import lfx, sqlite3, ssl, pyexpat, gzip, tarfile, ctypes.util; print(\"imports ok\")"'
echo "=== 3. lfx CLI + sqlite:"
docker run --rm --entrypoint sh $IMG -c '/app/.venv/bin/lfx --version | head -1; /app/.venv/bin/python -c "import sqlite3; print(\"sqlite\", sqlite3.sqlite_version)"'
echo "=== 4. размер:"
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep astra-exec
