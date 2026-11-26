#!/bin/bash
# Push job-а на Hub + ожидание результата
set -e
JOB=${1:-b501a50cb61a4074a43bfcf4a5ec46f0}
set -a; source /home/sorokin/crawler/langflow-v2/secrets.env; set +a
T=$(curl -sm15 -X POST 'http://127.0.0.1:7861/api/v1/login' -d "username=sorokin&password=${LANGFLOW_SUPERUSER_PASSWORD}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -sm10 -X POST "http://127.0.0.1:7870/api/builds/$JOB/push" -H "Authorization: Bearer $T"
echo
for i in $(seq 1 100); do
  P=$(python3 -c "import json;print(json.load(open('/home/sorokin/lfx-builder/jobs/$JOB/meta.json')).get('push',{}).get('status',''))" 2>/dev/null)
  [ "$P" = success -o "$P" = failed ] && break
  sleep 20
done
python3 -c "import json;m=json.load(open('/home/sorokin/lfx-builder/jobs/$JOB/meta.json'));print('push:',m.get('push'))"
