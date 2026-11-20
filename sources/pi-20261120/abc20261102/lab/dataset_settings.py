"""Data-service settings and measurement units without importing the vision runtime."""
import json
import os
from pathlib import Path


def read_settings(root):
    path = Path(os.environ.get('ABC_CONFIG', Path(root) / 'config.json'))
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError('settings must be a JSON object')
    return data


def human_bytes(value):
    value = float(max(0, value))
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return '%d B' % value if unit == 'B' else '%.1f %s' % (value, unit)
        value /= 1024
