"""Tests for memory-bounded ULog parsing."""
#pylint: disable=protected-access,invalid-name

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)

from logs import ulog_parse  # noqa: E402  pylint: disable=wrong-import-position
from pyulog import ULog  # noqa: E402  pylint: disable=wrong-import-position


ULOG_HEADER = b'\x55\x4c\x6f\x67\x01\x12\x35'


def _msg(msg_type: int, payload: bytes) -> bytes:
    return struct.pack('<HB', len(payload), msg_type) + payload


def write_synthetic_ulog(path: str, n_status: int = 5, n_fifo: int = 0,
                         n_logged: int = 0) -> None:
    """Write a tiny but valid .ulg with vehicle_status and optional FIFO data."""
    buf = bytearray()
    buf.extend(ULOG_HEADER)
    buf.extend(struct.pack('B', 1))
    buf.extend(struct.pack('<Q', 1000))

    buf.extend(_msg(ord('B'), bytes(8) + bytes(8) + struct.pack('<QQQ', 0, 0, 0)))
    buf.extend(_msg(ord('F'), b'vehicle_status:uint64_t timestamp;uint8_t nav_state;'))
    buf.extend(_msg(ord('F'), b'sensor_gyro_fifo:uint64_t timestamp;int16_t x;'))

    key = b'char[3] sys_name'
    buf.extend(_msg(ord('I'), bytes([len(key)]) + key + b'PX4'))
    pkey = b'int32_t SYS_AUTOSTART'
    buf.extend(_msg(ord('P'), bytes([len(pkey)]) + pkey + struct.pack('<i', 4001)))

    buf.extend(_msg(ord('A'), struct.pack('<BH', 0, 1) + b'vehicle_status'))
    if n_fifo:
        buf.extend(_msg(ord('A'), struct.pack('<BH', 0, 2) + b'sensor_gyro_fifo'))

    for i in range(n_status):
        ts = 1000 + i * 1000
        payload = struct.pack('<H', 1) + struct.pack('<Q', ts) + struct.pack('B', 2)
        buf.extend(_msg(ord('D'), payload))

    for i in range(n_fifo):
        ts = 1000 + i * 100
        payload = struct.pack('<H', 2) + struct.pack('<Q', ts) + struct.pack('<h', i % 100)
        buf.extend(_msg(ord('D'), payload))

    for i in range(n_logged):
        payload = struct.pack('<BQ', ord('6'), 1000 + i) + b'hello'
        buf.extend(_msg(ord('L'), payload))

    with open(path, 'wb') as handle:
        handle.write(buf)


class UlogParseTests(unittest.TestCase):
    def test_synthetic_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=8)
            ulog = ulog_parse.parse_ulog(path, ['vehicle_status'])
            status = ulog.get_dataset('vehicle_status')
            self.assertEqual(len(status.data['timestamp']), 8)
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')
            self.assertEqual(ulog.initial_parameters.get('SYS_AUTOSTART'), 4001)

    def test_topic_buffer_cap_truncates_instead_of_oom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'big.ulg')
            write_synthetic_ulog(path, n_status=400)
            # 9 bytes/sample; cap at ~12 samples and ensure we still parse.
            with mock.patch.object(ulog_parse, 'MAX_TOPIC_BUFFER_BYTES', 12 * 9), \
                    mock.patch.object(ulog_parse, 'MAX_NON_CORE_TOPIC_BUFFER_BYTES', 12 * 9), \
                    mock.patch.object(ulog_parse, 'MAX_TOTAL_BUFFER_BYTES', 12 * 9), \
                    mock.patch.object(ulog_parse, 'CORE_RESERVE_BYTES', 0):
                ulog = ulog_parse.SafeULog(path, ['vehicle_status'],
                                           disable_str_exceptions=True)
            status = ulog.get_dataset('vehicle_status')
            n_samples = len(status.data['timestamp'])
            self.assertGreater(n_samples, 0)
            self.assertLess(n_samples, 400)
            # Timestamp still tracks the end of the log (samples are strided,
            # not only the first N).
            self.assertGreater(int(ulog.last_timestamp), 1000)

    def test_upload_parse_large_file_is_header_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'huge.ulg')
            write_synthetic_ulog(path, n_status=4, n_fifo=20)
            with mock.patch.object(ulog_parse, 'UPLOAD_SCAN_MAX_BYTES', 1):
                ulog = ulog_parse.parse_ulog_for_upload(path)
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')
            self.assertEqual(ulog.data_list, [])

    def test_upload_parse_skips_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fifo.ulg')
            write_synthetic_ulog(path, n_status=4, n_fifo=50)
            ulog = ulog_parse.parse_ulog_for_upload(path)
            names = {d.name for d in ulog.data_list}
            self.assertIn('vehicle_status', names)
            self.assertNotIn('sensor_gyro_fifo', names)
            self.assertEqual(len(ulog.get_dataset('vehicle_status').data['timestamp']), 4)

    def test_full_parse_keeps_fifo_when_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fifo.ulg')
            write_synthetic_ulog(path, n_status=3, n_fifo=7)
            ulog = ulog_parse.parse_ulog(
                path, ['vehicle_status', 'sensor_gyro_fifo'])
            names = {d.name for d in ulog.data_list}
            self.assertEqual(names, {'vehicle_status', 'sensor_gyro_fifo'})
            self.assertEqual(len(ulog.get_dataset('sensor_gyro_fifo').data['timestamp']), 7)

    def test_logged_messages_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'logs.ulg')
            write_synthetic_ulog(path, n_status=1, n_logged=30)
            with mock.patch.object(ulog_parse, 'MAX_LOGGED_MESSAGES', 5):
                ulog = ulog_parse.SafeULog(path, ['vehicle_status'],
                                           disable_str_exceptions=True)
            self.assertEqual(len(ulog.logged_messages), 5)
            self.assertGreater(ulog.logged_messages.dropped, 0)

    def test_memoryerror_retries_smaller_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=3)
            full = ['vehicle_status', 'sensor_gyro_fifo']
            calls = {'n': 0}

            orig = ulog_parse.SafeULog

            class Flaky(orig):
                def __init__(self, log_file, message_name_filter_list=None,
                             disable_str_exceptions=True, parse_header_only=False):
                    calls['n'] += 1
                    if calls['n'] == 1:
                        raise MemoryError('simulated')
                    super().__init__(log_file, message_name_filter_list,
                                     disable_str_exceptions, parse_header_only)

            with mock.patch.object(ulog_parse, 'SafeULog', Flaky):
                # Small file: first attempt includes FIFO, which we fail on
                # purpose; the retry drops FIFO and succeeds.
                ulog = ulog_parse.parse_ulog(path, full)
            self.assertGreaterEqual(calls['n'], 2)
            self.assertEqual(len(ulog.get_dataset('vehicle_status').data['timestamp']), 3)

    def test_upload_parse_falls_back_to_header_on_memoryerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=3)

            class Boom(ulog_parse.SafeULog):
                def __init__(self, *args, **kwargs):
                    raise MemoryError('simulated')

            with mock.patch.object(ulog_parse, 'SafeULog', Boom):
                ulog = ulog_parse.parse_ulog_for_upload(path)
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')
            self.assertEqual(ulog.data_list, [])

    def test_header_only_has_metadata_without_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=6)
            ulog = ulog_parse.parse_ulog_header(path)
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')
            self.assertEqual(ulog.initial_parameters.get('SYS_AUTOSTART'), 4001)
            self.assertEqual(ulog.data_list, [])

    def test_filters_for_large_file_skip_full_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'big.ulg')
            write_synthetic_ulog(path, n_status=1)
            os.truncate(path, ulog_parse.LARGE_LOG_BYTES + 10)
            filters = ulog_parse.filters_for_file(
                path, ['vehicle_status', 'sensor_gyro_fifo', 'sensor_accel'])
            self.assertTrue(all('sensor_gyro_fifo' not in f for f in filters))

    def test_plain_ulog_still_parses_uncapped(self):
        """Sanity: stock pyulog accepts the synthetic file too."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=4)
            ulog = ULog(path, ['vehicle_status'], disable_str_exceptions=True)
            self.assertEqual(len(ulog.get_dataset('vehicle_status').data['timestamp']), 4)


if __name__ == '__main__':
    unittest.main()
