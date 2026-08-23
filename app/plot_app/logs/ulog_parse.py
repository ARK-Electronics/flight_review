"""Memory-bounded ULog parsing.

pyulog accumulates every matching DATA sample in RAM (`subscription.buffer +=`).
High-rate FIFO topics (sensor_*_fifo) on a long flight, or a slightly corrupt
file that still passes per-message size checks, can grow those buffers until
Python raises MemoryError. Upload then reports the file as corrupt.

This module:
  * caps per-topic and total DATA buffers (with stride so long flights still
    cover the whole timespan at a lower rate)
  * caps logged string / parameter-change lists
  * retries with a smaller topic set after MemoryError
  * offers a header-only fallback for upload metadata
"""
#pylint: disable=protected-access,too-few-public-methods,too-many-branches
#pylint: disable=too-many-locals,too-many-arguments,too-many-statements

from __future__ import annotations

import gc
import os
import struct
from typing import List, Optional, Sequence

from pyulog import ULog


FIFO_TOPICS = ('sensor_accel_fifo', 'sensor_gyro_fifo')

HIGH_RATE_TOPICS = FIFO_TOPICS + (
    'sensor_accel',
    'sensor_combined',
    'ekf2_timestamps',
    'vehicle_imu_status',
    'vehicle_angular_acceleration',
    'sensor_baro',
    'esc_status',
)

# Enough for vehicle DB, emails, PX4ULog.get_mav_type(), airframe name.
ULOG_UPLOAD_MSG_FILTER = [
    'vehicle_status',
    'vehicle_gps_position',
    'battery_status',
    'failsafe_flags',
    'logger_status',
]

CORE_TOPICS = [
    'vehicle_status',
    'vehicle_gps_position',
    'vehicle_local_position',
    'vehicle_global_position',
    'vehicle_attitude',
    'vehicle_angular_velocity',
    'battery_status',
    'failsafe_flags',
    'manual_control_setpoint',
    'rc_channels',
    'estimator_status',
    'logger_status',
]

CORE_TOPIC_SET = set(CORE_TOPICS)

# Defaults sized for a ~1–2 GiB app container (parent + parser worker).
MAX_TOPIC_BUFFER_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_MAX_TOPIC_BUFFER_BYTES', str(64 * 1024 * 1024)))
MAX_NON_CORE_TOPIC_BUFFER_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_MAX_NON_CORE_TOPIC_BUFFER_BYTES', str(32 * 1024 * 1024)))
MAX_TOTAL_BUFFER_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_MAX_TOTAL_BUFFER_BYTES', str(192 * 1024 * 1024)))
CORE_RESERVE_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_CORE_RESERVE_BYTES', str(32 * 1024 * 1024)))
MAX_LOGGED_MESSAGES = int(os.environ.get(
    'FLIGHT_REVIEW_MAX_LOGGED_MESSAGES', '20000'))
MAX_CHANGED_PARAMETERS = int(os.environ.get(
    'FLIGHT_REVIEW_MAX_CHANGED_PARAMETERS', '50000'))
LARGE_LOG_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_LARGE_LOG_BYTES', str(80 * 1024 * 1024)))
# Upload POST must stay fast and cheap: scanning a 200–500 MB ULog (even with
# a topic filter) still walks every DATA message and can stall or OOM the
# request. Above this size, store the file from the definition section only.
UPLOAD_SCAN_MAX_BYTES = int(os.environ.get(
    'FLIGHT_REVIEW_UPLOAD_SCAN_MAX_BYTES', str(32 * 1024 * 1024)))

_PARSE_BUDGET = {'used': 0}


class _CappedList(list):
    """list.append that silently drops items past a maximum length."""

    def __init__(self, max_items, iterable=()):
        super().__init__(iterable)
        self._max_items = int(max_items)
        self.dropped = 0

    def append(self, item):
        if len(self) >= self._max_items:
            self.dropped += 1
            return
        super().append(item)

    def extend(self, items):
        for item in items:
            self.append(item)


def _reset_parse_budget():
    _PARSE_BUDGET['used'] = 0


def _log_cap_once(subscription, reason):
    if getattr(subscription, '_fr_cap_logged', False):
        return
    subscription._fr_cap_logged = True
    name = getattr(subscription, 'message_name', '?')
    multi_id = getattr(subscription, 'multi_id', 0)
    buf = getattr(subscription, 'buffer', b'') or b''
    print('ulog parse: capping %s (instance %s) at %d bytes (%s)' % (
        name, multi_id, len(buf), reason), flush=True)


def _set_timestamp_from_payload(msg_data, data, subscriptions):
    """Best-effort timestamp so last_timestamp still covers the whole log."""
    try:
        msg_id = struct.unpack_from('<H', data, 0)[0]
        subscription = subscriptions.get(msg_id)
        t_off = getattr(subscription, 'timestamp_offset', 0) if subscription else 0
        if t_off + 10 <= len(data):
            msg_data.timestamp = struct.unpack_from('<Q', data, t_off + 2)[0]
            return
    except (struct.error, TypeError, AttributeError):
        pass
    msg_data.timestamp = 0


def _should_drop_sample(data, subscriptions):
    """Return True if this DATA payload should not be appended to a topic buffer."""
    if not data or len(data) < 2 or not isinstance(subscriptions, dict):
        return False
    msg_id = struct.unpack_from('<H', data, 0)[0]
    subscription = subscriptions.get(msg_id)
    if subscription is None:
        return False
    buf = getattr(subscription, 'buffer', None)
    if buf is None:
        return False
    if not isinstance(buf, bytearray):
        try:
            subscription.buffer = bytearray(buf)
            buf = subscription.buffer
        except (TypeError, MemoryError):
            return True

    extra = max(0, len(data) - 2)
    name = getattr(subscription, 'message_name', '') or ''
    is_core = name in CORE_TOPIC_SET
    topic_limit = MAX_TOPIC_BUFFER_BYTES if is_core else min(
        MAX_TOPIC_BUFFER_BYTES, MAX_NON_CORE_TOPIC_BUFFER_BYTES)

    sample_i = getattr(subscription, '_fr_i', 0) + 1
    subscription._fr_i = sample_i
    stride = getattr(subscription, '_fr_stride', 1)
    half = max(1, topic_limit // 2)
    three_q = max(1, (topic_limit * 3) // 4)
    if len(buf) >= three_q:
        stride = max(stride, 4)
    elif len(buf) >= half:
        stride = max(stride, 2)
    subscription._fr_stride = stride

    if len(buf) + extra > topic_limit:
        _log_cap_once(subscription, 'topic-limit')
        return True

    used = _PARSE_BUDGET['used']
    reserved = CORE_RESERVE_BYTES
    if not is_core and used + extra > max(0, MAX_TOTAL_BUFFER_BYTES - reserved):
        _log_cap_once(subscription, 'total-limit')
        return True
    if used + extra > MAX_TOTAL_BUFFER_BYTES:
        _log_cap_once(subscription, 'total-limit')
        return True

    if stride > 1 and (sample_i % stride) != 0:
        return True

    _PARSE_BUDGET['used'] = used + extra
    return False


class SafeULog(ULog):
    """ULog that will not grow topic buffers / log-message lists without bound."""

    def __init__(self, log_file, message_name_filter_list=None,
                 disable_str_exceptions=True, parse_header_only=False):
        _reset_parse_budget()
        super().__init__(log_file, message_name_filter_list,
                         disable_str_exceptions, parse_header_only)

    class _MessageData(ULog._MessageData):
        def initialize(self, data, header, subscriptions, ulog_object):  # noqa: D102
            if _should_drop_sample(data, subscriptions):
                _set_timestamp_from_payload(self, data, subscriptions)
                return False
            return super().initialize(data, header, subscriptions, ulog_object)

    def _read_file_data(self, message_name_filter_list, read_until=None):
        self._logged_messages = _CappedList(MAX_LOGGED_MESSAGES, self._logged_messages)
        self._changed_parameters = _CappedList(
            MAX_CHANGED_PARAMETERS, self._changed_parameters)
        return super()._read_file_data(message_name_filter_list, read_until)


def _unique_filters(filters: Sequence[Sequence[str]]) -> List[List[str]]:
    seen = set()
    out: List[List[str]] = []
    for item in filters:
        key = tuple(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(list(item))
    return out


def filters_for_file(file_name: str, full_filter: Sequence[str]) -> List[List[str]]:
    """Return topic-filter attempts, dropping FIFO/high-rate topics as size grows."""
    full = list(full_filter)
    no_fifo = [name for name in full if name not in FIFO_TOPICS]
    no_high_rate = [name for name in full if name not in HIGH_RATE_TOPICS]
    core = [name for name in full if name in CORE_TOPIC_SET]
    if not core:
        core = list(CORE_TOPICS)

    try:
        size = os.path.getsize(file_name)
    except OSError:
        size = 0

    attempts: List[List[str]] = []
    if size < LARGE_LOG_BYTES:
        attempts.append(full)
    if size < LARGE_LOG_BYTES * 2:
        attempts.append(no_fifo)
    attempts.append(no_high_rate)
    attempts.append(core)
    return _unique_filters(attempts)


def parse_ulog(file_name: str, message_name_filter_list: Optional[Sequence[str]] = None):
    """Load a ULog with buffer caps and a MemoryError retry ladder."""
    if message_name_filter_list is None:
        attempts = [None]
    else:
        attempts = filters_for_file(file_name, message_name_filter_list)

    last_memory_error: Optional[MemoryError] = None
    for msg_filter in attempts:
        try:
            return SafeULog(file_name, msg_filter, disable_str_exceptions=True)
        except MemoryError as exc:
            last_memory_error = exc
            n_topics = 'all' if msg_filter is None else str(len(msg_filter))
            print('ulog parse: MemoryError with %s topics for %s; retrying smaller set' % (
                n_topics, file_name), flush=True)
            gc.collect()
    if last_memory_error is not None:
        raise last_memory_error
    raise RuntimeError('parse_ulog: no filter attempts')


def parse_ulog_header(file_name: str):
    """Parse only the ULog definition section (info, formats, parameters)."""
    return ULog(file_name, None, disable_str_exceptions=True, parse_header_only=True)


def parse_ulog_for_upload(file_name: str):
    """Cheap parse for upload metadata.

    Upload only needs msg_info_dict, parameters, and (when cheap) vehicle_status
    for the vehicle DB / notification email. Large files skip the DATA section
    entirely so POST /upload cannot stall or OOM while walking FIFO topics.
    """
    try:
        size = os.path.getsize(file_name)
    except OSError:
        size = 0
    if size >= UPLOAD_SCAN_MAX_BYTES:
        print('ulog parse: %s is %d bytes; upload uses header-only' % (
            file_name, size), flush=True)
        return parse_ulog_header(file_name)
    try:
        return SafeULog(file_name, ULOG_UPLOAD_MSG_FILTER, disable_str_exceptions=True)
    except MemoryError:
        print('ulog parse: MemoryError on upload filter for %s; trying header-only' % (
            file_name,), flush=True)
        gc.collect()
        return parse_ulog_header(file_name)
