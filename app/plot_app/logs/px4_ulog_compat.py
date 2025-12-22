"""Compatibility wrapper for PX4ULog usage.

Some UI helpers expect a PX4ULog-like object with methods such as:
- get_mav_type()
- get_estimator()
- add_roll_pitch_yaw()
- get_configured_rc_input_names()

For non-PX4 logs we provide conservative fallbacks.
"""

from __future__ import annotations

from typing import Any, List, Optional


class PX4ULogCompat:
    def __init__(self, ulog: Any, source_name: str = 'Log'):
        self._ulog = ulog
        self._source_name = source_name

    def add_roll_pitch_yaw(self) -> None:
        # For PX4 ULog this mutates datasets. For compat logs, we already provide roll/pitch/yaw.
        return

    def get_mav_type(self) -> str:
        # Prefer explicit if present
        mt = None
        try:
            mt = self._ulog.msg_info_dict.get('mav_type', None)
        except Exception:
            mt = None
        if mt:
            return str(mt)
        return self._source_name

    def get_estimator(self) -> str:
        try:
            est = self._ulog.msg_info_dict.get('estimator', None)
            if est:
                return str(est)
        except Exception:
            pass
        return ''

    def get_configured_rc_input_names(self, _channel_index: int) -> Optional[List[str]]:
        return None
