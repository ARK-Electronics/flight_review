"""Integration tests for parser termination and process isolation."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plot_app'))
from tests.test_ulog_parse import write_synthetic_ulog
from tornado_handlers import security


class ParseLogBoundedTests(unittest.TestCase):
    def test_parses_real_ulog_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ok.ulg')
            write_synthetic_ulog(path, n_status=3)
            ulog = asyncio.run(security.parse_log_bounded(path))
            self.assertEqual(ulog.msg_info_dict.get('sys_name'), 'PX4')

    def test_missing_file_reports_failure(self):
        with self.assertRaises(security.ParserCrashed):
            asyncio.run(security.parse_log_bounded('missing.ulg'))

    def test_timeout_kills_real_child_and_releases_slot(self):
        original = asyncio.create_subprocess_exec
        children = []
        async def sleeper(*args, **kwargs):
            proc = await original(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
            children.append(proc)
            return proc
        async def run():
            with mock.patch.object(security.asyncio, 'create_subprocess_exec', side_effect=sleeper), \
                 mock.patch.object(security, 'PARSER_WALL_TIMEOUT_SECONDS', 0.1):
                for _ in range(2):
                    with self.assertRaises(security.ParserTimeout):
                        await security.parse_log_bounded('irrelevant.ulg')
            self.assertTrue(all(p.returncode is not None for p in children))
        asyncio.run(run())
