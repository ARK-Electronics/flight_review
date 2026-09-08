"""Support imports must never be accessible through a shared log URL alone."""
from contextlib import closing
from config import get_db_connection


def require_support_access(log_id, username):
    """Keep existing sharing behavior for other sources; restrict support imports."""
    with closing(get_db_connection()) as con:
        log = con.execute('SELECT Source, Uploader FROM Logs WHERE Id=?', (log_id,)).fetchone()
        if not log or log[0] != 'support-api':
            return
        user = con.execute('SELECT Approved, IsAdmin FROM Users WHERE Username=?',
                           (username,)).fetchone() if username else None
    if not user or not user[0] or not (user[1] or log[1] == username):
        raise PermissionError('Log not found')
