"""ArduPilot DataFlash (.bin) log reader.

This decodes a subset of common signals and maps them into PX4-like topic names
so existing plots can be reused.

Notes:
- DataFlash message field availability varies by vehicle type and log settings.
- We treat timestamps as microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
import contextlib
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .compat_ulog import CompatDataset, CompatULog


class _CappedTextIO(io.TextIOBase):
    """Text sink that captures up to N characters then discards the rest."""

    def __init__(self, cap_chars: int = 4096):
        super().__init__()
        self._cap = int(cap_chars)
        self._buf = io.StringIO()
        self._len = 0

    def write(self, s: str) -> int:
        if not s:
            return 0
        if self._len < self._cap:
            remaining = self._cap - self._len
            chunk = s[:remaining]
            self._buf.write(chunk)
            self._len += len(chunk)
        return len(s)

    def getvalue(self) -> str:
        return self._buf.getvalue()


def _looks_like_dataflash_bin(path: str) -> bool:
    """Heuristic check to avoid feeding non-logs into DFReader.

    DataFlash binary logs are delimited by a 2-byte sync marker 0xA3 0x95.
    If it's not present near the start, pymavlink will emit thousands of
    'bad header' lines while scanning.
    """

    try:
        if os.path.getsize(path) < 64:
            return False
        with open(path, 'rb') as f:
            head = f.read(32768)
        # Sync marker must appear near the start.
        if not ((b'\xA3\x95' in head) or (b'\x95\xA3' in head)):
            return False
        # Most valid DataFlash logs include FMT definitions very early; requiring this
        # avoids feeding random binaries into DFReader (which can be very noisy).
        return b'FMT' in head
    except Exception:
        return False


def _msg_time_us(msg: Any) -> Optional[int]:
    """Best-effort extraction of message time in microseconds."""
    for key in ('TimeUS', 'time_us', 'time_usec', 'timeUS'):
        if hasattr(msg, key):
            try:
                return int(getattr(msg, key))
            except Exception:
                pass
    for key in ('TimeMS', 'time_ms', 'timeMS'):
        if hasattr(msg, key):
            try:
                return int(getattr(msg, key)) * 1000
            except Exception:
                pass
    return None


def _as_float_array(values: List[float]) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def _as_int_array(values: List[int], dtype=np.int64) -> np.ndarray:
    return np.asarray(values, dtype=dtype)


def _ensure_monotonic(t_us: np.ndarray) -> np.ndarray:
    if len(t_us) == 0:
        return t_us
    # Ensure non-decreasing timestamps (DataFlash is usually monotonic but can have duplicates)
    return np.maximum.accumulate(t_us.astype(np.int64))


def _quat_from_rpy(roll: np.ndarray, pitch: np.ndarray, yaw: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Convert Euler angles (rad) to quaternion (w, x, y, z) in PX4 q[0..3] order."""
    cr = np.cos(roll * 0.5)
    sr = np.sin(roll * 0.5)
    cp = np.cos(pitch * 0.5)
    sp = np.sin(pitch * 0.5)
    cy = np.cos(yaw * 0.5)
    sy = np.sin(yaw * 0.5)

    qw = cr * cp * cy + sr * sp * sy
    qx = sr * cp * cy - cr * sp * sy
    qy = cr * sp * cy + sr * cp * sy
    qz = cr * cp * sy - sr * sp * cy
    return qw, qx, qy, qz


def read_ardupilot_bin(path: str) -> CompatULog:
    """Parse a DataFlash .bin log and return a CompatULog."""

    if not _looks_like_dataflash_bin(path):
        raise ValueError(
            'File does not look like an ArduPilot DataFlash .bin log '
            '(missing sync marker 0xA395 and/or early FMT definitions).'
        )

    try:
        from pymavlink import DFReader  # local import for optional dependency
    except ModuleNotFoundError as e:
        # Keep this as a user-facing error (upload handler shows exception message)
        raise ValueError(
            'ArduPilot .bin logs require the optional dependency "pymavlink". '
            'Install it (pip install pymavlink) or rebuild the Docker image with it installed.'
        ) from e

    # NOTE: pymavlink can use a Cython fast-indexer (dfindexer) which has, in some
    # cases, been observed to terminate the process on malformed logs. For server
    # robustness we force the legacy (pure-Python) indexer for parsing.
    reader_out = _CappedTextIO(4096)
    reader_err = _CappedTextIO(4096)

    # Raw accumulators — must be initialized before the parse loop that appends
    # to them (previously these sat after the loop and raised UnboundLocalError
    # on the first ATT/IMU/... message).
    att_t: List[int] = []
    att_roll: List[float] = []
    att_pitch: List[float] = []
    att_yaw: List[float] = []

    gyro_t: List[int] = []
    gyro_x: List[float] = []
    gyro_y: List[float] = []
    gyro_z: List[float] = []

    gps_t: List[int] = []
    gps_lat: List[int] = []
    gps_lon: List[int] = []
    gps_alt_m: List[float] = []
    gps_fix: List[int] = []

    rcin_t: List[int] = []
    rcin: List[List[int]] = []

    rcou_t: List[int] = []
    rcou: List[List[int]] = []

    bat_t: List[int] = []
    bat_v: List[float] = []
    bat_a: List[float] = []

    orig_fast_index = os.environ.get('PYMAVLINK_FAST_INDEX')
    os.environ['PYMAVLINK_FAST_INDEX'] = '0'
    try:
        with contextlib.redirect_stdout(reader_out), contextlib.redirect_stderr(reader_err):
            reader = DFReader.DFReader_binary(path)

            while True:
                msg = reader.recv_msg()
                if msg is None:
                    break
                mtype = msg.get_type()
                t_us = _msg_time_us(msg)
                if t_us is None:
                    continue

                if mtype in ('ATT', 'AHR2'):
                    # ATT: Roll,Pitch,Yaw in degrees (typically)
                    if hasattr(msg, 'Roll') and hasattr(msg, 'Pitch') and hasattr(msg, 'Yaw'):
                        att_t.append(t_us)
                        att_roll.append(float(getattr(msg, 'Roll')))
                        att_pitch.append(float(getattr(msg, 'Pitch')))
                        att_yaw.append(float(getattr(msg, 'Yaw')))
                    elif hasattr(msg, 'roll') and hasattr(msg, 'pitch') and hasattr(msg, 'yaw'):
                        att_t.append(t_us)
                        att_roll.append(float(getattr(msg, 'roll')))
                        att_pitch.append(float(getattr(msg, 'pitch')))
                        att_yaw.append(float(getattr(msg, 'yaw')))

                elif mtype in ('IMU', 'IMU2', 'IMU3'):
                    # IMU: GyrX/Y/Z are usually deg/s
                    gx = getattr(msg, 'GyrX', None)
                    gy = getattr(msg, 'GyrY', None)
                    gz = getattr(msg, 'GyrZ', None)
                    if gx is not None and gy is not None and gz is not None:
                        gyro_t.append(t_us)
                        gyro_x.append(float(gx))
                        gyro_y.append(float(gy))
                        gyro_z.append(float(gz))

                elif mtype in ('GYR', 'GYR2', 'GYR3'):
                    # Some logs have GYR in rad/s or deg/s; we treat as deg/s if magnitude looks like deg/s.
                    gx = getattr(msg, 'GyrX', None)
                    if gx is None:
                        gx = getattr(msg, 'X', None)
                    gy = getattr(msg, 'GyrY', None)
                    if gy is None:
                        gy = getattr(msg, 'Y', None)
                    gz = getattr(msg, 'GyrZ', None)
                    if gz is None:
                        gz = getattr(msg, 'Z', None)
                    if gx is not None and gy is not None and gz is not None:
                        gyro_t.append(t_us)
                        gyro_x.append(float(gx))
                        gyro_y.append(float(gy))
                        gyro_z.append(float(gz))

                elif mtype in ('GPS', 'GPS2'):
                    lat = getattr(msg, 'Lat', None)
                    lon = getattr(msg, 'Lng', None)
                    alt = getattr(msg, 'Alt', None)
                    fix = getattr(msg, 'Status', None)
                    if lat is not None and lon is not None and alt is not None:
                        # ArduPilot logs may store Lat/Lng either as degrees*1e7 (int)
                        # or as plain degrees (int/float). PX4 expects degrees*1e7 ints.
                        try:
                            lat_v = float(lat)
                            lon_v = float(lon)
                            if abs(lat_v) < 1000.0 and abs(lon_v) < 1000.0:
                                lat_i = int(round(lat_v * 1e7))
                                lon_i = int(round(lon_v * 1e7))
                            else:
                                lat_i = int(round(lat_v))
                                lon_i = int(round(lon_v))
                        except Exception:
                            continue
                        gps_t.append(t_us)
                        gps_lat.append(lat_i)
                        gps_lon.append(lon_i)
                        gps_alt_m.append(float(alt))
                        gps_fix.append(int(fix) if fix is not None else 0)

                elif mtype == 'RCIN':
                    # RCIN: C1..C16 PWM
                    chans: List[int] = []
                    for i in range(1, 17):
                        v = getattr(msg, f'C{i}', None)
                        if v is None:
                            break
                        chans.append(int(v))
                    if chans:
                        rcin_t.append(t_us)
                        rcin.append(chans)

                elif mtype == 'RCOU':
                    # RCOU: C1..C16 PWM outputs
                    chans = []
                    for i in range(1, 17):
                        v = getattr(msg, f'C{i}', None)
                        if v is None:
                            break
                        chans.append(int(v))
                    if chans:
                        rcou_t.append(t_us)
                        rcou.append(chans)

                elif mtype in ('BAT', 'BATT'):
                    # BAT: Volt, Curr
                    v = getattr(msg, 'Volt', None)
                    if v is None:
                        v = getattr(msg, 'V', None)
                    c = getattr(msg, 'Curr', None)
                    if c is None:
                        c = getattr(msg, 'I', None)
                    if v is not None:
                        bat_t.append(t_us)
                        bat_v.append(float(v))
                        bat_a.append(float(c) if c is not None else 0.0)
    finally:
        if orig_fast_index is None:
            os.environ.pop('PYMAVLINK_FAST_INDEX', None)
        else:
            os.environ['PYMAVLINK_FAST_INDEX'] = orig_fast_index

    # Build datasets
    datasets: List[CompatDataset] = []

    # GPS -> PX4 vehicle_gps_position
    if gps_t:
        t = _ensure_monotonic(_as_int_array(gps_t, np.int64))
        lat = _as_int_array(gps_lat, np.int32)
        lon = _as_int_array(gps_lon, np.int32)
        alt_m = _as_float_array(gps_alt_m)
        fix = _as_int_array(gps_fix, np.uint8)

        gps_data: Dict[str, np.ndarray] = {
            'timestamp': t,
            'lat': lat,
            'lon': lon,
            'altitude_msl_m': alt_m,
            'fix_type': fix,
            'time_utc_usec': np.zeros_like(t, dtype=np.int64),
        }
        datasets.append(CompatDataset('vehicle_gps_position', gps_data))

        # Approximate global position from GPS (PX4 uses degrees and meters)
        datasets.append(
            CompatDataset(
                'vehicle_global_position',
                {
                    'timestamp': t,
                    'lat': lat.astype(np.float64) * 1e-7,
                    'lon': lon.astype(np.float64) * 1e-7,
                    'alt': alt_m,
                },
            )
        )

        # Local position from GPS projection
        # Use first valid fix as anchor if possible
        anchor_idx = 0
        good = np.nonzero(fix > 2)[0]
        if len(good) > 0:
            anchor_idx = int(good[0])
        anchor_lat = (lat[anchor_idx] * 1e-7) * np.pi / 180.0
        anchor_lon = (lon[anchor_idx] * 1e-7) * np.pi / 180.0

        lat_rad = (lat.astype(np.float64) * 1e-7) * np.pi / 180.0
        lon_rad = (lon.astype(np.float64) * 1e-7) * np.pi / 180.0

        # Reuse the existing map projection math (duplicated here to avoid import cycles)
        sin_lat = np.sin(lat_rad)
        cos_lat = np.cos(lat_rad)
        cos_d_lon = np.cos(lon_rad - anchor_lon)
        sin_anchor_lat = np.sin(anchor_lat)
        cos_anchor_lat = np.cos(anchor_lat)

        arg = sin_anchor_lat * sin_lat + cos_anchor_lat * cos_lat * cos_d_lon
        arg = np.clip(arg, -1.0, 1.0)

        c = np.arccos(arg)
        k = np.ones_like(lat_rad)
        small = np.abs(c) >= np.finfo(float).eps
        k[small] = c[small] / np.sin(c[small])

        R = 6371000.0
        x = k * (cos_anchor_lat * sin_lat - sin_anchor_lat * cos_lat * cos_d_lon) * R
        y = k * cos_lat * np.sin(lon_rad - anchor_lon) * R
        z = -(alt_m - alt_m[anchor_idx])

        # crude velocities
        dt = np.diff(t).astype(np.float64) * 1e-6
        dt = np.where(dt <= 0, np.nan, dt)
        vx = np.concatenate([[np.nan], np.diff(x) / dt])
        vy = np.concatenate([[np.nan], np.diff(y) / dt])
        vz = np.concatenate([[np.nan], np.diff(z) / dt])

        datasets.append(
            CompatDataset(
                'vehicle_local_position',
                {
                    'timestamp': t,
                    'x': x,
                    'y': y,
                    'z': z,
                    'vx': vx,
                    'vy': vy,
                    'vz': vz,
                },
            )
        )

    # Attitude
    if att_t:
        t = _ensure_monotonic(_as_int_array(att_t, np.int64))
        # ATT roll/pitch/yaw are typically degrees
        roll = np.deg2rad(_as_float_array(att_roll))
        pitch = np.deg2rad(_as_float_array(att_pitch))
        yaw = np.deg2rad(_as_float_array(att_yaw))
        qw, qx, qy, qz = _quat_from_rpy(roll, pitch, yaw)
        datasets.append(
            CompatDataset(
                'vehicle_attitude',
                {
                    'timestamp': t,
                    'roll': roll,
                    'pitch': pitch,
                    'yaw': yaw,
                    'q[0]': qw,
                    'q[1]': qx,
                    'q[2]': qy,
                    'q[3]': qz,
                },
            )
        )

    # Angular velocity (gyro)
    if gyro_t:
        t = _ensure_monotonic(_as_int_array(gyro_t, np.int64))
        # We interpret as deg/s and convert to rad/s
        gx = np.deg2rad(_as_float_array(gyro_x))
        gy = np.deg2rad(_as_float_array(gyro_y))
        gz = np.deg2rad(_as_float_array(gyro_z))
        datasets.append(
            CompatDataset(
                'vehicle_angular_velocity',
                {
                    'timestamp': t,
                    'xyz[0]': gx,
                    'xyz[1]': gy,
                    'xyz[2]': gz,
                },
            )
        )

        # For PID analysis fallback: rate_ctrl_status legacy fields
        datasets.append(
            CompatDataset(
                'rate_ctrl_status',
                {
                    'timestamp': t,
                    'rollspeed': gx,
                    'pitchspeed': gy,
                    'yawspeed': gz,
                },
            )
        )

    # Manual control setpoint from RCIN (best-effort mapping)
    if rcin_t and rcin:
        t = _ensure_monotonic(_as_int_array(rcin_t, np.int64))
        chans = np.asarray(rcin, dtype=np.float64)
        # Normalize PWM ~[1000,2000]
        def norm_bi(pwm: np.ndarray) -> np.ndarray:
            return np.clip((pwm - 1500.0) / 500.0, -1.0, 1.0)

        def norm_th(pwm: np.ndarray) -> np.ndarray:
            return np.clip((pwm - 1000.0) / 1000.0, 0.0, 1.0)

        roll = norm_bi(chans[:, 0]) if chans.shape[1] >= 1 else np.full(len(t), np.nan)
        pitch = norm_bi(chans[:, 1]) if chans.shape[1] >= 2 else np.full(len(t), np.nan)
        throttle = norm_th(chans[:, 2]) if chans.shape[1] >= 3 else np.full(len(t), np.nan)
        yaw = norm_bi(chans[:, 3]) if chans.shape[1] >= 4 else np.full(len(t), np.nan)

        datasets.append(
            CompatDataset(
                'manual_control_setpoint',
                {
                    'timestamp': t,
                    'roll': roll,
                    'pitch': pitch,
                    'yaw': yaw,
                    'throttle': throttle,
                },
            )
        )

        # rc_channels (legacy)
        num = min(chans.shape[1], 8)
        rc_data: Dict[str, np.ndarray] = {
            'timestamp': t,
            'channel_count': np.full(len(t), num, dtype=np.uint8),
        }
        for i in range(num):
            rc_data[f'channels[{i}]'] = norm_bi(chans[:, i])
        datasets.append(CompatDataset('rc_channels', rc_data))

    # Actuator outputs from RCOU
    if rcou_t and rcou:
        t = _ensure_monotonic(_as_int_array(rcou_t, np.int64))
        chans = np.asarray(rcou, dtype=np.float64)
        # Normalize PWM to [0,1]
        out = np.clip((chans - 1000.0) / 1000.0, 0.0, 1.0)
        act_data: Dict[str, np.ndarray] = {'timestamp': t}
        num = min(out.shape[1], 12)
        # PX4 actuator_outputs includes noutputs (number of valid outputs).
        # Some plots rely on this to decide how many channels to render.
        act_data['noutputs'] = np.full(len(t), num, dtype=np.int32)
        for i in range(num):
            act_data[f'output[{i}]'] = out[:, i]
        datasets.append(CompatDataset('actuator_outputs', act_data))

    # Battery
    if bat_t and bat_v:
        t = _ensure_monotonic(_as_int_array(bat_t, np.int64))
        datasets.append(
            CompatDataset(
                'battery_status',
                {
                    'timestamp': t,
                    'voltage_v': _as_float_array(bat_v),
                    'current_a': _as_float_array(bat_a) if bat_a else np.zeros(len(t)),
                    'discharged_mah': np.zeros(len(t), dtype=np.float64),
                    'remaining': np.ones(len(t), dtype=np.float64),
                },
            )
        )

    # vehicle_status: minimal fields for plot background logic
    # nav_state is PX4-specific; keep constant.
    if datasets:
        # pick a timebase
        t = None
        for name in ('vehicle_local_position', 'vehicle_gps_position', 'vehicle_attitude', 'vehicle_angular_velocity'):
            try:
                t = next(d.data['timestamp'] for d in datasets if d.name == name)
                break
            except StopIteration:
                continue
        if t is None:
            t = np.array([0], dtype=np.int64)
        datasets.append(
            CompatDataset(
                'vehicle_status',
                {
                    'timestamp': t,
                    'nav_state': np.zeros(len(t), dtype=np.uint8),
                    'is_vtol': np.zeros(len(t), dtype=np.uint8),
                    'in_transition_mode': np.zeros(len(t), dtype=np.uint8),
                    'is_vtol_tailsitter': np.zeros(len(t), dtype=np.uint8),
                },
            )
        )

    if not datasets:
        captured = (reader_err.getvalue() + reader_out.getvalue()).strip()
        if captured:
            # Don't spam logs; show a short summary to the user instead.
            captured_lines = captured.splitlines()[:3]
            detail = " / ".join(l.strip() for l in captured_lines if l.strip())
            raise ValueError(f'Failed to parse ArduPilot DataFlash .bin log: {detail}')
        raise ValueError('Failed to parse ArduPilot DataFlash .bin log: no supported messages found.')

    # Determine start/end
    all_ts = np.concatenate([d.data['timestamp'].astype(np.int64) for d in datasets if 'timestamp' in d.data])
    start_ts = int(np.nanmin(all_ts))
    end_ts = int(np.nanmax(all_ts))

    msg_info = {
        'sys_name': 'ArduPilot',
        'mav_type': 'ArduPilot',
        'estimator': '',
        'ver_data_format': 2,
    }

    return CompatULog(datasets, start_ts, end_ts, msg_info_dict=msg_info, initial_parameters={})
