"""Финальная подготовка flow basicrag для официального lfx-executor (один проход)."""
import json
import subprocess

SRC = '/tmp/opencode/basicrag_flow.json'
OUT = '/tmp/opencode/basicrag_lfx_final.json'

subprocess.run(['python3', '/root/hybrid/lfx_builder/lf2lfx.py', '--flow', SRC, '-o', OUT],
               check=True, capture_output=True)

d = json.load(open(OUT))
s = json.dumps(d, ensure_ascii=False)
# 1) адреса сервисов -> localhost (сервисы крутятся на хосте, контейнер в host-network)
s = s.replace('http://192.168.100.9:8081/v1', 'http://127.0.0.1:8081/v1')
s = s.replace('http://192.168.100.9:8082/v1/rerank', 'http://127.0.0.1:8082/v1/rerank')
s = s.replace('http://qdrant:6333', 'http://127.0.0.1:6333')
# 2) провайдер чата vLLM -> встроенный OpenAI (standalone lfx его не знает);
#    "OpenAI Compatible" для эмбеддингов ОСТАВЛЯЕМ: lfx сам ставит ему
#    check_embedding_ctx_length=False (иначе langchain шлёт llama-server токены cl100k)
s = s.replace('"provider": "vLLM"', '"provider": "OpenAI"')
d = json.loads(s)
data = d.get('data', d)
# 3) query-only: убираем ingest-ветку (executor билдит ВСЕ ноды, Docling без файла падает)
edges = data.get('edges', [])
incoming = {e.get('target') for e in edges}
drop = {n['id'] for n in data['nodes']
        if n.get('data', {}).get('type') == 'ChatOutput' and n['id'] not in incoming}
drop |= {n['id'] for n in data['nodes']
         if n.get('data', {}).get('type') in ('DoclingInline', 'ChunkDoclingDocument', 'SessionEnricher')}
data['nodes'] = [n for n in data['nodes'] if n['id'] not in drop]
data['edges'] = [e for e in edges if e.get('source') not in drop and e.get('target') not in drop]
# 4) креды: поля api_key резолвятся как ИМЕНА переменных из env (значения — в env контейнера)
LLM_URL = 'http://192.168.100.9:8000/chat122b/v1'
for n in data['nodes']:
    t = n.get('data', {}).get('type', '')
    tpl = n.get('data', {}).get('node', {}).get('template', {})
    if t == 'EmbeddingModel':
        tpl.setdefault('api_key', {'value': ''})['value'] = 'OPENAI_COMPATIBLE_API_KEY'
    if t == 'LanguageModelComponent':
        tpl.setdefault('api_base', {'value': ''})['value'] = LLM_URL
        tpl.setdefault('api_key', {'value': ''})['value'] = 'TYK_LLM_KEY'
        tpl.setdefault('model_name', {'value': ''})['value'] = 'qwen3.5-122b'
json.dump(d, open(OUT, 'w'), ensure_ascii=False)
print(f'готово: удалено мёртвых ChatOutput: {len(drop)}, нод: {len(data["nodes"])}')
