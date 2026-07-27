"""Per-account API keys for machine clients (e.g. logloader).

Keys are high-entropy secrets. Only a SHA-256 hash is stored; the full key is
shown once at generation time. Uploads can authenticate with:

  * Header:  Authorization: Bearer <key>
  * Header:  X-API-Key: <key>
  * Query:   /upload?api_key=<key>
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import sys
import time
import traceback

import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             '../plot_app'))
from config import get_db_filename

#pylint: disable=relative-beyond-top-level
from .common import TornadoRequestHandlerBase


_log = logging.getLogger('flight_review.api_key')

# Visible prefix length (after the "fr_" product prefix).
_KEY_PREFIX_LEN = 8
_KEY_RANDOM_BYTES = 32


def generate_api_key() -> str:
    """Return a new high-entropy API key string."""
    return 'fr_' + secrets.token_urlsafe(_KEY_RANDOM_BYTES)


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest of the raw key (for storage / lookup)."""
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


def api_key_prefix(raw_key: str) -> str:
    """Short display prefix derived from the raw key."""
    if not raw_key:
        return ''
    # Keep the product prefix so the UI still looks like a key.
    if raw_key.startswith('fr_') and len(raw_key) > 3 + _KEY_PREFIX_LEN:
        return raw_key[:3 + _KEY_PREFIX_LEN]
    return raw_key[:_KEY_PREFIX_LEN]


def extract_api_key_from_request(handler) -> str:
    """Pull an API key from headers or query string (available before body).

    Intentionally does not read form fields: upload uses a streamed body, so
    body args are not available in prepare() where auth must run.
    """
    auth = handler.request.headers.get('Authorization', '') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()

    header_key = handler.request.headers.get('X-API-Key', '') or ''
    if header_key.strip():
        return header_key.strip()

    # Query string: POST /upload?api_key=... (logloader-friendly).
    args = handler.request.query_arguments.get('api_key')
    if args:
        try:
            return args[0].decode('utf-8').strip()
        except (UnicodeDecodeError, AttributeError):
            return str(args[0]).strip()

    return ''

def lookup_user_by_api_key(raw_key: str):
    """Return (username, approved: bool) for a valid key, or None."""
    if not raw_key or len(raw_key) < 16:
        return None
    key_hash = hash_api_key(raw_key)
    con = sqlite3.connect(get_db_filename())
    try:
        cur = con.cursor()
        try:
            cur.execute(
                "SELECT Username, Approved FROM Users WHERE ApiKeyHash=?",
                (key_hash,))
        except sqlite3.OperationalError:
            # Column not migrated yet.
            return None
        row = cur.fetchone()
        if not row:
            return None
        return row[0], bool(row[1])
    finally:
        con.close()


def authenticate_request_api_key(handler):
    """If the request carries a valid API key for an approved user, return
    the username; otherwise return None. Does not reject invalid keys itself.
    """
    raw_key = extract_api_key_from_request(handler)
    if not raw_key:
        return None
    result = lookup_user_by_api_key(raw_key)
    if result is None:
        _log.warning('invalid_api_key ip=%s',
                     handler.request.remote_ip or 'unknown')
        return None
    username, approved = result
    if not approved:
        _log.warning('api_key_unapproved user=%s', username)
        return None
    return username


def _get_user_api_key_meta(username: str):
    """Return (prefix, created_ts) or ('', 0) if no key."""
    con = sqlite3.connect(get_db_filename())
    try:
        cur = con.cursor()
        try:
            cur.execute(
                "SELECT ApiKeyPrefix, ApiKeyCreated FROM Users WHERE Username=?",
                (username,))
        except sqlite3.OperationalError:
            return '', 0.0
        row = cur.fetchone()
        if not row or not row[0]:
            return '', 0.0
        return row[0] or '', float(row[1] or 0)
    finally:
        con.close()


def _store_api_key(username: str, raw_key: str) -> None:
    con = sqlite3.connect(get_db_filename())
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE Users SET ApiKeyHash=?, ApiKeyPrefix=?, ApiKeyCreated=? "
            "WHERE Username=?",
            (hash_api_key(raw_key), api_key_prefix(raw_key), time.time(),
             username))
        con.commit()
    finally:
        con.close()


def _revoke_api_key(username: str) -> None:
    con = sqlite3.connect(get_db_filename())
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE Users SET ApiKeyHash='', ApiKeyPrefix='', ApiKeyCreated=0 "
            "WHERE Username=?",
            (username,))
        con.commit()
    finally:
        con.close()


def _user_is_approved(username: str) -> bool:
    con = sqlite3.connect(get_db_filename())
    try:
        cur = con.cursor()
        cur.execute("SELECT Approved FROM Users WHERE Username=?", (username,))
        row = cur.fetchone()
        return bool(row and row[0])
    finally:
        con.close()


class AccountHandler(TornadoRequestHandlerBase):
    """Account page: view / generate / revoke the per-user upload API key."""

    @tornado.web.authenticated
    def get(self):
        """Render the account / API key management page."""
        self._render_page()

    @tornado.web.authenticated
    def post(self):
        """Generate or revoke the current user's API key."""
        username = self.current_user
        action = self.get_argument('action', default='')
        new_key = None
        error = None
        message = None

        if not _user_is_approved(username):
            error = ("Your account is not approved yet. "
                     "API keys are only available for approved accounts.")
            self._render_page(error=error)
            return

        try:
            if action == 'generate':
                # Replace any existing key (acts as rotation).
                new_key = generate_api_key()
                _store_api_key(username, new_key)
                message = (
                    "API key generated. Copy it now — it will not be shown again.")
                _log.info('api_key_generated user=%s', username)
            elif action == 'revoke':
                _revoke_api_key(username)
                message = "API key revoked. Existing clients will no longer work."
                _log.info('api_key_revoked user=%s', username)
            else:
                error = "Unknown action."
        except Exception as exc:
            traceback.print_exc()
            error = f"Failed to update API key: {exc}"

        self._render_page(error=error, message=message, new_key=new_key)

    def _render_page(self, error=None, message=None, new_key=None):
        username = self.current_user
        prefix, created = _get_user_api_key_meta(username)
        created_str = ''
        if created:
            try:
                created_str = time.strftime(
                    '%Y-%m-%d %H:%M UTC', time.gmtime(created))
            except (OverflowError, ValueError, OSError):
                created_str = ''

        domain = ''
        try:
            from config import get_domain_name, get_http_protocol
            domain = get_domain_name() or ''
            protocol = get_http_protocol() or 'https'
            base_url = f'{protocol}://{domain}' if domain else ''
        except Exception:
            base_url = ''

        self.render_jinja(
            'account.html',
            error=error,
            message=message,
            new_key=new_key,
            has_api_key=bool(prefix),
            api_key_prefix=prefix,
            api_key_created=created_str,
            base_url=base_url,
            account_approved=_user_is_approved(username),
            is_plot_page=False)
