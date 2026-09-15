"""Bind Bokeh documents and websocket tokens to the authenticated browser."""
import hashlib
import hmac

from bokeh.server.auth_provider import AuthProvider
from bokeh.util.token import check_token_signature, get_token_payload
from tornado.web import HTTPError
from tornado.websocket import WebSocketHandler

from tornado_handlers.security import authenticated_username


def session_options(secret):
    """Sign Bokeh credentials with a separate key derived from the cookie key."""
    return {
        'sign_sessions': True,
        'secret_key': hmac.digest(secret.encode(), b'flight-review-bokeh-sessions-v1',
                                  hashlib.sha256),
        'include_headers': [],
        'include_cookies': ['user'],
    }


async def _authenticated_user(handler):
    """Authenticate before websocket upgrade and reject shared document IDs."""
    username = authenticated_username(handler)
    if username is None:
        if isinstance(handler, WebSocketHandler):
            raise HTTPError(403, reason='Sign in before opening a plot session')
        return None

    # Flight Review creates a fresh document for every page request. Accepting
    # a caller-chosen ID lets a link fix another user's document to a known ID.
    if (any(name in handler.request.arguments
            for name in ('bokeh-session-id', 'bokeh-token'))
            or 'Bokeh-Session-Id' in handler.request.headers):
        raise HTTPError(403, reason='Explicit plot sessions are not supported')

    if isinstance(handler, WebSocketHandler):
        protocols = handler.request.headers.get('Sec-WebSocket-Protocol', '').split(',')
        if len(protocols) != 2 or protocols[0].strip() != 'bokeh':
            raise HTTPError(403, reason='Invalid plot session')
        token = protocols[1].strip()
        try:
            valid = check_token_signature(token, secret_key=handler.application.secret_key,
                                          signed=True)
            payload = get_token_payload(token) if valid else {}
            token_cookie = payload.get('cookies', {}).get('user')
        except (ValueError, TypeError, KeyError):
            token_cookie = None
        cookie = handler.get_cookie('user')
        if (not isinstance(token_cookie, str) or not cookie
                or not hmac.compare_digest(token_cookie.encode(), cookie.encode())):
            raise HTTPError(403, reason='Plot session belongs to another browser session')
    else:
        # Bokeh's HTML GET must set the token cookie for the page's AJAX forms.
        _ = handler.xsrf_token
    return username


class FlightReviewAuthProvider(AuthProvider):
    """Apply current-account authentication to Bokeh HTTP and websocket requests."""

    @property
    def get_user_async(self):
        return _authenticated_user

    @property
    def login_url(self):
        return '/login'
