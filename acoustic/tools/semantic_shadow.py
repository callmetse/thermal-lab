"""Read recorded transcript events and query the existing 7B, without live writes.

Run beside the existing evaluation helper. Outputs are model candidates only;
no MQTT, no confirmation endpoint, no actuator control.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from llm_semantics_eval import llm_labels, rule_labels

def main():
    p=argparse.ArgumentParser();p.add_argument('snapshot');p.add_argument('output');args=p.parse_args()
    source=json.loads(Path(args.snapshot).read_text())
    rows=[]
    for e in reversed(source['events']):
        if e.get('kind')!='transcript':continue
        text=e.get('text','')
        try:pred,ms,error=llm_labels(text)
        except Exception as ex:pred,ms,error=None,None,str(ex)
        spans=(pred or {}).get('evidence',[])
        spans_valid=isinstance(spans,list) and all(isinstance(x,str) and x in text for x in spans)
        row={'event_id':e['event_id'],'session_id':e['session_id'],'asr_text':text,'rule':rule_labels(text),'llm':pred,'llm_ms':ms,'error':error,'evidence_spans_valid':spans_valid,'status':'candidate_only_not_confirmed','input_type':'recorded_asr_not_manual_transcript'}
        rows.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    Path(args.output).write_text(json.dumps({'mode':'offline_shadow_no_mqtt','model':'qwen2.5:7b','rows':rows},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
