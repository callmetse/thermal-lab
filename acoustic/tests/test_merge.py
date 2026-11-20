"""Merge rules: adjacency, same session, originals never clobbered."""
import unittest

import merge


def transcript(eid, session='s1', start_s=0.0, dur=1.0, text='甲'):
    return dict(event_id=eid, kind='transcript', session_id=session,
                speech_start_sample=int(start_s * 16000), speech_seconds=dur,
                text=text, candidate={'sensation': None, 'preference': None},
                source={'session_id': session, 'seq': 1})


class MergeRules(unittest.TestCase):
    def test_adjacent_merge_keeps_both_sources(self):
        events = [transcript('a', start_s=1.0, dur=1.0, text='我觉得'),
                  transcript('b', start_s=2.1, dur=0.9, text='现在有点热')]
        merged = merge.merge_transcripts(events, 'b', 'a')       # order must not matter
        self.assertEqual(merged['text'], '我觉得现在有点热')
        self.assertEqual(merged['source_event_ids'], ['a', 'b'])
        self.assertEqual(merged['kind'], 'merged_transcript')
        self.assertEqual(len(events), 2)                          # originals untouched

    def test_rejects_far_apart(self):
        events = [transcript('a', start_s=1.0), transcript('b', start_s=9.0)]
        with self.assertRaises(merge.MergeError):
            merge.merge_transcripts(events, 'a', 'b')

    def test_rejects_different_sessions(self):
        events = [transcript('a'), transcript('b', session='s2', start_s=1.0)]
        with self.assertRaises(merge.MergeError):
            merge.merge_transcripts(events, 'a', 'b')

    def test_rejects_missing_event(self):
        with self.assertRaises(merge.MergeError):
            merge.merge_transcripts([transcript('a')], 'a', 'missing')

    def test_same_pair_gets_the_same_merge_id(self):
        events = [transcript('a', start_s=1.0), transcript('b', start_s=2.1)]
        again = [transcript('a', start_s=1.0), transcript('b', start_s=2.1)]
        first = merge.merge_transcripts(events, 'a', 'b')['merged_event_id']
        second = merge.merge_transcripts(again, 'b', 'a')['merged_event_id']
        self.assertEqual(first, second)
        stored = events + [merge.merge_transcripts(events, 'a', 'b')]
        self.assertIsNotNone(merge.existing_merge(stored, first))

    def test_rejects_cross_vad_epoch(self):
        a = transcript('a', start_s=1.0)
        b = transcript('b', start_s=2.1)
        a['source']['vad_epoch'] = 1
        b['source']['vad_epoch'] = 2
        with self.assertRaises(merge.MergeError):
            merge.merge_transcripts([a, b], 'a', 'b')

    def test_keeps_sample_span(self):
        events = [transcript('a', start_s=1.0, dur=1.0), transcript('b', start_s=2.1, dur=0.9)]
        merged = merge.merge_transcripts(events, 'a', 'b')
        self.assertEqual(merged['span_start_sample'], int(1.0 * 16000))
        self.assertEqual(merged['span_end_sample'], int(2.1 * 16000) + int(0.9 * 16000))

    def test_rejects_non_transcript(self):
        events = [transcript('a'), dict(event_id='b', kind='sound_events', session_id='s1')]
        with self.assertRaises(merge.MergeError):
            merge.merge_transcripts(events, 'a', 'b')


if __name__ == '__main__':
    unittest.main()
