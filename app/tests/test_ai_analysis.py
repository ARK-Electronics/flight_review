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
from tornado_handlers import ai_chat  # noqa: E402  pylint: disable=wrong-import-position


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

    def test_grok_46_sends_selected_reasoning_effort(self):
        captured = {}

        class FakeClient:
            def fetch(self, request, raise_error=False):
                captured['body'] = json.loads(request.body)
                body = json.dumps({
                    'choices': [{'message': {'content': 'ok'}}]
                }).encode('utf-8')
                return _future_result(type('Resp', (), {'code': 200, 'body': body})())

        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            ok, payload, status = _run(
                lambda: ai_analysis._call_grok(
                    'key', 'grok-4.6', 'sys', 'user', effort='low'))

        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(payload['effort'], 'low')
        self.assertEqual(captured['body']['reasoning'], {'effort': 'low'})

    def test_grok_build_omits_reasoning_effort(self):
        captured = {}

        class FakeClient:
            def fetch(self, request, raise_error=False):
                captured['body'] = json.loads(request.body)
                body = json.dumps({
                    'choices': [{'message': {'content': 'ok'}}]
                }).encode('utf-8')
                return _future_result(type('Resp', (), {'code': 200, 'body': body})())

        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            _run(lambda: ai_analysis._call_grok(
                'key', 'grok-build', 'sys', 'user', effort='high'))

        self.assertNotIn('reasoning', captured['body'])

    def test_extra_messages_are_sent_after_system(self):
        captured = {}

        class FakeClient:
            def fetch(self, request, raise_error=False):
                captured['body'] = json.loads(request.body)
                body = json.dumps({
                    'choices': [{'message': {'content': 'reply'}}]
                }).encode('utf-8')
                return _future_result(type('Resp', (), {'code': 200, 'body': body})())

        extra = [
            {'role': 'user', 'content': 'log context'},
            {'role': 'assistant', 'content': 'ack'},
            {'role': 'user', 'content': 'why did it crash?'},
        ]
        with mock.patch.object(ai_analysis, 'AsyncHTTPClient', return_value=FakeClient()):
            ok, payload, _status = _run(
                lambda: ai_analysis._call_grok(
                    'key', 'grok-4.6', 'sys', extra_messages=extra, effort='medium'))

        self.assertTrue(ok)
        self.assertEqual(payload['analysis'], 'reply')
        roles = [m['role'] for m in captured['body']['messages']]
        self.assertEqual(roles, ['system', 'user', 'assistant', 'user'])
        self.assertEqual(captured['body']['messages'][-1]['content'], 'why did it crash?')


class ModelAndEffortTests(unittest.TestCase):
    def test_default_model_is_newest_flagship(self):
        newest = ai_analysis.pick_newest_grok_model([
            'grok-build',
            'grok-4-fast',
            'grok-4.5',
            'grok-4.6',
            'grok-3',
        ])
        self.assertEqual(newest, 'grok-4.6')

    def test_default_skips_grok_build_and_imagine(self):
        newest = ai_analysis.pick_newest_grok_model([
            'grok-build',
            'grok-imagine-image',
            'grok-4.5',
        ])
        self.assertEqual(newest, 'grok-4.5')

    def test_sanitize_effort_defaults_to_medium(self):
        self.assertEqual(ai_analysis._sanitize_effort(None), 'medium')
        self.assertEqual(ai_analysis._sanitize_effort('nope'), 'medium')
        self.assertEqual(ai_analysis._sanitize_effort('HIGH'), 'high')
        self.assertEqual(ai_analysis._sanitize_effort('xhigh'), 'xhigh')

    def test_supports_reasoning_effort(self):
        self.assertTrue(ai_analysis._supports_reasoning_effort('grok-4.6'))
        self.assertTrue(ai_analysis._supports_reasoning_effort('grok-4.5'))
        self.assertTrue(ai_analysis._supports_reasoning_effort('grok-4-latest'))
        self.assertTrue(ai_analysis._supports_reasoning_effort('grok-4-fast'))
        self.assertFalse(ai_analysis._supports_reasoning_effort('grok-build'))
        self.assertFalse(ai_analysis._supports_reasoning_effort('grok-imagine-image'))

    def test_sanitize_chat_messages_drops_system_and_empty(self):
        cleaned = ai_chat._sanitize_chat_messages([
            {'role': 'system', 'content': 'ignore me'},
            {'role': 'user', 'content': '  hello  '},
            {'role': 'assistant', 'content': 'hi'},
            {'role': 'user', 'content': ''},
            'nope',
            {'role': 'user', 'content': 123},
        ])
        self.assertEqual(cleaned, [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi'},
        ])

    def test_chat_prompt_is_not_a_full_report_request(self):
        prompt = ai_analysis._build_analysis_prompt(
            {'duration_s': 12, 'mav_type': 'Quadrotor'},
            {}, {}, {}, {}, [], for_chat=True)
        self.assertIn('Flight Log Context', prompt)
        self.assertNotIn('Overall Flight Safety Rating', prompt)


if __name__ == '__main__':
    unittest.main()
