"""Security, durable queue, and recovery regression tests."""
import json
import os
import shutil
import sqlite3
from sqlite_utils import connect
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plot_app'))
import tornado.web
from runtime_config import cookie_secret, require_persistent_storage
from ops_backup import backup, restore
from tornado_handlers import analysis_jobs as jobs, ai_chat, ai_analysis


class LocalStore:
    def __init__(self, path):
        self.path = Path(path)
    def put(self, key, path):
        destination = self.path / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    def get(self, key, path):
        shutil.copyfile(self.path / key, path)
    def exists(self, key):
        return (self.path / key).exists()


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.source / 'logs.sqlite'
        with connect(self.db) as con:
            con.execute('CREATE TABLE Users (Username TEXT, Approved INTEGER, IsAdmin INTEGER)')
            con.execute("INSERT INTO Users VALUES ('alice', 1, 0)")
            con.execute("INSERT INTO Users VALUES ('bob', 1, 0)")
            con.execute('CREATE TABLE Logs (Id TEXT, Public INTEGER, Uploader TEXT)')
            con.execute("INSERT INTO Logs VALUES ('flight', 0, 'alice')")
        self.patch = mock.patch.object(jobs, 'get_db_filename', return_value=str(self.db))
        self.patch.start()
        jobs.setup_jobs()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_secret_required_and_local_secret_not_shared(self):
        with mock.patch.dict(os.environ, {'COOKIE_SECRET': ''}):
            with self.assertRaises(RuntimeError):
                cookie_secret()
            self.assertNotEqual(cookie_secret(local=True), cookie_secret(local=True))
        with mock.patch.dict(os.environ, {'COOKIE_SECRET': 'change_me_to_a_random_string'}):
            with self.assertRaises(RuntimeError):
                cookie_secret()

    def test_production_refuses_unmounted_storage(self):
        with mock.patch.dict(os.environ, {'FLIGHT_REVIEW_ENV': 'production', 'STORAGE_PATH': str(self.root)}):
            with self.assertRaises(RuntimeError):
                require_persistent_storage()

    def test_jobs_persist_deduplicate_and_enforce_account_quota(self):
        first = jobs.enqueue('alice', 'flight', 'full', {'model': 'test'})
        self.assertEqual(first, jobs.enqueue('alice', 'flight', 'full', {'model': 'test'}))
        jobs.enqueue('alice', 'flight', 'pid', {'model': 'test'})
        with self.assertRaises(tornado.web.HTTPError) as error:
            jobs.enqueue('alice', 'flight', 'chat', {'message': 'why'})
        self.assertEqual(error.exception.status_code, 429)
        jobs.setup_jobs()
        self.assertEqual(jobs.claim_job()['Id'], first)
        self.assertIsNone(jobs.claim_job())

    def test_stale_jobs_are_not_silently_retried(self):
        job_id = jobs.enqueue('alice', 'flight', 'full', {})
        jobs.claim_job()
        with jobs.connection() as con:
            con.execute('UPDATE AnalysisJobs SET Updated=? WHERE Id=?', (time.time() - 90, job_id))
        self.assertIsNone(jobs.claim_job())
        with jobs.connection() as con:
            self.assertEqual(con.execute('SELECT State FROM AnalysisJobs').fetchone()[0], 'failed')

    def test_private_chat_history_is_user_scoped(self):
        cache = self.root / 'cache'
        cache.mkdir()
        with mock.patch.object(ai_analysis, '_AI_CACHE_DIR', str(cache)):
            ai_analysis._save_cached_analysis('flight', {'messages': [{'role': 'user', 'content': 'private'}]},
                                              kind=ai_chat.chat_kind('alice'))
            self.assertEqual(ai_chat._chat_history_payload('flight', 'bob')['messages'], [])
            self.assertEqual(len(ai_chat._chat_history_payload('flight', 'alice')['messages']), 1)

    def test_backup_restore_database_and_log_and_refuse_overwrite(self):
        logs = self.source / 'log_files'
        logs.mkdir()
        (logs / 'flight.ulg').write_bytes(b'flight log payload')
        store = LocalStore(self.root / 'bucket')
        snapshot = backup(self.source, store)
        destination = self.root / 'restore'
        self.assertEqual(restore(snapshot, destination, store), 2)
        self.assertEqual((destination / 'log_files/flight.ulg').read_bytes(), b'flight log payload')
        with connect(destination / 'logs.sqlite') as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM Users').fetchone()[0], 2)
        with self.assertRaises(ValueError):
            restore(snapshot, destination, store)

    def test_restore_rejects_manifest_path_traversal(self):
        store = LocalStore(self.root / 'bucket')
        snapshot = backup(self.source, store)
        path = store.path / snapshot
        manifest = json.loads(path.read_text())
        manifest['files'][0]['path'] = '../escape.sqlite'
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            restore(snapshot, self.root / 'restore', store)
