"""Machine auth and privacy boundaries for support log imports."""
import json
from sqlite_utils import connect
import tempfile
from pathlib import Path
from unittest.mock import patch
import tornado.web
from tornado.testing import AsyncHTTPTestCase
from tornado_handlers import support_api, analysis_jobs, api_key, ai_analysis, upload
import support_access


class SupportAPITests(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'db.sqlite')
        self.log_id = '12345678-1234-1234-1234-123456789abc'
        with connect(self.db) as con:
            con.execute('CREATE TABLE Users (Username TEXT, Approved INT, IsAdmin INT, ApiKeyHash TEXT)')
            con.execute('CREATE TABLE Logs (Id TEXT, Public INT, Uploader TEXT, Source TEXT, ContentHash TEXT)')
            for user in ['service', 'other']:
                con.execute('INSERT INTO Users VALUES (?,1,0,?)', (user, api_key.hash_api_key(user+'-secret-key-123456789')))
            con.execute('INSERT INTO Logs VALUES (?,0,?,?,?)', (self.log_id, 'service', 'support-api', 'hash'))
        self.patches = [patch.object(api_key, 'get_db_filename', return_value=self.db),
                        patch.object(analysis_jobs, 'get_db_filename', return_value=self.db)]
        for module in [ai_analysis, upload, support_access]:
            self.patches.append(patch.object(module, 'get_db_connection', side_effect=lambda: connect(self.db)))
        for item in self.patches:
            item.start()
        analysis_jobs.setup_jobs()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def get_app(self):
        return tornado.web.Application([
            ('/analysis', support_api.SupportAnalysisHandler),
            (r'/jobs/([a-f0-9]{32})', support_api.SupportJobHandler),
            ('/upload', support_api.SupportUploadHandler)], xsrf_cookies=True, cookie_secret='test-secret')

    def headers(self, user='service'):
        return {'Authorization': 'Bearer '+user+'-secret-key-123456789', 'Content-Type':'application/json'}

    def test_missing_invalid_and_query_keys_rejected(self):
        for path, headers in [('/analysis', {}), ('/analysis?api_key=service-secret-key-123456789', {}),
                              ('/analysis', {'Authorization':'Bearer wrong'})]:
            self.assertEqual(self.fetch(path, headers=headers).code, 401)

    def test_submit_and_poll_require_same_approved_account(self):
        with patch.object(ai_analysis, 'get_xai_api_key', return_value='test'), patch.object(ai_analysis, '_default_analysis_model', return_value='test-model'):
            response = self.fetch('/analysis?log='+self.log_id, method='POST', headers=self.headers(), body='{}')
        self.assertEqual(response.code, 202)
        job = json.loads(response.body)['job_id']
        self.assertEqual(self.fetch('/jobs/'+job, headers=self.headers()).code, 200)
        self.assertEqual(self.fetch('/jobs/'+job, headers=self.headers('other')).code, 404)
        self.assertEqual(self.fetch('/analysis?log='+self.log_id, headers=self.headers('other')).code, 404)
        with connect(self.db) as con:
            con.execute("UPDATE Users SET Approved=0 WHERE Username='service'")
        self.assertEqual(self.fetch('/jobs/'+job, headers=self.headers()).code, 401)

    def test_support_log_access_and_deduplication_are_owner_scoped(self):
        support_access.require_support_access(self.log_id, 'service')
        for user in ['other', None]:
            with self.assertRaises(PermissionError):
                support_access.require_support_access(self.log_id, user)
        self.assertEqual(upload._find_existing_log_by_hash('hash', 'service', True), self.log_id)
        self.assertIsNone(upload._find_existing_log_by_hash('hash', 'other', True))
        self.assertIsNone(upload._find_existing_log_by_hash('hash', 'service', False))

    def test_upload_rejects_no_key_before_parsing(self):
        self.assertEqual(self.fetch('/upload', method='POST', body='garbage').code, 401)
