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
    def __init__(self, path, source_db, clock=None):
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
            for name,definition in [('favorite','INTEGER NOT NULL DEFAULT 0'),('deleted_ms','INTEGER')]:
                if name not in columns:db.execute('ALTER TABLE batches ADD COLUMN '+name+' '+definition)
            # A process restart ends an unfinished batch at its last known sync,
            # rather than silently associating the entire outage with that place.
            db.execute("UPDATE batches SET status='interrupted',ended_ms=COALESCE(last_sync_ms,started_ms) WHERE status='active'")
            db.commit()

    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=5)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def source_connection(self):
        db=sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
        db.row_factory=sqlite3.Row
        return db

    def start(self, run):
        run_id=run['run_id'];batch_id=run.get('collection_batch_id') or run_id
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',batch_id):raise ValueError('Invalid batch id')
        location=run.get('collection_location','').strip() or '未声明'
        name=run.get('collection_name','').strip() or '视觉与环境同步采集'
        zone=run.get('source_zone_id','lab')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',zone):raise ValueError('Invalid source zone')
        if len(location)>160 or len(name)>160:raise ValueError('Location/name too long')
        now=self.clock()
        with closing(self.source_connection()) as src:
            cursor=src.execute('SELECT COALESCE(MAX(rowid),0) FROM measurements WHERE received_at_ms < ?', (now,)).fetchone()[0]
        with closing(self.connect()) as db:
            db.execute('''INSERT INTO batches(batch_id,run_id,location,name,zone_id,started_ms,status,
                          source_cursor,last_sync_ms,run_json) VALUES(?,?,?,?,?,?,'active',?,?,?)''',
                       (batch_id,run_id,location,name,zone,now,cursor,now,json.dumps(run,ensure_ascii=False)))
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
                db.execute('UPDATE batches SET capture_error=? WHERE batch_id=?',(type(e).__name__+': '+str(e),batch_id));db.commit();return 0

    def finish(self,batch_id):
        now=self.clock()
        # Drain bounded pages, so a briefly delayed poll does not truncate a batch.
        for _ in range(100):
            if self.sync(batch_id,now)<2000:break
        with closing(self.connect()) as db:
            db.execute("UPDATE batches SET ended_ms=?,status='completed' WHERE batch_id=? AND status='active'",(now,batch_id));db.commit()

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
