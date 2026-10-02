"""Betaflight Blackbox CSV exports from Blackbox Explorer and blackbox_decode."""
import os
import sys
import tempfile
import unittest

import numpy as np

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)

from logs.betaflight_csv import read_betaflight_csv  # noqa: E402  pylint: disable=wrong-import-position

# Blackbox Explorer "Export CSV": sysConfig "key","value" rows precede the columns.
EXPLORER = '''"Product","Blackbox flight data recorder by Nicholas Sherlock"
"firmwareType",3
"Firmware revision","Betaflight 4.5.1 (77d01ba3b) STM32H743"
"rollPID","45,80,40"
"debug_mode",""
"loopIteration","time","rcCommand[0]","rcCommand[1]","rcCommand[2]","rcCommand[3]",\
"gyroADC[0]","gyroADC[1]","gyroADC[2]","motor[0]","motor[1]","heading[0]"
0,1000000,250,0,0,500,90,0,-90,1500,2000,NaN
1,1000250,250,0,0,500,90,0,-90,1500,2000,NaN
'''

# blackbox_decode: unit suffixes, ", " separators and text flag columns.
DECODE = '''loopIteration, time (ms), rcCommand[0], gyroADC[0], gyroADC[1], gyroADC[2], \
motor[0], flightModeFlags (flags)
0, 1000, 250, 90, 0, -90, 1500, ANGLE_MODE
1, 1001, 250, 90, 0, -90, 1500, ANGLE_MODE|HORIZON
'''


class BetaflightCSVTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'log.csv')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write(text)
            return read_betaflight_csv(path)

    def test_blackbox_explorer_export_skips_header_rows(self):
        ulog = self.read(EXPLORER)
        self.assertEqual((ulog.start_timestamp, ulog.last_timestamp), (1000000, 1000250))
        rates = ulog.get_dataset('vehicle_angular_velocity').data
        np.testing.assert_allclose(rates['xyz[0]'], np.deg2rad([90, 90]))
        motors = ulog.get_dataset('actuator_motors').data
        np.testing.assert_allclose(motors['control[1]'], [1.0, 1.0])
        np.testing.assert_allclose(ulog.get_dataset('manual_control_setpoint').data['roll'], [0.5, 0.5])

    def test_blackbox_decode_units_and_text_columns(self):
        ulog = self.read(DECODE)
        self.assertEqual((ulog.start_timestamp, ulog.last_timestamp), (1000000, 1001000))
        rates = ulog.get_dataset('vehicle_angular_velocity').data
        np.testing.assert_allclose(rates['xyz[2]'], np.deg2rad([-90, -90]))

    def test_missing_time_column_is_rejected(self):
        with self.assertRaises(ValueError):
            self.read('"Product","Blackbox"\n"a","b","c"\n1,2,3\n')
        with self.assertRaises(ValueError):
            self.read('loopIteration,time,gyroADC[0]\n')


if __name__ == '__main__':
    unittest.main()
