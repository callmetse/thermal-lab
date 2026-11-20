from contextlib import closing
import hashlib,json,sqlite3,tempfile,unittest,zipfile,shutil
from pathlib import Path
from lab.collection_batches import CollectionBatches
from lab.experiment_archive import ExperimentArchive

class ArchiveTests(unittest.TestCase):
    def test_dedup_export_hashes_sensor_separation_and_history(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source.db'
            with closing(sqlite3.connect(source)) as db:db.execute('CREATE TABLE measurements(received_at_ms INTEGER)')
            store=CollectionBatches(root/'archive.db',source,clock=lambda:1000)
            store.start({'run_id':'test','source':'camera'})
            archive=ExperimentArchive(store,root)
            try:
                archive.begin({'run_id':'test','source':'camera'})
                payload={'run_id':'test','producer_instance_id':'p','observation_seq':1,'tracks':[{}],'crossing_events':[{}]}
                archive.submit('observation',payload);archive.submit('observation',payload)
                archive.pending.join()
                with self.assertRaises(ValueError):archive.export('test')
                with closing(store.connect()) as db:
                    for sensor,value in [('sht4x',20),('scd41',30)]:
                        raw={'sensor_id':sensor,'values':{'temperature_c':value}}
                        db.execute('INSERT INTO batch_environment(batch_id,event_id,sensor_id,raw_json) VALUES(?,?,?,?)',('test',sensor,sensor,json.dumps(raw)))
                    db.execute("UPDATE batches SET status='completed',ended_ms=2000 WHERE batch_id='test'")
                    db.commit()
                summary=archive.summary('test')
                self.assertEqual(summary['observation_count'],1)
                self.assertEqual(summary['environment_statistics']['sht4x/temperature_c']['mean'],20)
                self.assertEqual(summary['environment_statistics']['scd41/temperature_c']['mean'],30)
                exported=archive.export('test')
                try:
                    with zipfile.ZipFile(exported) as z:
                        manifest=json.loads(z.read('checksums.json'))
                        for name,digest in manifest.items():self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),digest)
                        self.assertEqual(len(z.read('observations.jsonl').splitlines()),1)
                        self.assertFalse(json.loads(z.read('assets_manifest.json'))['media_included'])
                finally:shutil.rmtree(exported.parent)
                store.start({'run_id':'old'})
                self.assertTrue(archive.summary('old')['historical_missing'])
            finally:archive.close()
