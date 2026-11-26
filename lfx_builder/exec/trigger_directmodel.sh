#!/bin/bash
# Постановка в очередь сборки lfx-directmodel из flow DirectModel (прод langflow 7861)
set -e
set -a; source /home/sorokin/crawler/langflow-v2/secrets.env; set +a
T=$(curl -sm15 -X POST 'http://127.0.0.1:7861/api/v1/login' -d "username=sorokin&password=${LANGFLOW_SUPERUSER_PASSWORD}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
python3 - "$T" <<'PYEOF'
import json, sys, urllib.request
tok = sys.argv[1]
def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok})))
fl = get('http://127.0.0.1:7861/api/v1/flows/')
fl = fl if isinstance(fl, list) else fl['flows']
f = next(x for x in fl if x['name'] == 'DirectModel')
flow = get('http://127.0.0.1:7861/api/v1/flows/' + f['id'])
body = json.dumps({'flow_id': f['id'], 'name': 'directmodel', 'flow': flow}).encode()
req = urllib.request.Request('http://127.0.0.1:7870/api/builds', data=body,
    headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
print('job:', urllib.request.urlopen(req).read().decode())
PYEOF
