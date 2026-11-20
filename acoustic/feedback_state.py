"""Feedback lifecycle: immutable events in, derived current state out.

Design follows the 2026-11-04 review reply (section "Q1"):
  * events are never mutated; a correction references the event it corrects;
  * "current state" is a projection per (subject, zone, dimension), rebuilt from
    events at any time;
  * three time axes are kept apart: when the speech happened, when the sentence
    describes (现在/刚才/将来), and when the user confirmed it;
  * a denied value is not the same as "not mentioned" and not the same as neutral.

Storage is a standalone SQLite file. The JSONL journals stay the source of truth;
this database is a disposable projection, so rebuilding it must always be safe.
"""
import json
import os
import re
import sqlite3
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 'feedback_projection_v1'
DISPLAY_POLICY = 'display_policy_v1'
DEFAULT_TTL_S = 300.0            # engineering default, to be compared against 30/60/300

DIMENSIONS = ('sensation', 'preference', 'device_action')

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_event (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  subject_id TEXT,
  zone_id TEXT NOT NULL,
  transcript_event_id TEXT,
  event_type TEXT NOT NULL,
  target_event_id TEXT,
  recorded_at_ms INTEGER NOT NULL,
  confirmed_at_ms INTEGER,
  reported_for_ms INTEGER,
  reported_time_scope TEXT NOT NULL,
  clock_id TEXT,
  timestamp_uncertainty_ms REAL,
  raw_text TEXT,
  corrected_text TEXT,
  dimensions_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_current (
  subject_key TEXT NOT NULL,
  zone_id TEXT NOT NULL,
  dimension TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  value TEXT,
  state TEXT NOT NULL,
  valid_from_ms INTEGER,
  expires_at_ms INTEGER,
  projection_version TEXT NOT NULL,
  PRIMARY KEY (subject_key, zone_id, dimension)
);
CREATE INDEX IF NOT EXISTS feedback_event_order ON feedback_event (recorded_at_ms);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


PAST_WORDS = ('刚才', '刚刚', '之前', '以前', '刚才那')
FUTURE_WORDS = ('等会', '待会', '一会儿', '一会', '明天', '以后')
NOW_WORDS = ('现在', '目前', '此刻', '这会儿', '这会')


def _clause_scope(clause):
    if any(word in clause for word in PAST_WORDS):
        return 'past'
    if any(word in clause for word in FUTURE_WORDS):
        return 'future'
    if any(word in clause for word in NOW_WORDS):
        return 'now'
    return None


def _scope_from_text(text):
    """Reported-time scope per clause, decided by the last clause that names a time.

    A whole-sentence keyword test was wrong: '刚才冷，现在舒服了' is about now, and
    '明天想凉一点' is a future want that must not become a current preference.
    """
    if not text:
        return 'unspecified'
    clauses = [part for part in re.split(r'[，。；！？,.;!?\s]+', text) if part]
    decided = 'unspecified'
    for clause in clauses:
        scope = _clause_scope(clause)
        if scope:
            decided = scope
    return decided


def confirmation_event(confirmed, session_id, zone_id='room-1', subject_id=None,
                       clock_id='pi-boot', uncertainty_ms=None):
    """Turn a stored `confirmed_feedback` event into an immutable feedback event.

    Only human confirmations may reach the current state; ASR output and model
    candidates never call this function.
    """
    dimensions = {}
    for name in DIMENSIONS:
        value = confirmed.get(name)
        dimensions[name] = {'value': value, 'assertion': 'affirmed' if value else 'not_mentioned'}
    text = confirmed.get('transcript_override') or confirmed.get('asr_text') or ''
    source = confirmed.get('source') if isinstance(confirmed.get('source'), dict) else {}
    reported_for = confirmed.get('reported_for_ms') or source.get('pi_wall_ms')
    return dict(
        event_id=confirmed['event_id'],
        session_id=session_id,
        subject_id=subject_id,
        zone_id=zone_id,
        transcript_event_id=confirmed.get('transcript_event_id'),
        event_type='confirmation',
        target_event_id=None,
        recorded_at_ms=int(confirmed['recorded_wall_ms']),
        confirmed_at_ms=int(confirmed.get('confirmed_wall_ms') or confirmed['recorded_wall_ms']),
        reported_for_ms=int(reported_for) if reported_for else None,
        reported_time_scope=_scope_from_text(text),
        clock_id=clock_id,
        timestamp_uncertainty_ms=uncertainty_ms,
        raw_text=confirmed.get('asr_text'),
        corrected_text=confirmed.get('transcript_override'),
        dimensions=dimensions,
        provenance={'confirmation_method': confirmed.get('confirmation_method'),
                    'candidate': confirmed.get('candidate'),
                    'source': 'confirmed_feedback'},
    )


def correction_event(new_event, target_event_id, kind='correction'):
    """A correction keeps the old event and points at it."""
    event = dict(new_event)
    event['event_type'] = kind if kind in ('correction', 'retraction') else 'correction'
    event['target_event_id'] = target_event_id
    return event


def insert(db, event):
    db.execute(
        "INSERT OR REPLACE INTO feedback_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event['event_id'], event['session_id'], event.get('subject_id'), event['zone_id'],
         event.get('transcript_event_id'), event['event_type'], event.get('target_event_id'),
         event['recorded_at_ms'], event.get('confirmed_at_ms'), event.get('reported_for_ms'),
         event['reported_time_scope'], event.get('clock_id'), event.get('timestamp_uncertainty_ms'),
         event.get('raw_text'), event.get('corrected_text'),
         json.dumps(event['dimensions'], ensure_ascii=False),
         json.dumps(event.get('provenance') or {}, ensure_ascii=False)))


def rebuild(db):
    """Recompute feedback_current from the immutable event log."""
    db.execute("DELETE FROM feedback_current")
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM feedback_event ORDER BY recorded_at_ms, event_id")]
    # Retracted events keep their history but stop contributing.
    retracted = {r['target_event_id'] for r in rows if r['event_type'] == 'retraction'}

    latest = {}
    for row in rows:
        if row['event_type'] not in ('confirmation', 'correction'):
            continue
        if row['event_id'] in retracted:
            continue
        # Ordering uses when the sentence describes, falling back to when it was
        # recorded: a late confirmation of an old sentence must not overwrite a
        # newer description.
        order_key = row['reported_for_ms'] or row['recorded_at_ms']
        dimensions = json.loads(row['dimensions_json'])
        for name, dim in dimensions.items():
            if dim.get('assertion') == 'not_mentioned':
                continue                       # this sentence said nothing about it
            subject_key = row['subject_id'] or 'unknown_speaker'
            key = (subject_key, row['zone_id'], name)
            previous = latest.get(key)
            if previous is None or order_key >= previous['order_key']:
                latest[key] = dict(order_key=order_key, value=dim.get('value'),
                                   assertion=dim.get('assertion'), row=row)

    for (subject_key, zone_id, dimension), item in latest.items():
        row = item['row']
        if row['reported_time_scope'] == 'past':
            state = 'reported_past'            # "刚才冷" never becomes current cold
        elif row['reported_time_scope'] == 'future':
            state = 'reported_future'          # a want for later is not a current state
        elif item['assertion'] == 'denied':
            state = 'denied'
        elif item['assertion'] == 'uncertain':
            state = 'uncertain'
        elif item['value']:
            state = 'affirmed'
        else:
            state = 'unknown'
        valid_from = row['reported_for_ms'] or row['recorded_at_ms']
        expires = valid_from + int(DEFAULT_TTL_S * 1000) if state == 'affirmed' else None
        db.execute(
            "INSERT OR REPLACE INTO feedback_current VALUES (?,?,?,?,?,?,?,?,?)",
            (subject_key, zone_id, dimension, row['event_id'], item['value'], state,
             valid_from, expires, SCHEMA_VERSION))
    db.commit()


def current(db, subject_id=None, zone_id='room-1', now_ms=None):
    """Projected state with the display-policy window applied."""
    subject_key = subject_id or 'unknown_speaker'
    now_ms = now_ms if now_ms is not None else int(__import__('time').time() * 1000)
    out = {}
    for row in db.execute(
            "SELECT * FROM feedback_current WHERE subject_key=? AND zone_id=?",
            (subject_key, zone_id)):
        item = dict(row)
        if item['state'] == 'affirmed' and item['expires_at_ms'] and now_ms > item['expires_at_ms']:
            item['display_state'] = 'stale'
            item['age_s'] = round((now_ms - item['valid_from_ms']) / 1000, 1)
        else:
            item['display_state'] = item['state']
            item['age_s'] = round((now_ms - item['valid_from_ms']) / 1000, 1)
        out[item['dimension']] = item
    return out


def ingest_journal(db, path, zone_id='room-1', subject_id=None):
    """Build feedback events from a `data/events.jsonl` journal (Pi layout)."""
    seen = 0
    with open(path, encoding='utf8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get('kind') != 'confirmed_feedback':
                continue
            s = record.get('source') if isinstance(record.get('source'), dict) else {}
            event = confirmation_event(dict(
                record,
                transcript_event_id=s.get('transcript_event_id'),
                candidate=record.get('candidate'),
            ), session_id=record.get('session_id'), zone_id=zone_id, subject_id=subject_id)
            insert(db, event)
            seen += 1
    db.commit()
    rebuild(db)
    return seen


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('journal')
    parser.add_argument('--db', default='results/feedback_projection.sqlite')
    parser.add_argument('--subject', default=None)
    args = parser.parse_args()
    os.makedirs(os.path.dirname(args.db) or '.', exist_ok=True)
    connection = connect(args.db)
    count = ingest_journal(connection, args.journal, subject_id=args.subject)
    print('ingested %d confirmations' % count)
    print(json.dumps(current(connection, subject_id=args.subject), ensure_ascii=False, indent=1))
