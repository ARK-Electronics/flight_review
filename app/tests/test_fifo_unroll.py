"""Tests for FIFO packet unrolling and plot gating."""
#pylint: disable=invalid-name

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)

from config import plot_config, colors3  # noqa: E402  pylint: disable=wrong-import-position
from plotting import (  # noqa: E402  pylint: disable=wrong-import-position
    DataPlotFFT, DataPlotSpec, add_virtual_fifo_topic_data,
    unroll_fifo_arrays, median_sampling_frequency_hz,
)

LOG100 = (
    '/mnt/c/Users/bluea/SynologyDrive/Research and Development/'
    'ARK Projects/ARK FPV/Flight Logs/ST IMU/log100.ulg'
)


def _fifo_packets(n_packets, n_samples=8, dt_us=130.20833, scale=0.002,
                  start_us=1000.0, include_samples=True):
    """Build a packed FIFO dict like pyulog's sensor_gyro_fifo dataset."""
    data = {
        'timestamp': (start_us + np.arange(n_packets) * n_samples * dt_us).astype(np.uint64),
        'timestamp_sample': (
            start_us + np.arange(n_packets) * n_samples * dt_us + (n_samples - 1) * dt_us
        ).astype(np.float64),
        'dt': np.full(n_packets, dt_us, dtype=np.float32),
        'scale': np.full(n_packets, scale, dtype=np.float32),
    }
    if include_samples:
        data['samples'] = np.full(n_packets, n_samples, dtype=np.uint8)
    for axis, offset in (('x', 1.0), ('y', 2.0), ('z', 3.0)):
        for idx in range(32):
            # Raw int16 counts; unroll multiplies by scale.
            data['{}[{:d}]'.format(axis, idx)] = np.full(
                n_packets, (offset + idx) / scale, dtype=np.float64)
    return data


class FifoUnrollTests(unittest.TestCase):
    def test_unroll_constant_sample_count(self):
        data = _fifo_packets(4, n_samples=8)
        time_us, x_new, y_new, z_new = unroll_fifo_arrays(data)
        self.assertEqual(len(time_us), 4 * 8)
        dt = np.diff(time_us)
        self.assertTrue(np.allclose(dt, 130.20833, atol=1e-3))
        self.assertTrue(np.allclose(x_new[0:8], np.arange(1.0, 9.0)))
        self.assertTrue(np.allclose(y_new[0:8], np.arange(2.0, 10.0)))
        self.assertTrue(np.allclose(z_new[0:8], np.arange(3.0, 11.0)))
        hz = median_sampling_frequency_hz(time_us)
        self.assertGreater(hz, 7000.0)

    def test_unroll_variable_sample_count(self):
        data = _fifo_packets(3, n_samples=10)
        data['samples'] = np.array([4, 10, 6], dtype=np.uint8)
        time_us, x_new, _, _ = unroll_fifo_arrays(data)
        self.assertEqual(len(time_us), 4 + 10 + 6)
        # First packet only uses x[0]..x[3]
        self.assertTrue(np.allclose(x_new[0:4], np.arange(1.0, 5.0)))

    def test_unroll_without_samples_field_uses_all_slots(self):
        data = _fifo_packets(2, n_samples=32, include_samples=False)
        time_us, x_new, _, _ = unroll_fifo_arrays(data)
        self.assertEqual(len(time_us), 2 * 32)
        self.assertEqual(len(x_new), 2 * 32)

    def test_unroll_strides_when_over_budget(self):
        data = _fifo_packets(20, n_samples=10)
        time_us, _, _, _ = unroll_fifo_arrays(data, max_samples=50)
        # 20 packets * 10 samples = 200, budget 50 -> stride 4 -> 5 packets * 10
        self.assertEqual(len(time_us), 50)
        hz = median_sampling_frequency_hz(time_us)
        # Intra-packet dt is unchanged, so FFT still sees the FIFO ODR.
        self.assertGreater(hz, 7000.0)

    def test_virtual_topic_and_fft_plot(self):
        class FakeULog:
            def __init__(self):
                self.data_list = []
                self._ds = type('D', (), {})()
                self._ds.name = 'sensor_gyro_fifo'
                self._ds.multi_id = 0
                self._ds.data = _fifo_packets(64, n_samples=8)

            def get_dataset(self, name, instance=0):
                self.assert_name = name
                self.assert_instance = instance
                return self._ds

        ulog = FakeULog()
        self.assertTrue(add_virtual_fifo_topic_data(ulog, 'sensor_gyro_fifo', 0))
        virtual = ulog.data_list[-1]
        self.assertEqual(virtual.name, 'sensor_gyro_fifo_virtual')
        self.assertEqual(len(virtual.data['x']), 64 * 8)
        plot = DataPlotFFT([virtual], plot_config, 'sensor_gyro_fifo_virtual',
                           title='Raw Gyro FFT (FIFO, IMU0)')
        plot.add_graph(['x', 'y', 'z'], colors3, ['X', 'Y', 'Z'])
        self.assertFalse(plot.had_error)
        self.assertIsNotNone(plot.finalize())
        spec = DataPlotSpec([virtual], plot_config, 'sensor_gyro_fifo_virtual',
                            title='Gyro Power Spectral Density (FIFO, IMU0)')
        spec.add_graph(['x', 'y', 'z'], ['X', 'Y', 'Z'])
        self.assertFalse(spec.had_error)
        self.assertIsNotNone(spec.finalize())


@unittest.skipUnless(os.path.isfile(LOG100), 'log100.ulg not present')
class Log100FifoTests(unittest.TestCase):
    def test_log100_unroll_and_fft(self):
        from helper import ULOG_MSG_FILTER  # pylint: disable=import-outside-toplevel
        from logs.ulog_parse import parse_ulog  # pylint: disable=import-outside-toplevel

        ulog = parse_ulog(LOG100, ULOG_MSG_FILTER)
        names = {d.name for d in ulog.data_list}
        self.assertIn('sensor_gyro_fifo', names)
        self.assertTrue(add_virtual_fifo_topic_data(ulog, 'sensor_gyro_fifo', 0))
        virtual = [d for d in ulog.data_list if d.name == 'sensor_gyro_fifo_virtual'][0]
        hz = median_sampling_frequency_hz(virtual.data['timestamp'])
        self.assertGreater(hz, 1000.0)
        plot = DataPlotFFT([virtual], plot_config, 'sensor_gyro_fifo_virtual',
                           title='Raw Gyro FFT (FIFO, IMU0)')
        plot.add_graph(['x', 'y', 'z'], colors3, ['X', 'Y', 'Z'])
        self.assertFalse(plot.had_error)
        self.assertIsNotNone(plot.finalize())


if __name__ == '__main__':
    unittest.main()
