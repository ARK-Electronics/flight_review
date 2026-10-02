"""Betaflight Blackbox CSV reader.

Betaflight Blackbox native logs are typically .bbl (binary) or .txt.
Implementing a correct binary decoder in this repo would be a substantial
project. Instead, we support the common workflow of exporting the log to CSV
(via Blackbox Explorer) and ingesting that CSV.

Both common CSV layouts are accepted:
- Blackbox Explorer "Export CSV": quoted "key","value" header rows (firmware,
  PIDs, filters) followed by the column row and the data.
- blackbox_decode: a single column row with unit suffixes, e.g. "time (us)".

Expected CSV columns (best-effort; not all are required):
- time (microseconds) OR time_us OR time_ms OR time (us|ms|s)
- gyroADC[0], gyroADC[1], gyroADC[2] (deg/s) OR gyro[0..2]
- motor[0..]
- rcCommand[0..3] OR rc[0..3]

This is enough to populate angular rate, RC setpoints, and motor outputs.
"""

from __future__ import annotations

import csv
import re
from typing import Dict, List, Optional

import numpy as np

from .compat_ulog import CompatDataset, CompatULog

_TIME_COLUMN_SCALE_US = {
    'time': 1, 'time_us': 1, 'TimeUS': 1, 'time_usec': 1,
    'time_ms': 1000, 'TimeMS': 1000, 'timeMS': 1000, 'time_s': 1000000,
}
_UNIT_SUFFIX = re.compile(r'^(.*?)\s*\(([^()]*)\)$')
_SIGNAL_COLUMN = re.compile(r'(gyroADC|gyro|motor|rcCommand)\[\d+\]')
# Blackbox Explorer writes one header row per sysConfig entry (~200 today).
_MAX_HEADER_ROWS = 5000


def _column_name(raw: str) -> str:
    """Normalize blackbox_decode names: " time (us)" -> time_us, "vbat (V)" -> vbat."""
    name = raw.strip()
    match = _UNIT_SUFFIX.match(name)
    if not match:
        return name
    base, unit = match.group(1), match.group(2).strip()
    if base == 'time' and unit in ('us', 'ms', 's'):
        return 'time_' + unit
    return base


def _is_column_row(row: List[str]) -> bool:
    """The column row has a time column; sysConfig rows are "key","value" pairs,
    so a two-field row only counts when its other field is a signal we read."""
    names = [_column_name(c) for c in row]
    if not any(n in _TIME_COLUMN_SCALE_US for n in names):
        return False
    return len(names) > 2 or any(_SIGNAL_COLUMN.fullmatch(n) for n in names)


def _load_csv(path: str) -> Dict[str, np.ndarray]:
    import pandas as pd

    with open(path, encoding='utf-8-sig', errors='replace', newline='') as stream:
        # Skip Blackbox Explorer's "key","value" rows; data starts at the column row.
        for _ in range(_MAX_HEADER_ROWS):
            offset = stream.tell()
            if _is_column_row(next(csv.reader([stream.readline()]), [])):
                stream.seek(offset)
                break
        else:
            raise ValueError('CSV missing a column row with a time column '
                             '(expected time/time_us/time_ms)')
        # low_memory=False avoids DtypeWarning on mixed-type columns common in
        # Blackbox Explorer exports (e.g. mode flags as int/str in later rows).
        df = pd.read_csv(stream, skipinitialspace=True, low_memory=False)
    df.columns = [_column_name(str(c)) for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    # Text values (e.g. blackbox_decode flag names) become NaN rather than failing casts.
    df = df.apply(pd.to_numeric, errors='coerce')
    time_column = next(k for k in _TIME_COLUMN_SCALE_US if k in df.columns)
    df = df[df[time_column].notna()]
    if df.empty:
        raise ValueError('CSV contains no data rows')
    data: Dict[str, np.ndarray] = {c: df[c].to_numpy() for c in df.columns}
    return data


def _time_us_from_columns(cols: Dict[str, np.ndarray]) -> np.ndarray:
    for k, scale in _TIME_COLUMN_SCALE_US.items():
        if k in cols:
            return np.rint(cols[k].astype(np.float64) * scale).astype(np.int64)
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
