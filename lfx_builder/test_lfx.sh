#!/bin/bash
# test_lfx.sh — e2e-проверка запущенного LFX: upload → ingest → query → ответ.
# usage: test_lfx.sh <base-url> <api-key> <flow-id>   (base-url: http://host:port или https://…/lfx)
# Формат /run: input_request.input_value (плоский input игнорируется молча!).
set -euo pipefail
B=${1:?"base-url, напр. http://127.0.0.1:7867"}
K=${2:?"api-key (sk-…)"}
FID=${3:?"flow id"}
API=$B/api/v1

printf 'Протокол собрания СНТ Радуга. Председатель: Иванов И.И. Ремонт дороги: до 15 декабря 2026 года. Голосовали: за 12, против 3.\n' > /tmp/lfx_test_doc.txt
echo "--- upload ---"
UP=$(curl -sm60 -H "x-api-key: $K" -F "file=@/tmp/lfx_test_doc.txt;type=text/plain" "$API/files/upload/$FID")
PF=$(echo "$UP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("file_path",""))')
[ -n "$PF" ] || { echo "upload FAILED: $UP"; exit 1; }
echo "path=$PF"
echo "--- query (до 5 мин) ---"
SID=lfx-test-$(date +%s)
curl -sm300 -X POST "$API/run/$FID" -H "x-api-key: $K" -H "Content-Type: application/json" \
  -d "{\"input_request\":{\"input_value\":\"Кто председатель и до какого срока ремонт дороги?\",\"input_type\":\"chat\",\"output_type\":\"chat\",\"session_id\":\"$SID\",\"tweaks\":{\"ChatInput-fZXD3\":{\"files\":[\"$PF\"]}}}}" \
  -o /tmp/lfx_test_out.json -w "HTTP=%{http_code}\n"
python3 - <<'PYEOF'
import json
d = json.load(open("/tmp/lfx_test_out.json"))
for o in d.get("outputs", []):
    for oo in o.get("outputs", []):
        t = ((oo.get("results") or {}).get("message") or {}).get("data", {}).get("text")
        if t:
            print("ANSWER:", t)
PYEOF
