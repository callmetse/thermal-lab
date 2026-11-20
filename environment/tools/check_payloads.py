"""把串口日志里的 JSON 报文拿回网关契约里校验一遍。

日志里能出现的报文有两类：
- `env-selftest`：启动时打印的样例，用来在没接传感器时核对格式；
- `env`: `post measurements` / `post status`：真实上报前打印的内容。
两者都走同一个 `env_publish.h`，这里用网关同一份 `env_schema.py` 校验。

用法：python tools/check_payloads.py results/serial_xxx.log
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import env_schema as S  # noqa: E402

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def payload_of(line):
    start = line.find('{')
    end = line.rfind('}')
    if start < 0 or end <= start:
        return None
    return line[start:end + 1]


def main(argv):
    if len(argv) < 2:
        print('usage: check_payloads.py <serial-log>')
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding='utf-8', errors='replace')
    print('log: %s  size: %d bytes' % (path, len(text)))

    checked = failures = 0
    for raw_line in text.splitlines():
        line = ANSI.sub('', raw_line)
        if 'env-selftest' not in line and 'post measurements' not in line and 'post status' not in line:
            continue
        payload = payload_of(line)
        if payload is None:
            continue
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            failures += 1
            print('FAIL json: %s\n      %s' % (exc, payload[:200]))
            continue
        try:
            if 'values' in raw:
                event = S.validate_measurement(raw)
                summary = '%-6s %s seq=%-3d %s' % (
                    'real' if 'post measurements' in line else 'sample',
                    event['sensor_id'], event['seq'],
                    json.dumps(event['values'], sort_keys=True))
            else:
                event = S.validate_status(raw)
                summary = '%-6s status rssi=%s clock=%s' % (
                    'real' if 'post status' in line else 'sample',
                    event['wifi_rssi_dbm'], event['clock_status'])
        except S.SchemaError as exc:
            failures += 1
            print('FAIL contract: %s\n      %s' % (exc, payload[:200]))
            continue
        checked += 1
        print('OK   %s' % summary)

    print('\nchecked=%d failures=%d' % (checked, failures))
    if not checked:
        print('没有找到报文：确认固件已烧录、串口是 COM6、抓到了启动阶段。')
        return 1
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
