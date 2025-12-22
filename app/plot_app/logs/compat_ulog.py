"""A minimal ULog-compatible interface for non-ULog sources.

The plotting code in this repository expects a pyulog.ULog-like object with:
- .data_list: list of dataset objects with .name, .multi_id, .data (dict of arrays)
- .get_dataset(name, instance=0)
- .start_timestamp, .last_timestamp (microseconds)
- .msg_info_dict, .initial_parameters
- .dropouts, .changed_parameters, .logged_messages

This module provides a lightweight compatibility layer so ArduPilot/BetaFlight
logs can reuse the existing plot pipeline with minimal changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class CompatMessage:
    """Mimics pyulog.ULogMessage enough for UI tables."""

    timestamp: int
    log_level: int
    message: str

    def log_level_str(self) -> str:
        # pyulog uses syslog-like levels encoded as ord('0'..). We keep it simple.
        if self.log_level <= ord('3'):
            return 'ERR'
        if self.log_level == ord('4'):
            return 'WARN'
        if self.log_level == ord('6'):
            return 'INFO'
        return 'INFO'


class CompatDropout:
    """Mimics pyulog.Dropout."""

    def __init__(self, timestamp: int, duration_ms: int):
        self.timestamp = int(timestamp)
        self.duration = int(duration_ms)


class CompatDataset:
    """Mimics pyulog.ULog.Data enough for DataPlot."""

    def __init__(self, name: str, data: Dict[str, np.ndarray], multi_id: int = 0):
        self.name = name
        self.multi_id = int(multi_id)
        self.data = data

    def list_value_changes(self, field: str) -> List[Tuple[int, Any]]:
        """Return list of (timestamp, value) when the given field changes."""
        if 'timestamp' not in self.data or field not in self.data:
            return []
        t = self.data['timestamp']
        v = self.data[field]
        if len(t) == 0 or len(v) == 0:
            return []
        out: List[Tuple[int, Any]] = [(int(t[0]), v[0].item() if hasattr(v[0], 'item') else v[0])]
        last = v[0]
        for i in range(1, min(len(t), len(v))):
            if v[i] != last:
                out.append((int(t[i]), v[i].item() if hasattr(v[i], 'item') else v[i]))
                last = v[i]
        return out


class CompatULog:
    """A ULog-like container for non-ULog logs."""

    def __init__(
        self,
        datasets: Sequence[CompatDataset],
        start_timestamp: int,
        last_timestamp: int,
        *,
        msg_info_dict: Optional[Dict[str, Any]] = None,
        initial_parameters: Optional[Dict[str, Any]] = None,
        logged_messages: Optional[List[CompatMessage]] = None,
    ):
        self.data_list: List[CompatDataset] = list(datasets)
        self.start_timestamp = int(start_timestamp)
        self.last_timestamp = int(last_timestamp)
        self.msg_info_dict: Dict[str, Any] = dict(msg_info_dict or {})
        # pyulog provides both msg_info_dict (single value per key) and
        # msg_info_multiple_dict (list of values per key). Some UI elements
        # check the latter for hardfault/console outputs.
        self.msg_info_multiple_dict: Dict[str, List[Any]] = {}
        self.initial_parameters: Dict[str, Any] = dict(initial_parameters or {})

        self.dropouts: List[CompatDropout] = []
        self.changed_parameters: List[Tuple[int, str, Any]] = []
        self.logged_messages: List[CompatMessage] = list(logged_messages or [])

        # pyulog sets this when it detects corruption while parsing.
        # For non-ULog sources, default to "not corrupt".
        self.file_corruption = False

        self.has_default_parameters = False

    def get_dataset(self, name: str, instance: int = 0) -> CompatDataset:
        for d in self.data_list:
            if d.name == name and int(d.multi_id) == int(instance):
                return d
        raise KeyError(name)

    def get_version_info(self) -> Optional[Tuple[int, int, int, int]]:
        # pyulog returns (major, minor, patch, type). Not available here.
        return None

    def get_version_info_str(self, key: str = 'ver_sw_release') -> Optional[str]:
        v = self.msg_info_dict.get(key)
        return str(v) if v is not None and str(v) != '' else None

    def get_default_parameters(self, _idx: int) -> Dict[str, Any]:
        return {}
