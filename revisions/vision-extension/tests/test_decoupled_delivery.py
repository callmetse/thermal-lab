"""Acceptance for the split service using isolated loopback and SQLite."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from test_archive_ingest import DatasetApi, free_port, observation, wait_for
from lab.archive_ingest import ArchiveIngest


def measurements(path, received_ms):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE measurements (
            event_id TEXT, sensor_id TEXT, data_time_ms INTEGER, received_at_ms INTEGER,
            zone_id TEXT, values_json TEXT, units_json TEXT)""")
        db.execute("INSERT INTO measurements VALUES ('e1','sht4x',?,?, 'lab','{}','{}')",
                   (received_ms, received_ms))


def count(store, batch_id):
    with closing(store.connect()) as db:
        return db.execute('SELECT COUNT(*) FROM experiment_records WHERE batch_id=?', (batch_id,)).fetchone()[0]


def test_outage_and_environment(root):
    port = free_port()
    addr = 'http://127.0.0.1:%d' % port
    sensor = root / 'sensor.sqlite3'
    sender = ArchiveIngest(addr, journal=root/'sessions.sqlite3', timeout=0.5)
    api = None
    try:
        sender.start_session({'run_id':'offline', 'generation':1, 'arm':'C', 'ui_channel':'D'})
        for seq in range(1000):
            sender.submit('observation', observation(seq, 'offline'))
        assert sender.stats['backlog'] == 1000
        assert wait_for(lambda: sender.stats['failed_attempts'] > 0)
        api = DatasetApi(root=root, env_db=sensor, port=port).start()
        assert sender.flush(25), sender.stats
        assert sender.stats['delivered'] == count(api.batch_store, 'offline') == 1000
        batch = api.batch_store.get('offline')
        assert batch['channel'] == 'D' and api.batch_store.list_filtered(channel='D')['total'] == 1
        assert batch['capture_error'].startswith('environment_source_unavailable')
        measurements(sensor, batch['started_ms'])
        assert wait_for(lambda: api.batch_store.get('offline')['environment_rows'] == 1)
        api.stop()
        api = None
        sender.submit('event', {'run_id':'offline','kind':'role_finished'})
        sender.finish_session('offline', 'failed', 'acceptance_failure')
        api = DatasetApi(root=root, env_db=sensor, port=port).start()
        assert sender.flush(15), sender.stats
        finished = api.batch_store.get('offline')
        assert finished['archive_status'] == 'ok' and finished['execution_status'] == 'failed', finished
        assert finished['ended_reason'] == 'acceptance_failure'
        assert count(api.batch_store, 'offline') == 1001
        print('PASS 1000 queued observations + final event persisted over start/finish outages; environment recovered; reason saved')
    finally:
        sender.close()
        if api:
            api.stop()


def test_receipt_and_restart(root):
    api = DatasetApi(root=root, env_db=root/'absent.sqlite3', port=0).start()
    url = 'http://127.0.0.1:%d' % api.port
    journal = root/'sessions.sqlite3'
    sender = ArchiveIngest(url, max_queue=1, journal=journal)
    try:
        sender.start_session({'run_id':'receipt', 'generation':1})
        assert sender.flush()
        original = api.archive.persist_delivery
        def write_unavailable(items):
            raise OSError('test failure before commit')
        api.archive.persist_delivery = write_unavailable
        sender.submit('observation', observation(1, 'receipt'))
        assert wait_for(lambda: sender.stats['failed_attempts'] > 0)
        assert sender.stats['delivered'] == count(api.batch_store, 'receipt') == 0
        sender.submit('observation', observation(2, 'receipt'))
        assert sender.stats['dropped'] == 1
        request = sender._request
        lost_receipt = False
        def lose_first_receipt(endpoint, data):
            nonlocal lost_receipt
            response = request(endpoint, data)
            if endpoint == 'records' and not lost_receipt:
                lost_receipt = True
                raise OSError('test lost response after commit')
            return response
        sender._request = lose_first_receipt
        api.archive.persist_delivery = original
        assert sender.flush()
        assert lost_receipt
        assert sender.stats['delivered'] == count(api.batch_store, 'receipt') == 1
        sender.finish_session('receipt')
        assert sender.flush()
        detail = json.loads(api.batch_store.get('receipt')['quality_detail'])
        assert detail['ingest_dropped'] == 1 and detail['environment_unavailable']
        sender.start_session({'run_id':'no_preview', 'generation':2, 'collection_batch_id':'separate_id'})
        sender.submit('observation', {'run_id':'no_preview', **{
            k:v for k,v in observation(1, 'no_preview').items() if k not in ('collection_batch_id','run_id')}})
        assert sender.flush()
        assert count(api.batch_store, 'separate_id') == 1
        sender.close()
        sender = ArchiveIngest(url, journal=journal)
        assert sender.flush()
        batch = api.batch_store.get('separate_id')
        detail = json.loads(batch['quality_detail'])
        assert batch['execution_status'] == 'interrupted' and batch['archive_status'] == 'partial'
        assert detail['delivery_uncertain'] and detail['ingest_dropped'] == 0
        print('PASS write failure and lost receipt retried; overflow counted per run; startup reconciled open session without preview')
    finally:
        sender.close()
        api.stop()


if __name__ == '__main__':
    for test in (test_outage_and_environment, test_receipt_and_restart):
        with TemporaryDirectory() as directory:
            test(Path(directory))
