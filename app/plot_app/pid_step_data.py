"""Extract PID step-response traces and metrics used by plots and AI analysis."""

from __future__ import print_function

import numpy as np
from scipy.interpolate import interp1d

from helper import ActuatorControls
from pid_analysis import Trace

# pylint: disable=invalid-name

# Same sample budget as the PID analysis plots page. Very long high-rate logs
# allocate several arrays that scale with length * oversampling.
PID_MAX_SAMPLES = 600000


def _resample(time_array, data, desired_time):
    """Resample data at a given time to a vector of desired_time."""
    data_f = interp1d(time_array, data, fill_value='extrapolate')
    return data_f(desired_time)


def _decimate_to_budget(*arrays, max_samples=PID_MAX_SAMPLES):
    """Uniformly decimate parallel arrays so each has at most max_samples."""
    if not arrays:
        return arrays
    n = len(arrays[0])
    if n <= max_samples:
        return arrays
    step = int(np.ceil(n / max_samples))
    return tuple(a[::step] for a in arrays)


def _quat_axis_deg(qw, qx, qy, qz, axis):
    """Euler angle in degrees from quaternion components."""
    if axis == 'roll':
        return np.rad2deg(np.arctan2(2.0 * (qw * qx + qy * qz),
                                     1.0 - 2.0 * (qx * qx + qy * qy)))
    if axis == 'pitch':
        sinp = np.clip(2.0 * (qw * qy - qz * qx), -1.0, 1.0)
        return np.rad2deg(np.arcsin(sinp))
    if axis == 'yaw':
        return np.rad2deg(np.arctan2(2.0 * (qw * qz + qx * qy),
                                     1.0 - 2.0 * (qy * qy + qz * qz)))
    raise KeyError(axis)


def _attitude_axis_deg(dataset, axis):
    """Return roll/pitch/yaw in degrees from Euler fields or quaternion.

    Handles vehicle_attitude (roll / q[]) and vehicle_attitude_setpoint
    (roll_d / q_d[]) after or without px4_ulog.add_roll_pitch_yaw().
    """
    if axis in dataset.data:
        return np.rad2deg(dataset.data[axis])
    d_name = axis + '_d'
    if d_name in dataset.data:
        return np.rad2deg(dataset.data[d_name])
    for prefix in ('q', 'q_d'):
        try:
            qw = dataset.data[prefix + '[0]']
            qx = dataset.data[prefix + '[1]']
            qy = dataset.data[prefix + '[2]']
            qz = dataset.data[prefix + '[3]']
        except KeyError:
            continue
        return _quat_axis_deg(qw, qx, qy, qz, axis)
    raise KeyError(axis)


# Angle-loop step response: the rate analyzer's 20 deg/s noise floor would
# drop typical 5–15 deg attitude steps, leaving a flat zero plot.
ANGLE_MIN_INPUT_DEG = 2.0
ANGLE_HIGH_INPUT_DEG = 90.0


def _downsample_pairs(time_s, values, max_points=80):
    """Downsample two aligned arrays to at most max_points."""
    n = len(time_s)
    if n == 0:
        return [], []
    if n <= max_points:
        return [round(float(t), 4) for t in time_s], [round(float(v), 4) for v in values]
    indices = np.linspace(0, n - 1, max_points, dtype=int)
    return ([round(float(time_s[i]), 4) for i in indices],
            [round(float(values[i]), 4) for i in indices])


def step_response_metrics(time_resp, resp):
    """Compute rise time, overshoot, settling time, and related metrics.

    The step response is a reconstructed unit-step (target = 1.0).
    Returns a dict of scalar metrics. Missing crossings become None.
    """
    metrics = {
        'peak': None,
        'overshoot_pct': None,
        'peak_time_ms': None,
        'rise_time_10_90_ms': None,
        'response_time_ms': None,
        'settling_time_5pct_ms': None,
        'settling_time_2pct_ms': None,
        'final_value': None,
        'steady_state_error': None,
        'oscillation_crossings': 0,
        'undershoot': False,
    }
    if time_resp is None or resp is None:
        return metrics
    time_resp = np.asarray(time_resp, dtype=float)
    resp = np.asarray(resp, dtype=float)
    if time_resp.size == 0 or resp.size == 0 or time_resp.size != resp.size:
        return metrics

    peak_idx = int(np.argmax(resp))
    peak = float(resp[peak_idx])
    metrics['peak'] = round(peak, 4)
    metrics['overshoot_pct'] = round(max(0.0, (peak - 1.0) * 100.0), 2)
    metrics['peak_time_ms'] = round(float(time_resp[peak_idx]) * 1000.0, 1)
    metrics['final_value'] = round(float(resp[-1]), 4)
    metrics['steady_state_error'] = round(float(resp[-1] - 1.0), 4)
    metrics['undershoot'] = bool(peak < 0.95)

    def _first_crossing(threshold):
        idx = np.where(resp >= threshold)[0]
        if idx.size == 0:
            return None
        i = int(idx[0])
        if i == 0:
            return float(time_resp[0])
        y0, y1 = float(resp[i - 1]), float(resp[i])
        t0, t1 = float(time_resp[i - 1]), float(time_resp[i])
        if y1 == y0:
            return t1
        frac = (threshold - y0) / (y1 - y0)
        return t0 + frac * (t1 - t0)

    t10 = _first_crossing(0.1)
    t90 = _first_crossing(0.9)
    if t10 is not None and t90 is not None and t90 >= t10:
        metrics['rise_time_10_90_ms'] = round((t90 - t10) * 1000.0, 1)

    t1 = _first_crossing(1.0)
    if t1 is not None:
        metrics['response_time_ms'] = round(t1 * 1000.0, 1)

    def _settling_time(band):
        unsettle = np.where(np.abs(resp - 1.0) > band)[0]
        if unsettle.size == 0:
            return round(float(time_resp[0]) * 1000.0, 1)
        last = int(unsettle[-1])
        # If it never settles inside the window, report the last sample
        if last >= len(time_resp) - 1:
            return round(float(time_resp[-1]) * 1000.0, 1)
        return round(float(time_resp[last + 1]) * 1000.0, 1)

    metrics['settling_time_5pct_ms'] = _settling_time(0.05)
    metrics['settling_time_2pct_ms'] = _settling_time(0.02)

    # Count sign changes of (resp-1) after the first rise through 0.5.
    rise_idx = np.where(resp >= 0.5)[0]
    if rise_idx.size > 0:
        after = resp[int(rise_idx[0]):] - 1.0
        signs = np.sign(after)
        signs[signs == 0] = 1
        if signs.size > 1:
            metrics['oscillation_crossings'] = int(np.sum(np.diff(signs) != 0))

    return metrics


def _trace_to_payload(trace, axis, loop):
    """Serialize a Trace into metrics + a compact curve for the model."""
    payload = {
        'axis': axis,
        'loop': loop,
        'low_rate': None,
        'high_rate': None,
    }
    if getattr(trace, 'resp_low', None) is not None:
        resp = trace.resp_low[0]
        t = trace.time_resp
        t_ds, y_ds = _downsample_pairs(t, resp)
        payload['low_rate'] = {
            'label': 'average step response (inputs < 500 deg/s)' if loop == 'rate'
                     else 'average step response',
            'metrics': step_response_metrics(t, resp),
            'time_s': t_ds,
            'response': y_ds,
        }
    if getattr(trace, 'high_mask', None) is not None and trace.high_mask.sum() > 0:
        if getattr(trace, 'resp_high', None) is not None:
            resp = trace.resp_high[0]
            t = trace.time_resp
            t_ds, y_ds = _downsample_pairs(t, resp)
            payload['high_rate'] = {
                'label': 'average step response (inputs > 500 deg/s)',
                'metrics': step_response_metrics(t, resp),
                'time_s': t_ds,
                'response': y_ds,
            }
    return payload


def collect_pid_step_responses(ulog):
    """Build step-response payloads for rate (R/P/Y) and attitude (R/P) loops.

    Returns a dict:
      {
        'responses': [payload, ...],
        'errors': [str, ...],
        'has_rate': bool,
        'has_attitude': bool,
      }
    """
    result = {
        'responses': [],
        'errors': [],
        'has_rate': False,
        'has_attitude': False,
    }
    data = ulog.data_list

    if any(elem.name == 'vehicle_angular_velocity' for elem in data):
        rate_topic_name = 'vehicle_angular_velocity'
        rate_field_names = ['xyz[0]', 'xyz[1]', 'xyz[2]']
    else:
        rate_topic_name = 'rate_ctrl_status'
        rate_field_names = ['rollspeed', 'pitchspeed', 'yawspeed']

    dynamic_control_alloc = any(elem.name in ('actuator_motors', 'actuator_servos')
                                for elem in data)
    actuator_controls_0 = ActuatorControls(ulog, dynamic_control_alloc, 0)

    try:
        rate_data = ulog.get_dataset(rate_topic_name)
        gyro_time = rate_data.data['timestamp']
        vehicle_rates_setpoint = ulog.get_dataset('vehicle_rates_setpoint')
        actuator_controls_0_data = ulog.get_dataset(actuator_controls_0.thrust_sp_topic)
        throttle = _resample(actuator_controls_0_data.data['timestamp'],
                             actuator_controls_0.thrust * 100, gyro_time)
        time_seconds = gyro_time / 1e6
    except (KeyError, IndexError, ValueError, TypeError) as error:
        result['errors'].append(
            'Missing topics or data for rate step response '
            '(need angular velocity, rates setpoint, and thrust): {}'.format(error))
        return result

    for index, axis in enumerate(['roll', 'pitch', 'yaw']):
        try:
            gyro_rate = np.rad2deg(rate_data.data[rate_field_names[index]])
            setpoint = _resample(vehicle_rates_setpoint.data['timestamp'],
                                 np.rad2deg(vehicle_rates_setpoint.data[axis]),
                                 gyro_time)
            t_s, gyro_rate_d, setpoint_d, throttle_d = _decimate_to_budget(
                time_seconds, gyro_rate, setpoint, throttle)
            trace = Trace(axis, t_s, gyro_rate_d, setpoint_d, throttle_d)
            result['responses'].append(_trace_to_payload(trace, axis, 'rate'))
            result['has_rate'] = True
        except Exception as error:  # pylint: disable=broad-except
            result['errors'].append(
                'Rate step response failed for {}: {}'.format(axis, error))

    try:
        vehicle_attitude = ulog.get_dataset('vehicle_attitude')
        attitude_time = vehicle_attitude.data['timestamp']
        vehicle_attitude_setpoint = ulog.get_dataset('vehicle_attitude_setpoint')
        att_throttle = _resample(actuator_controls_0_data.data['timestamp'],
                                 actuator_controls_0.thrust * 100, attitude_time)
        att_time_s = attitude_time / 1e6
    except (KeyError, IndexError, ValueError, TypeError) as error:
        result['errors'].append(
            'Attitude topics missing; skipping angle-loop step response: {}'.format(error))
        return result

    for index, axis in enumerate(['roll', 'pitch']):
        try:
            attitude_estimated = _attitude_axis_deg(vehicle_attitude, axis)
            setpoint_deg = _attitude_axis_deg(vehicle_attitude_setpoint, axis)
            setpoint = _resample(vehicle_attitude_setpoint.data['timestamp'],
                                 setpoint_deg, attitude_time)
            t_s, att_d, setpoint_d, throttle_d = _decimate_to_budget(
                att_time_s, attitude_estimated, setpoint, att_throttle)
            trace = Trace(axis, t_s, att_d, setpoint_d, throttle_d,
                          high_input_threshold=ANGLE_HIGH_INPUT_DEG,
                          min_input_threshold=ANGLE_MIN_INPUT_DEG)
            result['responses'].append(_trace_to_payload(trace, axis, 'attitude'))
            result['has_attitude'] = True
        except Exception as error:  # pylint: disable=broad-except
            result['errors'].append(
                'Attitude step response failed for {}: {}'.format(axis, error))

    return result
