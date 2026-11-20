"""网关 HTTP 层的回归测试（需要 aiohttp）。

跑法：python -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import env_gateway  # noqa: E402


def measurement(seq=1, temperature_c=23.4, humidity_rh=41.2, **overrides):
    event = {
        'schema_version': 1,
        'device_id': 'env01',
        'sensor_id': 'sht4x',
        'zone_id': 'lab',
        'boot_id': 7,
        'seq': seq,
        'event_id': 'env01-7-%d' % seq,
        'device_uptime_ms': 12345,
        'measured_at_ms': int(time.time() * 1000),
        'clock_status': 'synced',
        'values': {'temperature_c': temperature_c, 'humidity_rh': humidity_rh},
        'units': {'temperature_c': 'degC', 'humidity_rh': '%RH'},
        'quality': 'ok',
        'error_code': None,
        'firmware_version': 'esphome-2026.10.2',
    }
    event.update(overrides)
    return event


def device_status(seq=1):
    return {'schema_version': 1, 'device_id': 'env01', 'zone_id': 'lab',
            'boot_id': 7, 'seq': seq, 'event_id': 'env01-7-%d' % seq,
            'uptime_ms': 5000, 'clock_status': 'synced', 'wifi_rssi_dbm': -61,
            'ip': None, 'firmware_version': 'esphome-2026.10.2'}


class GatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = env_gateway.build_app(str(Path(self.tmp.name) / 'env.sqlite3'))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_post_accepts_then_dedupes(self):
        first = await self.client.post('/env/v1/measurements', json=measurement())
        self.assertEqual(first.status, 201)
        self.assertFalse((await first.json())['duplicate'])

        second = await self.client.post('/env/v1/measurements', json=measurement())
        self.assertEqual(second.status, 200)
        self.assertTrue((await second.json())['duplicate'])

    async def test_invalid_event_rejected_with_reason(self):
        bad = measurement(event_id='env01-7-999')
        resp = await self.client.post('/env/v1/measurements', json=bad)
        self.assertEqual(resp.status, 400)
        self.assertIn('event_id', (await resp.json())['error'])

    async def test_non_json_body_rejected(self):
        resp = await self.client.post('/env/v1/measurements', data='not json',
                                      headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status, 400)

    async def test_latest_reports_value_and_age(self):
        await self.client.post('/env/v1/measurements', json=measurement())
        await self.client.post('/env/v1/status', json=device_status())
        resp = await self.client.get('/env/v1/latest?zone_id=lab')
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        temperature = body['fields']['temperature_c']
        self.assertTrue(temperature['present'])
        self.assertEqual(temperature['value'], 23.4)
        self.assertFalse(temperature['stale'])
        self.assertTrue(body['device']['online'])

    async def test_history_returns_rows_and_unit(self):
        for seq in (1, 2, 3):
            await self.client.post('/env/v1/measurements',
                                   json=measurement(seq=seq, temperature_c=20.0 + seq))
        resp = await self.client.get(
            '/env/v1/history?zone_id=lab&field=temperature_c&minutes=10')
        body = await resp.json()
        self.assertEqual(body['count'], 3)
        self.assertEqual([row['value'] for row in body['rows']], [21.0, 22.0, 23.0])
        self.assertEqual(body['rows'][0]['unit'], 'degC')

    async def test_history_malformed_parameters_are_client_errors(self):
        for query in ('since_ms=bad', 'until_ms=bad', 'minutes=nan', 'minutes=inf', 'limit=0', 'since_ms=20&until_ms=10'):
            response = await self.client.get('/env/v1/history?' + query)
            self.assertEqual(response.status, 400, query)
            self.assertFalse((await response.json())['ok'])

    async def test_health_reports_schema_version(self):
        resp = await self.client.get('/env/v1/health')
        body = await resp.json()
        self.assertEqual(body['schema_version'], 1)
        self.assertFalse(body['auth'])


class GatewayAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = env_gateway.build_app(str(Path(self.tmp.name) / 'env.sqlite3'),
                                         token='secret-token')
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_missing_token_rejected(self):
        resp = await self.client.post('/env/v1/measurements', json=measurement())
        self.assertEqual(resp.status, 401)

    async def test_correct_token_accepted(self):
        resp = await self.client.post('/env/v1/measurements', json=measurement(),
                                      headers={'X-Env-Token': 'secret-token'})
        self.assertEqual(resp.status, 201)


if __name__ == '__main__':
    unittest.main()
