"""在终端里实时看网关的最新环境值。

用法：
    python tools/watch_env.py
    python tools/watch_env.py --url http://192.0.2.11:28984 --interval 5
"""
import argparse
import json
import sys
import time
import urllib.request


# 本机设了 HTTP_PROXY 时，urllib 会把局域网地址也丢给代理，查 Pi 会一直挂住。
# 这个工具只查内网网关，直接禁用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url):
    with _OPENER.open(url, timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='实时查看环境网关最新值')
    parser.add_argument('--url', default='http://127.0.0.1:28984')
    parser.add_argument('--zone', default='lab')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--count', type=int, default=0, help='0 表示一直看')
    args = parser.parse_args(argv)

    base = args.url.rstrip('/')
    shown = 0
    print('%-8s %-10s %-9s %-9s %-8s %s'
          % ('time', 'temp_C', 'rh_%', 'co2_ppm', 'age_s', 'device'))
    while args.count <= 0 or shown < args.count:
        try:
            latest = fetch('%s/env/v1/latest?zone_id=%s' % (base, args.zone))
        except Exception as exc:  # 网关没起来时只报一行，不退出
            print('! gateway unreachable: %s' % exc, flush=True)
            time.sleep(max(args.interval, 2.0))
            continue

        fields = latest['fields']

        def cell(name):
            item = fields[name]
            if not item['present']:
                return '—'
            if item['quality'] != 'ok':
                return 'invalid'
            return '%.2f' % item['value']

        ages = [fields[name]['age_ms'] / 1000.0 for name in fields
                if fields[name]['age_ms'] is not None]
        age = '%.1f' % min(ages) if ages else '—'
        device = latest['device']
        state = 'online' if device['online'] else 'offline'
        print('%-8s %-10s %-9s %-9s %-8s %s (%s)'
              % (time.strftime('%H:%M:%S'), cell('temperature_c'),
                 cell('humidity_rh'), cell('co2_ppm'), age,
                 device['device_id'] or '—', state), flush=True)
        shown += 1
        if args.count <= 0 or shown < args.count:
            time.sleep(args.interval)
    return 0


if __name__ == '__main__':
    sys.exit(main())
