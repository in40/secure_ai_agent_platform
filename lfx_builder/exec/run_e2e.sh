#!/bin/bash
set -e
grep -E '^(VLLM_|EPA_)' /home/sorokin/crawler/langflow-v2/secrets.env > /tmp/vllm.env || true
wc -l /tmp/vllm.env
docker rm -f astra-exec >/dev/null 2>&1 || true
docker run -d --name astra-exec --network host \
  --env-file /tmp/vllm.env \
  -v /tmp/astraexec/docqa_skel.json:/flows/docqa_skel.json:ro \
  -v /tmp/astraexec/gen_tyk_node.py:/tmp/gen_tyk_node.py:ro \
  -v /tmp/ingestdata:/data:ro \
  -e LANGFLOW_API_KEY=sk-EoOXgTH8sIca7j-EcT96vZr7P_ZzkxynS8-RQIxkKaA \
  -e LANGFLOW_RESTRICT_LOCAL_FILE_ACCESS=false \
  -e PYTHONPATH=/app:/app/custom_components \
  -e PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
  -e HOME=/app/data -e HF_HOME=/app/data/.cache/huggingface -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 \
  -e VLLM_EPA_AUTH=false \
  -e VLLM_API_BASE=http://192.168.100.9:8000/chat122b/v1 \
  -e VLLM_API_KEY=sk-22c7d786a8c5f6cdd6e0ef3b88cdb9d1d7f435ba31726dff \
  -e VLLM_MODEL_CHOICES=qwen3.5-122b \
  astra-exec:0.1 sh -c '/app/.venv/bin/python3 /tmp/gen_tyk_node.py && exec /app/.venv/bin/lfx serve /tmp/docqa.json --host 127.0.0.1 --port 7896' >/dev/null
for i in $(seq 1 120); do curl -sm2 http://127.0.0.1:7896/health -o /dev/null 2>/dev/null && break; sleep 1; done
docker logs astra-exec 2>&1 | tail -3
echo "health=$(curl -sm5 -o /dev/null -w %{http_code} http://127.0.0.1:7896/health)"
echo "=== DOCQA RUN ==="
time curl -sm600 -X POST http://127.0.0.1:7896/flows/c87ad34f-9b22-473a-90b7-d590f71f685a/run \
  -H 'x-api-key: sk-EoOXgTH8sIca7j-EcT96vZr7P_ZzkxynS8-RQIxkKaA' -H 'Content-Type: application/json' \
  -d '{"input_value": "/data/contract.md---Кто ответственный по договору и до какой даты?", "session_id": "docqa1"}' | head -c 1200
