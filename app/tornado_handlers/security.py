"""
Security helpers shared by tornado handlers:
  * Bounded log parsing in a thread pool (no fork)
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
from concurrent.futures import BrokenExecutor, ThreadPoolExecutor
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
# Maximum concurrent upload parses. These run in threads in the main process
# (not forked workers) so they share the container's RAM with Bokeh.
PARSER_MAX_CONCURRENCY = int(os.environ.get(
    'FLIGHT_REVIEW_PARSER_MAX_CONCURRENCY', '1'))


def _worker_load_log(file_name: str):
    """Parse a just-uploaded log for metadata (no FIFO topics)."""
    from helper import load_log_file_for_upload  # type: ignore
    return load_log_file_for_upload(file_name)


_parser_pool: Optional[ThreadPoolExecutor] = None
_parser_semaphore: Optional[asyncio.Semaphore] = None


def _get_parser_pool() -> ThreadPoolExecutor:
    global _parser_pool
    if _parser_pool is None:
        # Threads, not processes. ProcessPoolExecutor forks this Bokeh process
        # (including any cached ULog objects), doubles RSS, and OOM-kills the
        # container. That showed up as SIGTERM + BrokenProcessPool on recv()
        # and the upload never reached the header-only fallback.
        _parser_pool = ThreadPoolExecutor(
            max_workers=max(1, PARSER_MAX_CONCURRENCY),
            thread_name_prefix='ulog-parse')
    return _parser_pool


def _get_parser_semaphore() -> asyncio.Semaphore:
    global _parser_semaphore
    if _parser_semaphore is None:
        _parser_semaphore = asyncio.Semaphore(max(1, PARSER_MAX_CONCURRENCY))
    return _parser_semaphore


class ParserTimeout(Exception):
    """Raised when log parsing exceeded the allowed wall-clock budget."""


class ParserCrashed(Exception):
    """Raised when the parser worker process died (OOM kill, segfault, ...)."""


async def parse_log_bounded(file_name: str):
    """Parse `file_name` in a thread with concurrency + time bounds.

    A process pool is intentionally not used: forking the Bokeh server to parse
    a log doubles memory and is what OOM-killed uploads (BrokenProcessPool).
    Memory is bounded by SafeULog topic caps instead.
    """
    loop = asyncio.get_event_loop()
    sem = _get_parser_semaphore()
    async with sem:
        try:
            pool = _get_parser_pool()
            future = loop.run_in_executor(pool, _worker_load_log, file_name)
            return await asyncio.wait_for(future, timeout=PARSER_WALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise ParserTimeout(
                f'Parsing exceeded {PARSER_WALL_TIMEOUT_SECONDS}s budget') from exc
        except (BrokenPipeError, EOFError, BrokenExecutor) as exc:
            _shutdown_parser_pool()
            raise ParserCrashed('Parser worker terminated unexpectedly') from exc
        except Exception:
            # Real parse errors (ULogException etc.) propagate as-is.
            raise


def _shutdown_parser_pool():
    global _parser_pool
    pool = _parser_pool
    _parser_pool = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple per-key sliding-window limiter.

    Keys are (bucket, identifier) so different endpoints can share an instance.
    Single-process only — for production, also configure an edge limit (nginx
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
    xff = handler.request.headers.get('X-Forwarded-For')
    if xff:
        # left-most entry is the original client
        return xff.split(',')[0].strip()
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
