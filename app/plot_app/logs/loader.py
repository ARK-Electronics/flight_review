"""Unified log loader.

This repo historically assumes PX4 .ulg (ULog). To support other ecosystems we
load the file based on its extension and return either a pyulog.ULog or a
CompatULog.
"""

from __future__ import annotations

import os
from typing import Any

from pyulog import ULog

from .ardupilot_bin import read_ardupilot_bin
from .betaflight_csv import read_betaflight_csv


class UnsupportedLogFormat(ValueError):
    pass


def detect_log_extension(path: str) -> str:
    _, ext = os.path.splitext(path)
    return ext.lower()


def load_log(path: str) -> Any:
    ext = detect_log_extension(path)

    if ext in ('.ulg',):
        # Use existing helper-level caching for ULog files.
        # Import lazily to avoid cycles.
        from helper import load_ulog_file

        return load_ulog_file(path)

    if ext in ('.bin',):
        return read_ardupilot_bin(path)

    if ext in ('.csv',):
        return read_betaflight_csv(path)

    if ext in ('.bbl', '.txt'):
        raise UnsupportedLogFormat(
            'Betaflight Blackbox binary logs (.bbl/.txt) are not directly supported yet. '
            'Please export to CSV (Blackbox Explorer) and upload the CSV.'
        )

    raise UnsupportedLogFormat(f'Unsupported log extension: {ext}')
