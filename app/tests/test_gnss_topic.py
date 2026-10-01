"""Tests for reading GNSS data from vehicle_gnss and the older vehicle_gps_position."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)

from helper import ULOG_MSG_FILTER, GnssTopic, get_lat_lon_alt_deg  # noqa: E402  pylint: disable=wrong-import-position
from leaflet import ulog_to_polyline  # noqa: E402  pylint: disable=wrong-import-position
from logs.compat_ulog import CompatDataset, CompatULog  # noqa: E402  pylint: disable=wrong-import-position
from logs.ulog_parse import ULOG_UPLOAD_MSG_FILTER  # noqa: E402  pylint: disable=wrong-import-position

TIMESTAMPS = np.array([1_000_000, 2_000_000, 3_000_000], dtype=np.uint64)
LATITUDE = np.array([47.1, 47.2, 47.3])
LONGITUDE = np.array([8.1, 8.2, 8.3])
ALTITUDE = np.array([400.0, 410.0, 420.0])
FIX_TYPE = np.array([0, 3, 6], dtype=np.uint8)


def _ulog(name, prefix, latitude, longitude, altitude, fix_type):
    data = {
        'timestamp': TIMESTAMPS,
        prefix + latitude: LATITUDE,
        prefix + longitude: LONGITUDE,
        prefix + altitude: ALTITUDE,
        prefix + 'fix_type': FIX_TYPE,
    }
    return CompatULog([CompatDataset(name, data)], int(TIMESTAMPS[0]), int(TIMESTAMPS[-1]),
                      msg_info_dict={'ver_data_format': 2})


def vehicle_gnss_ulog():
    return _ulog('vehicle_gnss', 'receiver.', 'latitude', 'longitude', 'altitude_msl', 'fix_type')


def vehicle_gps_position_ulog():
    return _ulog('vehicle_gps_position', '', 'latitude_deg', 'longitude_deg', 'altitude_msl_m',
                 'fix_type')


class GnssTopicTests(unittest.TestCase):
    def test_vehicle_gnss(self):
        gnss = GnssTopic(vehicle_gnss_ulog())
        self.assertEqual(gnss.name, 'vehicle_gnss')
        self.assertEqual(gnss.field('speed_accuracy'), 'receiver.speed_accuracy')
        self.assertEqual(gnss.field('eph'), 'receiver.eph')

    def test_vehicle_gps_position(self):
        gnss = GnssTopic(vehicle_gps_position_ulog())
        self.assertEqual(gnss.name, 'vehicle_gps_position')
        self.assertEqual(gnss.field('speed_accuracy'), 's_variance_m_s')
        self.assertEqual(gnss.field('eph'), 'eph')

    def test_lat_lon_alt(self):
        for ulog in (vehicle_gnss_ulog(), vehicle_gps_position_ulog()):
            dataset = ulog.get_dataset(GnssTopic(ulog).name)
            lat, lon, alt = get_lat_lon_alt_deg(ulog, dataset)
            np.testing.assert_array_equal(lat, LATITUDE)
            np.testing.assert_array_equal(lon, LONGITUDE)
            np.testing.assert_array_equal(alt, ALTITUDE)

    def test_polyline_skips_samples_without_fix(self):
        for ulog in (vehicle_gnss_ulog(), vehicle_gps_position_ulog()):
            pos_datas, _ = ulog_to_polyline(ulog, [])
            self.assertEqual(pos_datas, [[47.2, 8.2], [47.3, 8.3]])

    def test_both_topics_are_loaded(self):
        for msg_filter in (ULOG_MSG_FILTER, ULOG_UPLOAD_MSG_FILTER):
            self.assertIn('vehicle_gnss', msg_filter)
            self.assertIn('vehicle_gps_position', msg_filter)


if __name__ == '__main__':
    unittest.main()
