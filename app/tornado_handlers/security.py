# pylint: disable=line-too-long
"""
Security helpers shared by tornado handlers:
  * Bounded log parsing in fresh subprocesses (no fork)
  * In-process per-IP rate limiting
  * Default HTTP security headers

This is a defense-in-depth layer. Anything externally exposed (reverse
proxy rate limits, WAF, mandatory sandboxing) should still be configured
separately at the infrastructure level.
"""
#pylint: disable=global-statement,invalid-name,import-outside-toplevel,missing-function-docstring

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import defaultdict, deque
import pickle
import tempfile
import weakref
from typing import Deque, Dict, Optional, Tuple

# Make plot_app importable for the worker process too.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))


# ---------------------------------------------------------------------------
# Bounded parsing
# ---------------------------------------------------------------------------

# Wall-clock timeout for a single upload parse. Header-only / metadata parses
# are typically well under a second; this is a safety net for huge files.
PARSER_WALL_TIMEOUT_SECONDS = int(os.environ.get(
    'FLIGHT_REVIEW_PARSER_WALL_TIMEOUT_SECONDS', '240'))
# Fresh interpreters avoid copying Bokeh's in-memory log cache.
PARSER_MAX_CONCURRENCY = int(os.environ.get('FLIGHT_REVIEW_PARSER_MAX_CONCURRENCY', '1'))
_parser_semaphores = weakref.WeakKeyDictionary()


def _get_parser_semaphore():
    loop = asyncio.get_running_loop()
    if loop not in _parser_semaphores:
        _parser_semaphores[loop] = asyncio.Semaphore(max(1, PARSER_MAX_CONCURRENCY))
    return _parser_semaphores[loop]


class ParserTimeout(Exception):
    """The parser exceeded its wall-clock budget and was killed."""


class ParserCrashed(Exception):
    """The isolated parser exited without a valid result."""


async def parse_log_bounded(file_name: str):
    """Kill and reap timed-out work before releasing the concurrency slot."""
    async with _get_parser_semaphore():
        with tempfile.TemporaryDirectory(prefix='flight-parse-') as tmp:
            output = os.path.join(tmp, 'result.pickle')
            worker = os.path.abspath(os.path.join(os.path.dirname(__file__), '../isolated_worker.py'))
            env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
            proc = await asyncio.create_subprocess_exec(
                sys.executable, worker, os.path.abspath(file_name), output,
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), PARSER_WALL_TIMEOUT_SECONDS)
                if proc.returncode != 0 or not os.path.isfile(output):
                    raise ParserCrashed('Parser failed; check file format and resource limits')
                # This file is created by our worker in a private, unpredictable directory.
                with open(output, 'rb') as stream:
                    return pickle.load(stream)
            except asyncio.TimeoutError as exc:
                raise ParserTimeout('Parsing exceeded its time budget') from exc
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple per-key sliding-window limiter.

    Keys are (bucket, identifier) so different endpoints can share an instance.
    Single-process only â€” for production, also configure an edge limit (nginx
    limit_req_zone). With multiple bokeh worker processes the per-process limit
    multiplies by num_procs.
    """

    def __init__(self):
        # (bucket, key) -> deque of timestamps within the largest window we use
        self._events: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, window_seconds: float) -> bool:
        """Return True if the request is allowed, False if it should be blocked.

        On True the call is recorded against the bucket.
        """
        now = time.monotonic()
        events = self._events[(bucket, key)]
        cutoff = now - window_seconds
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True

    def prune(self, max_age_seconds: float = 3600.0):
        """Drop entries with no recent events to avoid unbounded growth."""
        now = time.monotonic()
        cutoff = now - max_age_seconds
        dead = [k for k, ev in self._events.items() if not ev or ev[-1] < cutoff]
        for k in dead:
            del self._events[k]


_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def client_ip(handler) -> str:
    """Best-effort client IP, honouring X-Forwarded-For when we run behind nginx."""
    return handler.request.remote_ip or 'unknown'


# ---------------------------------------------------------------------------
# Default HTTP security headers
# ---------------------------------------------------------------------------

def apply_default_security_headers(handler) -> None:
    """Apply low-risk security headers compatible with Bokeh's inline scripts.

    A strict CSP is intentionally NOT set here because Bokeh emits inline
    <script> blocks and connects via websockets; configure CSP at the reverse
    proxy if you want stricter rules.
    """
    handler.set_header('X-Content-Type-Options', 'nosniff')
    handler.set_header('X-Frame-Options', 'SAMEORIGIN')
    handler.set_header('Referrer-Policy', 'same-origin')
    handler.set_header('Permissions-Policy',
                       'geolocation=(), microphone=(), camera=()')
