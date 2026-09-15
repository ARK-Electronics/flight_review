"""Exercise plot session isolation through Bokeh's HTTP and websocket handlers."""
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.models import Div
from bokeh.server.tornado import BokehTornado
from bokeh.util.token import generate_jwt_token, get_token_payload
from tornado.httpclient import HTTPClientError, HTTPRequest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import create_signed_value
from tornado.websocket import websocket_connect

from bokeh_security import FlightReviewAuthProvider, session_options
from tornado_handlers import security


class BokehSecurityTests(AsyncHTTPTestCase):
    def setUp(self):
        self.secret = 'test-only-bokeh-cookie-secret-' * 2
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / 'users.sqlite'
        with sqlite3.connect(db) as con:
            con.execute('CREATE TABLE Users (Username TEXT, PasswordHash TEXT, Approved INTEGER)')
            con.executemany('INSERT INTO Users VALUES (?, ?, 1)',
                            [('alice', 'alice-hash'), ('bob', 'bob-hash')])
        self.patch = mock.patch.object(security, 'get_db_filename', return_value=str(db))
        self.patch.start()
        self.cookies = {
            username: create_signed_value(
                self.secret, 'user', security.session_cookie_value(
                    username, username + '-hash', self.secret)).decode()
            for username in ('alice', 'bob')
        }
        super().setUp()

    def tearDown(self):
        self._app.stop()
        super().tearDown()
        self.patch.stop()
        self.tmp.cleanup()

    def get_app(self):
        def plot(doc):
            doc.add_root(Div(text='private plot'))

        app = BokehTornado(
            {'/plot_app': Application(FunctionHandler(plot))},
            auth_provider=FlightReviewAuthProvider(), cookie_secret=self.secret,
            extra_websocket_origins=['localhost'], xsrf_cookies=True,
            **session_options(self.secret))
        app.initialize(self.io_loop)
        return app

    def headers(self, username='alice'):
        return {'Cookie': 'user=' + self.cookies[username] + '; unrelated=private',
                'Origin': 'http://localhost', 'Authorization': 'Bearer private'}

    def test_http_session_fixation_is_rejected(self):
        for suffix in ('?bokeh-session-id=attacker-chosen', '?bokeh-token=attacker-token'):
            response = self.fetch('/plot_app' + suffix, headers=self.headers())
            self.assertEqual(response.code, 403)
        headers = self.headers()
        headers['Bokeh-Session-Id'] = 'attacker-chosen'
        self.assertEqual(self.fetch('/plot_app', headers=headers).code, 403)
        self.assertEqual(self._app.get_sessions('/plot_app'), [])

    def test_fresh_page_sets_xsrf_and_excludes_unrelated_credentials(self):
        response = self.fetch('/plot_app', headers=self.headers())
        self.assertEqual(response.code, 200)
        self.assertTrue(any(value.startswith('_xsrf=')
                            for value in response.headers.get_list('Set-Cookie')))
        session = self._app.get_sessions('/plot_app')[0]
        payload = get_token_payload(session.token)
        self.assertEqual(payload['cookies'], {'user': self.cookies['alice']})
        self.assertEqual(payload['headers'], {})
        # Even an authentic server-issued session ID cannot be shared by URL.
        shared = '/plot_app?bokeh-session-id=' + quote(session.id)
        self.assertEqual(self.fetch(shared, headers=self.headers('bob')).code, 403)

    async def connect_plot(self, token, username='alice'):
        request = HTTPRequest(self.get_url('/plot_app/ws').replace('http:', 'ws:'),
                              headers=self.headers(username))
        return await websocket_connect(request, subprotocols=['bokeh', token])

    @gen_test
    async def test_websocket_accepts_owner_and_rejects_another_account(self):
        await self.http_client.fetch(self.get_url('/plot_app'), headers=self.headers())
        token = self._app.get_sessions('/plot_app')[0].token
        socket = await self.connect_plot(token)
        try:
            self.assertIn('ACK', await socket.read_message())
        finally:
            socket.close()
        with self.assertRaises(HTTPClientError) as error:
            await self.connect_plot(token, 'bob')
        self.assertEqual(error.exception.code, 403)

    @gen_test
    async def test_websocket_rejects_forged_token_before_creating_document(self):
        token = generate_jwt_token('attacker-chosen', signed=False,
                                   extra_payload={'cookies': {'user': self.cookies['alice']}})
        with self.assertRaises(HTTPClientError) as error:
            await self.connect_plot(token)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self._app.get_sessions('/plot_app'), [])

    @gen_test
    async def test_revoked_account_cannot_reconnect_existing_plot(self):
        await self.http_client.fetch(self.get_url('/plot_app'), headers=self.headers())
        token = self._app.get_sessions('/plot_app')[0].token
        with sqlite3.connect(security.get_db_filename()) as con:
            con.execute("UPDATE Users SET Approved=0 WHERE Username='alice'")
        with self.assertRaises(HTTPClientError) as error:
            await self.connect_plot(token)
        self.assertEqual(error.exception.code, 403)
