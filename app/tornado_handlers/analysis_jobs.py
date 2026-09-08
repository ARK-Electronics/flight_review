# pylint: disable=relative-beyond-top-level,invalid-name,line-too-long
"""Durable AI queue on the single persistent SQLite volume.

SQLite transactions enforce account quotas and one expensive job globally. A
fresh child owns parsing/model calls; requests only submit or poll persisted state.
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid

import tornado.web
from tornado.ioloop import PeriodicCallback
from config import get_db_filename
from sqlite_utils import connect
from .common import TornadoRequestHandlerBase

JOB_TIMEOUT = 3900
_runner = None


def connection():
    """Keep lock waits short in HTTP handlers."""
    con = connect(get_db_filename(), timeout=0.25)
    con.row_factory = sqlite3.Row
    return con


def setup_jobs():
    """Add queue tables without changing existing log/user data."""
    with connection() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS AnalysisJobs (
            Id TEXT PRIMARY KEY, Username TEXT NOT NULL, LogId TEXT NOT NULL,
            Kind TEXT NOT NULL, Request TEXT NOT NULL, Fingerprint TEXT NOT NULL,
            State TEXT NOT NULL, Created REAL NOT NULL, Updated REAL NOT NULL,
            Result TEXT, Error TEXT)''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_jobs_user_created '
                    'ON AnalysisJobs(Username, Created)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_jobs_state ON AnalysisJobs(State, Created)')


def enqueue(username, log_id, kind, payload):
    """Deduplicate active work and enforce per-account/global queue budgets."""
    now = time.time()
    request = json.dumps(payload, sort_keys=True)
    fingerprint = hashlib.sha256((kind + log_id + request).encode()).hexdigest()
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        active = con.execute("SELECT Id FROM AnalysisJobs WHERE Username=? AND Fingerprint=? "
                             "AND State IN ('pending','running')", (username, fingerprint)).fetchone()
        if active:
            return active['Id']
        count = con.execute('SELECT COUNT(*) FROM AnalysisJobs WHERE Username=? AND Created>?',
                            (username, now - 3600)).fetchone()[0]
        queued = con.execute("SELECT COUNT(*) FROM AnalysisJobs WHERE State IN ('pending','running')"
                             ).fetchone()[0]
        own = con.execute("SELECT COUNT(*) FROM AnalysisJobs WHERE Username=? "
                          "AND State IN ('pending','running')", (username,)).fetchone()[0]
        if count >= int(os.environ.get('AI_JOBS_PER_HOUR', '12')) or own >= 2 or queued >= 16:
            raise tornado.web.HTTPError(429, reason='Analysis quota reached; try again later')
        job_id = uuid.uuid4().hex
        con.execute('INSERT INTO AnalysisJobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (job_id, username, log_id, kind, request, fingerprint,
                     'pending', now, now, None, None))
        return job_id


def submit_analysis(handler, kind):
    """Validate permissions/options before accepting work; no API key in job payloads."""
    from .ai_analysis import _begin_analysis_request, _request_json  # pylint: disable=import-outside-toplevel
    log_id, api_key, model, effort = _begin_analysis_request(handler)
    if not log_id or not api_key:
        return
    payload = {'model': model, 'effort': effort}
    if kind == 'chat':
        message = _request_json(handler).get('message')
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise tornado.web.HTTPError(400, reason='message must contain 1-8000 characters')
        payload['message'] = message.strip()
    try:
        job_id = enqueue(handler.current_user, log_id, kind, payload)
    except sqlite3.OperationalError as exc:
        raise tornado.web.HTTPError(503, reason='Analysis queue is busy') from exc
    handler.set_status(202)
    handler.set_header('Cache-Control', 'no-store')
    handler.write({'job_id': job_id, 'state': 'pending'})


class AnalysisJobHandler(TornadoRequestHandlerBase):
    """Only the submitting, still-approved user may read or cancel a job."""

    def _job(self, job_id):
        with connection() as con:
            job = con.execute('SELECT j.* FROM AnalysisJobs j JOIN Users u ON u.Username=j.Username '
                              'WHERE j.Id=? AND j.Username=? AND u.Approved=1',
                              (job_id, self.current_user)).fetchone()
        if job is None:
            raise tornado.web.HTTPError(404)
        return job

    @tornado.web.authenticated
    def get(self, job_id):
        """Return durable status/results after refresh or reconnection."""
        job = self._job(job_id)
        self.set_header('Cache-Control', 'no-store')
        self.write({'job_id': job_id, 'state': job['State'],
                    'result': json.loads(job['Result']) if job['Result'] else None,
                    'error': job['Error']})

    @tornado.web.authenticated
    def delete(self, job_id):
        """Signal cancellation; the runner kills and reaps the subprocess."""
        self._job(job_id)
        with connection() as con:
            con.execute("UPDATE AnalysisJobs SET State='cancelled', Updated=? "
                        "WHERE Id=? AND State IN ('pending','running')", (time.time(), job_id))
        self.write({'state': 'cancelled'})


def claim_job():
    """Serialize workers; stale jobs fail explicitly rather than repeat paid calls."""
    now = time.time()
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE AnalysisJobs SET State='failed', Error='Worker interrupted; submit again', "
                    "Updated=? WHERE State='running' AND Updated<?", (now, now - 60))
        con.execute("DELETE FROM AnalysisJobs WHERE State NOT IN ('pending','running') AND Updated<?",
                    (now - 7 * 86400,))
        if con.execute("SELECT 1 FROM AnalysisJobs WHERE State='running'").fetchone():
            return None
        job = con.execute("SELECT * FROM AnalysisJobs WHERE State='pending' ORDER BY Created LIMIT 1"
                          ).fetchone()
        if job:
            con.execute("UPDATE AnalysisJobs SET State='running', Updated=? WHERE Id=?", (now, job['Id']))
        return job


async def run_next_job():
    """Run one memory-limited child, with heartbeat, cancellation, and wall timeout."""
    try:
        job = claim_job()
    except sqlite3.OperationalError:
        return
    if not job:
        return
    proc = None
    try:
        env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        worker = os.path.abspath(os.path.join(os.path.dirname(__file__), '../analysis_worker.py'))
        proc = await asyncio.create_subprocess_exec(sys.executable, worker, job['Id'], env=env)
        deadline = time.monotonic() + JOB_TIMEOUT
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                pass
            with connection() as con:
                state = con.execute('SELECT State FROM AnalysisJobs WHERE Id=?', (job['Id'],)).fetchone()
                if not state or state[0] != 'running':
                    break
                con.execute('UPDATE AnalysisJobs SET Updated=? WHERE Id=?', (time.time(), job['Id']))
            if time.monotonic() > deadline:
                break
    finally:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        with connection() as con:
            con.execute("UPDATE AnalysisJobs SET State='failed', Updated=?, "
                        "Error='Analysis stopped or exceeded resource limits' WHERE Id=? AND State='running'",
                        (time.time(), job['Id']))


def start_runner():
    """PeriodicCallback waits for this coroutine, preventing overlapping local runners."""
    global _runner  # pylint: disable=global-statement
    setup_jobs()
    _runner = PeriodicCallback(run_next_job, 2000)
    _runner.start()
