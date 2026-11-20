"""Tests for the feedback lifecycle projection (review reply: 方向3 必测清单)."""
import json
import unittest

import feedback_state as fs


def confirm(event_id, sensation=None, preference=None, at_ms=1000, text='',
            scope_text=None, confirmed_at=None, subject=None):
    return fs.confirmation_event(
        dict(event_id=event_id, recorded_wall_ms=at_ms, sensation=sensation,
             preference=preference, asr_text=scope_text if scope_text is not None else text,
             transcript_override=text or None,
             confirmation_method='explicit_user_selection'),
        session_id='s1', subject_id=subject)


class FeedbackLifecycle(unittest.TestCase):
    def setUp(self):
        self.db = fs.connect(':memory:')

    def add(self, event):
        fs.insert(self.db, event)
        fs.rebuild(self.db)

    def state(self, now_ms=2000, subject=None):
        return fs.current(self.db, subject_id=subject, now_ms=now_ms)

    # 1. new report becomes the current state
    def test_new_report_is_current(self):
        self.add(confirm('e1', 'cold', 'warmer', at_ms=1000))
        state = self.state(now_ms=2000)
        self.assertEqual(state['sensation']['value'], 'cold')
        self.assertEqual(state['sensation']['display_state'], 'affirmed')
        self.assertEqual(state['preference']['value'], 'warmer')

    # 2. a sentence that only names preference must not refresh sensation
    def test_single_dimension_update(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        self.add(confirm('e2', None, 'warmer', at_ms=2000))
        state = self.state(now_ms=3000)
        self.assertEqual(state['sensation']['source_event_id'], 'e1')
        self.assertEqual(state['preference']['source_event_id'], 'e2')

    # 3. late confirmation of an older sentence must not overwrite a newer one
    def test_late_confirmation_does_not_win(self):
        self.add(confirm('new', 'hot', None, at_ms=5000))
        late = confirm('old', 'cold', None, at_ms=9000)
        late['reported_for_ms'] = 1000            # describes something older
        self.add(late)
        state = self.state(now_ms=9500)
        self.assertEqual(state['sensation']['value'], 'hot')
        self.assertEqual(state['sensation']['source_event_id'], 'new')

    # 4. denial is neither the old value nor neutral
    def test_denial_is_its_own_state(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        denied = confirm('e2', None, None, at_ms=2000, text='我不冷')
        denied['dimensions']['sensation'] = {'value': 'cold', 'assertion': 'denied'}
        self.add(denied)
        state = self.state(now_ms=3000)
        self.assertEqual(state['sensation']['display_state'], 'denied')
        self.assertNotEqual(state['sensation']['value'], 'neutral')

    # 5. "刚才冷" never refreshes the current value
    def test_past_scope_is_not_current(self):
        event = confirm('e1', 'cold', None, at_ms=1000, text='刚才有点冷')
        self.add(event)
        self.assertEqual(self.state(now_ms=2000)['sensation']['display_state'], 'reported_past')

    # 6. correction keeps the old event and points at it
    def test_correction_references_target(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        fixed = confirm('e2', 'hot', None, at_ms=2000)
        fs.insert(self.db, fs.correction_event(fixed, 'e1'))
        fs.rebuild(self.db)
        rows = [dict(r) for r in self.db.execute(
            "SELECT event_id, event_type, target_event_id FROM feedback_event ORDER BY recorded_at_ms")]
        self.assertEqual(len(rows), 2)                       # history kept
        self.assertEqual(rows[1]['event_type'], 'correction')
        self.assertEqual(rows[1]['target_event_id'], 'e1')
        self.assertEqual(self.state(now_ms=3000)['sensation']['value'], 'hot')

    # 7. retraction stops the target from contributing but keeps the row
    def test_retraction_hides_target(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        pull = confirm('e2', None, None, at_ms=2000)
        fs.insert(self.db, fs.correction_event(pull, 'e1', kind='retraction'))
        fs.rebuild(self.db)
        self.assertIsNone(self.state(now_ms=3000).get('sensation'))

    # 8. replay / duplicate ingest is idempotent
    def test_replay_is_idempotent(self):
        event = confirm('e1', 'cold', None, at_ms=1000)
        self.add(event)
        self.add(event)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM feedback_event").fetchone()[0], 1)

    # 9. TTL is a display policy, not a deletion
    def test_display_ttl_marks_stale(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        fresh = self.state(now_ms=1000 + 60 * 1000)
        stale = self.state(now_ms=1000 + 10 * 60 * 1000)
        self.assertEqual(fresh['sensation']['display_state'], 'affirmed')
        self.assertEqual(stale['sensation']['display_state'], 'stale')
        self.assertEqual(stale['sensation']['value'], 'cold')   # value kept

    # 10. unknown speaker is explicit, never guessed
    def test_unknown_speaker_key(self):
        self.add(confirm('e1', 'cold', None, at_ms=1000))
        self.assertIn('sensation', self.state(now_ms=2000, subject=None))
        self.assertNotIn('sensation', self.state(now_ms=2000, subject='person-2'))

    # 11. rebuild is repeatable: same events, same projection
    def test_rebuild_is_repeatable(self):
        self.add(confirm('e1', 'cold', 'warmer', at_ms=1000))
        first = self.state(now_ms=2000)
        fs.rebuild(self.db)
        second = self.state(now_ms=2000)
        self.assertEqual(first['sensation']['source_event_id'], second['sensation']['source_event_id'])
        self.assertEqual(first['preference']['value'], second['preference']['value'])

    # 12. a want for later must never become the current state
    def test_future_scope_is_not_current(self):
        event = confirm('e1', None, 'cooler', at_ms=1000, text='明天想凉一点')
        self.add(event)
        state = self.state(now_ms=2000)
        self.assertEqual(state['preference']['display_state'], 'reported_future')
        self.assertEqual(state['preference']['value'], 'cooler')   # kept as history

    # 13. scope is decided per clause: "刚才冷，现在舒服了" is about now
    def test_clause_scope_prefers_the_last_time_marker(self):
        self.assertEqual(fs._scope_from_text('刚才有点冷，现在舒服了'), 'now')
        self.assertEqual(fs._scope_from_text('现在有点热，刚才很冷'), 'past')
        self.assertEqual(fs._scope_from_text('明天想凉一点'), 'future')

    # 14. ordering uses the audio wall clock when the source carries it
    def test_reported_for_comes_from_the_source_clock(self):
        raw = dict(event_id='e1', recorded_wall_ms=5000, sensation='cold', preference=None,
                   asr_text='昨天很冷', transcript_override=None,
                   confirmation_method='explicit_user_selection',
                   source={'pi_wall_ms': 1234.0})
        event = fs.confirmation_event(raw, session_id='s1')
        self.assertEqual(event['reported_for_ms'], 1234)

    # 15. journal ingest maps a real confirmed_feedback line
    def test_ingest_journal(self):
        import tempfile
        import os
        line = json.dumps({
            'kind': 'confirmed_feedback', 'event_id': 'c1', 'session_id': 's9',
            'recorded_wall_ms': 1793761653676.679,
            'asr_text': '我有点冷希望暖一点', 'transcript_override': None,
            'sensation': 'cold', 'preference': 'warmer',
            'confirmation_method': 'explicit_user_selection', 'source': {}}, ensure_ascii=False)
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf8') as handle:
            handle.write(line + '\n')
            path = handle.name
        try:
            count = fs.ingest_journal(self.db, path)
        finally:
            os.unlink(path)
        self.assertEqual(count, 1)
        state = fs.current(self.db, now_ms=1793761653676 + 1000)
        self.assertEqual(state['sensation']['value'], 'cold')


if __name__ == '__main__':
    unittest.main()
