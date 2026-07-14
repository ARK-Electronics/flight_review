"""
Tornado handler for AI-powered flight analysis using xAI Grok API.
Extracts key ULog data (PID response, EKF innovations, sensor biases, vehicle status)
and sends to Grok for failure analysis and tuning recommendations.
"""
from __future__ import print_function
import json
import os
import sys
import traceback

import numpy as np
import tornado.web
import tornado.gen
from tornado.httpclient import AsyncHTTPClient, HTTPRequest

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from config import get_xai_api_key, get_xai_model, get_cache_filepath
from helper import validate_log_id, get_log_filename, load_log_file, \
    get_flight_mode_changes, flight_modes_table

from pyulog.px4 import PX4ULog

#pylint: disable=relative-beyond-top-level
#pylint: disable=invalid-name,line-too-long
from .common import get_jinja_env, TornadoRequestHandlerBase

AI_ANALYSIS_TEMPLATE = 'ai_analysis.html'

# AI analysis cache directory
_AI_CACHE_DIR = os.path.join(get_cache_filepath(), 'ai_analysis')
os.makedirs(_AI_CACHE_DIR, exist_ok=True)


def _get_cache_path(log_id):
    """Get the cache file path for a given log ID."""
    return os.path.join(_AI_CACHE_DIR, f'{log_id}.json')


def _load_cached_analysis(log_id):
    """Load cached analysis result for a log ID. Returns dict or None."""
    cache_path = _get_cache_path(log_id)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_cached_analysis(log_id, data):
    """Save analysis result to cache."""
    cache_path = _get_cache_path(log_id)
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except OSError:
        pass


# System prompt grounded in UAV-SEAD paper findings and PX4 domain knowledge
SYSTEM_PROMPT = """You are an expert PX4 flight data analyst specializing in UAV state estimation
anomaly detection, PID controller tuning, and EKF parameter optimization. You have deep knowledge
of the PX4 EKF2 estimator, PID rate and attitude controllers, and common failure modes.

Your analysis is informed by research on UAV State Estimation Anomaly Detection (UAV-SEAD),
which categorizes anomalies into:
- **Sensor failures**: GPS glitches, magnetometer interference, barometer drift, IMU vibration/clipping
- **EKF estimation divergence**: Innovation test ratio failures, covariance growth, filter resets
- **Control performance issues**: PID oscillation, overshoot, inadequate tracking, actuator saturation
- **Vehicle health problems**: Motor/ESC failures, battery degradation, structural issues
- **Motor failure signatures**: A single motor output going to 100% (saturation) while others
  remain lower is a strong indicator of motor or propeller failure. The flight controller
  compensates for the lost thrust by maxing out the failed/opposite motor. Look for sustained
  saturation (>95% for >1s), large motor output asymmetry, and correlate with attitude divergence.

When analyzing flight data, you should:
1. **Identify failures and anomalies** - Look for sensor dropouts, EKF innovation spikes,
   attitude tracking errors, unexpected flight mode changes, and failsafe events.
2. **Diagnose root causes** - Distinguish between sensor issues, parameter mistuning,
   environmental factors (wind, magnetic interference), and hardware problems.
3. **Recommend PID tuning improvements** - Based on rate controller tracking error,
   overshoot, oscillation frequency, and phase lag. Reference specific PX4 parameters
   (MC_ROLLRATE_P, MC_ROLLRATE_I, MC_ROLLRATE_D, MC_PITCHRATE_P, etc.).
4. **Recommend EKF parameter tuning** - Based on innovation ratios, sensor biases,
   and filter health. Reference specific EKF2 parameters (EKF2_GPS_V_NOISE,
   EKF2_GPS_P_NOISE, EKF2_BARO_NOISE, EKF2_MAG_NOISE, EKF2_ACC_B_NOISE, etc.).
5. **Assess overall flight safety** - Rate the severity of any issues found.

Format your response with clear sections using markdown headers. Be specific with parameter
values and explain the reasoning behind each recommendation. If data is insufficient for a
confident recommendation, say so explicitly.
"""


def _downsample(arr, max_points=500):
    """Downsample an array to max_points using uniform selection."""
    if len(arr) <= max_points:
        return arr.tolist()
    indices = np.linspace(0, len(arr) - 1, max_points, dtype=int)
    return arr[indices].tolist()


def _safe_get_field(data, field_name, default=None):
    """Safely get a field from ULog data dict."""
    if field_name in data:
        return data[field_name]
    return default


def _extract_flight_summary(ulog, px4_ulog):
    """Extract high-level flight summary info."""
    summary = {}

    # Basic info
    summary['duration_s'] = (ulog.last_timestamp - ulog.start_timestamp) / 1e6
    summary['mav_type'] = px4_ulog.get_mav_type()

    if 'sys_name' in ulog.msg_info_dict:
        summary['sys_name'] = ulog.msg_info_dict['sys_name']
    if 'ver_sw' in ulog.msg_info_dict:
        summary['firmware_version'] = ulog.msg_info_dict['ver_sw']
    if 'ver_hw' in ulog.msg_info_dict:
        summary['hardware'] = ulog.msg_info_dict['ver_hw']

    # Flight modes used
    flight_mode_changes = get_flight_mode_changes(ulog)
    modes = []
    for t, mode_id in flight_mode_changes:
        mode_name = 'Unknown'
        if mode_id in flight_modes_table:
            mode_name = flight_modes_table[mode_id][0]
        modes.append({'time_s': round(t / 1e6, 1), 'mode': mode_name})
    summary['flight_modes'] = modes

    return summary


def _extract_pid_data(ulog, max_points=300):
    """Extract PID controller performance data."""
    pid_data = {}

    # Attitude setpoint vs actual
    try:
        att = ulog.get_dataset('vehicle_attitude')
        att_sp = ulog.get_dataset('vehicle_attitude_setpoint')

        # Roll, Pitch, Yaw from quaternion -> simplified to roll/pitch fields if available
        pid_data['attitude'] = {
            'timestamp_s': _downsample(att.data['timestamp'] / 1e6, max_points),
        }

        # Get roll/pitch/yaw if available (added by px4_ulog.add_roll_pitch_yaw())
        for field in ['roll', 'pitch', 'yaw']:
            if field in att.data:
                pid_data['attitude'][f'{field}_deg'] = _downsample(
                    np.degrees(att.data[field]), max_points)

        pid_data['attitude_setpoint'] = {
            'timestamp_s': _downsample(att_sp.data['timestamp'] / 1e6, max_points),
        }
        for field in ['roll_body', 'pitch_body', 'yaw_body']:
            if field in att_sp.data:
                short = field.replace('_body', '')
                pid_data['attitude_setpoint'][f'{short}_deg'] = _downsample(
                    np.degrees(att_sp.data[field]), max_points)
    except (KeyError, IndexError):
        pass

    # Rate controller
    try:
        rate_topic = None
        for topic in ulog.data_list:
            if topic.name == 'vehicle_angular_velocity':
                rate_topic = topic
                break
        if rate_topic is None:
            for topic in ulog.data_list:
                if topic.name == 'vehicle_attitude':
                    rate_topic = topic
                    break

        if rate_topic is not None:
            rate_data = rate_topic.data
            pid_data['angular_velocity'] = {
                'timestamp_s': _downsample(rate_data['timestamp'] / 1e6, max_points),
            }
            if 'xyz[0]' in rate_data:
                for i, axis in enumerate(['roll', 'pitch', 'yaw']):
                    pid_data['angular_velocity'][f'{axis}_rad_s'] = _downsample(
                        rate_data[f'xyz[{i}]'], max_points)
            elif 'rollspeed' in rate_data:
                for field in ['rollspeed', 'pitchspeed', 'yawspeed']:
                    if field in rate_data:
                        pid_data['angular_velocity'][f'{field}_rad_s'] = _downsample(
                            rate_data[field], max_points)
    except (KeyError, IndexError):
        pass

    # Rate setpoint
    try:
        rate_sp = ulog.get_dataset('vehicle_rates_setpoint')
        pid_data['rate_setpoint'] = {
            'timestamp_s': _downsample(rate_sp.data['timestamp'] / 1e6, max_points),
        }
        for field in ['roll', 'pitch', 'yaw']:
            if field in rate_sp.data:
                pid_data['rate_setpoint'][f'{field}_rad_s'] = _downsample(
                    rate_sp.data[field], max_points)
    except (KeyError, IndexError):
        pass

    # Actuator outputs
    try:
        for topic_name in ['actuator_controls_0', 'actuator_motors']:
            try:
                act = ulog.get_dataset(topic_name)
                act_data = {'timestamp_s': _downsample(act.data['timestamp'] / 1e6, max_points)}
                for key in act.data:
                    if key != 'timestamp' and not key.startswith('_'):
                        act_data[key] = _downsample(act.data[key], max_points)
                pid_data[topic_name] = act_data
                break
            except (KeyError, IndexError):
                continue
    except Exception:
        pass

    return pid_data


def _extract_ekf_data(ulog, max_points=300):
    """Extract EKF health and innovation data."""
    ekf_data = {}

    # Estimator status (test ratios, flags)
    try:
        est = ulog.get_dataset('estimator_status')
        es = est.data
        ekf_data['estimator_status'] = {
            'timestamp_s': _downsample(es['timestamp'] / 1e6, max_points),
        }
        for field in ['vel_test_ratio', 'pos_test_ratio', 'hgt_test_ratio',
                       'mag_test_ratio', 'tas_test_ratio', 'hagl_test_ratio']:
            if field in es:
                values = es[field]
                ekf_data['estimator_status'][field] = _downsample(values, max_points)
                ekf_data['estimator_status'][f'{field}_max'] = float(np.max(values))
                ekf_data['estimator_status'][f'{field}_mean'] = float(np.mean(values))

        # Innovation check flags summary
        if 'innovation_check_flags' in es:
            flags = es['innovation_check_flags']
            flag_pct = 100.0 * np.sum(flags > 0) / len(flags)
            ekf_data['estimator_status']['innovation_rejection_pct'] = round(flag_pct, 2)

        # Health/timeout flags
        if 'health_flags' in es:
            ekf_data['estimator_status']['health_flags_nonzero_pct'] = round(
                100.0 * np.sum(es['health_flags'] > 0) / len(es['health_flags']), 2)
        if 'timeout_flags' in es:
            ekf_data['estimator_status']['timeout_flags_nonzero_pct'] = round(
                100.0 * np.sum(es['timeout_flags'] > 0) / len(es['timeout_flags']), 2)

    except (KeyError, IndexError):
        pass

    # Sensor biases
    try:
        bias = ulog.get_dataset('estimator_sensor_bias')
        bd = bias.data
        ekf_data['sensor_bias'] = {
            'timestamp_s': _downsample(bd['timestamp'] / 1e6, max_points),
        }
        for i, axis in enumerate(['x', 'y', 'z']):
            for sensor in ['gyro_bias', 'accel_bias', 'mag_bias']:
                field = f'{sensor}[{i}]'
                if field in bd:
                    values = bd[field]
                    ekf_data['sensor_bias'][f'{sensor}_{axis}'] = _downsample(values, max_points)
                    ekf_data['sensor_bias'][f'{sensor}_{axis}_max'] = float(np.max(np.abs(values)))
                    ekf_data['sensor_bias'][f'{sensor}_{axis}_mean'] = float(np.mean(values))
    except (KeyError, IndexError):
        pass

    # Innovations
    try:
        inn = ulog.get_dataset('estimator_innovations')
        inn_d = inn.data
        ekf_data['innovations'] = {
            'timestamp_s': _downsample(inn_d['timestamp'] / 1e6, max_points),
        }
        for field in ['gps_hvel[0]', 'gps_hvel[1]', 'gps_vvel',
                       'gps_hpos[0]', 'gps_hpos[1]', 'gps_vpos',
                       'baro_vpos', 'mag_field[0]', 'mag_field[1]', 'mag_field[2]',
                       'heading', 'flow[0]', 'flow[1]']:
            if field in inn_d:
                clean_name = field.replace('[', '_').replace(']', '')
                values = inn_d[field]
                ekf_data['innovations'][clean_name] = _downsample(values, max_points)
                ekf_data['innovations'][f'{clean_name}_rms'] = float(
                    np.sqrt(np.mean(values ** 2)))
    except (KeyError, IndexError):
        pass

    # Vibration
    try:
        est = ulog.get_dataset('estimator_status')
        for i, axis in enumerate(['x', 'y', 'z']):
            field = f'vibe[{i}]'
            if field in est.data:
                if 'vibration' not in ekf_data:
                    ekf_data['vibration'] = {}
                values = est.data[field]
                ekf_data['vibration'][f'vibe_{axis}_max'] = float(np.max(values))
                ekf_data['vibration'][f'vibe_{axis}_mean'] = float(np.mean(values))
    except (KeyError, IndexError):
        pass

    return ekf_data


def _extract_vehicle_status(ulog, max_points=200):
    """Extract vehicle status, failsafe, and battery data."""
    status_data = {}

    # Vehicle status
    try:
        vs = ulog.get_dataset('vehicle_status')
        vsd = vs.data
        status_data['vehicle_status'] = {
            'timestamp_s': _downsample(vsd['timestamp'] / 1e6, max_points),
        }
        if 'arming_state' in vsd:
            status_data['vehicle_status']['arming_state'] = _downsample(vsd['arming_state'], max_points)
        if 'failsafe' in vsd:
            failsafe_pct = 100.0 * np.sum(vsd['failsafe'] > 0) / len(vsd['failsafe'])
            status_data['vehicle_status']['failsafe_pct'] = round(failsafe_pct, 2)
            if failsafe_pct > 0:
                status_data['vehicle_status']['failsafe'] = _downsample(vsd['failsafe'], max_points)
    except (KeyError, IndexError):
        pass

    # Battery status
    try:
        bat = ulog.get_dataset('battery_status')
        bd = bat.data
        status_data['battery'] = {}
        if 'voltage_v' in bd:
            status_data['battery']['voltage_min'] = float(np.min(bd['voltage_v']))
            status_data['battery']['voltage_max'] = float(np.max(bd['voltage_v']))
            status_data['battery']['voltage_v'] = _downsample(bd['voltage_v'], max_points)
        if 'current_a' in bd:
            status_data['battery']['current_max'] = float(np.max(bd['current_a']))
            status_data['battery']['current_a'] = _downsample(bd['current_a'], max_points)
        if 'remaining' in bd:
            status_data['battery']['remaining_min'] = float(np.min(bd['remaining']))
        status_data['battery']['timestamp_s'] = _downsample(bd['timestamp'] / 1e6, max_points)
    except (KeyError, IndexError):
        pass

    # Failsafe flags
    try:
        ff = ulog.get_dataset('failsafe_flags')
        ffd = ff.data
        active_flags = []
        for field_name in ffd:
            if field_name == 'timestamp' or field_name.startswith('mode_req_'):
                continue
            if np.max(ffd[field_name]) >= 1:
                pct = 100.0 * np.sum(ffd[field_name] >= 1) / len(ffd[field_name])
                active_flags.append({'flag': field_name, 'active_pct': round(pct, 1)})
        if active_flags:
            status_data['failsafe_flags'] = active_flags
    except (KeyError, IndexError):
        pass

    return status_data


def _detect_motor_failure(ulog, max_points=300):
    """Detect potential motor/propeller failure from actuator output patterns.

    A motor failure typically manifests as:
    - One motor output saturating at ~100% for a sustained period
    - Large asymmetry between motor outputs
    - The flight controller compensating for lost thrust

    Returns a dict with detection results, or empty dict if no actuator data.
    """
    motor_failure = {}

    for topic_name in ['actuator_motors', 'actuator_controls_0']:
        try:
            act = ulog.get_dataset(topic_name)
        except (KeyError, IndexError):
            continue

        act_data = act.data
        timestamps_s = act_data['timestamp'] / 1e6
        dt = np.diff(timestamps_s)
        mean_dt = float(np.mean(dt)) if len(dt) > 0 else 0.02  # fallback sample period

        # Collect motor channels
        motor_keys = []
        for key in sorted(act_data.keys()):
            if key == 'timestamp' or key.startswith('_'):
                continue
            motor_keys.append(key)

        if not motor_keys:
            continue

        n_motors = len(motor_keys)
        motor_arrays = {k: act_data[k] for k in motor_keys}

        # Only analyze samples where at least one motor is active (> 0.05)
        any_active = np.zeros(len(timestamps_s), dtype=bool)
        for k in motor_keys:
            any_active |= (motor_arrays[k] > 0.05)

        if np.sum(any_active) < 10:
            continue  # not enough active samples

        SATURATION_THRESHOLD = 0.95  # consider motor saturated above this
        SUSTAINED_SECONDS = 0.5  # minimum duration to flag as sustained

        per_motor = {}
        for k in motor_keys:
            values = motor_arrays[k]
            active_values = values[any_active]

            saturated_mask = values >= SATURATION_THRESHOLD
            # Count sustained saturation: consecutive saturated samples
            sat_samples = int(np.sum(saturated_mask & any_active))
            sat_duration_s = round(sat_samples * mean_dt, 2)

            # Find longest continuous saturation run
            longest_run = 0
            current_run = 0
            for i in range(len(values)):
                if saturated_mask[i] and any_active[i]:
                    current_run += 1
                    longest_run = max(longest_run, current_run)
                else:
                    current_run = 0
            longest_run_s = round(longest_run * mean_dt, 2)

            per_motor[k] = {
                'mean': round(float(np.mean(active_values)), 4),
                'max': round(float(np.max(active_values)), 4),
                'std': round(float(np.std(active_values)), 4),
                'saturation_total_s': sat_duration_s,
                'saturation_longest_run_s': longest_run_s,
                'saturation_pct': round(
                    100.0 * sat_samples / max(np.sum(any_active), 1), 1),
            }

        # Detect asymmetry: check if one motor is significantly higher than others
        motor_means = np.array([per_motor[k]['mean'] for k in motor_keys])
        motor_sat_pcts = np.array([per_motor[k]['saturation_pct'] for k in motor_keys])
        motor_longest_runs = np.array([per_motor[k]['saturation_longest_run_s']
                                        for k in motor_keys])

        max_mean_idx = int(np.argmax(motor_means))
        others_mean = np.mean(np.delete(motor_means, max_mean_idx))
        asymmetry = round(float(motor_means[max_mean_idx] - others_mean), 4)

        # Flag potential failure
        failure_detected = False
        failure_reasons = []

        # Check for sustained saturation on any motor
        for i, k in enumerate(motor_keys):
            if motor_longest_runs[i] >= SUSTAINED_SECONDS:
                failure_detected = True
                failure_reasons.append(
                    f"{k} saturated at 100% for {motor_longest_runs[i]}s continuously"
                )

        # Check for large output asymmetry (one motor much higher)
        if asymmetry > 0.3 and motor_means[max_mean_idx] > 0.8:
            failure_detected = True
            failure_reasons.append(
                f"Large motor asymmetry: {motor_keys[max_mean_idx]} mean="
                f"{motor_means[max_mean_idx]:.3f} vs others mean={others_mean:.3f} "
                f"(diff={asymmetry:.3f})"
            )

        motor_failure = {
            'topic': topic_name,
            'n_motors': n_motors,
            'per_motor_stats': per_motor,
            'max_asymmetry': asymmetry,
            'highest_motor': motor_keys[max_mean_idx],
            'failure_detected': failure_detected,
            'failure_reasons': failure_reasons,
        }
        break  # use first available topic

    return motor_failure


def _extract_parameters(ulog):
    """Extract key PID and EKF parameters from the log."""
    params = {}
    if hasattr(ulog, 'initial_parameters') and ulog.initial_parameters:
        param_dict = ulog.initial_parameters
    elif hasattr(ulog, 'params'):
        param_dict = ulog.params
    else:
        return params

    # Key PID parameters
    pid_prefixes = [
        'MC_ROLLRATE_', 'MC_PITCHRATE_', 'MC_YAWRATE_',
        'MC_ROLL_', 'MC_PITCH_', 'MC_YAW_',
        'FW_RR_', 'FW_PR_', 'FW_YR_',
        'FW_R_', 'FW_P_',
    ]
    # Key EKF parameters
    ekf_prefixes = [
        'EKF2_', 'SENS_',
    ]
    # Other useful params
    other_params = [
        'MPC_XY_VEL_MAX', 'MPC_Z_VEL_MAX_UP', 'MPC_Z_VEL_MAX_DN',
        'MPC_THR_HOVER', 'MPC_XY_P', 'MPC_Z_P',
        'SYS_MC_EST_GROUP', 'IMU_GYRO_CUTOFF', 'IMU_DGYRO_CUTOFF',
        'IMU_ACCEL_CUTOFF',
    ]

    for param_name, param_value in param_dict.items():
        include = False
        for prefix in pid_prefixes + ekf_prefixes:
            if param_name.startswith(prefix):
                include = True
                break
        if not include and param_name in other_params:
            include = True
        if include:
            if isinstance(param_value, (np.integer, np.floating)):
                params[param_name] = float(param_value)
            else:
                params[param_name] = param_value

    return params


def _extract_logged_messages(ulog, max_messages=50):
    """Extract logged messages (warnings/errors)."""
    messages = []
    if hasattr(ulog, 'logged_messages') and ulog.logged_messages:
        for msg in ulog.logged_messages[:max_messages]:
            messages.append({
                'time_s': round(msg.timestamp / 1e6, 2),
                'level': msg.log_level_str() if hasattr(msg, 'log_level_str') else str(msg.log_level),
                'message': msg.message,
            })
    return messages


def _build_analysis_prompt(flight_summary, pid_data, ekf_data, vehicle_status,
                           parameters, logged_messages, motor_failure=None):
    """Build the user prompt with extracted flight data."""

    prompt = "# Flight Log Analysis Request\n\n"
    prompt += "Please analyze this PX4 flight log data and provide:\n"
    prompt += "1. **Failure Detection**: Any anomalies, sensor failures, or estimation problems\n"
    prompt += "2. **PID Tuning Assessment**: Rate and attitude controller performance with specific improvement recommendations\n"
    prompt += "3. **EKF Parameter Tuning**: Filter health assessment with specific EKF2 parameter recommendations\n"
    prompt += "4. **Overall Flight Safety Rating**: Rate 1-10 with justification\n\n"

    prompt += "## Flight Summary\n```json\n"
    prompt += json.dumps(flight_summary, indent=2, default=str)
    prompt += "\n```\n\n"

    if parameters:
        prompt += "## Current Parameters\n```json\n"
        prompt += json.dumps(parameters, indent=2, default=str)
        prompt += "\n```\n\n"

    if logged_messages:
        prompt += "## Logged Messages (Warnings/Errors)\n```json\n"
        prompt += json.dumps(logged_messages, indent=2, default=str)
        prompt += "\n```\n\n"

    if pid_data:
        prompt += "## PID Controller Data\n"
        # Only include summary statistics to save tokens
        for key, values in pid_data.items():
            if isinstance(values, dict):
                summary = {}
                for field, arr in values.items():
                    if isinstance(arr, list) and len(arr) > 0 and field != 'timestamp_s':
                        np_arr = np.array(arr)
                        summary[field] = {
                            'min': round(float(np.min(np_arr)), 4),
                            'max': round(float(np.max(np_arr)), 4),
                            'mean': round(float(np.mean(np_arr)), 4),
                            'std': round(float(np.std(np_arr)), 4),
                        }
                if summary:
                    prompt += f"### {key}\n```json\n{json.dumps(summary, indent=2)}\n```\n\n"

        # Include a small window of raw time series for rate tracking analysis
        if 'angular_velocity' in pid_data and 'rate_setpoint' in pid_data:
            prompt += "### Rate Controller Tracking (sampled time series)\n```json\n"
            rate_sample = {}
            for key in ['angular_velocity', 'rate_setpoint']:
                rate_sample[key] = {}
                for field, arr in pid_data[key].items():
                    if isinstance(arr, list):
                        # Take middle 50 samples for tracking analysis
                        mid = len(arr) // 2
                        start = max(0, mid - 25)
                        rate_sample[key][field] = [round(v, 5) for v in arr[start:start+50]]
            prompt += json.dumps(rate_sample, indent=2)
            prompt += "\n```\n\n"

    if ekf_data:
        prompt += "## EKF Data\n"
        for key, values in ekf_data.items():
            if isinstance(values, dict):
                # Include summary stats and full time series for key metrics
                summary = {}
                for field, val in values.items():
                    if field == 'timestamp_s':
                        continue
                    if isinstance(val, (int, float)):
                        summary[field] = val
                    elif isinstance(val, list) and len(val) > 0:
                        np_arr = np.array(val)
                        summary[field + '_stats'] = {
                            'min': round(float(np.min(np_arr)), 5),
                            'max': round(float(np.max(np_arr)), 5),
                            'mean': round(float(np.mean(np_arr)), 5),
                            'std': round(float(np.std(np_arr)), 5),
                        }
                if summary:
                    prompt += f"### {key}\n```json\n{json.dumps(summary, indent=2)}\n```\n\n"

    if motor_failure:
        prompt += "## Motor Failure Analysis\n"
        if motor_failure.get('failure_detected'):
            prompt += "**WARNING: Potential motor/propeller failure detected!**\n\n"
            prompt += "Failure indicators:\n"
            for reason in motor_failure.get('failure_reasons', []):
                prompt += f"- {reason}\n"
            prompt += "\n"
        prompt += "```json\n"
        prompt += json.dumps(motor_failure, indent=2, default=str)
        prompt += "\n```\n\n"

    if vehicle_status:
        prompt += "## Vehicle Status\n```json\n"
        # Only include non-time-series data
        vs_summary = {}
        for key, values in vehicle_status.items():
            if isinstance(values, dict):
                vs_summary[key] = {k: v for k, v in values.items()
                                   if not isinstance(v, list)}
            elif isinstance(values, list):
                vs_summary[key] = values
        prompt += json.dumps(vs_summary, indent=2, default=str)
        prompt += "\n```\n\n"

    return prompt


class AIAnalysisHandler(TornadoRequestHandlerBase):
    """Tornado Request Handler for AI-powered flight analysis page."""

    @tornado.web.authenticated
    def get(self, *args, **kwargs):
        """GET request - render the AI analysis page."""
        log_id = self.get_argument('log', '')
        if not validate_log_id(log_id):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        api_key = get_xai_api_key()
        has_api_key = bool(api_key and api_key.strip())

        template = get_jinja_env().get_template(AI_ANALYSIS_TEMPLATE)
        self.write(template.render(
            log_id=log_id,
            has_api_key=has_api_key,
            default_model=get_xai_model(),
            current_user=self.get_current_user(),
        ))


class AIAnalysisModelsHandler(TornadoRequestHandlerBase):
    """Tornado Request Handler that proxies the xAI models list."""

    @tornado.web.authenticated
    @tornado.gen.coroutine
    def get(self, *args, **kwargs):
        """Return the list of available models from the xAI API."""
        api_key = get_xai_api_key()
        if not api_key or not api_key.strip():
            self.set_status(500)
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps({'error': 'xAI API key not configured'}))
            return

        http_client = AsyncHTTPClient()
        request = HTTPRequest(
            url='https://api.x.ai/v1/language-models',
            method='GET',
            headers={
                'Authorization': f'Bearer {api_key}',
            },
            request_timeout=15,
            connect_timeout=10,
        )

        try:
            response = yield http_client.fetch(request, raise_error=False)
        except Exception as e:
            self.set_status(502)
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps({'error': f'Failed to fetch models: {str(e)}'}))
            return

        if response.code != 200:
            # Fallback to the standard /models endpoint (OpenAI-compatible)
            try:
                fallback_req = HTTPRequest(
                    url='https://api.x.ai/v1/models',
                    method='GET',
                    headers={'Authorization': f'Bearer {api_key}'},
                    request_timeout=15,
                    connect_timeout=10,
                )
                response = yield http_client.fetch(fallback_req, raise_error=False)
            except Exception as e:
                self.set_status(502)
                self.set_header('Content-Type', 'application/json')
                self.write(json.dumps({'error': f'Failed to fetch models: {str(e)}'}))
                return

        if response.code != 200:
            self.set_status(502)
            self.set_header('Content-Type', 'application/json')
            err = response.body.decode('utf-8', errors='replace')[:300] if response.body else ''
            self.write(json.dumps({'error': f'xAI API error (HTTP {response.code}): {err}'}))
            return

        try:
            payload = json.loads(response.body)
        except (ValueError, json.JSONDecodeError):
            self.set_status(502)
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps({'error': 'Invalid response from xAI API'}))
            return

        # Normalize to a simple list of model ids. xAI returns either
        # {"models": [{"id": ...}, ...]} or {"data": [{"id": ...}, ...]}.
        items = payload.get('models') or payload.get('data') or []
        model_ids = []
        for item in items:
            if isinstance(item, dict):
                mid = item.get('id') or item.get('name')
                if mid:
                    model_ids.append(mid)
            elif isinstance(item, str):
                model_ids.append(item)

        # Filter to chat-capable models when input_modalities present
        chat_ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get('id') or item.get('name')
            if not mid:
                continue
            input_mods = item.get('input_modalities')
            output_mods = item.get('output_modalities')
            if input_mods is not None and 'text' not in input_mods:
                continue
            if output_mods is not None and 'text' not in output_mods:
                continue
            chat_ids.append(mid)
        if chat_ids:
            model_ids = chat_ids

        # De-duplicate while preserving order
        seen = set()
        unique_ids = []
        for mid in model_ids:
            if mid not in seen:
                seen.add(mid)
                unique_ids.append(mid)

        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps({
            'models': unique_ids,
            'default': get_xai_model(),
        }))


class AIAnalysisAPIHandler(TornadoRequestHandlerBase):
    """API handler that performs the actual AI analysis."""

    @tornado.web.authenticated
    def get(self, *args, **kwargs):
        """GET request - return cached analysis if available."""
        log_id = self.get_argument('log', '')
        if not validate_log_id(log_id):
            self.set_status(400)
            self.write({'error': 'Invalid log ID'})
            return

        cached = _load_cached_analysis(log_id)
        if cached:
            cached['cached'] = True
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps(cached))
        else:
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps({'cached': False}))

    @tornado.web.authenticated
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        """POST request - run AI analysis on the log."""
        log_id = self.get_argument('log', '')
        if not validate_log_id(log_id):
            self.set_status(400)
            self.write({'error': 'Invalid log ID'})
            return

        api_key = get_xai_api_key()
        if not api_key or not api_key.strip():
            self.set_status(500)
            self.write({'error': 'xAI API key not configured. Set xai_api_key in config or XAI_API_KEY env var.'})
            return

        # Allow model override from request body, else fall back to configured default
        model = get_xai_model()
        try:
            body_args = self.request.body
            if body_args:
                try:
                    parsed = json.loads(body_args)
                    if isinstance(parsed, dict) and parsed.get('model'):
                        candidate = str(parsed['model']).strip()
                        # Only allow safe model id characters
                        if candidate and all(
                                c.isalnum() or c in '-_.:' for c in candidate):
                            model = candidate
                except (ValueError, json.JSONDecodeError):
                    pass
        except Exception:
            pass

        try:
            # Load the log file
            log_file_name = get_log_filename(log_id)
            ulog = load_log_file(log_file_name)
            px4_ulog = PX4ULog(ulog)
            try:
                px4_ulog.add_roll_pitch_yaw()
            except Exception:
                pass

            # Extract all data
            flight_summary = _extract_flight_summary(ulog, px4_ulog)
            pid_data = _extract_pid_data(ulog)
            ekf_data = _extract_ekf_data(ulog)
            vehicle_status = _extract_vehicle_status(ulog)
            parameters = _extract_parameters(ulog)
            logged_messages = _extract_logged_messages(ulog)
            motor_failure = _detect_motor_failure(ulog)

            # Build the prompt
            user_prompt = _build_analysis_prompt(
                flight_summary, pid_data, ekf_data, vehicle_status,
                parameters, logged_messages, motor_failure
            )

            # Call the xAI Grok API
            request_body = {
                'model': model,
                'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.3,
                'max_tokens': 4096,
            }

            # Check if this is a reasoning model
            is_reasoning = 'fast' in model.lower() and ('grok-3' in model.lower() or 'grok-4' in model.lower())
            if is_reasoning:
                request_body['reasoning'] = {
                    'effort': 'high'
                }

            http_client = AsyncHTTPClient()
            request = HTTPRequest(
                url='https://api.x.ai/v1/chat/completions',
                method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                },
                body=json.dumps(request_body),
                request_timeout=120,  # 2min timeout for reasoning models
                connect_timeout=30,
            )

            response = yield http_client.fetch(request, raise_error=False)

            if response.code == 200:
                result = json.loads(response.body)
                ai_response = result['choices'][0]['message']['content']

                # Extract reasoning content if available
                reasoning = None
                if 'reasoning_content' in result['choices'][0]['message']:
                    reasoning = result['choices'][0]['message']['reasoning_content']

                response_data = {
                    'analysis': ai_response,
                    'reasoning': reasoning,
                    'model': model,
                    'data_summary': {
                        'duration_s': flight_summary.get('duration_s', 0),
                        'mav_type': flight_summary.get('mav_type', 'Unknown'),
                        'num_parameters': len(parameters),
                        'has_ekf_data': bool(ekf_data),
                        'has_pid_data': bool(pid_data),
                        'num_messages': len(logged_messages),
                    }
                }

                # Cache the result
                _save_cached_analysis(log_id, response_data)

                self.set_header('Content-Type', 'application/json')
                self.write(json.dumps(response_data))
            else:
                error_msg = f'xAI API error (HTTP {response.code})'
                try:
                    error_body = json.loads(response.body)
                    if 'error' in error_body:
                        error_msg += ': ' + str(error_body['error'].get('message', ''))
                except Exception:
                    error_msg += ': ' + response.body.decode('utf-8', errors='replace')[:200]

                self.set_status(502)
                self.write({'error': error_msg})

        except Exception as e:
            traceback.print_exc()
            self.set_status(500)
            self.write({'error': f'Analysis failed: {str(e)}'})
