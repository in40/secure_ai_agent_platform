"""docqa-flow для lfx-executor: файл→Docling→Chunk→synthesizer→TykVLLMModel (без qdrant/embeddings)."""
import copy
import json
import subprocess

SRC = '/tmp/opencode/basicrag_flow.json'
RAW = '/tmp/opencode/docqa_skel_raw.json'
OUT = '/tmp/opencode/docqa_skel.json'
TYK_ID = 'TykVLLMModel-docqa1'

d = json.load(open(SRC))
data = d['data']
nodes, edges = data['nodes'], data['edges']
by_type = {n['data'].get('type'): n for n in nodes}

# 1) Router: путь к файлу = текст до '---' (если вложений нет)
router = by_type['ChatInputRouter']
code = router['data']['node']['template']['code']['value']
old = "            files = msg.files or []\n            if not files:\n                return []"
new = ("            files = msg.files or []\n"
       "            if not files:\n"
       "                _t = (msg.text or '').strip()\n"
       "                _p = _t.split('---', 1)[0].strip()\n"
       "                return [Data(data={'file_path': _p})] if _p else []")
assert old in code
router['data']['node']['template']['code']['value'] = code.replace(old, new, 1)

# 1b) synthesizer: DataFrame чанков -> полные записи (иначе str(df) = pandas-repr с обрезкой ячеек)
synth = by_type['answer_synthesizer']
scode = synth['data']['node']['template']['code']['value']
sold = '    def _extract_records_from_result(self, data: Any) -> list[dict]:\n        data = self._unwrap_data(data)'
snew = ('    def _extract_records_from_result(self, data: Any) -> list[dict]:\n'
        '        if hasattr(data, "to_dict") and hasattr(data, "columns"):\n'
        '            try:\n'
        '                _recs = data.to_dict("records")\n'
        '                if isinstance(_recs, list) and _recs:\n'
        '                    return [dict(r) for r in _recs]\n'
        '            except Exception:\n'
        '                pass\n'
        '        data = self._unwrap_data(data)')
assert sold in scode
synth['data']['node']['template']['code']['value'] = scode.replace(sold, snew, 1)

# 2) состав: ChatInput, Router, Docling, Chunk, synthesizer, живой ChatOutput
keep_types = {'ChatInput', 'ChatInputRouter', 'DoclingInline', 'ChunkDoclingDocument',
              'answer_synthesizer'}
live_out_edge = next(e for e in edges if e.get('source', '').startswith('LanguageModel')
                     and e.get('target', '').startswith('ChatOutput'))
keep = {n['id'] for n in nodes if n['data'].get('type') in keep_types} | {live_out_edge['target']}
nodes2 = [n for n in nodes if n['id'] in keep]
edges2 = [e for e in edges if e['source'] in keep and e['target'] in keep]

def h(obj):
    return json.dumps(obj, ensure_ascii=False).replace('"', 'œ')

synth_id = by_type['answer_synthesizer']['id']
chunk_id = by_type['ChunkDoclingDocument']['id']
co_id = live_out_edge['target']
# Chunk.dataframe -> synth.vector_results
edges2.append({'id': 'e-chunk-synth', 'source': chunk_id, 'target': synth_id,
               'sourceHandle': h({'dataType': 'ChunkDoclingDocument', 'id': chunk_id,
                                  'name': 'dataframe', 'output_types': ['DataFrame']}),
               'targetHandle': h({'fieldName': 'vector_results', 'id': synth_id,
                                  'inputTypes': ['Message', 'Data', 'JSON', 'DataFrame', 'Table', 'str'],
                                  'type': 'other'}),
               'data': {'sourceHandle': {'dataType': 'ChunkDoclingDocument', 'id': chunk_id,
                                         'name': 'dataframe', 'output_types': ['DataFrame']},
                        'targetHandle': {'fieldName': 'vector_results', 'id': synth_id}},
               'type': 'custom'})
# synth.combined_context -> Tyk.input_value (формат исходного edge synth->LLM)
e1 = copy.deepcopy(next(e for e in edges if e.get('source') == synth_id))
e1['sourceHandle'] = h({'dataType': 'answer_synthesizer', 'id': synth_id,
                        'name': 'combined_context', 'output_types': ['str']})
e1['targetHandle'] = h({'fieldName': 'input_value', 'id': TYK_ID,
                        'inputTypes': ['Message'], 'type': 'str'})
e1.update({'id': 'e-synth-tyk', 'source': synth_id, 'target': TYK_ID, 'data': {}})
e1['data'] = {'sourceHandle': json.loads(e1['sourceHandle'].replace('œ', '"')),
              'targetHandle': json.loads(e1['targetHandle'].replace('œ', '"'))}
edges2.append(e1)
# Tyk.text_output -> ChatOutput.input_value (формат исходного LLM->ChatOutput)
e2 = copy.deepcopy(live_out_edge)
e2['sourceHandle'] = h({'dataType': 'TykVLLMModel', 'id': TYK_ID,
                        'name': 'text_output', 'output_types': ['Message']})
e2.update({'id': 'e-tyk-out', 'source': TYK_ID, 'target': co_id, 'data': {}})
e2['data'] = {'sourceHandle': json.loads(e2['sourceHandle'].replace('œ', '"')),
              'targetHandle': json.loads(e2['targetHandle'].replace('œ', '"'))}
edges2.append(e2)

data['nodes'], data['edges'] = nodes2, edges2
json.dump(d, open(RAW, 'w'), ensure_ascii=False)
print(f'skel: nodes={len(nodes2)} edges={len(edges2)} (Tyk вставит контейнер)')

subprocess.run(['python3', '/root/hybrid/lfx_builder/lf2lfx.py', '--flow', RAW, '-o', OUT], check=True)
print('готово:', OUT)
