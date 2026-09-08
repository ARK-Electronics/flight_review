"""SQLite connections whose context managers commit/rollback AND close."""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    """The standard context manager leaves file descriptors open."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(*args, **kwargs):
    """Create a connection with deterministic transaction and file lifetime."""
    return sqlite3.connect(*args, factory=ClosingConnection, **kwargs)
