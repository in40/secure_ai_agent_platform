#!/bin/bash
# Деплой lfx-builder: backend (systemd, 127.0.0.1:7870) + статика + nginx. Запускать как sorokin.
set -e
BASE=/home/sorokin/lfx-builder
SRC=/home/sorokin/repos/secure_ai_agent_platform/lfx_builder/ui
mkdir -p "$BASE/jobs" /home/sorokin/lfx-builder-ui
[ -d "$BASE/venv" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q fastapi uvicorn httpx
cp "$SRC/backend.py" "$BASE/backend.py"
cp "$SRC/index.html" /home/sorokin/lfx-builder-ui/index.html

sudo -n tee /etc/systemd/system/lfx-builder.service >/dev/null <<'UNIT'
[Unit]
Description=LFX Builder API
After=network.target
[Service]
User=sorokin
WorkingDirectory=/home/sorokin/lfx-builder
ExecStart=/home/sorokin/lfx-builder/venv/bin/uvicorn backend:app --host 127.0.0.1 --port 7870
Restart=always
Environment=LFXB_LANGFLOW_URL=http://127.0.0.1:7861
[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now lfx-builder
sudo -n systemctl restart lfx-builder

sudo -n python3 - <<'PYEOF'
p = "/etc/nginx/sites-enabled/langflow2"
s = open(p).read()
if "lfx-builder" not in s:
    anchor = "    location /verai-test/ {"
    add = """    location /lfx-builder/ {
        alias /home/sorokin/lfx-builder-ui/;
        index index.html;
        try_files $uri $uri/ /lfx-builder/index.html =404;
    }
    location /lfx-builder-api/ {
        proxy_pass http://127.0.0.1:7870/;
        proxy_set_header Host $host;
    }

"""
    assert anchor in s, "anchor not found"
    open(p, "w").write(s.replace(anchor, add + anchor, 1))
    print("nginx: locations added")
else:
    print("nginx: already")
PYEOF
sudo -n nginx -t && sudo -n systemctl reload nginx

sleep 2
echo "backend: $(curl -sm5 http://127.0.0.1:7870/api/health)"
echo "page:    $(curl -sm5 -o /dev/null -w %{http_code} https://langflow2.sorokinonline.com/lfx-builder/)"
echo "api:     $(curl -sm5 -o /dev/null -w %{http_code} https://langflow2.sorokinonline.com/lfx-builder-api/api/health)"
