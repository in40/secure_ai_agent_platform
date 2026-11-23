"""Внутри контейнера: нода TykVLLMModel по образцу существующей custom-ноды скелета."""
import copy
import json
import sys

sys.path.insert(0, '/app')
sys.path.insert(0, '/app/custom_components')
from lfx.custom.utils import build_custom_component_template  # noqa: E402
import tyk_vllm_model  # noqa: E402

TYK_ID = 'TykVLLMModel-docqa1'
tpl, err = build_custom_component_template(tyk_vllm_model.TykVLLMModelComponent())
assert tpl, err

d = json.load(open('/flows/docqa_skel.json'))
proto = next(n for n in d['data']['nodes'] if n['data']['type'] == 'answer_synthesizer')
node = copy.deepcopy(proto)
node['id'] = TYK_ID
node['type'] = 'TykVLLMModel'
node['position'] = {'x': 760, 'y': 220}
nd = node['data']
nd['id'] = TYK_ID
nd['type'] = 'TykVLLMModel'
inner = nd['node']
template = tpl['template']
template['code'] = {'type': 'code', 'advanced': True, 'required': False,
                    'value': open('/app/custom_components/tyk_vllm_model.py').read()}
template.setdefault('model_name', {})['value'] = 'qwen3.5-122b'
template.setdefault('reasoning_mode', {})['value'] = 'off'
inner['template'] = template
inner['outputs'] = tpl['outputs']
inner['display_name'] = tpl['display_name']
inner['base_classes'] = tpl['base_classes']
inner['output_types'] = tpl.get('output_types', [])
inner['field_order'] = tpl.get('field_order', [])
inner['metadata'] = tpl.get('metadata', {})
inner['icon'] = tpl.get('icon', 'brain')
inner['documentation'] = tpl.get('documentation', '')
inner['conditional_paths'] = tpl.get('conditional_paths', [])
inner['tool_mode'] = tpl.get('tool_mode', True)

d['data']['nodes'].append(node)
d['name'] = 'docqa-executor'
json.dump(d, open('/tmp/docqa.json', 'w'), ensure_ascii=False)
print('Tyk node added; model =', template['model_name']['value'])
