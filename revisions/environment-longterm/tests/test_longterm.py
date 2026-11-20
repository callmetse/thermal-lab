import json,sqlite3,tempfile,unittest,sys
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from env_longterm import LongTerm
class Tests(unittest.TestCase):
 def test_windows_change_missing_restart(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/'e.db';now=[100000]
   with closing(sqlite3.connect(path)) as db:
    db.execute('CREATE TABLE measurements(sensor_id TEXT,zone_id TEXT,quality TEXT,received_at_ms INTEGER,values_json TEXT)');db.commit()
   store=LongTerm(path,lambda:now[0]);self.assertEqual(store.settings()['interval_s'],60)
   with closing(sqlite3.connect(path)) as db:
    for t,v in [(101000,20),(110000,22),(160000,99)]:db.execute('INSERT INTO measurements VALUES(?,?,?,?,?)',('sht4x','lab','ok',t,json.dumps({'temperature_c':v})))
    db.commit()
   now[0]=162000;store.tick();rows=store.history()['rows'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['sensors']['lab/sht4x']['fields']['temperature_c']['mean'],21)
   store.configure(5);self.assertTrue(store.history()['rows'][-1]['partial'])
   now[0]=169000;store.tick();self.assertEqual(store.history()['rows'][-1]['sensors'],{})
   for invalid in [1,6,0,True,86405]:
    with self.assertRaises(ValueError):store.configure(invalid)
   again=LongTerm(path,lambda:now[0]);self.assertEqual(again.settings()['interval_s'],5);self.assertEqual(len(again.history()['rows']),3)
if __name__=='__main__':unittest.main()
