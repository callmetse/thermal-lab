import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (ConfirmationError, check_confirmation, classify_audio_packet, confirm_transcript,
                    fuse, pack, split_candidates, thermal_candidate, unpack)


def audio(parent='a', end_ns=10, events=()):
    return {'event_id': 'ev-a', 'source': {'parent_run_id': parent, 'pi_end_mono_ns': end_ns},
            'events': list(events)}


def visual(run='a', received_ns=10, tracks=()):
    return {'run_id': run, 'pi_received_mono_ns': received_ns, 'source_frame_id': 7,
            'tracks': list(tracks)}


def track(action='walking_interaction', posture='standing'):
    return {'action': {'value': action, 'reason': 'offline_trained_candidate'},
            'posture': {'state': posture, 'reason': 'ok'}}


class Contract(unittest.TestCase):
    def test_reject_malformed(self):
        for raw in (b'', b'\0\0\xff\xffx', b'\0\0\0\x02{}a'):
            with self.assertRaises((ValueError, KeyError)):
                unpack(raw)

    def test_negation_is_not_cold(self):
        self.assertIsNone(thermal_candidate('我不冷')['sensation'])
        self.assertIsNone(thermal_candidate('我不想凉一点')['preference'])
        self.assertEqual(thermal_candidate('刚才冷，现在有点热')['sensation'], 'hot')

    def test_thermal_feedback_not_automatically_confirmed(self):
        self.assertEqual(thermal_candidate('我有点冷')['status'], 'needs_user_confirmation')

    def test_no_cross_run_fusion(self):
        a = audio(events=[{'name': 'Speech', 'score': .9}])
        v = visual(run='b', tracks=[{}])
        self.assertIsNone(fuse(a, v)['visual_ref'])

    def test_stale_visual_not_joined(self):
        a = audio(end_ns=10_000_000_000)
        v = visual(received_ns=1, tracks=[{}])
        self.assertIsNone(fuse(a, v)['person_count'])

    def test_future_observation_not_joined(self):
        a = audio(end_ns=10_000_000_000)
        v = visual(received_ns=11_000_000_000, tracks=[{}])
        self.assertIsNone(fuse(a, v)['person_count'])


class ComfortRuleRealUtterances(unittest.TestCase):
    """Cases taken verbatim from the first real 57 s capture (2026-11-03)."""

    def test_cold_with_a_wish_is_not_dropped(self):
        out = thermal_candidate('我有点冷，希望暖一点。')
        self.assertEqual((out['sensation'], out['preference']), ('cold', 'warmer'))

    def test_hot_with_a_wish(self):
        out = thermal_candidate('我有点热，希望凉一点。')
        self.assertEqual((out['sensation'], out['preference']), ('hot', 'cooler'))

    def test_past_state_is_not_the_current_label(self):
        out = thermal_candidate('刚才有点冷，现在不热了。')
        self.assertEqual((out['sensation'], out['preference']), (None, None))

    def test_unrecognised_sentence_emits_no_label(self):
        # ASR produced this for "我不冷"; whatever the words, no label may be invented.
        out = thermal_candidate('我不能。')
        self.assertEqual((out['sensation'], out['preference']), (None, None))

    def test_keep_as_is_is_not_read_as_a_time_marker(self):
        out = thermal_candidate('我不冷不热，保持现在这样。')
        self.assertEqual((out['sensation'], out['preference']), ('neutral', 'no_change'))

    def test_negated_wish_is_not_a_wish(self):
        self.assertIsNone(thermal_candidate('我不想凉一点。')['preference'])

    def test_current_hot_after_past_cold(self):
        self.assertEqual(thermal_candidate('刚才冷，现在有点热。')['sensation'], 'hot')

    def test_negated_now_is_not_cold(self):
        self.assertIsNone(thermal_candidate('现在不冷了，但刚才很冷。')['sensation'])

    def test_thermal_estimate_stays_unknown_value_but_reports_are_candidates(self):
        self.assertEqual(thermal_candidate('我有点热，但不想开空调。')['preference'], None)


class MergedClipIsSplitPerSentence(unittest.TestCase):
    """The VAD returns one clip when pauses are shorter than min_silence_duration."""

    MERGED = '我有点热，希望凉一点，我不冷，刚才有点冷，现在不热了。'

    def test_merged_clip_yields_independent_candidates(self):
        parts = split_candidates(self.MERGED)
        labels = [(p['candidate']['sensation'], p['candidate']['preference']) for p in parts]
        self.assertIn(('hot', 'cooler'), labels)
        self.assertIn((None, None), labels)
        self.assertEqual(sum(1 for l in labels if l == (None, None)), 1)

    def test_single_sentence_stays_single(self):
        parts = split_candidates('我有点冷，希望暖一点。')
        self.assertEqual(len(parts), 1)
        self.assertEqual((parts[0]['candidate']['sensation'], parts[0]['candidate']['preference']),
                         ('cold', 'warmer'))

    def test_short_negative_sentence_alone(self):
        parts = split_candidates('我不冷。')
        self.assertEqual(len(parts), 1)
        self.assertIsNone(parts[0]['candidate']['sensation'])


class FusionUsesRealAbcStructures(unittest.TestCase):
    def test_dict_action_value_is_read(self):
        a = audio(end_ns=1_000_000_000, events=[{'name': 'Footsteps', 'score': .8}])
        v = visual(received_ns=900_000_000, tracks=[track('walking_interaction')])
        self.assertEqual(fuse(a, v)['status'], 'visible_motion_and_footstep_candidate')

    def test_stringified_action_is_not_trusted(self):
        """An unknown action must not be read as movement just because a dict repr contains words."""
        a = audio(end_ns=1_000_000_000)
        v = visual(received_ns=900_000_000, tracks=[track('unknown')])
        self.assertEqual(fuse(a, v)['status'], 'people_visible_without_audio_candidate')

    def test_footsteps_with_unknown_action_are_not_reported_as_no_sound(self):
        a = audio(end_ns=1_000_000_000, events=[{'name': 'Walk, footsteps', 'score': .5}])
        v = visual(received_ns=900_000_000, tracks=[track('unknown')])
        out = fuse(a, v)
        self.assertEqual(out['status'], 'footstep_candidate_with_visible_person_not_attributed')
        self.assertIsNone(out['person_attribution'])

    def test_speech_next_to_visible_person_is_not_attribution(self):
        a = audio(end_ns=1_000_000_000, events=[{'name': 'Speech', 'score': .95}])
        v = visual(received_ns=900_000_000, tracks=[track('unknown')])
        out = fuse(a, v)
        self.assertEqual(out['status'], 'speech_candidate_with_visible_person_not_attributed')
        self.assertIsNone(out['person_attribution'])

    def test_audio_without_person_is_audio_only(self):
        a = audio(end_ns=1_000_000_000, events=[{'name': 'Speech', 'score': .9}])
        v = visual(received_ns=900_000_000, tracks=[])
        self.assertEqual(fuse(a, v)['status'], 'audio_candidate_without_visible_person')

    def test_no_evidence_status_is_explicit(self):
        a = audio(end_ns=1_000_000_000)
        v = visual(received_ns=900_000_000, tracks=[])
        out = fuse(a, v)
        self.assertEqual(out['status'], 'no_joint_event_confirmed')
        self.assertIsNone(out['person_attribution'])
        self.assertTrue(out['silence_is_not_negative_evidence'])
        self.assertIn('not_exposure_sync', out['alignment'])


class PacketClassification(unittest.TestCase):
    def test_duplicate_and_out_of_order_are_dropped(self):
        self.assertEqual(classify_audio_packet('s1', 5, {'session_id': 's1', 'seq': 5}), 'duplicate')
        self.assertEqual(classify_audio_packet('s1', 5, {'session_id': 's1', 'seq': 3}), 'duplicate')

    def test_next_chunk_continues(self):
        self.assertEqual(classify_audio_packet('s1', 5, {'session_id': 's1', 'seq': 6}), 'continue')

    def test_missing_chunk_is_gap(self):
        self.assertEqual(classify_audio_packet('s1', 5, {'session_id': 's1', 'seq': 8}), 'gap')

    def test_new_session_resets(self):
        self.assertEqual(classify_audio_packet('s1', 5, {'session_id': 's2', 'seq': 0}), 'new_session')

    def test_retired_session_late_packet_is_ignored(self):
        retired = {'s1': 1}
        self.assertEqual(classify_audio_packet('s1', 99, {'session_id': 's1', 'seq': 0}, retired),
                         'retired')


class Confirmation(unittest.TestCase):
    def setUp(self):
        self.transcript = {'kind': 'transcript', 'event_id': 't1', 'session_id': 's1',
                           'text': '我有点冷', 'source': {'parent_run_id': 'r1'}}
        self.events = [self.transcript]
        self.confirmed = {}

    def test_requires_at_least_one_label(self):
        with self.assertRaises(ConfirmationError):
            check_confirmation({'sensation': None, 'preference': None})

    def test_rejects_invalid_label(self):
        with self.assertRaises(ConfirmationError):
            check_confirmation({'sensation': 'warm', 'preference': None})

    def test_unknown_transcript_rejected(self):
        with self.assertRaises(ConfirmationError):
            confirm_transcript(self.events, self.confirmed,
                               {'event_id': 'nope', 'sensation': 'cold'})

    def test_confirm_is_idempotent(self):
        first, created = confirm_transcript(self.events, self.confirmed,
                                            {'event_id': 't1', 'sensation': 'cold'})
        second, created_again = confirm_transcript(self.events, self.confirmed,
                                                   {'event_id': 't1', 'sensation': 'hot'})
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first['event_id'], second['event_id'])
        self.assertEqual(second['sensation'], 'cold')
        self.assertEqual(len(self.confirmed), 1)

    def test_transcript_correction_is_kept(self):
        x, _ = confirm_transcript(self.events, self.confirmed,
                                  {'event_id': 't1', 'sensation': 'cold', 'transcript': '我有点冷啊'})
        self.assertEqual(x['asr_text'], '我有点冷')
        self.assertEqual(x['transcript_override'], '我有点冷啊')


if __name__ == '__main__':
    unittest.main()
