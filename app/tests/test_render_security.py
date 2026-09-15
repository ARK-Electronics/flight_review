"""Regression coverage for untrusted HTML and log mutation boundaries."""
import json
import tempfile
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import tornado.web
from tornado.testing import AsyncHTTPTestCase

import helper
import plotted_tables
from sqlite_utils import connect
from tornado_handlers import common, error_labels


class RenderingTests(TestCase):
    def test_rendered_inline_javascript_remains_valid_with_escaping(self):
        if shutil.which('node') is None:
            self.skipTest('Node is required to validate browser JavaScript')

        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inline = False
                self.sources = []

            def handle_starttag(self, tag, attrs):
                if tag == 'script':
                    self.inline = 'src' not in dict(attrs)

            def handle_endtag(self, tag):
                if tag == 'script':
                    self.inline = False

            def handle_data(self, data):
                if self.inline:
                    self.sources.append(data)

        context = dict(log_id='safe-log', default_model='model', default_effort='medium',
                       has_api_key=True, has_xai_api_key=True, is_plot_page=True,
                       cur_err_ids=[1], plots=[], has_position_data=True,
                       pos_datas='[[1,2],[3,4]]', pos_flight_modes='[["red",0],["blue",1]]',
                       mapbox_api_access_token='"</script><img src=x>', efforts=['medium'])
        env = common.get_jinja_env()
        for name in ['index.html', 'ai_analysis.html', 'pid_ai_analysis.html']:
            scripts = Scripts()
            scripts.feed(env.get_template(name).render(**context))
            self.assertTrue(scripts.sources, name)
            for source in scripts.sources:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.js') as stream:
                    stream.write(source)
                    stream.flush()
                    result = subprocess.run(['node', '--check', stream.name],
                                            capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, name + ': ' + result.stderr)

    def test_account_names_are_text_in_shared_and_ai_navigation(self):
        attack = '<img src=x onerror=alert(1)>'
        env = common.get_jinja_env()
        for template in ['header.html', 'ai_analysis.html']:
            rendered = env.get_template(template).render(
                current_user=attack, log_id='safe-log', default_model='test',
                default_effort='medium', has_api_key=False)
            self.assertNotIn(attack, rendered)
            self.assertIn('&lt;img', rendered)

    def test_plot_heading_escapes_vehicle_type_from_log(self):
        attack = '<svg onload=alert(1)>'
        rendered = plotted_tables.get_heading_html(
            SimpleNamespace(msg_info_dict={}, data_list=[]),
            SimpleNamespace(get_mav_type=lambda: attack),
            SimpleNamespace(description=''), None)
        self.assertNotIn(attack, rendered)
        self.assertIn('&lt;svg', rendered)

    def test_log_paths_reject_traversal_and_trailing_newlines(self):
        for value in ['../private', '/tmp/private', 'good\n', '', None]:
            self.assertFalse(helper.validate_log_id(value))
            with self.assertRaises(ValueError):
                helper.get_log_filename(value)
            with self.assertRaises(ValueError):
                helper.get_log_filename_with_ext(value, '.ulg')
        with self.assertRaises(ValueError):
            helper.get_log_filename_with_ext('valid-id', '/../private')
        self.assertTrue(helper.validate_log_id('1234-Ab_cd'))


class ErrorPage(common.TornadoRequestHandlerBase):
    def get(self):
        raise common.CustomHTTPError(400, '<img src=x onerror=alert(1)>')


class ErrorPageTests(AsyncHTTPTestCase):
    def get_app(self):
        return tornado.web.Application([('/', ErrorPage)], cookie_secret='test')

    def test_custom_errors_cannot_render_request_html(self):
        response = self.fetch('/')
        self.assertEqual(response.code, 400)
        self.assertNotIn(b'<img', response.body)
        self.assertIn(b'&lt;img', response.body)


class LabelHandler(error_labels.UpdateErrorLabelHandler):
    def get_current_user(self):
        # Isolate authorization from the cookie verification tested separately.
        return self.request.headers.get('Test-User')


class LabelSecurityTests(AsyncHTTPTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'labels.sqlite')
        with connect(self.db) as con:
            con.execute('CREATE TABLE Users (Username TEXT, Approved INT, IsAdmin INT)')
            con.execute('CREATE TABLE Logs (Id TEXT, Uploader TEXT, ErrorLabels TEXT)')
            con.executemany('INSERT INTO Users VALUES (?,?,?)',
                            [('owner', 1, 0), ('other', 1, 0), ('admin', 1, 1),
                             ('revoked', 0, 1)])
            con.execute("INSERT INTO Logs VALUES ('log', 'owner', '')")
        self.db_patch = patch.object(error_labels, 'get_db_connection',
                                     side_effect=lambda: connect(self.db))
        self.db_patch.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.db_patch.stop()
        self.temp.cleanup()

    def get_app(self):
        return tornado.web.Application([('/labels', LabelHandler)],
                                       cookie_secret='test', login_url='/login')

    def update(self, user=None, labels=None):
        return self.fetch('/labels', method='POST',
                          headers={'Test-User': user} if user else {},
                          body=json.dumps({'log': 'log', 'labels': labels or [next(iter(helper.error_labels_table))]}))

    def test_anonymous_and_unrelated_accounts_cannot_edit(self):
        self.assertEqual(self.update().code, 403)
        for user in ['other', 'revoked']:
            self.assertEqual(self.update(user).code, 404)
        with connect(self.db) as con:
            self.assertEqual(con.execute('SELECT ErrorLabels FROM Logs').fetchone()[0], '')

    def test_approved_owner_and_admin_can_edit(self):
        label = next(iter(helper.error_labels_table))
        for user in ['owner', 'admin']:
            self.assertEqual(self.update(user, [label]).code, 200)

    def test_invalid_json_shapes_are_rejected(self):
        for body in ['null', '[]', '{}', '{', '{"log":"log","labels":[[]]}']:
            response = self.fetch('/labels', method='POST',
                                  headers={'Test-User': 'owner'}, body=body)
            self.assertEqual(response.code, 400)
