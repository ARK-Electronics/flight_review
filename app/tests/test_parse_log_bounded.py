"""Tests for threaded upload parsing (no process-pool fork)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from concurrent.futures import BrokenExecutor
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..')
_PLOT_APP = os.path.join(_APP, 'plot_app')
for _path in (_APP, _PLOT_APP):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tests.test_ulog_parse import write_synthetic_ulog  # noqa: E402  pylint: disable=wrong-import-position
from tornado_handlers.security import (  # noqa: E402  pylint: disable=wrong-import-position
    ParserCrashed, ParserTimeout, parse_log_bounded,
)


class ParseLogBoundedTests(unittest.TestCase):
    def test_parses_small_ulog_in_thread(self):
        from logs.ulog_parse import parse_ulog_for_upload
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=3)
            with mock.patch(
                    'tornado_handlers.security._worker_load_log',
                    side_effect=parse_ulog_for_upload):
                ulog = asyncio.run(parse_log_bounded(path))
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')

    def test_broken_executor_becomes_parser_crashed(self):
        class DeadPool:
            def submit(self, fn, *args, **kwargs):
                raise BrokenExecutor('pool dead')

        async def _run():
            with mock.patch(
                    'tornado_handlers.security._get_parser_pool',
                    return_value=DeadPool()):
                await parse_log_bounded('missing.ulg')

        with self.assertRaises(ParserCrashed):
            asyncio.run(_run())

    def test_timeout_becomes_parser_timeout(self):
        async def _run():
            with mock.patch(
                    'tornado_handlers.security.PARSER_WALL_TIMEOUT_SECONDS', 0.05):
                with mock.patch(
                        'tornado_handlers.security._worker_load_log',
                        side_effect=lambda path: __import__('time').sleep(1)):
                    await parse_log_bounded('x.ulg')

        with self.assertRaises(ParserTimeout):
            asyncio.run(_run())


if __name__ == '__main__':
    unittest.main()
