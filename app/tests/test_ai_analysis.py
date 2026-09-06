"""Tests for Grok AI analysis HTTP client behavior."""
#pylint: disable=protected-access,invalid-name

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import tornado.concurrent
import tornado.ioloop
from tornado.httpclient import HTTPClientError

_APP = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..')
_PLOT_APP = os.path.join(_APP, 'plot_app')
for _path in (_APP, _PLOT_APP):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import numpy  # noqa: F401
except ImportError:
    # Full plot_app stack is not installed; stub it so timeout tests still run.
    _cache_dir = tempfile.mkdtemp(prefix='ai_analysis_test_')
    _config = mock.MagicMock()
    _config.get_cache_filepath.return_value = _cache_dir
    _config.get_xai_api_key.return_value = ''
    _config.get_xai_model.return_value = 'grok-4.6'
    for _name, _mod in (
            ('numpy', mock.MagicMock()),
            ('pyulog', mock.MagicMock()),
            ('pyulog.px4', mock.MagicMock()),
            ('config', _config),
            ('helper', mock.MagicMock()),
            ('db_entry', mock.MagicMock()),
            ('jinja2', mock.MagicMock()),
            ):
        sys.modules.setdefault(_name, _mod)

from tornado_handlers import ai_analysis  # noqa: E402  pylint: disable=wrong-import-position


def _run(coro_fn):
    return tornado.ioloop.IOLoop.current().run_sync(coro_fn)


def _future_result(value):
    fut = tornado.concurrent.Future()
    fut.set_result(value)
    return fut


def _future_exception(exc):
    fut = tornado.concurrent.Future()
    fut.set_exception(exc)
    return fut


class CallGrokTimeoutTests(unittest.TestCase):
    def test_request_timeout_matches_xai_reasoning_guidance(self):
        self.assertEqual(ai_analysis._GROK_REQUEST_TIMEOUT_SECONDS, 3600)

    def test_http_request_uses_long_timeout(self):
        captured = {}

        class FakeClient:
            def fetch(self, request, raise_error=False):
                captured['request'] = request
                captured['raise_error'] = raise_error
                body = json.dumps({
                    'choices': [{'message': {
                        'content': 'ok',
                        'reasoning_content': None,
                    }}]
                }).encode('utf-8')
                return _future_result(type('Resp', (), {'code': 200, 'body': body})())

        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            ok, payload, status = _run(
                lambda: ai_analysis._call_grok('key', 'grok-4.6', 'sys', 'user'))

        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(payload['analysis'], 'ok')
        self.assertEqual(captured['request'].request_timeout, 3600)
        self.assertEqual(captured['request'].connect_timeout, 30)
        self.assertFalse(captured['raise_error'])

    def test_http_timeout_returns_504(self):
        class FakeClient:
            def fetch(self, request, raise_error=False):
                return _future_exception(HTTPClientError(599, 'Timeout'))

        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            ok, payload, status = _run(
                lambda: ai_analysis._call_grok('key', 'grok-4.6', 'sys', 'user'))

        self.assertFalse(ok)
        self.assertEqual(status, 504)
        self.assertIn('timed out', payload)
        self.assertIn('grok-4.6', payload)

    def test_other_http_client_error_returns_502(self):
        class FakeClient:
            def fetch(self, request, raise_error=False):
                return _future_exception(HTTPClientError(500, 'upstream'))

        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            ok, payload, status = _run(
                lambda: ai_analysis._call_grok('key', 'grok-build', 'sys', 'user'))

        self.assertFalse(ok)
        self.assertEqual(status, 502)
        self.assertIn('xAI API error', payload)


if __name__ == '__main__':
    unittest.main()
