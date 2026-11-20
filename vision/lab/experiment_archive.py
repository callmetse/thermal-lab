"""Local-only experiment records. No network client or media retransmission."""
from contextlib import closing
from .aggregation import RULE as AGGREGATION_RULE, is_valid
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

from .dataset_settings import human_bytes, read_settings
from .model_manifest import manifest_id as compute_manifest_id
from .record_schema import HAS_JSONSCHEMA, RecordInvalid, schema_bundle, validate as validate_record

# 导出包与开始快照都要登记"当时跑的是哪份代码"，清单化才可核对。
SOFTWARE_FILES = ('lab/worker.py', 'lab/experiment_archive.py', 'lab/dataset_api.py',
                  'lab/local_preview.py', 'lab/video_uploads.py', 'lab/record_schema.py',
                  'lab/model_manifest.py', 'lab/collection_batches.py', 'lab/archive_ingest.py',
                  'lab/dataset_settings.py', 'lab/aggregation.py')


def coverage_declaration():
    """声明这个包没保存什么（P2-10 的 coverage）：不声明就等于暗示完整。"""
    return {
        'raw_camera_video': False,
        'raw_camera_video_note': '摄像头原片不保存；replay 线路只登记上传素材的 sha256',
        'observations': '仅已收到的观测；负载下丢的帧不会出现在包里',
        'cloud_events': '由生产者声明；不保证云端每个事件都被归档',
        'llm_outputs': '仅在生产者把它作为事件发出时才有',
        'environment': '批次时间窗内由 Pi 网关收到的行',
        'schema_validation': '非法 payload 被隔离在 experiment_errors，不写入正式记录',
    }

# P1-07：导出的容量与并发边界。临时目录要装下 JSONL+ZIP，故按估算 ×3 预留；
# 同一进程只允许一个导出（Semaphore 非阻塞获取），第二个直接报忙而不是排队堵线程。
EXPORT_SPACE_FACTOR = 3
EXPORT_OVERHEAD_BYTES = 2 * 1024 * 1024
EXPORT_SEMAPHORE = threading.Semaphore(1)
# 写线程停止标记：用哨兵对象而不是 None，避免和正常数据混淆。
_STOP = object()


class ExportBusy(ValueError):
    """已有导出正在进行（ValueError 子类，路由层按 409 返回）。"""


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def sha_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

class ExperimentArchive:
    BATCH_SIZE=50
    BATCH_INTERVAL_S=0.2

    def __init__(self,store,root):
        self.store,self.root=store,Path(root)
        self.pending=queue.Queue(512);self.error=None
        # P2-11：写队列水位与批量提交计数，供 /summary 与现场排障读取。
        self.stats={'records':0,'errors':0,'commits':0,'dropped':0,'write_errors':0,'queue_high_water':0,'rejected':0}
        with closing(store.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS experiment_snapshots(batch_id TEXT PRIMARY KEY,json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiment_records(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,received_ms INTEGER NOT NULL,json TEXT NOT NULL,UNIQUE(batch_id,kind,key));
            CREATE INDEX IF NOT EXISTS experiment_records_batch ON experiment_records(batch_id,id);
            CREATE TABLE IF NOT EXISTS experiment_errors(batch_id TEXT PRIMARY KEY,error TEXT NOT NULL);''')
        self.thread=threading.Thread(target=self._writer,daemon=True);self.thread.start()

    def begin(self,run):
        bid=run.get('collection_batch_id',run['run_id'])
        c=run.get('effective_settings')
        if not isinstance(c,dict):
            c=read_settings(self.root)
        keys=['sample_fps','capture_fps','width','height','encode_max_side','jpeg_quality','replay_file','llm_model','min_person_confidence','max_people','pose_max_det','pose_imgsz','gate_imgsz','rect','camera_backend','collaboration','hard_examples']
        # P2-10：模型清单随会话上下文进来；没有就如实标 unavailable，绝不伪造。
        manifest=run.get('model_manifest') if isinstance(run.get('model_manifest'),dict) else None
        if manifest is not None and manifest.get('models') is not None:
            manifest_id=compute_manifest_id(manifest)
            model_block={'manifest_id':manifest_id,'role':manifest.get('role'),'device':manifest.get('device'),
                         'generated_at_ms':manifest.get('generated_at_ms'),'models':manifest.get('models',[]),
                         'frameworks':manifest.get('frameworks',{}),'params':manifest.get('params',{}),
                         'provenance_file':manifest.get('provenance_file'),
                         'assets_provenance_sha256':manifest.get('assets_provenance_sha256')}
            provenance='worker model_manifest '+manifest_id
        else:
            manifest_id=None;model_block=None
            provenance='See recorded vision_ready events; remote model hash unavailable unless supplied by producer'
        snapshot={'schema_version':'1.0','batch_id':bid,'run':run,'settings':{k:c.get(k) for k in keys},
                  'software':{p:sha_file(self.root/p) for p in SOFTWARE_FILES if (self.root/p).exists()},
                  'model_provenance':provenance,'model_manifest':model_block,'model_manifest_id':manifest_id,
                  'coverage':coverage_declaration(),
                  'raw_camera_video_saved':False,'review_status':'unreviewed',
                  'environment_relation':'test_time_environment' if run.get('source')=='replay' else 'concurrent_gateway_received_time'}
        assets=[]
        if run.get('source')=='replay':
            if run.get('upload_id'):
                path=self.root/'data/uploads'/(run['upload_id']+'.video')
            else:
                path=self.root/c.get('replay_file','data/replay.mp4')
            if path.is_file() and path.resolve().is_relative_to(self.root.resolve()):
                # P2-11：素材 sha256 复用上传时算好的值，别为了记哈希把整段视频再读一遍。
                meta={}
                meta_path=path.with_suffix('.json')
                if meta_path.is_file():
                    try:meta=json.loads(meta_path.read_text(encoding='utf-8'))
                    except (OSError,ValueError):meta={}
                digest=meta.get('sha256')
                digest=digest if isinstance(digest,str) and len(digest)==64 else None
                assets.append({'path':str(path.relative_to(self.root)),'bytes':path.stat().st_size,
                               'sha256':digest or sha_file(path),
                               'sha256_source':'upload_metadata' if digest else 'recomputed',
                               'included_in_export':False})
        snapshot['assets']=assets
        with closing(self.store.connect()) as db:
            db.execute('INSERT OR IGNORE INTO experiment_snapshots VALUES(?,?)',(bid,encoded(snapshot).decode()));db.commit()

    def submit(self,kind,body):
        bid=body.get('collection_batch_id') or body.get('run_id')
        if not bid:return
        try:self.pending.put_nowait((bid,kind,encoded(body),int(time.time()*1000)))
        except (queue.Full,ValueError,TypeError) as e:self.fail(bid,'archive_queue_or_payload_error: '+type(e).__name__)
        depth=self.pending.qsize()
        if depth>self.stats['queue_high_water']:self.stats['queue_high_water']=depth

    def persist_delivery(self, items):
        """Commit validated records and quarantine outcomes before sending an HTTP receipt."""
        if shutil.disk_usage(self.store.path.parent).free < 256*1024*1024:
            raise OSError('low_disk_space')
        records, errors = [], []
        with closing(self.store.connect()) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for kind, body in items:
                run_id = body.get('run_id')
                batch = db.execute('SELECT batch_id,status FROM batches WHERE run_id=?', (run_id,)).fetchone()
                if not batch or batch['status'] != 'active':
                    raise KeyError('record does not match an active batch')
                bid = batch['batch_id']
                supplied = body.get('collection_batch_id')
                if supplied and supplied not in (bid, run_id):
                    raise KeyError('record batch identifier does not match its run')
                try:
                    records.append(self._record(bid,kind,encoded(body),int(time.time()*1000)))
                except (ValueError, TypeError) as invalid:
                    errors.append((bid,'schema_invalid: '+str(invalid)[:200]))
            db.executemany(
                'INSERT OR IGNORE INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)', records)
            db.executemany('INSERT OR REPLACE INTO experiment_errors VALUES(?,?)',errors)
        return {'accepted':len(records),'rejected':len(errors)}

    def fail(self,bid,message):
        """只投递，不在调用线程碰 SQLite：调用方可能是 MQTT 回调线程。"""
        self.error=message
        try:
            self.pending.put_nowait((bid,'__error__',message,0))
        except queue.Full:
            self.stats['dropped']+=1

    def flush(self,timeout=2.0):
        """等写队列落盘（含未满一批的尾巴）。需要 read-your-writes 的调用方用它。"""
        done=threading.Event()
        try:self.pending.put((None,'__flush__',done,0),timeout=timeout)
        except queue.Full:return False
        return done.wait(timeout)

    def _record(self,bid,kind,payload,received):
        body=json.loads(payload)
        validate_record(kind,body)   # P2-10：非法记录抛 RecordInvalid，由写线程隔离
        key=(str(body.get('producer_instance_id'))+':'+str(body.get('observation_seq'))) if kind=='observation' and body.get('observation_seq') is not None else hashlib.sha256(payload).hexdigest()
        if kind=='observation':
            body={k:v for k,v in body.items() if k not in ('project','run_id','collection_batch_id','collection_location','collection_name','source_zone_id','recognition_region','confidence_range')}
        record={'schema_version':'1.0','batch_id':bid,'record_type':kind,'record_id':hashlib.sha256((bid+kind+key).encode()).hexdigest(),'received_at_ms':received,'review_status':'unreviewed','payload':body}
        return (bid,kind,key,received,encoded(record).decode())

    def _flush(self,records,errors):
        """一次连接、一次 executemany、一次 commit 写完一批；失败不整批丢。"""
        if not records and not errors:return
        try:
            if shutil.disk_usage(self.store.path.parent).free<256*1024*1024:raise OSError('low_disk_space')
        except OSError:
            for bid,kind,key,received,doc in records:errors.append((bid,'low_disk_space'))
            records=[]
            self.stats['errors']+=1
        try:
            with closing(self.store.connect()) as db:
                if records:db.executemany('INSERT OR IGNORE INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',records)
                if errors:db.executemany('INSERT OR REPLACE INTO experiment_errors VALUES(?,?)',errors)
                db.commit()
            self.stats['commits']+=1
            self.stats['records']+=len(records)
            if errors:self.stats['errors']+=len(errors)
        except Exception as e:
            # 批量写失败时逐条重试：慢盘/写锁抖动不能把一整批安静地吞掉。
            self.error='batch_write_failed: '+type(e).__name__+': '+str(e)
            self.stats['write_errors']+=1
            self._flush_individually(records,errors)

    def _flush_individually(self,records,errors):
        try:
            with closing(self.store.connect()) as db:
                for row in records:
                    try:db.execute('INSERT OR IGNORE INTO experiment_records(batch_id,kind,key,received_ms,json) VALUES(?,?,?,?,?)',row)
                    except Exception:errors.append((row[0],'single_write_failed'))
                for bid,message in errors:
                    try:db.execute('INSERT OR REPLACE INTO experiment_errors VALUES(?,?)',(bid,message))
                    except Exception:pass
                db.commit()
        except Exception as e:self.error='archive_write_unavailable: '+type(e).__name__+': '+str(e)

    def _writer(self):
        records=[];errors=[];last=time.monotonic()
        while True:
            try:
                item=self.pending.get(timeout=self.BATCH_INTERVAL_S)
            except queue.Empty:
                if records or errors:
                    self._flush(records,errors);records=[];errors=[];last=time.monotonic()
                continue
            try:
                if item is _STOP:
                    self._flush(records,errors)
                    return
                bid,kind,payload,received=item
                if kind=='__flush__':
                    self._flush(records,errors);records=[];errors=[];last=time.monotonic()
                    if isinstance(payload,threading.Event):payload.set()
                elif kind=='__error__':
                    errors.append((bid,payload))
                else:
                    try:records.append(self._record(bid,kind,payload,received))
                    except RecordInvalid as invalid:
                        # 非法记录隔离到 experiment_errors，绝不写进正式记录、也不静默丢
                        errors.append((bid,'schema_invalid: '+str(invalid)[:200]))
                        self.stats['rejected']+=1
            except Exception as e:
                self.error='record_encode_failed: '+type(e).__name__+': '+str(e)
                self.stats['write_errors']+=1
            finally:
                self.pending.task_done()
            if len(records)>=self.BATCH_SIZE or errors or (records and time.monotonic()-last>=self.BATCH_INTERVAL_S):
                self._flush(records,errors);records=[];errors=[];last=time.monotonic()

    def close(self):
        self.pending.join()
        self.pending.put(_STOP)
        self.thread.join(timeout=5)

    def summary(self,bid,max_record_id=None,max_env_id=None):
        self.flush()
        b=self.store.get(bid)
        stats={};n=0;max_people=0;crossings=0;size=0
        with closing(self.store.connect()) as db:
            snapshot=db.execute('SELECT json FROM experiment_snapshots WHERE batch_id=?',(bid,)).fetchone()
            error=db.execute('SELECT error FROM experiment_errors WHERE batch_id=?',(bid,)).fetchone()
            env_rows=0;excluded=0
            env_sql='SELECT raw_json FROM batch_environment WHERE batch_id=?'
            env_args=[bid]
            if max_env_id is not None:env_sql+=' AND id<=?';env_args.append(int(max_env_id))
            for row in db.execute(env_sql,env_args):
                r=json.loads(row[0]);env_rows+=1
                if not is_valid(r.get('quality')):excluded+=1;continue
                for field,v in r.get('values',{}).items():
                    if type(v) not in (int,float) or not math.isfinite(v):continue
                    key=r['sensor_id']+'/'+field;s=stats.setdefault(key,{'count':0,'sum':0,'min':v,'max':v})
                    s['count']+=1;s['sum']+=v;s['min']=min(s['min'],v);s['max']=max(s['max'],v)
            rec_sql='SELECT kind,json FROM experiment_records WHERE batch_id=?'
            rec_args=[bid]
            if max_record_id is not None:rec_sql+=' AND id<=?';rec_args.append(int(max_record_id))
            for row in db.execute(rec_sql,rec_args):
                size+=len(row['json'].encode('utf-8'))
                if row['kind']=='observation':
                    o=json.loads(row['json'])['payload'];n+=1;max_people=max(max_people,len(o.get('tracks',[])));crossings+=len(o.get('crossing_events',[]))
        for s in stats.values():s['mean']=s.pop('sum')/s['count']
        conditions=json.loads(snapshot[0]) if snapshot else None
        return {'schema_version':'1.0','summary_algorithm':'1.0','batch':b,'conditions':conditions,
                'environment_statistics':stats,'statistics_basis':'quality=ok 样本均值，未做时间加权与校准验证',
                'aggregation_rule':AGGREGATION_RULE,'environment_rows':env_rows,'excluded_by_quality':excluded,
                'observation_count':n,'max_people_in_received_frame':max_people,'crossing_events':crossings,'record_json_bytes':size,
                'archive_error':error[0] if error else None,
                'coverage':(conditions or {}).get('coverage') or coverage_declaration(),
                'model_manifest_id':(conditions or {}).get('model_manifest_id'),
                'model_provenance':(conditions or {}).get('model_provenance'),
                'rejected_records':self.stats['rejected'],'schema_engine':'jsonschema' if HAS_JSONSCHEMA else 'builtin',
                'snapshot_boundary':{'max_record_id':max_record_id,'max_env_id':max_env_id},
                'snapshot_version':'atomic-v1',
                'archive_writer':{'queued':self.pending.qsize(),'high_water':self.stats['queue_high_water'],
                                  'commits':self.stats['commits'],'records':self.stats['records'],
                                  'errors':self.stats['errors'],'dropped':self.stats['dropped'],
                                  'write_errors':self.stats['write_errors'],'rejected':self.stats['rejected'],
                                  'batch_size':self.BATCH_SIZE,'batch_interval_s':self.BATCH_INTERVAL_S},
                'historical_missing':snapshot is None,'unique_person_count':None}

    def _settle_boundary(self,bid,timeout=2.0):
        """等本批次写入稳定：连续两次读到相同的最大 id 即认为落定（最多 timeout 秒）。
        只关心本批次的表，不等待其他实验的全局队列。"""
        deadline=time.monotonic()+timeout;last=None
        while time.monotonic()<deadline:
            with closing(self.store.connect()) as db:
                rec=db.execute('SELECT COALESCE(MAX(id),0) FROM experiment_records WHERE batch_id=?',(bid,)).fetchone()[0]
                env=db.execute('SELECT COALESCE(MAX(id),0) FROM batch_environment WHERE batch_id=?',(bid,)).fetchone()[0]
            if (rec,env)==last:return rec,env
            last=(rec,env);time.sleep(.1)
        return last or (0,0)

    def estimate_export_bytes(self,bid,max_record_id=None,max_env_id=None):
        """导出体积估算：记录与环境 JSON 的字符数之和 + 固定开销（ZIP 压缩只会更小）。"""
        rec_sql='SELECT COALESCE(SUM(LENGTH(json)),0) FROM experiment_records WHERE batch_id=?';rec_args=[bid]
        env_sql='SELECT COALESCE(SUM(LENGTH(raw_json)),0) FROM batch_environment WHERE batch_id=?';env_args=[bid]
        if max_record_id is not None:rec_sql+=' AND id<=?';rec_args.append(int(max_record_id))
        if max_env_id is not None:env_sql+=' AND id<=?';env_args.append(int(max_env_id))
        with closing(self.store.connect()) as db:
            records=int(db.execute(rec_sql,rec_args).fetchone()[0])
            environment=int(db.execute(env_sql,env_args).fetchone()[0])
        return records+environment+EXPORT_OVERHEAD_BYTES

    def export(self,bid):
        if self.store.get(bid)['status']=='active':raise ValueError('请结束实验后再导出完整数据包')
        if not EXPORT_SEMAPHORE.acquire(blocking=False):
            raise ExportBusy('已有导出任务正在进行，请等它下载完再导出（并发上限 1）')
        folder=None
        try:
            max_record_id,max_env_id=self._settle_boundary(bid)
            summary=self.summary(bid,max_record_id=max_record_id,max_env_id=max_env_id)
            estimate=self.estimate_export_bytes(bid,max_record_id,max_env_id)
            required=estimate*EXPORT_SPACE_FACTOR
            free=shutil.disk_usage(tempfile.gettempdir()).free
            if free<required:
                raise ValueError('临时空间不足：预计需要 %s（含 ×%d 余量），当前可用 %s；请清理 /tmp 或稍后再试'
                                 % (human_bytes(required),EXPORT_SPACE_FACTOR,human_bytes(free)))
            folder=Path(tempfile.mkdtemp(prefix='experiment-'))
            with closing(self.store.connect()) as db:
                db.execute('BEGIN')
                for filename,table,col in [('environment.jsonl','batch_environment','raw_json'),('observations.jsonl','experiment_records','json'),('events.jsonl','experiment_records','json')]:
                    query=f'SELECT {col} FROM {table} WHERE batch_id=?';params=[bid]
                    if table=='experiment_records':
                        query+=' AND kind=? AND id<=?';params+=['observation' if filename.startswith('observations') else 'event',max_record_id]
                    else:
                        query+=' AND id<=?';params.append(max_env_id)
                    query+=' ORDER BY id'
                    with (folder/filename).open('wb') as f:
                        for row in db.execute(query,params):
                            value=json.loads(row[0])
                            if table=='batch_environment':value={'schema_version':'1.0','batch_id':bid,'record_type':'environment','payload':value}
                            f.write(encoded(value)+b'\n')
            conditions=summary['conditions'] or {}
            export_manifest={'schema_version':'1.0','batch_id':bid,
                             'snapshot_version':summary['snapshot_version'],'generated_at_ms':int(time.time()*1000),
                             'manifest_id':summary.get('model_manifest_id'),
                             'model':conditions.get('model_manifest'),
                             'model_provenance':conditions.get('model_provenance'),
                             'software_at_start':conditions.get('software',{}),
                             'software':{p:sha_file(self.root/p) for p in SOFTWARE_FILES if (self.root/p).exists()},
                             'config':conditions.get('settings',{}),
                             'environment_relation':conditions.get('environment_relation'),
                             'coverage':summary.get('coverage') or coverage_declaration(),
                             'validation':{'engine':summary.get('schema_engine'),'rejected_records':summary.get('rejected_records',0),
                                           'policy':'非法 payload 隔离在 experiment_errors，不写入正式记录'}}
            for filename,value in [('experiment.json',conditions or {'schema_version':'1.0','historical_missing':True,'batch':summary['batch']}),('summary.json',summary),('manifest.json',export_manifest),('record_schema.json',schema_bundle()),('assets_manifest.json',{'schema_version':'1.0','assets':conditions.get('assets',[]),'media_included':False})]:
                (folder/filename).write_bytes(encoded(value))
            (folder/'checksums.json').write_bytes(encoded({p.name:sha_file(p) for p in folder.iterdir()}))
            target=folder/'experiment.zip'
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                for p in folder.iterdir():
                    if p!=target:z.write(p,p.name)
            return target
        except Exception:
            if folder is not None:shutil.rmtree(folder,ignore_errors=True)
            raise
        finally:
            EXPORT_SEMAPHORE.release()
