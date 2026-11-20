"""环境事件契约与存储的回归测试（不依赖 aiohttp，纯标准库）。

跑法：python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import env_schema as S  # noqa: E402
import env_store  # noqa: E402

NOW = 1_800_000_000_000


def measurement(**overrides):
    event = {
        'schema_version': 1,
        'device_id': 'env01',
        'sensor_id': 'sht4x',
        'zone_id': 'lab',
        'boot_id': 7,
        'seq': 3,
        'event_id': 'env01-7-3',
        'device_uptime_ms': 12345,
        'measured_at_ms': NOW,
        'clock_status': 'synced',
        'values': {'temperature_c': 23.4, 'humidity_rh': 41.2},
        'units': {'temperature_c': 'degC', 'humidity_rh': '%RH'},
        'quality': 'ok',
        'error_code': None,
        'firmware_version': 'esphome-2026.10.2',
    }
    event.update(overrides)
    return event


def scd(**overrides):
    event = measurement(
        sensor_id='scd41', event_id='env01-7-4', seq=4,
        values={'co2_ppm': 812.0, 'temperature_c': 25.1, 'humidity_rh': 38.0},
        units={'co2_ppm': 'ppm', 'temperature_c': 'degC', 'humidity_rh': '%RH'})
    event.update(overrides)
    return event


def status(**overrides):
    event = {'schema_version': 1, 'device_id': 'env01', 'zone_id': 'lab',
             'boot_id': 7, 'seq': 9, 'event_id': 'env01-7-9', 'uptime_ms': 99_000,
             'clock_status': 'synced', 'wifi_rssi_dbm': -63, 'ip': None,
             'firmware_version': 'esphome-2026.10.2'}
    event.update(overrides)
    return event


class SchemaTests(unittest.TestCase):
    def test_valid_sht_measurement_normalizes(self):
        event = S.validate_measurement(measurement(), now_ms=NOW)
        self.assertEqual(event['values']['temperature_c'], 23.4)
        self.assertEqual(event['quality'], 'ok')
        self.assertIsNone(event['error_code'])

    def test_valid_scd_measurement_keeps_three_fields(self):
        event = S.validate_measurement(scd(), now_ms=NOW)
        self.assertEqual(set(event['values']), {'co2_ppm', 'temperature_c', 'humidity_rh'})

    def test_event_id_must_match_device_boot_seq(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(measurement(event_id='env01-7-99'), now_ms=NOW)

    def test_unsynced_clock_must_not_carry_device_timestamp(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(measurement(clock_status='unsynced'), now_ms=NOW)
        event = S.validate_measurement(
            measurement(clock_status='unsynced', measured_at_ms=None), now_ms=NOW)
        self.assertIsNone(event['measured_at_ms'])

    def test_synced_clock_requires_integer_timestamp(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(measurement(measured_at_ms=None), now_ms=NOW)

    def test_out_of_range_is_marked_invalid_and_not_clamped(self):
        event = S.validate_measurement(
            measurement(values={'temperature_c': 120.0, 'humidity_rh': 41.2}),
            now_ms=NOW)
        self.assertEqual(event['quality'], 'invalid')
        self.assertEqual(event['error_code'], 'out_of_range')
        self.assertEqual(event['values']['temperature_c'], 120.0)

    def test_non_finite_is_marked_invalid_and_not_reported_as_number(self):
        event = S.validate_measurement(
            measurement(values={'temperature_c': float('inf'), 'humidity_rh': 41.2}),
            now_ms=NOW)
        self.assertEqual(event['quality'], 'invalid')
        self.assertEqual(event['error_code'], 'non_finite')
        self.assertIsNone(event['values']['temperature_c'])

    def test_null_value_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(
                measurement(values={'temperature_c': None, 'humidity_rh': 41.2}),
                now_ms=NOW)

    def test_wrong_unit_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(
                measurement(units={'temperature_c': 'F', 'humidity_rh': '%RH'}),
                now_ms=NOW)

    def test_sensor_cannot_report_field_it_does_not_own(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(
                measurement(values={'co2_ppm': 800.0},
                            units={'co2_ppm': 'ppm'}), now_ms=NOW)

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(measurement(extra_field=1), now_ms=NOW)

    def test_clock_far_in_the_future_is_invalid(self):
        event = S.validate_measurement(
            measurement(measured_at_ms=NOW + 3600_000), now_ms=NOW)
        self.assertEqual(event['quality'], 'invalid')
        self.assertEqual(event['error_code'], 'clock_ahead')

    def test_invalid_quality_needs_error_code(self):
        with self.assertRaises(S.SchemaError):
            S.validate_measurement(measurement(quality='invalid'), now_ms=NOW)

    def test_status_roundtrip(self):
        event = S.validate_status(status(), now_ms=NOW)
        self.assertEqual(event['wifi_rssi_dbm'], -63)
        self.assertIsNone(event['ip'])

    def test_status_rejects_impossible_rssi(self):
        with self.assertRaises(S.SchemaError):
            S.validate_status(status(wifi_rssi_dbm=30), now_ms=NOW)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.store = env_store.EnvStore(Path(self.tmp.name) / 'env.sqlite3',
                                        clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, raw, received_at_ms=None):
        event = S.validate_measurement(raw, now_ms=self.now)
        return self.store.insert(event, received_at_ms=received_at_ms)

    def test_duplicate_event_id_is_ignored(self):
        self.assertTrue(self.add(measurement()))
        self.assertFalse(self.add(measurement()))
        self.assertEqual(self.store.stats()['measurements'], 1)

    def test_latest_uses_newest_row_per_field(self):
        self.add(measurement())
        self.now += 2000
        self.add(measurement(seq=4, event_id='env01-7-4', measured_at_ms=self.now,
                             values={'temperature_c': 24.0, 'humidity_rh': 42.0}))
        latest = self.store.latest('lab')
        self.assertEqual(latest['fields']['temperature_c']['value'], 24.0)
        self.assertEqual(latest['fields']['temperature_c']['age_ms'], 0)
        self.assertFalse(latest['fields']['temperature_c']['stale'])

    def test_stale_threshold_differs_for_co2(self):
        self.add(scd())
        self.now += 12_000
        latest = self.store.latest('lab')
        self.assertTrue(latest['fields']['temperature_c']['stale'])
        self.assertFalse(latest['fields']['co2_ppm']['stale'])
        self.now += 10_000
        latest = self.store.latest('lab')
        self.assertTrue(latest['fields']['co2_ppm']['stale'])

    def test_missing_field_is_reported_missing_not_zero(self):
        self.add(measurement())
        latest = self.store.latest('lab')
        self.assertFalse(latest['fields']['co2_ppm']['present'])
        self.assertIsNone(latest['fields']['co2_ppm']['value'])

    def test_invalid_latest_is_not_replaced_by_older_value(self):
        self.add(measurement())
        self.now += 2000
        self.add(measurement(seq=5, event_id='env01-7-5', measured_at_ms=self.now,
                             values={'temperature_c': 200.0, 'humidity_rh': 41.0}))
        latest = self.store.latest('lab')
        self.assertEqual(latest['fields']['temperature_c']['value'], 200.0)
        self.assertEqual(latest['fields']['temperature_c']['quality'], 'invalid')

    def test_unsynced_measurement_ages_from_receive_time(self):
        self.add(measurement(clock_status='unsynced', measured_at_ms=None,
                             event_id='env01-7-6', seq=6), received_at_ms=self.now)
        self.now += 3000
        latest = self.store.latest('lab')
        self.assertEqual(latest['fields']['temperature_c']['age_ms'], 3000)
        self.assertEqual(latest['fields']['temperature_c']['source'], 'gateway_receive_time')

    def test_history_window_and_order(self):
        for i in range(5):
            self.now += 1000
            self.add(measurement(seq=10 + i, event_id='env01-7-%d' % (10 + i),
                                 measured_at_ms=self.now))
        rows = self.store.history('lab', since_ms=NOW + 2000, until_ms=NOW + 4000,
                                  field='temperature_c')
        self.assertEqual(len(rows), 3)
        times = [row['data_time_ms'] for row in rows]
        self.assertEqual(times, sorted(times))

    def test_latest_temperature_comes_from_sht4x_even_if_scd_is_newer(self):
        self.add(measurement())
        self.now += 1000
        self.add(scd(measured_at_ms=self.now))
        latest = self.store.latest('lab')
        self.assertEqual(latest['fields']['temperature_c']['sensor_id'], 'sht4x')
        self.assertEqual(latest['fields']['temperature_c']['value'], 23.4)
        self.assertEqual(
            latest['cross_check']['temperature_c_by_scd41']['value'], 25.1)
        self.assertEqual(latest['cross_check']['temperature_c_by_scd41']['note'],
                         'consistency_check_only')

    def test_temperature_missing_when_only_secondary_sensor_present(self):
        self.add(scd())
        latest = self.store.latest('lab')
        self.assertFalse(latest['fields']['temperature_c']['present'])
        self.assertTrue(latest['fields']['co2_ppm']['present'])
        self.assertEqual(latest['fields']['co2_ppm']['value'], 812.0)

    def test_device_online_window(self):
        self.store.insert_status(S.validate_status(status(), now_ms=self.now),
                                 received_at_ms=self.now)
        self.assertTrue(self.store.device('lab')['online'])
        self.now += 61_000
        self.assertFalse(self.store.device('lab')['online'])

    def test_prune_removes_only_old_rows(self):
        self.add(measurement())
        self.now += 10_000
        self.add(measurement(seq=11, event_id='env01-7-11', measured_at_ms=self.now))
        removed = self.store.prune(self.now - 5000)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.stats()['measurements'], 1)


if __name__ == '__main__':
    unittest.main()
