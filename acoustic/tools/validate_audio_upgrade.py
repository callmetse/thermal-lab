"""Isolated model check: no broker subscriptions, no live health, no camera."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from endpoint_ab import run
import worker

def main():
    outputs={}
    # Any accidental live I/O fails the validation immediately.
    with patch.object(worker,'mqtt',side_effect=AssertionError('offline test tried MQTT')):
        instance=worker.Worker(connect_io=False)
        assert instance.client is None
        del instance
    for value in (0.5,0.4):
        out=run('fixed',sys.argv[1],silence=value)
        outputs[str(value)]=out
        print(json.dumps({'silence':value,'texts':[e['text'] for e in out['transcripts']], 'stats':out['stats']},ensure_ascii=False),flush=True)
    (ROOT/'results').mkdir(exist_ok=True)
    (ROOT/'results'/'calibration_ab.json').write_text(json.dumps(outputs,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
