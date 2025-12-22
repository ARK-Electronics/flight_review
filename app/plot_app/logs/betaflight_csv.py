"""Betaflight Blackbox CSV reader.

Betaflight Blackbox native logs are typically .bbl (binary) or .txt.
Implementing a correct binary decoder in this repo would be a substantial
project. Instead, we support the common workflow of exporting the log to CSV
(via Blackbox Explorer) and ingesting that CSV.

Expected CSV columns (best-effort; not all are required):
- time (microseconds) OR time_us OR time_ms
- gyroADC[0], gyroADC[1], gyroADC[2] (deg/s) OR gyro[0..2]
- motor[0..]
- rcCommand[0..3] OR rc[0..3]

This is enough to populate angular rate, RC setpoints, and motor outputs.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from .compat_ulog import CompatDataset, CompatULog


def _load_csv(path: str) -> Dict[str, np.ndarray]:
    import pandas as pd

    df = pd.read_csv(path)
    data: Dict[str, np.ndarray] = {c: df[c].to_numpy() for c in df.columns}
    return data


def _time_us_from_columns(cols: Dict[str, np.ndarray]) -> np.ndarray:
    for k in ('time', 'time_us', 'TimeUS', 'time_usec'):
        if k in cols:
            t = cols[k].astype(np.int64)
            return t
    for k in ('time_ms', 'TimeMS', 'timeMS'):
        if k in cols:
            return cols[k].astype(np.int64) * 1000
    # Blackbox Explorer CSV sometimes uses "loopIteration" only; not supported.
    raise ValueError('CSV missing time column (expected time/time_us/time_ms)')


def read_betaflight_csv(path: str) -> CompatULog:
    cols = _load_csv(path)
    t = _time_us_from_columns(cols)
    t = np.maximum.accumulate(t)

    datasets: List[CompatDataset] = []

    # Gyro -> vehicle_angular_velocity
    gx = None
    gy = None
    gz = None
    for prefix in ('gyroADC', 'gyro'):
        if f'{prefix}[0]' in cols and f'{prefix}[1]' in cols and f'{prefix}[2]' in cols:
            gx = cols[f'{prefix}[0]'].astype(np.float64)
            gy = cols[f'{prefix}[1]'].astype(np.float64)
            gz = cols[f'{prefix}[2]'].astype(np.float64)
            break

    if gx is not None:
        # CSV gyro is typically deg/s
        datasets.append(
            CompatDataset(
                'vehicle_angular_velocity',
                {
                    'timestamp': t,
                    'xyz[0]': np.deg2rad(gx),
                    'xyz[1]': np.deg2rad(gy),
                    'xyz[2]': np.deg2rad(gz),
                },
            )
        )

        datasets.append(
            CompatDataset(
                'rate_ctrl_status',
                {
                    'timestamp': t,
                    'rollspeed': np.deg2rad(gx),
                    'pitchspeed': np.deg2rad(gy),
                    'yawspeed': np.deg2rad(gz),
                },
            )
        )

    # RC setpoint -> manual_control_setpoint
    # rcCommand[0..3] are usually in [-500,500] (roll/pitch/yaw) and [1000..2000] throttle or [-500..500]
    rc0 = rc1 = rc2 = rc3 = None
    if 'rcCommand[0]' in cols:
        rc0 = cols['rcCommand[0]'].astype(np.float64)
        rc1 = cols.get('rcCommand[1]', None)
        rc2 = cols.get('rcCommand[2]', None)
        rc3 = cols.get('rcCommand[3]', None)
        if rc1 is not None:
            rc1 = rc1.astype(np.float64)
        if rc2 is not None:
            rc2 = rc2.astype(np.float64)
        if rc3 is not None:
            rc3 = rc3.astype(np.float64)

        # Normalize assuming +/-500 for roll/pitch/yaw and 0..1000 for throttle
        roll = np.clip(rc0 / 500.0, -1.0, 1.0)
        pitch = np.clip((rc1 / 500.0) if rc1 is not None else np.nan, -1.0, 1.0)
        yaw = np.clip((rc2 / 500.0) if rc2 is not None else np.nan, -1.0, 1.0)
        # throttle might be rcCommand[3] (0..1000)
        throttle = np.clip((rc3 / 1000.0) if rc3 is not None else np.nan, 0.0, 1.0)

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

    # Motor outputs -> actuator_motors (preferred by plots)
    motor_cols = [c for c in cols.keys() if c.startswith('motor[')]
    if motor_cols:
        # Determine count and normalize to [0,1] based on common 1000..2000 range
        motor_cols_sorted = sorted(motor_cols, key=lambda x: int(x.split('[')[1].split(']')[0]))
        act: Dict[str, np.ndarray] = {'timestamp': t}
        for i, c in enumerate(motor_cols_sorted[:12]):
            v = cols[c].astype(np.float64)
            act[f'control[{i}]'] = np.clip((v - 1000.0) / 1000.0, 0.0, 1.0)
        datasets.append(CompatDataset('actuator_motors', act))

    # Minimal vehicle_status
    if datasets:
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

    start_ts = int(t[0]) if len(t) else 0
    end_ts = int(t[-1]) if len(t) else 0

    msg_info = {
        'sys_name': 'Betaflight',
        'mav_type': 'Betaflight',
        'estimator': '',
        'ver_data_format': 2,
    }

    return CompatULog(datasets, start_ts, end_ts, msg_info_dict=msg_info, initial_parameters={})
