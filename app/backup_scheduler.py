# pylint: disable=invalid-name
"""Run daily off-instance backups without blocking the web event loop."""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tornado.ioloop import IOLoop, PeriodicCallback

_timer = None


async def backup_if_due():
    """A failed backup is retried next hour and recorded in runtime logs."""
    if not os.environ.get('BACKUP_BUCKET'):
        return
    status = Path(os.environ['STORAGE_PATH']) / 'backup_status.json'
    if status.exists():
        data = json.loads(status.read_text(encoding='utf-8'))
        last = datetime.fromisoformat(data['created'])
        if (datetime.now(timezone.utc) - last).total_seconds() < 86400:
            return
    worker = Path(__file__).with_name('ops_backup.py')
    proc = await asyncio.create_subprocess_exec(sys.executable, str(worker), 'backup')
    try:
        await asyncio.wait_for(proc.wait(), 7200)
        if proc.returncode:
            logging.error('BACKUP_FAILED exit=%s', proc.returncode)
    except asyncio.TimeoutError:
        logging.error('BACKUP_FAILED timeout')
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def start_backups():
    """Single-instance persistent-storage deployment owns the timer."""
    global _timer  # pylint: disable=global-statement
    _timer = PeriodicCallback(backup_if_due, 3600 * 1000)
    _timer.start()
    IOLoop.current().spawn_callback(backup_if_due)
