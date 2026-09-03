"""Tests for FFT/spectrogram sampling-rate gating and raw IMU plots."""
#pylint: disable=protected-access,invalid-name

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)

from config import plot_config, colors3  # noqa: E402  pylint: disable=wrong-import-position
from helper import ULOG_MSG_FILTER  # noqa: E402  pylint: disable=wrong-import-position
from logs.ulog_parse import HIGH_RATE_TOPICS  # noqa: E402  pylint: disable=wrong-import-position
from plotting import (  # noqa: E402  pylint: disable=wrong-import-position
    DataPlotFFT, DataPlotSpec, MIN_FFT_SAMPLING_HZ,
    median_sampling_frequency_hz, dataset_sampling_frequency_hz,
)


class _FakeDataset:
    def __init__(self, name, data, multi_id=0):
        self.name = name
        self.multi_id = multi_id
        self.data = data


def _sine_dataset(name, hz, duration_s=1.0, field='x'):
    dt_us = int(round(1e6 / hz))
    n = int(duration_s * hz)
    timestamps = (np.arange(n, dtype=np.int64) * dt_us) + 1000
    t_s = timestamps.astype(np.float64) * 1e-6
    values = np.sin(2 * np.pi * 20.0 * t_s)
    return _FakeDataset(name, {
        'timestamp': timestamps,
        field: values,
        'y': values,
        'z': values,
        'gyro_rad[0]': values,
        'gyro_rad[1]': values,
        'gyro_rad[2]': values,
        'accelerometer_m_s2[0]': values,
        'accelerometer_m_s2[1]': values,
        'accelerometer_m_s2[2]': values,
    })


class SamplingFrequencyTests(unittest.TestCase):
    def test_median_ignores_dropout(self):
        # 200 Hz with one 20 ms dropout. Mean rate is pulled down; median is not.
        dt = np.full(199, 5000, dtype=np.int64)
        dt[100] = 20000
        ts = np.concatenate(([0], np.cumsum(dt)))
        hz = median_sampling_frequency_hz(ts)
        self.assertAlmostEqual(hz, 200.0, places=3)
        self.assertGreater(hz, MIN_FFT_SAMPLING_HZ)

    def test_zero_dt_is_not_a_rate(self):
        ts = np.array([1000, 1000, 1000], dtype=np.int64)
        self.assertEqual(median_sampling_frequency_hz(ts), 0.0)

    def test_prefers_timestamp_sample(self):
        dataset = _FakeDataset('sensor_gyro', {
            'timestamp': np.arange(10, dtype=np.int64) * 1000000,
            'timestamp_sample': np.arange(10, dtype=np.int64) * 5000,
        })
        self.assertAlmostEqual(dataset_sampling_frequency_hz(dataset), 200.0, places=3)


class FftPlotGatingTests(unittest.TestCase):
    def test_fft_shown_for_192hz_sensor_combined(self):
        dataset = _sine_dataset('sensor_combined', 192.0)
        plot = DataPlotFFT([dataset], plot_config, 'sensor_combined', title='Raw Gyro FFT')
        plot.add_graph(['gyro_rad[0]', 'gyro_rad[1]', 'gyro_rad[2]'],
                       colors3, ['X', 'Y', 'Z'])
        self.assertFalse(plot.had_error)
        self.assertIsNotNone(plot.finalize())

    def test_fft_hidden_below_100hz(self):
        dataset = _sine_dataset('vehicle_angular_velocity', 50.0, field='xyz[0]')
        dataset.data['xyz[1]'] = dataset.data['xyz[0]']
        dataset.data['xyz[2]'] = dataset.data['xyz[0]']
        plot = DataPlotFFT([dataset], plot_config, 'vehicle_angular_velocity',
                           title='Angular Velocity FFT')
        plot.add_graph(['xyz[0]', 'xyz[1]', 'xyz[2]'], colors3, ['X', 'Y', 'Z'])
        self.assertTrue(plot.had_error)
        self.assertIsNone(plot.finalize())

    def test_spectrogram_keeps_100hz_without_int_truncation(self):
        # 100.4 Hz used to become int(100.4)=100; 99.6 Hz became 99 and was dropped.
        dataset = _sine_dataset('sensor_combined', 100.4, duration_s=4.0)
        plot = DataPlotSpec([dataset], plot_config, 'sensor_combined',
                            title='Gyro Power Spectral Density')
        plot.add_graph(['gyro_rad[0]', 'gyro_rad[1]', 'gyro_rad[2]'], ['X', 'Y', 'Z'])
        self.assertFalse(plot.had_error)
        self.assertIsNotNone(plot.finalize())

    def test_sensor_gyro_is_parsed_for_plots(self):
        self.assertIn('sensor_gyro', ULOG_MSG_FILTER)
        self.assertIn('sensor_accel', ULOG_MSG_FILTER)
        self.assertIn('sensor_gyro', HIGH_RATE_TOPICS)


if __name__ == '__main__':
    unittest.main()
