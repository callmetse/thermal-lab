"""Persistent collection-place/batch metadata and immutable environment snapshots.

The sensor database is opened read-only. Association uses gateway reception time
on the same Pi, not an assumed synchronization with the ESP32 sampling clock.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time


class CollectionBatches:
    def __init__(self, path, source_db, clock=None, mark_interrupted=True):
        self.path=Path(path);self.source=Path(source_db)
        self.clock=clock or (lambda:int(time.time()*1000))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS batches(
                batch_id TEXT PRIMARY KEY,run_id TEXT UNIQUE NOT NULL,location TEXT NOT NULL,
                name TEXT NOT NULL,zone_id TEXT NOT NULL,started_ms INTEGER NOT NULL,
                ended_ms INTEGER,status TEXT NOT NULL,source_cursor INTEGER DEFAULT 0,
                last_sync_ms INTEGER,capture_error TEXT,run_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batch_environment(
                id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id TEXT NOT NULL,event_id TEXT NOT NULL,
                sensor_id TEXT NOT NULL,data_time_ms INTEGER,received_at_ms INTEGER,
                raw_json TEXT NOT NULL,UNIQUE(batch_id,event_id));
                CREATE INDEX IF NOT EXISTS idx_batch_environment ON batch_environment(batch_id,id);''')
            columns={row[1] for row in db.execute('PRAGMA table_info(batches)')}
            for name,definition in [('favorite','INTEGER NOT NULL DEFAULT 0'),('deleted_ms','INTEGER'),
                                    ('channel','TEXT'),('execution_status','TEXT'),('archive_status','TEXT'),
                                    ('data_quality','TEXT'),('ended_reason','TEXT'),('quality_detail','TEXT')]:
                if name not in columns:db.execute('ALTER TABLE batches ADD COLUMN '+name+' '+definition)
            # 回填历史批次的通道（ui_channel 优先，其次 arm）
            db.execute('''UPDATE batches SET channel=COALESCE(json_extract(run_json,'$.ui_channel'),
                          json_extract(run_json,'$.arm')) WHERE channel IS NULL''')
            db.commit()
            # R3：恢复动作不能藏在构造函数里——数据面需要先读到 active 批次才能接管。
            # 需要旧语义（放弃未完成批次）的调用方显式叫 recover_active_batches()。
            if mark_interrupted:
                self.recover_active_batches('process_restart')

    def recover_active_batches(self, reason, keep_latest=False):
        """把遗留 active 批次标为 interrupted，返回改动条数。

        keep_latest=True 时保留最新一条 active（接管用），只处理更早的残留。
        """
        with closing(self.connect()) as db:
            rows=db.execute("SELECT batch_id FROM batches WHERE status='active' ORDER BY started_ms DESC").fetchall()
            ids=[row[0] for row in rows]
            if keep_latest:
                ids=ids[1:]
            if not ids:return 0
            for batch_id in ids:
                db.execute("UPDATE batches SET status='interrupted',ended_ms=COALESCE(last_sync_ms,started_ms),"
                           "execution_status='interrupted',ended_reason=? WHERE batch_id=?",(reason,batch_id))
            db.commit()
            return len(ids)

    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=5)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def source_connection(self):
        db=sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
        db.row_factory=sqlite3.Row
        return db

    def start(self, run, started_ms=None):
        run_id=run['run_id'];batch_id=run.get('collection_batch_id') or run_id
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',batch_id):raise ValueError('Invalid batch id')
        location=run.get('collection_location','').strip() or '未声明'
        name=run.get('collection_name','').strip() or '视觉与环境同步采集'
        zone=run.get('source_zone_id','lab')
        channel=run.get('ui_channel') or run.get('arm')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',zone):raise ValueError('Invalid source zone')
        if len(location)>160 or len(name)>160:raise ValueError('Location/name too long')
        now=self.clock() if started_ms is None else started_ms
        cursor,error=0,None
        try:
            with closing(self.source_connection()) as src:
                cursor=src.execute('SELECT COALESCE(MAX(rowid),0) FROM measurements WHERE received_at_ms < ?', (now,)).fetchone()[0]
        except (sqlite3.Error,OSError):
            error='environment_source_unavailable'
        with closing(self.connect()) as db:
            db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                          source_cursor,last_sync_ms,capture_error,run_json,channel)
                          VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)''',
                       (batch_id,run_id,location,name,zone,now,cursor,None if error else now,error,
                        json.dumps(run,ensure_ascii=False),channel))
            db.commit()
        return batch_id

    def sync(self,batch_id,until_ms=None):
        now=self.clock() if until_ms is None else until_ms
        with closing(self.connect()) as db:
            b=db.execute('SELECT * FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
            if not b:raise KeyError(batch_id)
            # Stop new archival writes explicitly on low disk instead of filling
            # the Pi and taking down sensing/vision. Existing batch data stays.
            if shutil.disk_usage(self.path.parent).free<256*1024*1024:
                db.execute("UPDATE batches SET capture_error='low_disk_space' WHERE batch_id=?",(batch_id,));db.commit();return 0
            try:
                with closing(self.source_connection()) as src:
                    rows=src.execute('''SELECT rowid AS source_rowid,* FROM measurements
                       WHERE rowid>? AND received_at_ms>=? AND received_at_ms<=? AND zone_id=?
                       ORDER BY rowid LIMIT 2000''',(b['source_cursor'],b['started_ms'],now,b['zone_id'])).fetchall()
                for row in rows:
                    raw=dict(row);raw.pop('source_rowid')
                    raw['values']=json.loads(raw.pop('values_json'));raw['units']=json.loads(raw.pop('units_json'))
                    db.execute('''INSERT OR IGNORE INTO batch_environment
                        (batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) VALUES(?,?,?,?,?,?)''',
                        (batch_id,raw['event_id'],raw['sensor_id'],raw['data_time_ms'],raw['received_at_ms'],json.dumps(raw,ensure_ascii=False)))
                cursor=max([b['source_cursor']]+[r['source_rowid'] for r in rows])
                db.execute('UPDATE batches SET source_cursor=?,last_sync_ms=?,capture_error=NULL WHERE batch_id=?',(cursor,now,batch_id))
                db.commit();return len(rows)
            except (sqlite3.Error,OSError,ValueError) as e:
                db.execute('UPDATE batches SET capture_error=? WHERE batch_id=?',('environment_source_unavailable: '+type(e).__name__,batch_id));db.commit();return 0

    def finish(self,batch_id,reason=None,archive_error=None,ingest_dropped=0,execution_status=None,
               delivery_uncertain=False,ended_ms=None):
        """会话结束 ≠ 数据完整。这里把三件事分开记：
        execution_status（会话怎么结束的）/ archive_status（归档是否完整）/ data_quality（数据够不够用）。
        """
        now=self.clock() if ended_ms is None else ended_ms
        # 先记下结束前看到的错误：sync 成功会把 capture_error 清成 NULL，
        # 但"这次会话里发生过丢数据"不能因此被抹掉。
        with closing(self.connect()) as db:
            before=db.execute('SELECT capture_error FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
        pre_error=before['capture_error'] if before else None
        for _ in range(100):
            if self.sync(batch_id,now)<2000:break
        with closing(self.connect()) as db:
            row=db.execute('SELECT capture_error FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
            env_rows=db.execute('SELECT COUNT(*) FROM batch_environment WHERE batch_id=?',(batch_id,)).fetchone()[0]
            try:
                observations=db.execute("SELECT COUNT(*) FROM experiment_records WHERE batch_id=? AND kind='observation'",(batch_id,)).fetchone()[0]
                # R5 待办：门控有阳性样本却 0 条观测 → 云端没有产出，必须显式标出来。
                # 依据是批次里已有的 transport_stats 事件，不新增采集链路。
                gate_positive=None
                for event_row in db.execute("SELECT json FROM experiment_records WHERE batch_id=? AND kind='event'",(batch_id,)):
                    try:
                        payload=json.loads(event_row[0]).get('payload') or {}
                    except (TypeError,ValueError):
                        continue
                    if payload.get('kind')=='transport_stats' and 'gate_positive' in payload:
                        gate_positive=int(payload.get('gate_positive') or 0)
            except sqlite3.OperationalError:
                observations=None   # 归档表还没建：不拿"未知"当 0 判质量
                gate_positive=None
            current_error=row['capture_error'] if row else None
            capture_error=current_error or (pre_error if not str(pre_error).startswith('environment_source_unavailable') else None)
            environment_unavailable=str(capture_error).startswith('environment_source_unavailable')
            execution=execution_status or ('interrupted' if reason=='interrupted' else (
                'failed' if (capture_error and not environment_unavailable) or reason=='failed' else 'completed'))
            # R6：0 观测不能被判 good（会话跑了但没结果），投递丢失也不能当完整
            empty=observations==0
            cloud_missing=bool(empty and (gate_positive or 0)>0)
            incomplete=bool(env_rows==0 or ingest_dropped or environment_unavailable or delivery_uncertain)
            archive='error' if archive_error else ('partial' if incomplete or empty else 'ok')
            if (capture_error and not environment_unavailable) or archive_error:quality='poor'
            elif empty:quality='poor'
            elif incomplete:quality='degraded'
            else:quality='good'
            detail_dict={'capture_error':capture_error,'archive_error':archive_error,
                         'environment_rows':env_rows,'observations':observations,
                         'ingest_dropped':ingest_dropped,'gate_positive':gate_positive,'reason':reason,
                         'environment_unavailable':environment_unavailable,'delivery_uncertain':delivery_uncertain}
            if empty:detail_dict['no_observations']=True
            if cloud_missing:detail_dict['cloud_observations_missing']=True
            detail=json.dumps(detail_dict,ensure_ascii=False)
            db.execute("""UPDATE batches SET ended_ms=?,status='completed',execution_status=?,archive_status=?,
                          data_quality=?,ended_reason=?,quality_detail=? WHERE batch_id=? AND status='active'""",
                       (now,execution,archive,quality,reason,detail,batch_id));db.commit()
        return {'execution_status':execution,'archive_status':archive,'data_quality':quality}

    def list(self,limit=100,view="all"):
        if not 1<=limit<=1000:raise ValueError('limit must be 1..1000')
        if view not in ('all','favorites','trash'):raise ValueError('Invalid view')
        clause={'all':'b.deleted_ms IS NULL','favorites':'b.deleted_ms IS NULL AND b.favorite=1','trash':'b.deleted_ms IS NOT NULL'}[view]
        with closing(self.connect()) as db:
            rows=db.execute(f'''SELECT b.*,(SELECT COUNT(*) FROM batch_environment e WHERE e.batch_id=b.batch_id) AS environment_rows
                 FROM batches b WHERE {clause} ORDER BY started_ms DESC LIMIT ?''',(limit,)).fetchall()
        result=[]
        for row in rows:
            d=dict(row);d['run']=json.loads(d.pop('run_json'));d['association_basis']='gateway_received_at_ms_on_same_Pi'
            result.append(d)
        return result

    def list_filtered(self,query=None,view="all",channel=None,since_ms=None,until_ms=None,limit=1000,offset=0):
        """服务端筛选与分页：日期/地点/通道不再依赖浏览器过滤。"""
        if not 1<=limit<=1000:raise ValueError('limit must be 1..1000')
        if type(offset) is not int or offset<0:raise ValueError('offset must be >=0')
        if view not in ('all','favorites','trash'):raise ValueError('Invalid view')
        where={'all':['b.deleted_ms IS NULL'],'favorites':['b.deleted_ms IS NULL','b.favorite=1'],
               'trash':['b.deleted_ms IS NOT NULL']}[view]
        args=[]
        if query:
            where.append('(b.location LIKE ? OR b.name LIKE ?)');like='%'+str(query).strip()+'%';args+= [like,like]
        if channel:
            where.append('b.channel=?');args.append(str(channel))
        if since_ms is not None:
            where.append('b.started_ms>=?');args.append(int(since_ms))
        if until_ms is not None:
            where.append('b.started_ms<?');args.append(int(until_ms))
        clause=' AND '.join(where)
        with closing(self.connect()) as db:
            total=db.execute(f'SELECT COUNT(*) FROM batches b WHERE {clause}',args).fetchone()[0]
            rows=db.execute(f'''SELECT b.*,(SELECT COUNT(*) FROM batch_environment e WHERE e.batch_id=b.batch_id)
                 AS environment_rows FROM batches b WHERE {clause} ORDER BY started_ms DESC LIMIT ? OFFSET ?''',
                 (*args,limit,offset)).fetchall()
        result=[]
        for row in rows:
            d=dict(row);d['run']=json.loads(d.pop('run_json'))
            d['association_basis']='gateway_received_at_ms_on_same_Pi';result.append(d)
        return {'batches':result,'total':total,'limit':limit,'offset':offset,
                'has_more':offset+len(result)<total,'view':view}

    def get(self,batch_id):
        with closing(self.connect()) as db:
            row=db.execute('SELECT * FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
            if not row:raise KeyError(batch_id)
            result=dict(row);result['run']=json.loads(result.pop('run_json'))
            result['environment_rows']=db.execute('SELECT COUNT(*) FROM batch_environment WHERE batch_id=?',(batch_id,)).fetchone()[0]
            result['max_archive_id']=db.execute('SELECT COALESCE(MAX(id),0) FROM batch_environment WHERE batch_id=?',(batch_id,)).fetchone()[0]
        result['association_basis']='gateway_received_at_ms_on_same_Pi'
        return result

    def environment(self,batch_id,after_id=0,limit=1000):
        if not 1<=limit<=10000 or after_id<0:raise ValueError('Invalid pagination')
        batch=self.get(batch_id)
        with closing(self.connect()) as db:
            rows=db.execute('SELECT id,raw_json FROM batch_environment WHERE batch_id=? AND id>? ORDER BY id LIMIT ?',
                            (batch_id,after_id,limit)).fetchall()
        records=[{'archive_id':r['id'],**json.loads(r['raw_json'])} for r in rows]
        return {'batch':batch,'rows':records,'count':len(records),
                'next_after_id':rows[-1]['id'] if rows else after_id,
                'has_more':bool(rows) and len(rows)==limit}

    def manage(self,action,batch_ids):
        if action not in ('favorite','unfavorite','delete','restore'):raise ValueError('未知操作')
        if not isinstance(batch_ids,list) or not 1<=len(batch_ids)<=1000 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',x) for x in batch_ids):raise ValueError('请选择 1–1000 个有效批次')
        ids=list(dict.fromkeys(batch_ids));marks=','.join('?' for _ in ids)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute(f'SELECT batch_id,status,deleted_ms FROM batches WHERE batch_id IN ({marks})',ids).fetchall()
            if len(rows)!=len(ids):raise KeyError('有批次不存在，请刷新列表')
            if action=='delete' and any(r['status']=='active' for r in rows):raise RuntimeError('所选批次包含正在采集的实验，请先停止；本次未修改任何记录')
            if action in ('favorite','unfavorite') and any(r['deleted_ms'] is not None for r in rows):raise RuntimeError('请先恢复回收站记录再收藏')
            if action=='delete':db.execute(f'UPDATE batches SET deleted_ms=COALESCE(deleted_ms,?) WHERE batch_id IN ({marks})',[self.clock(),*ids])
            elif action=='restore':db.execute(f'UPDATE batches SET deleted_ms=NULL WHERE batch_id IN ({marks})',ids)
            else:db.execute(f'UPDATE batches SET favorite=? WHERE batch_id IN ({marks})',[int(action=='favorite'),*ids])
            db.commit()
        return {'action':action,'count':len(ids),'batch_ids':ids}
