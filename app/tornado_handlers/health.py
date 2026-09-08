# pylint: disable=abstract-method
"""Small probes; readiness never creates a missing database."""
import os
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tornado.web import RequestHandler
from config import get_db_filename, get_log_filepath
from sqlite_utils import connect


class HealthHandler(RequestHandler):
    """Check that the HTTP event loop is responding."""

    def get(self):
        """Return liveness and the immutable build revision."""
        self.set_header('Cache-Control', 'no-store')
        self.write({'status': 'ok', 'revision': os.environ.get('APP_REVISION', 'development')})


class ReadyHandler(HealthHandler):
    """Check that the migrated database and log storage are available."""

    def get(self):
        """Read schema without waiting for SQLite's normal long busy timeout."""
        try:
            with connect(Path(get_db_filename()).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=0.25) as con:
                con.execute('SELECT Username FROM Users LIMIT 0')
                con.execute('SELECT Id FROM Logs LIMIT 0')
            if not os.access(get_log_filepath(), os.W_OK):
                raise OSError('Storage is not writable')
        except (OSError, sqlite3.Error):
            self.set_status(503)
            self.write({'status': 'unavailable'})
            return
        super().get()


class OperationsHandler(HealthHandler):
    """Alert on backup age and storage headroom without restarting healthy pods."""

    def get(self):
        """Expose only pass/fail flags; no filenames, logs or account information."""
        self.set_header('Cache-Control', 'no-store')
        path = Path(get_db_filename()).parent
        usage = shutil.disk_usage(path)
        storage_ok = usage.free / usage.total > 0.10
        backup_ok = not os.environ.get('BACKUP_BUCKET')
        if not backup_ok:
            try:
                status = json.loads((path / 'backup_status.json').read_text(encoding='utf-8'))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(status['created'])
                backup_ok = 0 <= age.total_seconds() < 48 * 3600
            except (OSError, ValueError, KeyError):
                pass
        if not storage_ok or not backup_ok:
            self.set_status(503)
        self.write({'storage': 'ok' if storage_ok else 'low',
                    'backup': 'ok' if backup_ok else 'overdue'})
