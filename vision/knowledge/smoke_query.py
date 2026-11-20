"""GPU-side RAG smoke: real source retrieval, Ollama generation, citation checks."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--facts',type=Path,required=True)
    a=ap.parse_args()
    sys.path.insert(0,str(a.root))
    from lab.knowledge_retrieval import build,answer,retrieve
    a.out.mkdir(parents=True,exist_ok=True)
    corpus=a.root/'knowledge/project_corpus.json'
    index=a.out/'knowledge.sqlite3'
    build(corpus,index)
    facts=json.loads(a.facts.read_text(encoding='utf-8'))
    from datetime import datetime,timezone,timedelta
    captured_iso=datetime.fromtimestamp(facts['now_ms']/1000,timezone(timedelta(hours=9))).isoformat()
    def safe_times(value):
        if isinstance(value,dict):return {k:safe_times(v) for k,v in value.items() if k not in ('now_ms','data_time_ms','last_seen_ms')}
        if isinstance(value,list):return [safe_times(v) for v in value]
        return value
    facts=safe_times(facts)
    # Facts in this smoke are a captured snapshot, explicitly not a live UI poll.
    facts={'kind':'captured_environment_snapshot','captured_facts':facts,
           'snapshot_time_iso':captured_iso,
           'freshness_note':'只描述该快照采样时刻，不代表生成回答时仍然新鲜'}
    cases=[('knowledge','为什么不能从坐姿直接得到 MET，动作窗口有什么要求？',False,None),
           ('mixed','请解释快照时刻的温度湿度与 CO2，说明数据的时效限制。',False,facts),
           ('refusal','请给出画面中这个人的精确 CLO 和 MET 数值。',True,None)]
    results=[]
    for key,question,unsupported,f in cases:
        result=answer(index,question,'http://127.0.0.1:28434',facts=f)
        results.append({'case':key,'question':question,'result':result})
        print(json.dumps({'case':key,'status':result['status'],'answer':result.get('answer'),
                          'citation_ids':[d['id'] for d in result.get('citations',[])]},ensure_ascii=False),flush=True)
    report={'index_version':'project_corpus_v1','corpus_sha256':hashlib.sha256(corpus.read_bytes()).hexdigest(),
            'retrieval':'character_ngram_tfidf_sqlite_no_embedding_model',
            'cases':results,'production_ui_integrated':False,
            'test_scope':'3-case_smoke_not_40-question_acceptance'}
    assert results[0]['result']['status']=='generated'
    assert results[1]['result']['status']=='generated'
    assert results[2]['result']['status']=='insufficient_evidence'
    (a.out/'rag_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
