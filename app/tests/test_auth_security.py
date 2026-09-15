"""HTTP regressions for account privilege, session, and browser security."""

from html.parser import HTMLParser
from http.cookies import SimpleCookie
import json
import runpy
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode

import tornado.web
from tornado.testing import AsyncHTTPTestCase

from tornado_handlers import admin, api_key, auth, common, security
from plot_app import config as script_config


class _HTMLTags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class AuthSecurityTests(AsyncHTTPTestCase):
    secret = 'test-only-cookie-secret'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'users.sqlite')
        self.hasher = auth.bcrypt.using(rounds=4)
        self.password_hash = self.hasher.hash('original password')
        self.api_key = 'fr_existing_api_key_12345678901234567890'
        with sqlite3.connect(self.db) as con:
            con.execute('''CREATE TABLE Users (
                Username TEXT PRIMARY KEY, PasswordHash TEXT, Email TEXT,
                Approved INTEGER, IsAdmin INTEGER, AccountToken TEXT DEFAULT '',
                ResetToken TEXT DEFAULT '', ResetTokenExpiration REAL DEFAULT 0,
                ApiKeyHash TEXT DEFAULT '', ApiKeyPrefix TEXT DEFAULT '',
                ApiKeyCreated REAL DEFAULT 0)''')
            con.execute('CREATE TABLE DeletedUsers (Username TEXT PRIMARY KEY)')
            con.execute('''INSERT INTO Users
                (Username, PasswordHash, Email, Approved, IsAdmin, ApiKeyHash)
                VALUES ('alice', ?, 'alice@example.com', 1, 1, ?)''',
                        (self.password_hash, api_key.hash_api_key(self.api_key)))
        self.patches = [patch.object(module, 'get_db_filename', return_value=self.db)
                        for module in (admin, api_key, auth, common, security)]
        self.patches += [patch.object(auth, 'bcrypt', self.hasher),
                         patch.object(security, '_rate_limiter', security.RateLimiter()),
                         patch.object(auth, 'process_pending_logs_for_user'),
                         patch.object(admin, 'process_pending_logs_for_user'),
                         patch.object(auth, 'send_approval_email', return_value=True),
                         patch.object(auth, 'send_account_approved_email', return_value=True),
                         patch.object(auth, 'send_reset_password_email', return_value=False)]
        for item in self.patches:
            item.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def get_app(self):
        return tornado.web.Application([
            ('/login', auth.LoginHandler),
            ('/register', auth.RegisterHandler),
            ('/approve_user', auth.ApproveUserHandler),
            ('/forgot_password', auth.ForgotPasswordHandler),
            ('/reset_password', auth.ResetPasswordHandler),
            ('/account', api_key.AccountHandler),
            ('/admin', admin.AdminUsersHandler),
            ('/admin/api', admin.AdminUsersAPIHandler),
        ], cookie_secret=self.secret, xsrf_cookies=True, login_url='/login')

    def session_cookie(self, username='alice', password_hash=None, days_old=0):
        value = security.session_cookie_value(
            username, password_hash or self.password_hash, self.secret)
        return tornado.web.create_signed_value(
            self.secret, 'user', value,
            clock=lambda: time.time() - days_old * 86400).decode('ascii')

    def browser_post(self, path, data, session=None):
        page = self.fetch('/login')
        cookies = SimpleCookie()
        for value in page.headers.get_list('Set-Cookie'):
            cookies.load(value)
        token = cookies['_xsrf'].value
        if session is not None:
            cookies['user'] = session
        headers = {'Cookie': '; '.join(m.OutputString() for m in cookies.values()),
                   'Content-Type': 'application/x-www-form-urlencoded'}
        return self.fetch(path, method='POST', headers=headers,
                          body=urlencode(dict(data, _xsrf=token)), follow_redirects=False)

    def test_public_registration_cannot_claim_first_admin(self):
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM Users')
        response = self.browser_post('/register', {
            'username': 'first', 'password': 'public password', 'email': 'first@example.com'})
        self.assertEqual(response.code, 200)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                'SELECT Approved, IsAdmin FROM Users WHERE Username=?',
                ('first',)).fetchone(), (0, 0))
        login = self.browser_post('/login', {'username': 'first', 'password': 'public password'})
        self.assertIn(b'Account pending approval', login.body)
        self.assertNotIn('user=', '\n'.join(login.headers.get_list('Set-Cookie')))

    def test_cross_site_forms_cannot_change_account_or_admin_state(self):
        cookie = 'user=' + self.session_cookie()
        for path, body in [('/account', 'action=revoke'),
                           ('/admin/api', 'action=delete&username=alice'),
                           ('/login', 'username=alice&password=original+password'),
                           ('/register', 'username=attacker&password=test&email=a%40b.test'),
                           ('/forgot_password', 'email=alice%40example.com'),
                           ('/reset_password', 'token=test&password=changed')]:
            with self.subTest(path=path):
                response = self.fetch(path, method='POST', body=body,
                                      headers={'Cookie': cookie,
                                               'Origin': 'https://attacker.example'})
                self.assertEqual(response.code, 403)
        self.assertIsNotNone(api_key.lookup_user_by_api_key(self.api_key))
        response = self.browser_post('/account', {'action': 'revoke'}, self.session_cookie())
        self.assertEqual(response.code, 200)
        self.assertIsNone(api_key.lookup_user_by_api_key(self.api_key))

    def test_approval_link_requires_admin_confirmation_with_csrf(self):
        token = 'pending-account-approval-token'
        with sqlite3.connect(self.db) as con:
            con.execute('''INSERT INTO Users
                (Username, PasswordHash, Email, Approved, IsAdmin, AccountToken)
                VALUES ('pending', ?, 'pending@example.com', 0, 0, ?)''',
                        (self.password_hash, token))
            con.execute('''INSERT INTO Users
                (Username, PasswordHash, Approved, IsAdmin)
                VALUES ('member', ?, 1, 0), ('unapproved-admin', ?, 0, 1)''',
                        (self.password_hash, self.password_hash))

        path = '/approve_user?' + urlencode({'token': token})
        response = self.fetch(path, follow_redirects=False)
        self.assertEqual(response.code, 302)
        self.assertIn('next=', response.headers['Location'])
        response = self.fetch(path, headers={'Cookie': 'user=' + self.session_cookie()})
        self.assertEqual(response.code, 200)
        self.assertIn(b'pending@example.com', response.body)
        tags = _HTMLTags(response.body.decode()).tags
        self.assertTrue(any(tag == 'form' and attrs.get('method') == 'post'
                            and attrs.get('action') == '/approve_user' for tag, attrs in tags))
        self.assertTrue(any(tag == 'input' and attrs.get('name') == '_xsrf'
                            for tag, attrs in tags))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT Approved FROM Users WHERE Username='pending'").fetchone(), (0,))
        auth.send_account_approved_email.assert_not_called()
        auth.process_pending_logs_for_user.assert_not_called()

        for session in (None, self.session_cookie('member'), self.session_cookie('unapproved-admin')):
            response = self.browser_post('/approve_user', {'token': token}, session)
            self.assertEqual(response.code, 403)
        response = self.fetch('/approve_user', method='POST', body=urlencode({'token': token}),
                              headers={'Cookie': 'user=' + self.session_cookie()})
        self.assertEqual(response.code, 403)
        response = self.browser_post('/approve_user', {'token': 'invalid'}, self.session_cookie())
        self.assertEqual(response.code, 404)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT Approved FROM Users WHERE Username='pending'").fetchone(), (0,))

        response = self.browser_post('/approve_user', {'token': token}, self.session_cookie())
        self.assertEqual(response.code, 200)
        self.assertIn(b'This account is approved.', response.body)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT Approved, AccountToken FROM Users WHERE Username='pending'").fetchone(), (1, ''))
        auth.send_account_approved_email.assert_called_once()
        response = self.browser_post('/approve_user', {'token': token}, self.session_cookie())
        self.assertEqual(response.code, 404)
        auth.send_account_approved_email.assert_called_once()

    def test_sessions_expire_and_reject_legacy_or_forged_cookies(self):
        valid = self.session_cookie()
        self.assertEqual(security.decode_session_cookie(valid, self.secret), 'alice')
        legacy = tornado.web.create_signed_value(self.secret, 'user', 'alice')
        for cookie in (legacy, self.session_cookie(days_old=8), valid + 'tamper',
                       self.session_cookie(password_hash='wrong-password-hash')):
            with self.subTest(cookie=cookie):
                self.assertIsNone(security.decode_session_cookie(cookie, self.secret))
                response = self.fetch('/account', headers={'Cookie': 'user=' + (
                    cookie.decode() if isinstance(cookie, bytes) else cookie)},
                    follow_redirects=False)
                self.assertEqual(response.code, 302)

    def test_sessions_reject_unapproved_deleted_and_recreated_accounts(self):
        cookie = self.session_cookie()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE Users SET Approved=0 WHERE Username='alice'")
        self.assertIsNone(security.decode_session_cookie(cookie, self.secret))
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM Users WHERE Username='alice'")
        self.assertIsNone(security.decode_session_cookie(cookie, self.secret))
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO Users (Username, PasswordHash, Approved) VALUES ('alice', ?, 1)",
                        (self.hasher.hash('original password'),))
        self.assertIsNone(security.decode_session_cookie(cookie, self.secret))

    def test_password_reset_revokes_sessions_and_api_keys_and_is_single_use(self):
        cookie = self.session_cookie()
        token = 'unique-reset-token'
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE Users SET ResetToken=?, ResetTokenExpiration=? WHERE Username='alice'",
                        (token, time.time() + 300))
        response = self.browser_post('/reset_password', {'token': token, 'password': 'new password'})
        self.assertIn(b'Password reset successful', response.body)
        self.assertIsNone(security.decode_session_cookie(cookie, self.secret))
        self.assertIsNone(api_key.lookup_user_by_api_key(self.api_key))
        response = self.browser_post('/reset_password', {'token': token, 'password': 'attacker password'})
        self.assertIn(b'Invalid password reset link', response.body)
        with sqlite3.connect(self.db) as con:
            password_hash = con.execute("SELECT PasswordHash FROM Users WHERE Username='alice'").fetchone()[0]
        self.assertTrue(self.hasher.verify('new password', password_hash))
        response = self.browser_post('/login', {'username': 'alice', 'password': 'new password'})
        self.assertEqual(response.code, 302)
        cookies = SimpleCookie()
        for value in response.headers.get_list('Set-Cookie'):
            cookies.load(value)
        self.assertEqual(security.decode_session_cookie(cookies['user'].value, self.secret), 'alice')

    def test_deleted_username_cannot_inherit_previous_ownership(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO Users (Username, PasswordHash, Approved) VALUES ('former', ?, 1)",
                        (self.password_hash,))
            con.execute('CREATE TABLE Logs (Id TEXT, Uploader TEXT)')
            con.execute("INSERT INTO Logs VALUES ('private-log', 'former')")
        old_cookie = self.session_cookie('former')
        response = self.browser_post('/admin/api', {'action': 'delete', 'username': 'former'},
                                     self.session_cookie())
        self.assertTrue(json.loads(response.body)['success'])
        self.assertIsNone(security.decode_session_cookie(old_cookie, self.secret))
        response = self.browser_post('/register', {
            'username': 'former', 'password': 'new owner password', 'email': 'attacker@example.com'})
        self.assertIn(b'Username is unavailable', response.body)
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM Users WHERE Username='former'").fetchone())
            self.assertEqual(con.execute("SELECT Uploader FROM Logs WHERE Id='private-log'").fetchone(),
                             ('former',))

    def test_login_blocks_backslash_and_external_redirects(self):
        for target in ('/\\attacker.example', '//attacker.example', 'https://attacker.example', '/\t/attacker.example'):
            response = self.browser_post('/login', {
                'username': 'alice', 'password': 'original password', 'next': target})
            self.assertEqual(response.headers['Location'], '/')
        response = self.browser_post('/login', {
            'username': 'alice', 'password': 'original password', 'next': '/browse?owner=alice'})
        self.assertEqual(response.headers['Location'], '/browse?owner=alice')

    def test_public_login_attempts_are_limited_before_password_verification(self):
        for _ in range(auth.LoginHandler.rate_limit):
            response = self.browser_post('/login', {'username': 'alice', 'password': 'incorrect'})
            self.assertEqual(response.code, 200)
        with patch.object(auth.bcrypt, 'verify') as verify:
            response = self.browser_post('/login', {'username': 'alice', 'password': 'original password'})
        self.assertEqual(response.code, 429)
        self.assertIn('Retry-After', response.headers)
        verify.assert_not_called()

    def test_admin_renders_signup_payload_as_data(self):
        payload = "x');alert(1);//<img src=x onerror=alert(2)>"
        response = self.browser_post('/register', {
            'username': payload, 'password': 'public password', 'email': payload})
        self.assertEqual(response.code, 200)
        response = self.fetch('/admin', headers={'Cookie': 'user=' + self.session_cookie()})
        self.assertEqual(response.code, 200)
        tags = _HTMLTags(response.body.decode()).tags
        self.assertFalse(any(tag == 'img' and attrs.get('src') == 'x' for tag, attrs in tags))
        buttons = [attrs for tag, attrs in tags if tag == 'button' and attrs.get('data-username') == payload]
        self.assertEqual(len(buttons), 2)
        self.assertEqual({attrs['onclick'] for attrs in buttons}, {
            'approveUser(this.dataset.username)', 'confirmDelete(this.dataset.username)'})
        response = self.browser_post('/admin/api', {'action': 'approve', 'username': payload}, self.session_cookie())
        self.assertTrue(json.loads(response.body)['success'])

    def test_mail_failure_does_not_reveal_registered_email(self):
        registered = self.browser_post('/forgot_password', {'email': 'alice@example.com'})
        unknown = self.browser_post('/forgot_password', {'email': 'nobody@example.com'})
        message = b'If an account with that email exists'
        self.assertIn(message, registered.body)
        self.assertIn(message, unknown.body)


class AccountMigrationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / 'logs.sqlite')
        self.app_path = Path(__file__).resolve().parents[1]
        self.patch = patch.multiple(
            script_config,
            get_db_filename=lambda: self.db,
            get_log_filepath=lambda: str(self.root / 'logs'),
            get_cache_filepath=lambda: str(self.root / 'cache'),
            get_kml_filepath=lambda: str(self.root / 'kml'),
            get_overview_img_filepath=lambda: str(self.root / 'images'))
        self.patch.start()
        runpy.run_path(str(self.app_path / 'setup_db.py'))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_upgrade_reserves_historical_log_and_job_owners(self):
        with sqlite3.connect(self.db) as con:
            con.execute('DROP TABLE DeletedUsers')
            con.execute("INSERT INTO Users (Username) VALUES ('active')")
            con.execute("INSERT INTO Logs (Id, Uploader) VALUES ('log1', 'former-log')")
            con.execute("INSERT INTO Logs (Id, Uploader) VALUES ('log2', 'active')")
            con.execute("INSERT INTO Logs (Id, Uploader) VALUES ('log3', '')")
            con.execute('CREATE TABLE AnalysisJobs (Username TEXT)')
            con.execute("INSERT INTO AnalysisJobs VALUES ('former-job')")
            con.execute("INSERT INTO AnalysisJobs VALUES ('active')")
        runpy.run_path(str(self.app_path / 'setup_db.py'))
        runpy.run_path(str(self.app_path / 'setup_db.py'))
        with sqlite3.connect(self.db) as con:
            reserved = {row[0] for row in con.execute('SELECT Username FROM DeletedUsers')}
        self.assertEqual(reserved, {'former-log', 'former-job'})

    def test_operator_can_bootstrap_an_approved_administrator(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO Users (Username) VALUES ('operator')")
        with patch.object(sys, 'argv', ['set_admin.py', 'operator']):
            runpy.run_path(str(self.app_path / 'set_admin.py'), run_name='__main__')
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute(
                "SELECT Approved, IsAdmin FROM Users WHERE Username='operator'").fetchone(), (1, 1))
