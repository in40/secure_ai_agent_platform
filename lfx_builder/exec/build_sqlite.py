#!/usr/bin/env python3
"""Сборка свежего sqlite3 (amalgamation) и замена системной libsqlite3.so.0.
Закрывает CVE по libsqlite3-0 Astra 1.8.7 (3.40.1) — единственный пакет зачистки,
который нельзя удалить (к нему линкован _sqlite3 из venv).
Запускается на этапе docker build ДО удаления тулчейна (нужен gcc)."""
import hashlib
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

BASE = "https://www.sqlite.org"
LIB = "/usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6"
FLAGS = [
    "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_FTS3",
    "-DSQLITE_ENABLE_FTS3_PARENTHESIS", "-DSQLITE_ENABLE_FTS4",
    "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_JSON1",
    "-DSQLITE_ENABLE_LOAD_EXTENSION", "-DSQLITE_ENABLE_RTREE",
    "-DSQLITE_ENABLE_STAT4", "-DSQLITE_ENABLE_UPDATE_DELETE_LIMIT",
    "-DSQLITE_SECURE_DELETE",
]


def main() -> int:
    html = urllib.request.urlopen(BASE + "/download.html", timeout=60).read().decode()
    m = re.search(
        r"PRODUCT,([\d.]+),(\d+/sqlite-amalgamation-\d+\.zip),\d+,([0-9a-f]{64})", html)
    if not m:
        print("build_sqlite: не нашёл amalgamation на download.html", file=sys.stderr)
        return 1
    version, path, sha3 = m.groups()
    url = BASE + "/" + path
    print(f"build_sqlite: {version} <- {url}")
    raw = urllib.request.urlopen(url, timeout=120).read()
    got = hashlib.sha3_256(raw).hexdigest()
    if got != sha3:
        print(f"build_sqlite: SHA3 не совпал: {got} != {sha3}", file=sys.stderr)
        return 1
    with open("/tmp/sql.zip", "wb") as f:
        f.write(raw)
    zipfile.ZipFile("/tmp/sql.zip").extractall("/tmp/sql")
    src = next(p for p in os.listdir("/tmp/sql") if p.startswith("sqlite-amalgamation-"))
    cmd = (["gcc", "-shared", "-fPIC", "-O2", "-pthread", "-std=c11",
            "-Wl,-soname,libsqlite3.so.0", "-o", LIB + ".new"]
           + FLAGS + [f"/tmp/sql/{src}/sqlite3.c", "-lm", "-ldl", "-lpthread"])
    subprocess.run(cmd, check=True)
    os.replace(LIB + ".new", LIB)
    subprocess.run(["/sbin/ldconfig"], check=True)
    out = subprocess.run(
        [sys.executable, "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
        check=True, capture_output=True, text=True).stdout.strip()
    print(f"build_sqlite: sqlite3 в python теперь {out}")
    if not out.startswith("3.5"):
        print("build_sqlite: замена не применилась", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
