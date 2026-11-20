"""Compact copies for display; original inference observations remain unchanged."""


def next_preview_slot(timestamp, hz, previous_slot):
    if hz <= 0:
        return None
    slot = int(timestamp * hz + 1e-6)
    return slot if slot > previous_slot else None


def compact_observation(observation):
    keys = ('project', 'run_id', 'arm', 'source', 'source_frame_id', 'source_pts_s',
            'encoded_size', 'jpeg_sha256', 'kind', 'producer', 'status', 'infer_ms',
            'producer_instance_id', 'observation_seq')
    output = {k: observation[k] for k in keys if k in observation}
    output['tracks'] = []
    for track in observation.get('tracks', []):
        output['tracks'].append({
            'track_id': track['track_id'], 'bbox': track['bbox'],
            'posture': {k: track.get('posture', {})[k] for k in ('state', 'reason') if k in track.get('posture', {})},
            'action': {k: track.get('action', {})[k] for k in ('value', 'reason') if k in track.get('action', {})},
            'clothing': [{k: item[k] for k in ('category', 'confidence', 'ambiguous_with') if k in item}
                         for item in track.get('clothing', [])],
            'clothing_age_s': track.get('clothing_age_s'),
            'clo': track.get('clo'), 'met': track.get('met'),
            'estimate_status': track.get('estimate_status'),
        })
    return output
