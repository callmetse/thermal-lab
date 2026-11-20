"""Versioned multimodal events; no imports or writes to the ABC application."""
import json
import re
import struct
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOPIC = 'labs/mm_audio_v1/v1'
IDENT = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')
LOCK = threading.Lock()


def journal(name, event, max_bytes=32 * 1024 * 1024):
    """Append one JSON line, rotating to <name>.1 once the file passes max_bytes.

    Keeps a long-running module from filling the disk; the rotated file is kept
    (not deleted) so evidence is still recoverable.
    """
    path = ROOT / 'data' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        try:
            if path.stat().st_size > max_bytes:
                rotated = path.with_name(path.name + '.1')
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
        except FileNotFoundError:
            pass
        with path.open('a', encoding='utf8') as f:
            f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')


def event(kind, session_id, **values):
    return dict(schema_version='1.0', event_id=uuid.uuid4().hex,
                kind=kind, session_id=session_id, recorded_wall_ms=time.time()*1000,
                zone_id='room-1', subject_id=None, **values)


def pack(header, pcm):
    h = json.dumps(header, separators=(',', ':')).encode()
    return struct.pack('!I', len(h)) + h + pcm


def unpack(payload):
    if len(payload) < 5 or len(payload) > 180000:
        raise ValueError('invalid audio payload size')
    n = struct.unpack('!I', payload[:4])[0]
    if n > 8192 or n + 4 >= len(payload):
        raise ValueError('invalid audio header')
    h = json.loads(payload[4:4+n])
    pcm = payload[4+n:]
    if not IDENT.fullmatch(str(h.get('session_id', ''))):
        raise ValueError('invalid session')
    if h.get('sample_rate') != 16000 or h.get('format') != 'S16_LE' or len(pcm) % 2:
        raise ValueError('unsupported PCM')
    if type(h.get('seq')) is not int or h['seq'] < 0:
        raise ValueError('invalid sequence')
    return h, pcm


def mqtt(role, on_message, subscriptions, inflight=8, queued=8, nodelay=True):
    """Paho client tuned the way the ABC transport was measured to work.

    Reference (same hosts, same broker, same tunnel):
      * ABC keeps image traffic on QoS0 and results on QoS1, each on its own
        connection, with TCP_NODELAY and inflight 8; with a 12 Hz vision run
        active that measured P50 87 ms / P95 123 ms on the Pi.
      * This module originally used one connection, QoS1 audio and inflight 2.
        Under vision load the inflight window filled and audio round-trip grew to
        ~900 ms while the worker stayed idle — an application-level stall, not
        compute and not bandwidth.
      * Broker side is already tuned (max_inflight 8, max_queued 8,
        set_tcp_nodelay true) and is shared, read-only.
    """
    import os
    import paho.mqtt.client as m
    from socket import IPPROTO_TCP, TCP_NODELAY
    c = m.Client(m.CallbackAPIVersion.VERSION2,
                 client_id='mm-audio-'+role+'-'+uuid.uuid4().hex[:8], protocol=m.MQTTv5)
    c.max_queued_messages_set(queued)
    c.max_inflight_messages_set(inflight)
    c.reconnect_delay_set(1, 10)

    def on_socket_open(client, userdata, sock):
        if nodelay:
            try:
                sock.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
            except OSError:
                pass

    def connected(client, userdata, flags, rc, properties):
        if not rc.is_failure:
            for topic in subscriptions:
                client.subscribe(topic, qos=1)

    c.on_socket_open = on_socket_open
    c.on_connect = connected
    c.on_message = on_message
    host = os.environ.get('MM_MQTT_HOST', '127.0.0.1')
    port = int(os.environ.get('MM_MQTT_PORT', '28883'))
    c.connect_async(host, port, keepalive=20)
    c.loop_start()
    return c


def publish(c, suffix, data, expiry=15, qos=1):
    from paho.mqtt.properties import Properties
    from paho.mqtt.packettypes import PacketTypes
    if not c.is_connected():
        return False
    p = Properties(PacketTypes.PUBLISH)
    p.MessageExpiryInterval = expiry
    payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    return c.publish(TOPIC+'/'+suffix, payload, qos=qos, retain=False, properties=p).rc == 0


def _phrase_hit(text, stems, look_behind=3):
    """True when a stem occurs without an immediately preceding negation."""
    for stem in stems:
        start = text.find(stem)
        while start != -1:
            before = text[max(0, start - look_behind):start]
            if not any(w in before for w in ('不', '没', '别')):
                return True
            start = text.find(stem, start + 1)
    return False


CLAUSE_SPLIT = re.compile(r'[\s，。！？,.!?；;、]+')
PAST_MARKERS = ('刚才', '刚刚', '之前', '以前', '那时', '先前', '昨天')
NOW_MARKERS = ('现在', '目前', '当前', '这会儿', '眼下')
# "保持现在这样" is a preference template, not a time reference; do not let it
# move the scope window past the sensation clause.
NOW_FALSE_FRIENDS = ('保持现在', '维持现在', '就现在这样')
NEUTRAL_PHRASES = ('不冷不热', '不热不冷', '不冷也不热', '不热也不冷', '温度正好', '刚好适中')


def _state_in_clause(clause):
    """'cold' / 'hot' / 'neutral' / None for a single clause, honouring negation."""
    if any(x in clause for x in NEUTRAL_PHRASES):
        return 'neutral'
    hits = []
    for i, ch in enumerate(clause):
        if ch not in '冷热':
            continue
        before = clause[max(0, i - 2):i]
        if any(n in before for n in ('不', '没', '别')):
            continue
        hits.append(ch)
    cold, hot = hits.count('冷'), hits.count('热')
    if cold and not hot:
        return 'cold'
    if hot and not cold:
        return 'hot'
    return None


def thermal_candidate(text):
    """Conservative explicit feedback extraction, never a predicted comfort label.

    Scope rules (each one exists because a real utterance needed it):
      * a clause after the last 现在/目前 is the current state; a clause that
        starts from 刚才/之前 describes the past and is ignored for the *current* label;
      * negation is checked on the two characters right before 冷/热, so 不冷 and
        不太热 do not become colder labels;
      * the preference clause (希望暖一点) must NOT suppress the sensation: those
        are two different facts, and the first real test utterance ("我有点冷，
        希望暖一点") was wrongly dropped by an earlier blanket guard.
    """
    t = re.sub(r'<\|.*?\|>', '', text)
    clauses = [c for c in CLAUSE_SPLIT.split(t) if c]
    now_at = None
    for i, c in enumerate(clauses):
        probe = c
        for phrase in NOW_FALSE_FRIENDS:
            probe = probe.replace(phrase, '')
        if any(m in probe for m in NOW_MARKERS):
            now_at = i
    scope = clauses[now_at:] if now_at is not None else clauses
    sensation = None
    for clause in scope:
        if any(p in clause for p in PAST_MARKERS):
            continue
        state = _state_in_clause(clause)
        if state:
            sensation = state
    # Preference is scanned clause by clause (last clause wins) so that a "不"
    # belonging to a neighbouring clause cannot veto it: in "我不冷不热，保持现在
    # 这样" the 不 inside 不冷 must not cancel 保持现在.
    preference = None
    for clause in reversed(clauses):
        if _phrase_hit(clause, ('希望凉', '想凉', '凉一点', '凉一些', '凉快', '更凉', '降点温')):
            preference = 'cooler'
            break
        if _phrase_hit(clause, ('希望暖', '想暖', '暖一点', '暖一些', '暖和', '更暖', '升高一点')):
            preference = 'warmer'
            break
        if _phrase_hit(clause, ('保持现在', '保持这样', '不用调整', '不变', '别动', '就这样')):
            preference = 'no_change'
            break
    return {'sensation': sensation, 'preference': preference,
            'status': 'needs_user_confirmation', 'source': 'explicit_speech_rule_v3',
            'rule_scope': 'clauses_from_last_now_marker_skipping_past',
            'negation_checked': True}


SENTENCE_END = re.compile(r'[。！？!?；;]+')
SPEAKER_START = ('我', '我们', '咱们')


def split_sentences(text):
    """Group comma clauses into sentences when the ASR drops sentence punctuation.

    Real case (2026-11-03): three spoken sentences came back as
    "我有点热，希望凉一点，我不冷，刚才有点冷，现在不热了。" — commas only. Split
    on strong punctuation first, then start a new sentence when a clause begins
    a new first-person statement while the current group already carries a
    state or a preference (a bare "希望凉一点" keeps attaching to its state).
    """
    out = []
    for strong in SENTENCE_END.split(text or ''):
        group = []
        for part in [p.strip() for p in strong.split('，') if p.strip()]:
            if group:
                current = thermal_candidate(''.join(group))
                carries = bool(current['sensation'] or current['preference'])
                if carries and part.startswith(SPEAKER_START):
                    out.append(''.join(group))
                    group = [part]
                    continue
            group.append(part)
        if group:
            out.append(''.join(group))
    return [x for x in out if x.strip()]


def split_candidates(text):
    """One candidate per spoken sentence (see split_sentences for the reason)."""
    sentences = split_sentences(text)
    if not sentences:
        sentences = [(text or '').strip()]
    return [{'text': sentence, 'candidate': thermal_candidate(sentence)} for sentence in sentences]


SENSATIONS = (None, 'cold', 'neutral', 'hot')
PREFERENCES = (None, 'cooler', 'no_change', 'warmer')


class ConfirmationError(ValueError):
    pass


def check_confirmation(body):
    """Validate a user confirmation request; at least one label must be chosen."""
    sensation, preference = body.get('sensation'), body.get('preference')
    if sensation not in SENSATIONS:
        raise ConfirmationError('invalid sensation')
    if preference not in PREFERENCES:
        raise ConfirmationError('invalid preference')
    if sensation is None and preference is None:
        raise ConfirmationError('select at least one label')
    return sensation, preference


def classify_audio_packet(session_id, seq, header, retired_sessions=()):
    """Decide what an incoming audio chunk means for one worker's state.

    'retired'  a session this worker already flushed: buffer and ignore, so an
               old-round packet can never reopen a finished session;
    'duplicate' same session, sequence already seen (QoS1 replay / reorder);
    'continue'  same session, exactly the next sequence number;
    'gap'       same session, missing chunk(s): VAD and sample counters restart;
    'new_session' different session: full reset before consuming it.
    """
    sid, sq = header['session_id'], header['seq']
    if sid in retired_sessions:
        return 'retired'
    if session_id == sid and sq <= seq:
        return 'duplicate'
    if session_id == sid and sq == seq + 1:
        return 'continue'
    if session_id == sid:
        return 'gap'
    return 'new_session'


def confirm_transcript(events, confirmed, body):
    """Idempotent confirmation of one transcript event.

    `confirmed` maps transcript event_id -> confirmation event. A repeated POST
    returns the first confirmation instead of creating a duplicate, so a double
    tap, a retried request or a page refresh cannot double-count a label.
    """
    sensation, preference = check_confirmation(body)
    event_id = body.get('event_id')
    if event_id in confirmed:
        return confirmed[event_id], False
    source = next((x for x in events if x.get('event_id') == event_id
                   and x.get('kind') == 'transcript'), None)
    if source is None:
        raise ConfirmationError('unknown transcript')
    text = body.get('transcript')
    if text is not None and (not isinstance(text, str) or len(text) > 500):
        raise ConfirmationError('invalid transcript correction')
    x = event('confirmed_feedback', source['session_id'],
              evidence_refs=[source['event_id']], sensation=sensation, preference=preference,
              asr_text=source.get('text'),
              transcript_override=text if text and text != source.get('text') else None,
              confirmation_method='explicit_user_selection', source=source.get('source'))
    confirmed[event_id] = x
    return x, True


# Activity vocabulary actually produced by the ABC pipeline
# (vendor/algorithm/temporal_v1/temporal.py: CLASSES = ['other','sit_down','stand_up','walking_interaction']).
# posture.state comes from lab/posture.py: lying / seated / standing / unknown.
MOVING_ACTIONS = ('walking_interaction', 'sit_down', 'stand_up')
FOOTSTEP_WORDS = ('footstep', 'footsteps', 'walk', 'walking')
SPEECH_WORDS = ('speech', 'conversation', 'talking', 'narration', 'speech_synthesizer')


def action_value(track):
    """Read the real ABC structure ({'value': ..., 'reason': ...}), not a stringified dict."""
    action = track.get('action')
    if isinstance(action, dict):
        return str(action.get('value') or 'unknown').lower()
    return str(action or 'unknown').lower()


def posture_state(track):
    posture = track.get('posture')
    if isinstance(posture, dict):
        return str(posture.get('state') or 'unknown').lower()
    return str(posture or 'unknown').lower()


def fuse(audio, visual, window_ns=4e9):
    """Coarse co-occurrence only.

    The Pi receives ABC observations over MQTT, so the visual timestamp is the
    receive time on the Pi, not the camera exposure time. This links "there was
    a visual observation of this run shortly before this audio window ended";
    it does NOT identify who spoke, and it does not synchronise clocks.
    """
    h = audio.get('source', {})
    delta_ns = None
    if visual and h.get('pi_end_mono_ns') is not None \
            and visual.get('pi_received_mono_ns') is not None:
        delta_ns = h['pi_end_mono_ns'] - visual['pi_received_mono_ns']
    valid = bool(visual) and visual.get('run_id') == h.get('parent_run_id') \
        and delta_ns is not None and 0 <= delta_ns <= window_ns
    tracks = visual.get('tracks', []) if valid else []
    names = [str(x.get('name', '')) for x in audio.get('events', []) if x.get('score', 0) >= .2]
    moves = [t for t in tracks if action_value(t) in MOVING_ACTIONS]
    steps = any(any(w in n.lower() for w in FOOTSTEP_WORDS) for n in names)
    speech = any(n.lower() in SPEECH_WORDS for n in names)
    if not valid:
        status = 'audio_only_visual_missing_or_stale'
    elif moves and steps:
        status = 'visible_motion_and_footstep_candidate'
    elif moves:
        status = 'visible_motion_without_footstep_candidate'
    elif tracks and speech:
        # Co-occurrence only. A phone playing speech next to a visible person
        # produces exactly this status, so the name must not read as attribution.
        status = 'speech_candidate_with_visible_person_not_attributed'
    elif tracks and steps:
        # Footsteps were detected but the visible track's action model says
        # "unknown" (it fires rarely). Calling this "without sound" would be
        # wrong: the sound candidate exists, the motion evidence does not.
        status = 'footstep_candidate_with_visible_person_not_attributed'
    elif tracks:
        status = 'people_visible_without_audio_candidate'
    elif steps or speech:
        status = 'audio_candidate_without_visible_person'
    else:
        status = 'no_joint_event_confirmed'
    return {'status': status, 'person_count': len(tracks) if valid else None,
            'visual_ref': {'run_id': visual['run_id'], 'frame_id': visual.get('source_frame_id'),
                           'actions': sorted({action_value(t) for t in tracks}),
                           'postures': sorted({posture_state(t) for t in tracks})} if valid else None,
            'audio_ref': audio.get('event_id'), 'person_attribution': None,
            'window_ns': window_ns, 'receive_delta_ms': None if delta_ns is None else delta_ns / 1e6,
            'alignment': 'coarse_pi_receive_time_4s_window_not_exposure_sync',
            'rule_version': 'cooccurrence-v3-named-evidence',
            'joint_probability': None,
            'silence_is_not_negative_evidence': True}
