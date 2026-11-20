"""Human merge of two adjacent transcript fragments.

Nothing is overwritten: the two original transcript events stay in the journal and
the merged result is a new event that points at both. That keeps history replayable
and makes the merge itself an explicit human annotation rather than a silent edit.
"""
import hashlib
import time

from common import event

# A deliberate mid-sentence pause is 2-3 s in the calibration recording, so the
# guard only rejects clearly separate utterances; the user's click is the real check.
ADJACENCY_TOLERANCE_S = 4.0


class MergeError(ValueError):
    pass


def pick(events, event_id):
    for item in events:
        if item.get('event_id') == event_id:
            return item
    return None


def transcript_span(item):
    start = item.get('speech_start_sample') or 0
    duration = item.get('speech_seconds') or 0
    return start / 16000.0, start / 16000.0 + duration


def merge_id(session_id, first_id, second_id):
    """Deterministic: the same pair always produces the same merge id (idempotent)."""
    digest = hashlib.sha256(('|'.join([session_id or '', *sorted([first_id, second_id])])).encode())
    return 'merge_' + digest.hexdigest()[:32]


def existing_merge(events, merged_event_id):
    for item in events:
        if item.get('merged_event_id') == merged_event_id:
            return item
    return None


def merge_transcripts(events, first_id, second_id, zone_id=None, subject_id=None):
    """Build the merged event, or raise MergeError with a user-facing reason."""
    if first_id == second_id:
        raise MergeError('same event twice')
    first = pick(events, first_id)
    second = pick(events, second_id)
    if first is None or second is None:
        raise MergeError('event not in the current ring; reload the page')
    for item in (first, second):
        if item.get('kind') != 'transcript':
            raise MergeError('only transcript events can be merged')
    if first.get('session_id') != second.get('session_id'):
        raise MergeError('different sessions')

    first_start, first_end = transcript_span(first)
    second_start, second_end = transcript_span(second)
    if second_start < first_start:
        first, second = second, first
        first_start, first_end = second_start, second_end
        second_start, second_end = transcript_span(second)[0], transcript_span(second)[1]

    gap = second_start - first_end
    if gap > ADJACENCY_TOLERANCE_S:
        raise MergeError('not adjacent: %.2f s apart' % gap)
    # After a packet gap the worker restarts its sample counter, so sample positions
    # from different VAD epochs are not comparable and must not be merged.
    epoch_first = (first.get('source') or {}).get('vad_epoch', first.get('vad_epoch'))
    epoch_second = (second.get('source') or {}).get('vad_epoch', second.get('vad_epoch'))
    if epoch_first is not None and epoch_second is not None and epoch_first != epoch_second:
        raise MergeError('different VAD epochs (%s vs %s)' % (epoch_first, epoch_second))

    text = (first.get('text') or '') + (second.get('text') or '')
    merged = event('merged_transcript', first.get('session_id'),
                   source=dict(first.get('source') or {}),
                   merged_at_ms=time.time() * 1000,
                   merged_event_id=merge_id(first.get('session_id'), first['event_id'], second['event_id']),
                   source_event_ids=[first['event_id'], second['event_id']],
                   source_texts=[first.get('text'), second.get('text')],
                   source_candidates=[first.get('candidate'), second.get('candidate')],
                   text=text,
                   speech_seconds=(first.get('speech_seconds') or 0) + (second.get('speech_seconds') or 0),
                   speech_start_sample=first.get('speech_start_sample'),
                   span_start_sample=first.get('speech_start_sample'),
                   span_end_sample=(second.get('speech_start_sample') or 0)
                                   + int((second.get('speech_seconds') or 0) * 16000),
                   gap_seconds=round(gap, 3),
                   merge_basis='explicit human merge; originals kept',
                   validation='human_merge_unconfirmed')
    if zone_id:
        merged['zone_id'] = zone_id
    if subject_id:
        merged['subject_id'] = subject_id
    return merged
