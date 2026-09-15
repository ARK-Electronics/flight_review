"""Regression tests for hostile uploads, log edits, and HTML injection."""
import asyncio
from datetime import datetime
from http.cookies import SimpleCookie
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import tornado.web
from tornado.testing import AsyncHTTPTestCase, gen_test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plot_app'))
import support_access
from tornado_handlers import browse, common, download, edit_entry, upload
from tornado_handlers.multipart_streamer import MultiPartStreamer, ParseError, SizeLimitError
from tornado_handlers.security import ParserCrashed, RateLimiter


def multipart(parts, boundary=b'test-boundary'):
    """Build actual wire-format parts, including binary file data."""
    body = b''
    for name, filename, data in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body += b'--' + boundary + b'\r\n' + disposition.encode() + b'\r\n\r\n'
        body += data + b'\r\n'
    return body + b'--' + boundary + b'--\r\n'


class MultipartSecurityTests(unittest.TestCase):
    def test_binary_file_and_small_fields_survive_every_chunk_boundary(self):
        data = b'\x00file\r\n--test-boundaryXYstill file\xff'
        body = multipart([('email', None, b'a@b.example'), ('empty', None, b''),
                          ('filearg', 'flight.ulg', data)])
        for chunk_size in range(1, 40):
            stream = MultiPartStreamer(len(body))
            try:
                for offset in range(0, len(body), chunk_size):
                    stream.data_received(body[offset:offset + chunk_size])
                stream.data_complete()
                self.assertEqual(stream.get_values(['email', 'empty']),
                                 {'email': b'a@b.example', 'empty': b''})
                self.assertEqual(stream.get_parts_by_name('filearg')[0].get_payload(), data)
            finally:
                stream.release_parts()

    def test_header_part_field_and_body_limits_apply_while_streaming(self):
        cases = [
            ({'max_header_size': 64}, b'--test\r\nX-Header: ' + b'x' * 65),
            ({'max_header_size': 64}, b'--test\r\n' + b'X: y\r\n' * 15),
            ({'max_parts': 2}, multipart([('f', None, b'') for _ in range(3)])),
            ({'max_field_size': 8}, multipart([('email', None, b'x' * 100)])),
            ({'max_size': 32}, b'x' * 33),
        ]
        for limits, body in cases:
            stream = MultiPartStreamer(0, **limits)
            try:
                with self.assertRaises(SizeLimitError):
                    stream.data_received(body)
            finally:
                stream.release_parts()
            self.assertTrue(all(not Path(part.f_out.name).exists() for part in stream.parts))
            stream.release_parts()  # Disconnect and normal finish may both release.

    def test_truncated_body_is_not_accepted(self):
        stream = MultiPartStreamer(0)
        try:
            stream.data_received(multipart([('filearg', 'flight.ulg', b'payload')])[:-8])
            with self.assertRaises(ParseError):
                stream.data_complete()
        finally:
            stream.release_parts()


class _SessionUser:
    """Keep authentication independent from the file endpoint regression tests."""
    def get_current_user(self):
        return 'owner'


class _Upload(_SessionUser, upload.UploadHandler):
    pass


class _Edit(_SessionUser, edit_entry.EditEntryHandler):
    pass


class _Browse(_SessionUser, browse.BrowseHandler):
    pass


class _Download(_SessionUser, download.DownloadHandler):
    pass


class FileEndpointSecurityTests(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'test.sqlite')
        self.log_id = 'test-log'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE Users (Username TEXT, Email TEXT, Approved INT, IsAdmin INT)')
            con.execute('CREATE TABLE Logs (Id TEXT, Token TEXT, Uploader TEXT, Email TEXT, '
                        'Source TEXT, Description TEXT)')
            for column in ['Title', 'OriginalFilename', 'Date', 'AllowForAnalysis', 'Obfuscated',
                           'WindSpeed', 'Rating', 'Feedback', 'Type', 'videoUrl', 'ErrorLabels',
                           'Public', 'Pending', 'ContentHash']:
                con.execute('ALTER TABLE Logs ADD COLUMN ' + column)
            con.execute("INSERT INTO Users VALUES ('owner','same@example.com',1,0)")
            con.execute("INSERT INTO Users VALUES ('attacker','same@example.com',1,0)")
            con.execute("INSERT INTO Users VALUES ('admin','admin@example.com',1,1)")
            con.execute("INSERT INTO Logs (Id, Token, Uploader, Email, Source, Description) "
                        "VALUES ('test-log','secret-token','owner',"
                        "'same@example.com','webui','original')")
        self.patches = [patch.object(module, 'get_db_connection',
                                     side_effect=lambda: sqlite3.connect(self.db))
                        for module in [browse, edit_entry, upload, support_access]]
        self.patches += [patch.object(common, 'get_db_filename', return_value=self.db),
                         patch.object(upload, 'get_rate_limiter', return_value=RateLimiter()),
                         patch.object(upload, 'authenticate_request_api_key',
                                      side_effect=lambda handler: 'owner' if
                                      handler.request.headers.get('X-API-Key') == 'valid' else None)]
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
            ('/upload', _Upload), ('/edit_entry', _Edit), ('/browse', _Browse),
            ('/download', _Download),
        ], xsrf_cookies=True, cookie_secret='test-secret')

    @staticmethod
    def xsrf_headers(response):
        cookies = SimpleCookie()
        for header in response.headers.get_list('Set-Cookie'):
            cookies.load(header)
        token = cookies['_xsrf'].value
        return {'Cookie': '_xsrf=' + token, 'X-XSRFToken': token}

    def test_unverified_matching_email_never_grants_edit_or_delete(self):
        with sqlite3.connect(self.db) as con:
            for user, token, allowed in [('owner', '', True), ('admin', '', True),
                                         ('attacker', '', False), (None, 'secret-token', True)]:
                self.assertEqual(edit_entry.EditEntryHandler._is_authorized(
                    con.cursor(), self.log_id, token, user), allowed)

    def test_delete_get_is_read_only_and_post_requires_xsrf(self):
        path = '/edit_entry?action=delete&log=test-log&confirm=1'
        with patch.object(edit_entry.EditEntryHandler, 'delete_log_entry', return_value=True) as delete:
            response = self.fetch(path)
            self.assertEqual(response.code, 200)
            self.assertIn(b'method="post"', response.body)
            delete.assert_not_called()
            self.assertEqual(self.fetch(path, method='POST', body='').code, 403)
            delete.assert_not_called()
            self.assertEqual(self.fetch(path, method='POST', body='',
                                        headers=self.xsrf_headers(response)).code, 200)
            delete.assert_called_once_with('test-log', '', 'owner')

    def test_download_rejects_traversal_before_opening_file(self):
        with patch.object(download, 'get_log_filename', return_value='/missing-test-file') as filename:
            for log_id in ['../outside', '/tmp/outside', 'test-log\n']:
                response = self.fetch('/download?' + urlencode({'log': log_id}))
                self.assertEqual(response.code, 400)
            filename.assert_not_called()

    def test_browse_search_cannot_close_script_element(self):
        payload = '</script><script>alert(1)</script>'
        response = self.fetch('/browse?' + urlencode({'search': payload}))
        self.assertEqual(response.code, 200)
        self.assertNotIn(payload.encode(), response.body)
        self.assertIn(b'\\u003c/script\\u003e', response.body)

    def test_download_streams_complete_binary_payload(self):
        filename = Path(self.tmp.name) / 'flight.ulg'
        payload = bytes(range(256)) * 4096
        filename.write_bytes(payload)
        with patch.object(download, 'get_log_filename', return_value=str(filename)):
            response = self.fetch('/download?log=test-log')
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, payload)

    def test_upload_requires_xsrf_but_accepts_explicit_api_credentials(self):
        body = multipart([('description', None, b'desc'), ('email', None, b'owner@example.com')])
        headers = {'Content-Type': 'multipart/form-data; boundary=test-boundary'}
        self.assertEqual(self.fetch('/upload', method='POST', body=body, headers=headers).code, 403)
        headers['X-API-Key'] = 'valid'
        self.assertEqual(self.fetch('/upload', method='POST', body=body, headers=headers).code, 400)

    def test_rejected_upload_removes_permanent_file_and_never_retries_in_web_process(self):
        body = multipart([('description', None, b'desc'), ('email', None, b'owner@example.com'),
                          ('filearg', 'flight.ulg', upload.ULog.HEADER_BYTES + b'x' * 1024)])
        filename = str(Path(self.tmp.name) / 'new.ulg')
        with patch.object(upload.UploadHandler, '_generate_unique_log_filename',
                          return_value=('new', filename)), \
                patch.object(upload, 'get_log_filename', return_value=filename), \
                patch.object(upload, '_find_existing_log_by_hash', return_value=None), \
                patch.object(upload, 'parse_log_bounded', new=AsyncMock(side_effect=ParserCrashed)):
            response = self.fetch('/upload', method='POST', body=body, headers={
                'X-API-Key': 'valid', 'Content-Type': 'multipart/form-data; boundary=test-boundary'})
        self.assertEqual(response.code, 400)
        self.assertFalse(Path(filename).exists())

    def test_upload_rate_limit_rejects_before_allocating_multipart_storage(self):
        with patch.object(upload, 'UPLOAD_RATE_LIMIT_PER_MINUTE', 0), \
                patch.object(upload, 'MultiPartStreamer') as streamer:
            response = self.fetch('/upload', method='POST', body=b'body', headers={
                'X-API-Key': 'valid', 'Content-Type': 'multipart/form-data; boundary=test-boundary'})
        self.assertEqual(response.code, 429)
        streamer.assert_not_called()

    def test_successful_upload_keeps_committed_log(self):
        payload = upload.ULog.HEADER_BYTES + b'x' * 1024
        body = multipart([('description', None, b'desc'), ('email', None, b'owner@example.com'),
                          ('source', None, b'CI'), ('redirect', None, b'false'),
                          ('filearg', 'flight.ulg', payload)])
        filename = str(Path(self.tmp.name) / 'new.ulg')
        with patch.object(upload.UploadHandler, '_generate_unique_log_filename',
                          return_value=('new', filename)), \
                patch.object(upload, 'send_notification_email'), \
                patch.object(upload, 'send_admin_notification_email'):
            response = self.fetch('/upload', method='POST', body=body, headers={
                'X-API-Key': 'valid', 'Content-Type': 'multipart/form-data; boundary=test-boundary'})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)['url'], '/plot_app?log=new')
        self.assertEqual(Path(filename).read_bytes(), payload)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT Uploader FROM Logs WHERE Id=?',
                                         ('new',)).fetchone(), ('owner',))

    @gen_test
    async def test_aborted_stream_discards_temporary_files(self):
        parts = []
        original = MultiPartStreamer.create_part

        def track(streamer, headers):
            part = original(streamer, headers)
            parts.append(part)
            return part

        with patch.object(MultiPartStreamer, 'create_part', new=track):
            reader, writer = await asyncio.open_connection('127.0.0.1', self.get_http_port())
            writer.write(b'POST /upload HTTP/1.1\r\nHost: localhost\r\nX-API-Key: valid\r\n'
                         b'Content-Type: multipart/form-data; boundary=test-boundary\r\n'
                         b'Content-Length: 100000\r\n\r\n--test-boundary\r\n'
                         b'Content-Disposition: form-data; name="filearg"; filename="f.ulg"\r\n'
                         b'\r\n' + b'x' * 100)
            await writer.drain()
            for _ in range(100):
                if parts:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(parts)
            self.assertTrue(Path(parts[0].f_out.name).exists())
            writer.close()
            await writer.wait_closed()
            for _ in range(100):
                if not Path(parts[0].f_out.name).exists():
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(Path(parts[0].f_out.name).exists())


class BrowseStoredHTMLTests(unittest.TestCase):
    def test_admin_email_column_escapes_upload_content(self):
        payload = '<img src=x onerror=alert(1)>'
        row = ('test-log', datetime.now(), '', 0, '', '', 'test-log',
               1, 'Quad', '', 0, '', '', 0, 0, '', '', '', '', 0, payload, 1)
        with patch.object(browse, 'get_airframe_data', return_value=None):
            columns = browse._get_columns_from_tuple(row, 1, set(), None, None, is_admin=True)
        self.assertEqual(columns[11], '&lt;img src=x onerror=alert(1)&gt;')
