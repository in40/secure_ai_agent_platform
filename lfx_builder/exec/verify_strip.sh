#!/bin/bash
# Верификация зачистки ОС-стека в astra-exec-slim:0.2 + serve-смоук.
IMG=astra-exec-slim:0.2
echo "=== 1. остатки уязвимых пакетов (ожидаем 0):"
docker run --rm --entrypoint sh $IMG -c 'dpkg -l 2>/dev/null | grep ^ii | grep -cE "apt|perl|python3.11|curl|brotli|gzip|dpkg|udev|systemd|tar" || echo dpkg-gone'
echo "=== 2. бинарники (ожидаем пусто):"
docker run --rm --entrypoint sh $IMG -c 'ls /usr/bin/apt /usr/bin/dpkg /usr/bin/perl /usr/bin/curl /usr/bin/wget /usr/bin/gzip /usr/bin/tar 2>&1 | grep -v "No such" || true'
echo "=== 3. импорты venv:"
docker run --rm --entrypoint sh $IMG -c '/app/.venv/bin/python -c "import lfx, sqlite3, ssl, pyexpat, gzip, tarfile, ctypes.util; print(\"imports ok\")"'
echo "=== 4. lfx CLI:"
docker run --rm --entrypoint sh $IMG -c '/app/.venv/bin/lfx --version | head -1'
