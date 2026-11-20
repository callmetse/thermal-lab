import json,sqlite3,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from lab.collection_batches import CollectionBatches


class BatchTests(unittest.TestCase):
    def test_management_is_atomic_reversible_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.db'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE measurements(received_at_ms INTEGER)');db.commit()
            store=CollectionBatches(Path(td)/'b.db',source,clock=lambda:1000)
            for bid in ['one','two']:store.start({'run_id':bid})
            with closing(store.connect()) as db:
                db.execute("UPDATE batches SET status='completed' WHERE batch_id='one'");db.commit()
            with self.assertRaises(RuntimeError):store.manage('delete',['one','two'])
            self.assertIsNone(store.get('one')['deleted_ms'])
            with self.assertRaises(KeyError):store.manage('favorite',['one','missing'])
            self.assertEqual(store.get('one')['favorite'],0)
            store.manage('favorite',['one']);store.manage('delete',['one'])
            self.assertEqual(len(store.list(view='trash')),1)
            self.assertEqual(len(store.list(view='favorites')),0)
            store.manage('restore',['one'])
            self.assertEqual(store.list(view='favorites')[0]['batch_id'],'one')
            store.manage('unfavorite',['one'])
            self.assertFalse(store.list(view='favorites'))
            self.assertEqual(store.get('one')['run']['run_id'],'one')

    def test_complete_csv_streams_multiple_pages_and_escapes_labels(self):
        import asyncio,csv,io
        from types import SimpleNamespace
        from aiohttp import web
        from aiohttp.test_utils import TestServer,TestClient
        from lab.collection_routes import install
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.db'
            with closing(sqlite3.connect(str(source))) as db:
                db.execute('CREATE TABLE measurements(received_at_ms INTEGER)');db.commit()
            store=CollectionBatches(Path(td)/'b.db',source,clock=lambda:1000)
            store.start({'run_id':'stream','collection_location':'=SUM(1,2)'})
            with closing(store.connect()) as db:
                for i in range(2001):
                    raw={'event_id':str(i),'sensor_id':'sht4x','values':{'temperature_c':20},'quality':'ok'}
                    db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,data_time_ms,received_at_ms,raw_json) VALUES(?,?,?,?,?,?)',
                               ('stream',str(i),'sht4x',i,i,json.dumps(raw)))
                db.commit()
            async def check():
                app=web.Application()
                install(SimpleNamespace(batch_store=store,active_batch_id=None,batch_capture_error=None),app)
                async with TestClient(TestServer(app)) as client:
                    response=await client.get('/api/collection-batches/stream/environment?format=csv&all=1&limit=7')
                    self.assertEqual(response.status,200)
                    rows=list(csv.reader(io.StringIO((await response.text()).lstrip('\ufeff'))))
                    self.assertEqual(len(rows),2002)
                    self.assertEqual(rows[1][2],"'=SUM(1,2)")
                    response=await client.get('/api/collection-batches/stream/environment?limit=5')
                    self.assertEqual((await response.json())['count'],5)
            asyncio.run(check())

    def test_time_zone_snapshot_pagination_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.db';archive=Path(td)/'batches.db';now=[1000]
            with closing(sqlite3.connect(str(source))) as db:
                db.execute('CREATE TABLE measurements(event_id TEXT,device_id TEXT,sensor_id TEXT,zone_id TEXT,data_time_ms INTEGER,received_at_ms INTEGER,values_json TEXT,units_json TEXT,quality TEXT)')
                db.execute('INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?)',('before','e','sht4x','lab',900,900,'{}','{}','ok'));db.commit()
            store=CollectionBatches(archive,source,clock=lambda:now[0]);bid=store.start({'run_id':'r','collection_location':'实验室A','collection_name':'第一批'})
            with closing(sqlite3.connect(str(source))) as db:
                for eid,sensor,zone in [('one','sht4x','lab'),('two','scd41','lab'),('foreign','sht4x','other')]:
                    db.execute('INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?)',(eid,'e',sensor,zone,1001,1001,json.dumps({'temperature_c':20}),json.dumps({'temperature_c':'degC'}),'ok'))
                db.commit()
            now[0]=1010;self.assertEqual(store.sync(bid),2);self.assertEqual(store.sync(bid),0)
            page=store.environment(bid,limit=1);self.assertEqual(page['count'],1);self.assertTrue(page['has_more'])
            self.assertEqual(store.environment(bid,after_id=page['next_after_id'])['count'],1)
            now[0]=1020;store.finish(bid)
            self.assertEqual(store.get(bid)['status'],'completed');self.assertEqual(store.get(bid)['location'],'实验室A')
            # The archived raw record survives source DB retention/pruning.
            with closing(sqlite3.connect(str(source))) as db:db.execute('DELETE FROM measurements');db.commit()
            self.assertEqual(store.environment(bid)['count'],2)
            store.start({'run_id':'second'})
            source.rename(Path(td)/'offline.db')
            self.assertEqual(store.sync('second'),0)
            self.assertIsNotNone(store.get('second')['capture_error'])
            restarted=CollectionBatches(archive,source,clock=lambda:1030)
            self.assertEqual(restarted.get('second')['status'],'interrupted')
            with self.assertRaises(KeyError):store.get('missing')
            with self.assertRaises(ValueError):store.environment(bid,limit=10001)


if __name__=='__main__':unittest.main()
