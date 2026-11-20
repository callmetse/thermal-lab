"""Offline comparison: current rule vs 7B vs rule-first-with-7B fallback.

Runs on the GPU host next to the audio module (imports its common.thermal_candidate
so the rule baseline is the real production rule, not a copy).

Nothing here touches the live pipeline: no microphone, no MQTT publish, no page.
The 7B call reuses the ABC project's existing Ollama protocol (qwen2.5:7b,
temperature 0, JSON schema), not a new inference stack.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, '/root/mm_audio_v1')
from common import thermal_candidate  # noqa: E402

LLM_URL = 'http://127.0.0.1:28434'
MODEL = 'qwen2.5:7b'
CASES = Path(sys.argv[1] if len(sys.argv) > 1 else '/root/mm_audio_v1/cases.json')
OUT = Path('/root/mm_audio_v1/results/llm_semantics_eval.json')

SYSTEM = """你是热舒适实验的语义解析器。输入是一句中文语音转写（可能有识别错误）。
只做两件事：
1. 判断说话人**当前**的热感觉：cold（偏冷）、neutral（适中/正好）、hot（偏热）、none（没有说或说不清）。
2. 判断说话人**希望环境怎么调整**：cooler（想更凉）、warmer（想更暖）、no_change（明确要求保持/别动）、none（没有说或说不清）。

规则：
- 只依据**当前**状态；"刚才/之前/以前"描述的是过去，不能当作当前感受。
- 否定要严格：不冷 → 不是 cold；不热 → 不是 hot；"不想开空调"不等于"不冷"。
- 感受与偏好是两件事，可以只出现一个；两者冲突时分别给出，不要合并。
- 与冷热无关的闲聊（累、闷、想去通风、聊天）一律 none/none，不要硬套标签。
- 不确定就填 none，不要猜。
- evidence 里放支撑判断的原文片段（原样引用，最多 3 段）。

参考示例（注意 none 的用法）：
输入：{"transcript": "我不冷。"} → {"sensation": "none", "preference": "none", "evidence": ["我不冷"], "uncertainty": "否定句，未表达偏好"}
输入：{"transcript": "今天上课挺累的。"} → {"sensation": "none", "preference": "none", "evidence": [], "uncertainty": "与冷热无关"}
输入：{"transcript": "刚才有点冷，现在不热了。"} → {"sensation": "none", "preference": "none", "evidence": ["现在不热了"], "uncertainty": "过去状态不算当前"}

硬性要求：句中**没有**任何对当前冷热的描述时，sensation 必须是 none；
句中**没有**提到想怎么调整时，preference 必须是 none。宁可 none，不要猜。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "sensation": {"type": "string", "enum": ["cold", "neutral", "hot", "none"]},
        "preference": {"type": "string", "enum": ["cooler", "no_change", "warmer", "none"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "uncertainty": {"type": "string"},
    },
    "required": ["sensation", "preference"],
    "additionalProperties": False,
}

SENS = {'cold', 'neutral', 'hot', 'none'}
PREF = {'cooler', 'no_change', 'warmer', 'none'}


def rule_labels(text):
    out = thermal_candidate(text)
    return out['sensation'] or 'none', out['preference'] or 'none'


def llm_labels(text, timeout=60):
    payload = {
        "model": MODEL, "system": SYSTEM,
        "prompt": json.dumps({"transcript": text}, ensure_ascii=False),
        "format": SCHEMA, "stream": False, "keep_alive": "10m",
        "options": {"temperature": 0, "seed": 42, "num_predict": 200},
    }
    req = urllib.request.Request(LLM_URL + '/api/generate',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = json.load(response)
    elapsed_ms = (time.time() - started) * 1000
    try:
        draft = json.loads(body['response'])
    except Exception as ex:
        return None, elapsed_ms, 'invalid_json: %s' % ex
    sensation = str(draft.get('sensation', 'none')).lower()
    preference = str(draft.get('preference', 'none')).lower()
    if sensation not in SENS or preference not in PREF:
        return None, elapsed_ms, 'out_of_enum'
    return {'sensation': sensation, 'preference': preference,
            'evidence': draft.get('evidence') or [],
            'uncertainty': draft.get('uncertainty') or ''}, elapsed_ms, ''


def main():
    cases = json.loads(CASES.read_text('utf8'))
    rows = []
    for case in cases:
        text = case['text']
        truth = (case['sensation'], case['preference'])
        rule = rule_labels(text)
        try:
            llm, llm_ms, error = llm_labels(text)
        except Exception as ex:
            llm, llm_ms, error = None, None, '%s: %s' % (type(ex).__name__, ex)
        # rule-first, 7B fills only where the rule produced nothing
        if llm is None:
            hybrid = rule
        else:
            hybrid = (rule[0] if rule[0] != 'none' else llm['sensation'],
                      rule[1] if rule[1] != 'none' else llm['preference'])
        rows.append({
            'id': case['id'], 'text': text, 'note': case['note'], 'truth': truth,
            'rule': rule, 'llm': None if llm is None else (llm['sensation'], llm['preference']),
            'llm_evidence': None if llm is None else llm['evidence'],
            'llm_error': error, 'llm_ms': None if llm_ms is None else round(llm_ms),
            'hybrid': hybrid,
            'ok': {
                'rule': rule == truth,
                'llm': llm is not None and (llm['sensation'], llm['preference']) == truth,
                'hybrid': hybrid == truth,
            },
            'field_ok': {
                'rule_sensation': rule[0] == truth[0], 'rule_preference': rule[1] == truth[1],
                'llm_sensation': llm is not None and llm['sensation'] == truth[0],
                'llm_preference': llm is not None and llm['preference'] == truth[1],
            },
        })
        print('%s %-26s truth=%-14s rule=%-16s llm=%-16s %s' % (
            case['id'], text[:22], str(truth), str(rule),
            str(None if llm is None else (llm['sensation'], llm['preference'])),
            '规则✔' if rows[-1]['ok']['rule'] else '规则✘'), flush=True)

    def rate(key):
        return sum(1 for r in rows if r['ok'][key])
    latencies = [r['llm_ms'] for r in rows if r['llm_ms'] is not None]
    unknown = sum(1 for r in rows if r['llm'] == ('none', 'none'))
    summary = {
        'cases': len(rows),
        'exact_both_fields': {'rule': rate('rule'), 'llm': rate('llm'), 'hybrid': rate('hybrid')},
        'sensation_correct': {'rule': sum(r['field_ok']['rule_sensation'] for r in rows),
                              'llm': sum(r['field_ok']['llm_sensation'] for r in rows)},
        'preference_correct': {'rule': sum(r['field_ok']['rule_preference'] for r in rows),
                               'llm': sum(r['field_ok']['llm_preference'] for r in rows)},
        'llm_errors': [r['id'] for r in rows if r['llm_error']],
        'llm_all_none': unknown,
        'llm_ms': {'min': min(latencies), 'max': max(latencies),
                   'mean': round(sum(latencies) / len(latencies))} if latencies else None,
        'model': MODEL, 'temperature': 0,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=2),
                   'utf8')
    print('\n--- summary ---')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('wrote', OUT)


if __name__ == '__main__':
    main()
