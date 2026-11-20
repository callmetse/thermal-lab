"""Ordered delivery to the dataset service with bounded memory and durable session boundaries."""
from collections import deque
from contextlib import closing
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request


BATCH_SIZE = 50
REQUEST_BYTES = 1024 * 1024
MAX_BACKLOG_BYTES = 16 * 1024 * 1024
RECORD_PREFIX = b'{"records":['
RECORD_SUFFIX = b']}'


class ArchiveIngest:
    MAX_QUEUE = 2048
    MAX_SESSIONS = 32

    def __init__(self, url, max_queue=None, batch_size=None, timeout=None, journal=None):
        self.url = url.rstrip('/')
        self.max_queue = max_queue or self.MAX_QUEUE
        self.batch_size = min(batch_size or BATCH_SIZE, BATCH_SIZE)
        self.timeout = timeout or 3.0
        self.condition = threading.Condition()
        self.pending = deque()
        self.runs = {}
        self.record_count = self.record_bytes = 0
        self.error = None
        self.active_batch_id = None
        self.closing = False
        self.stats = {'submitted': 0, 'delivered': 0, 'dropped': 0, 'failed_attempts': 0,
                      'retries': 0, 'backlog': 0, 'oldest_age_s': None, 'flush_timeouts': 0}
        self.journal = journal
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            self._restore_sessions()
        self.thread = threading.Thread(target=self._run, daemon=True, name='archive-ingest')
        self.thread.start()

    def _restore_sessions(self):
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (run_id TEXT PRIMARY KEY, start TEXT NOT NULL, finish TEXT)')
            rows = db.execute('SELECT run_id,start,finish FROM sessions ORDER BY rowid').fetchall()
        for run_id, start, finish in rows:
            start = json.loads(start)
            finish = json.loads(finish) if finish else self._finish_body(
                run_id, start['generation'], 'interrupted', 'edge_process_restart',
                {'delivery_uncertain': True},
            )
            finish['ingest_stats']['delivery_uncertain'] = True
            self.runs[run_id] = {'generation': start['generation'], 'closed': True}
            self.pending.extend([('start', run_id, start), ('finish', run_id, finish)])

    def _save(self, sql, values):
        if self.journal:
            with closing(sqlite3.connect(self.journal)) as db, db:
                db.execute(sql, values)

    def start_session(self, run):
        run_id = run['run_id']
        start = {'run_id': run_id, 'generation': run.get('generation'), 'context': run,
                 'started_ms': int(time.time() * 1000)}
        with self.condition:
            if run_id in self.runs:
                return
            if len(self.runs) >= self.MAX_SESSIONS:
                raise RuntimeError('archive session backlog is full')
            self._save('INSERT INTO sessions(run_id,start) VALUES (?,?)', (run_id, json.dumps(start)))
            self.runs[run_id] = {'generation': start['generation'], 'closed': False, 'dropped': 0}
            self.pending.append(('start', run_id, start))
            self.condition.notify()

    @staticmethod
    def _finish_body(run_id, generation, status, reason, stats):
        return {'run_id': None, 'target_run_id': run_id, 'generation': generation,
                'execution_status': status, 'ended_reason': reason, 'ingest_stats': stats,
                'ended_ms': int(time.time() * 1000)}

    def finish_session(self, run_id, status='completed', reason=None):
        with self.condition:
            run = self.runs.get(run_id)
            if not run or run['closed']:
                return
            finish = self._finish_body(run_id, run['generation'], status, reason, {'dropped': run['dropped']})
            self._save('UPDATE sessions SET finish=? WHERE run_id=?', (json.dumps(finish), run_id))
            run['closed'] = True
            self.pending.append(('finish', run_id, finish))
            self.condition.notify()

    def submit(self, kind, body):
        run_id = body.get('run_id')
        try:
            payload = json.dumps({'kind': kind, 'body': body}, ensure_ascii=False, allow_nan=False,
                                 separators=(',', ':')).encode()
        except (ValueError, TypeError):
            payload = b''
        with self.condition:
            run = self.runs.get(run_id)
            if (self.journal and not run) or (run and run['closed']):
                return
            if (not payload or len(payload) + len(RECORD_PREFIX) + len(RECORD_SUFFIX) > REQUEST_BYTES
                    or self.record_count >= self.max_queue
                    or self.record_bytes + len(payload) > MAX_BACKLOG_BYTES):
                self.stats['dropped'] += 1
                if run:
                    run['dropped'] += 1
                self.error = 'archive_ingest_capacity_or_payload'
                return
            self.pending.append(('record', run_id, (payload, time.monotonic())))
            self.record_count += 1
            self.record_bytes += len(payload)
            self.stats['submitted'] += 1
            self.stats['backlog'] = self.record_count
            self.condition.notify()

    def _request(self, endpoint, data):
        request = urllib.request.Request(self.url + '/api/dataset/' + endpoint, data=data,
                                         headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.load(response)

    def _record_batch(self):
        batch = []
        size = len(RECORD_PREFIX) + len(RECORD_SUFFIX)
        for entry in self.pending:
            if entry[0] != 'record' or entry[1] != self.pending[0][1] or len(batch) >= self.batch_size:
                break
            payload, _ = entry[2]
            if size + len(payload) + bool(batch) > REQUEST_BYTES:
                break
            size += len(payload) + bool(batch)
            batch.append(entry)
        return batch

    def _run(self):
        delay = 0.2
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closing or self.pending)
                if self.closing:
                    return
                kind, run_id, body = self.pending[0]
                if kind == 'record':
                    age = time.monotonic() - body[1]
                    self.stats['oldest_age_s'] = round(age, 1)
                    if age < 0.2 and len(self.pending) < self.batch_size:
                        self.condition.wait(0.2 - age)
                        continue
                    batch = self._record_batch()
                else:
                    batch = [self.pending[0]]
            try:
                if kind == 'record':
                    data = RECORD_PREFIX + b','.join(item[2][0] for item in batch) + RECORD_SUFFIX
                    receipt = self._request('records', data)
                    accepted, rejected = receipt.get('accepted'), receipt.get('rejected')
                    if type(accepted) is not int or type(rejected) is not int or accepted + rejected != len(batch):
                        raise ValueError('archive receipt does not account for the submitted batch')
                else:
                    if kind == 'finish':
                        # Include terminal delivery errors encountered while this command waited behind records.
                        with self.condition:
                            body['ingest_stats']['dropped'] = self.runs[run_id].get(
                                'dropped', body['ingest_stats'].get('dropped', 0))
                    receipt = self._request('session', json.dumps(body).encode())
                    if kind == 'finish':
                        self._save('DELETE FROM sessions WHERE run_id=?', (run_id,))
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if kind == 'record' and code in (400, 413, 415, 422):
                    self._discard_records(batch, code)
                    continue
                self._retry(delay, 'HTTP ' + str(code))
                delay = min(delay * 2, 5.0)
                continue
            except (OSError, ValueError, sqlite3.Error) as error:
                self._retry(delay, type(error).__name__)
                delay = min(delay * 2, 5.0)
                continue
            with self.condition:
                self.active_batch_id = receipt.get('active_batch_id')
                if kind == 'record':
                    self._remove_records(batch)
                    self.stats['delivered'] += accepted
                    self.stats['dropped'] += rejected
                    if run_id in self.runs:
                        self.runs[run_id]['dropped'] += rejected
                else:
                    self.pending.popleft()
                    if kind == 'finish':
                        self.runs.pop(run_id, None)
                self.error = None
                self.condition.notify_all()
            delay = 0.2

    def _remove_records(self, batch):
        for _, _, (payload, _) in batch:
            self.pending.popleft()
            self.record_count -= 1
            self.record_bytes -= len(payload)
        self.stats['backlog'] = self.record_count
        if not self.record_count:
            self.stats['oldest_age_s'] = None

    def _discard_records(self, batch, code):
        with self.condition:
            for _, run_id, _ in batch:
                if run_id in self.runs:
                    self.runs[run_id]['dropped'] += 1
            self._remove_records(batch)
            self.stats['dropped'] += len(batch)
            self.error = 'archive_ingest_rejected: HTTP ' + str(code)
            self.condition.notify_all()

    def _retry(self, delay, error):
        with self.condition:
            self.stats['failed_attempts'] += 1
            self.stats['retries'] += 1
            self.error = 'archive_ingest_failed: ' + error
            self.condition.wait_for(lambda: self.closing, timeout=delay)

    def flush(self, timeout=5.0):
        with self.condition:
            if self.condition.wait_for(lambda: not self.pending, timeout):
                return True
            self.stats['flush_timeouts'] += 1
            return False

    def close(self, timeout=1.0):
        self.flush(timeout)
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.thread.join(timeout=self.timeout + 1)
