"""Local-only experiment records. No network client or media retransmission."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
import zipfile

def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def sha_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

class ExperimentArchive:
    def __init__(self,store,root):
        self.store,self.root=store,Path(root)
        self.pending=queue.Queue(512);self.error=None
        with closing(store.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS experiment_snapshots(batch_id TEXT PRIMARY KEY,json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiment_records(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,received_ms INTEGER NOT NULL,json TEXT NOT NULL,UNIQUE(batch_id,kind,key));
            CREATE INDEX IF NOT EXISTS experiment_records_batch ON experiment_records(batch_id,id);
            CREATE TABLE IF NOT EXISTS experiment_errors(batch_id TEXT PRIMARY KEY,error TEXT NOT NULL);''')
        self.thread=threading.Thread(target=self._writer,daemon=True);self.thread.start()

    def begin(self,run):
        bid=run.get('collection_batch_id',run['run_id'])
        from .common import config
        c=config();keys=['sample_fps','capture_fps','width','height','encode_max_side','jpeg_quality','replay_file','llm_model','min_person_confidence','max_people','pose_max_det','pose_imgsz','gate_imgsz','rect','camera_backend','collaboration','hard_examples']
        snapshot={'schema_version':'1.0','batch_id':bid,'run':run,'settings':{k:c.get(k) for k in keys},
                  'software':{p:sha_file(self.root/p) for p in ['lab/worker.py','lab/experiment_archive.py'] if (self.root/p).exists()},
                  'model_provenance':'See recorded vision_ready events; remote model hash unavailable unless supplied by producer',
                  'raw_camera_video_saved':False,'review_status':'unreviewed',
                  'environment_relation':'test_time_environment' if run.get('source')=='replay' else 'concurrent_gateway_received_time'}
        assets=[]
        if run.get('source')=='replay':
            path=self.root/'data/uploads'/ (run['upload_id']+'.video') if run.get('upload_id') else self.root/c['replay_file']
            if path.exists():assets.append({'path':str(path.relative_to(self.root)),'bytes':path.stat().st_size,'sha256':sha_file(path),'included_in_export':False})
        snapshot['assets']=assets
        with closing(self.store.connect()) as db:
            db.execute('INSERT OR IGNORE INTO experiment_snapshots VALUES(?,?)',(bid,encoded(snapshot).decode()));db.commit()

    def submit(self,kind,body):
        bid=body.get('collection_batch_id') or body.get('run_id')
        if not bid:return
        try:self.pending.put_nowait((bid,kind,encoded(body),int(time.time()*1000)))
        except (queue.Full,ValueError,TypeError) as e:self.fail(bid,'archive_queue_or_payload_error: '+type(e).__name__)

    def fail(self,bid,message):
        self.error=message
        try:
            with closing(self.store.connect()) as db:
                db.execute('INSERT OR REPLACE INTO experiment_errors VALUES(?,?)',(bid,message));db.commit()
        except Exception:pass

    def _writer(self):
        while True:
            item=self.pending.get()
            try:
                if item is None:return
                bid,kind,payload,received=item
                if shutil.disk_usage(self.store.path.parent).free<256*1024*1024:raise OSError('low_disk_space')
                body=json.loads(payload)
                key=(str(body.get('producer_instance_id'))+':'+str(body.get('observation_seq'))) if kind=='observation' and body.get('observation_seq') is not None else hashlib.sha256(payload).hexdigest()
                if kind=='observation':
                    body={k:v for k,v in body.items() if k not in ('project','run_id','collection_batch_id','collection_location','collection_name','source_zone_id','recognition_region','confidence_range')}
                record={'schema_version':'1.0','batch_id':bid,'record_type':kind,'record_id':hashlib.sha256((bid+kind+key).encode()).hexdigest(),'received_at_ms':received,'review_status':'unreviewed','payload':body}
                with closing(self.store.connect()) as db:
                    db.execute('INSERT OR IGNORE INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',(bid,kind,key,received,encoded(record).decode()));db.commit()
            except Exception as e:self.fail(item[0],str(e))
            finally:self.pending.task_done()

    def close(self):
        self.pending.join()
        self.pending.put(None)
        self.thread.join(timeout=5)

    def summary(self,bid):
        b=self.store.get(bid)
        stats={};n=0;max_people=0;crossings=0;size=0
        with closing(self.store.connect()) as db:
            snapshot=db.execute('SELECT json FROM experiment_snapshots WHERE batch_id=?',(bid,)).fetchone()
            error=db.execute('SELECT error FROM experiment_errors WHERE batch_id=?',(bid,)).fetchone()
            for row in db.execute('SELECT raw_json FROM batch_environment WHERE batch_id=?',(bid,)):
                r=json.loads(row[0])
                for field,v in r.get('values',{}).items():
                    if type(v) not in (int,float) or not math.isfinite(v):continue
                    key=r['sensor_id']+'/'+field;s=stats.setdefault(key,{'count':0,'sum':0,'min':v,'max':v})
                    s['count']+=1;s['sum']+=v;s['min']=min(s['min'],v);s['max']=max(s['max'],v)
            for row in db.execute('SELECT kind,json FROM experiment_records WHERE batch_id=?',(bid,)):
                size+=len(row['json'].encode('utf-8'))
                if row['kind']=='observation':
                    o=json.loads(row['json'])['payload'];n+=1;max_people=max(max_people,len(o.get('tracks',[])));crossings+=len(o.get('crossing_events',[]))
        for s in stats.values():s['mean']=s.pop('sum')/s['count']
        return {'schema_version':'1.0','summary_algorithm':'1.0','batch':b,'conditions':json.loads(snapshot[0]) if snapshot else None,
                'environment_statistics':stats,'statistics_basis':'finite numeric reported values, by sensor; sample mean, not time weighted or calibration validated',
                'observation_count':n,'max_people_in_received_frame':max_people,'crossing_events':crossings,'record_json_bytes':size,
                'archive_error':error[0] if error else None,'coverage':'received observations only; no guarantee of all source frames',
                'historical_missing':snapshot is None,'unique_person_count':None}

    def export(self,bid):
        if self.store.get(bid)['status']=='active':raise ValueError('请结束实验后再导出完整数据包')
        self.pending.join()
        summary=self.summary(bid)
        folder=Path(tempfile.mkdtemp(prefix='experiment-'))
        try:
            with closing(self.store.connect()) as db:
                db.execute('BEGIN')
                for filename,table,col in [('environment.jsonl','batch_environment','raw_json'),('observations.jsonl','experiment_records','json'),('events.jsonl','experiment_records','json')]:
                    query=f'SELECT {col} FROM {table} WHERE batch_id=?';params=[bid]
                    if table=='experiment_records':query+=' AND kind=?';params.append('observation' if filename.startswith('observations') else 'event')
                    query+=' ORDER BY id'
                    with (folder/filename).open('wb') as f:
                        for row in db.execute(query,params):
                            value=json.loads(row[0])
                            if table=='batch_environment':value={'schema_version':'1.0','batch_id':bid,'record_type':'environment','payload':value}
                            f.write(encoded(value)+b'\n')
            for filename,value in [('experiment.json',summary['conditions'] or {'schema_version':'1.0','historical_missing':True,'batch':summary['batch']}),('summary.json',summary),('record_schema.json',{'$schema':'https://json-schema.org/draft/2020-12/schema','title':'Experiment record envelope v1','type':'object','required':['schema_version','batch_id','record_type','payload'],'properties':{'schema_version':{'const':'1.0'},'batch_id':{'type':'string'},'record_type':{'enum':['observation','event','environment']},'payload':{'type':'object'}},'additionalProperties':True}),('assets_manifest.json',{'schema_version':'1.0','assets':(summary['conditions'] or {}).get('assets',[]),'media_included':False})]:
                (folder/filename).write_bytes(encoded(value))
            (folder/'checksums.json').write_bytes(encoded({p.name:sha_file(p) for p in folder.iterdir()}))
            target=folder/'experiment.zip'
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                for p in folder.iterdir():
                    if p!=target:z.write(p,p.name)
            return target
        except Exception:
            shutil.rmtree(folder);raise
