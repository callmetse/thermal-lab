"""Replay a 16 kHz mono wav into the worker over MQTT, at real-time pace.

Lets us A/B the endpoint rule without a person in front of the microphone: the
worker cannot tell replayed audio from the edge's, and the session is tagged so
its results are easy to find in the journals afterwards.
"""
import argparse
import sys
import time
import wave

sys.path.insert(0, '/home/pi/mm_audio_v1')
from common import mqtt, pack, publish  # noqa: E402

CHUNK_BYTES = 8000          # 0.25 s of S16 mono at 16 kHz


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wav')
    parser.add_argument('--session', required=True)
    parser.add_argument('--factor', type=float, default=1.0)
    parser.add_argument('--tail', type=float, default=2.0,
                        help='seconds of digital silence appended so the VAD can close')
    args = parser.parse_args()

    with wave.open(args.wav, 'rb') as handle:
        assert handle.getframerate() == 16000 and handle.getnchannels() == 1
        pcm = handle.readframes(handle.getnframes())
    pcm += b'\x00\x00' * int(16000 * args.tail)

    audio = mqtt('replay-audio', lambda *a: None, [])
    commands = mqtt('replay-commands', lambda *a: None, [])
    time.sleep(2.0)

    started = time.time()
    seq = 0
    for offset in range(0, len(pcm), CHUNK_BYTES):
        raw = pcm[offset:offset + CHUNK_BYTES]
        now_ns = time.monotonic_ns()
        header = dict(session_id=args.session, seq=seq, sample_rate=16000, format='S16_LE',
                      pi_end_mono_ns=now_ns, pi_start_mono_ns=now_ns - int(len(raw) / 32000 * 1e9),
                      time_basis='replay_from_file', parent_run_id=None,
                      pi_wall_ms=time.time() * 1000, hardware_sample_rate=16000)
        publish(audio, 'audio', pack(header, raw), expiry=8, qos=0)
        seq += 1
        delay = started + len(raw) / 32000.0 * args.factor - time.time()
        if delay > 0:
            time.sleep(delay)
    last_chunk_ms = time.time() * 1000
    print('replayed %d chunks (%.1f s), last_chunk_wall_ms=%.0f'
          % (seq, len(pcm) / 32000.0, last_chunk_ms), flush=True)

    time.sleep(0.5)
    publish(commands, 'commands', {'op': 'flush', 'session_id': args.session, 'reason': 'replay_end'})
    time.sleep(2.5)
    audio.loop_stop()
    commands.loop_stop()


if __name__ == '__main__':
    main()
