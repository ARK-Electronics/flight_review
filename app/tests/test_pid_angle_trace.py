"""Attitude PID step-response must not treat 5–15 deg steps as noise."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_PLOT_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plot_app')
if _PLOT_APP not in sys.path:
    sys.path.insert(0, _PLOT_APP)


def _synthetic_step(duration_s=8.0, hz=100.0, amplitude=10.0, delay_s=0.08):
    """Square-wave setpoint with a delayed first-order-ish response."""
    n = int(duration_s * hz)
    time_s = np.linspace(0.0, duration_s, n, dtype=np.float64)
    period = 2.0
    setpoint = np.where((time_s % period) < period / 2.0, amplitude, 0.0)
    # Simple lag: shift by delay samples (step response should rise toward 1)
    shift = int(delay_s * hz)
    measured = np.concatenate((np.zeros(shift), setpoint[:-shift]))
    throttle = np.full(n, 50.0)
    return time_s, measured, setpoint, throttle


class AttitudeTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from pid_analysis import Trace  # pylint: disable=import-outside-toplevel
            from pid_step_data import (  # pylint: disable=import-outside-toplevel
                ANGLE_HIGH_INPUT_DEG, ANGLE_MIN_INPUT_DEG)
        except ImportError as exc:
            raise unittest.SkipTest('scipy/pid_analysis not available: {}'.format(exc))
        cls.Trace = Trace
        cls.ANGLE_HIGH_INPUT_DEG = ANGLE_HIGH_INPUT_DEG
        cls.ANGLE_MIN_INPUT_DEG = ANGLE_MIN_INPUT_DEG

    def test_default_rate_threshold_flattens_10deg_attitude_steps(self):
        time_s, measured, setpoint, throttle = _synthetic_step(amplitude=10.0)
        trace = self.Trace('roll', time_s, measured, setpoint, throttle)
        # Legacy 20 deg/s noise floor: 10 deg steps are discarded -> zero line
        self.assertLess(float(np.max(np.abs(trace.resp_low[0]))), 0.05)

    def test_angle_thresholds_recover_10deg_step_response(self):
        time_s, measured, setpoint, throttle = _synthetic_step(amplitude=10.0)
        trace = self.Trace(
            'roll', time_s, measured, setpoint, throttle,
            high_input_threshold=self.ANGLE_HIGH_INPUT_DEG,
            min_input_threshold=self.ANGLE_MIN_INPUT_DEG)
        peak = float(np.max(trace.resp_low[0]))
        self.assertGreater(peak, 0.4)
        self.assertLess(peak, 2.0)


if __name__ == '__main__':
    unittest.main()
