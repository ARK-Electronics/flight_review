"""Tornado handler for asking follow-up questions about a flight log."""
from __future__ import print_function
import json
import os
import hashlib

import tornado.web

# pylint: disable=relative-beyond-top-level,invalid-name,line-too-long
from .common import TornadoRequestHandlerBase
from .ai_analysis import (
    _checked_log_id, _get_cache_path, _load_cached_analysis,
)


def chat_kind(username):
    """Use a filesystem-safe per-user namespace; never fall back to shared history."""
    return 'chat_' + hashlib.sha256(username.encode('utf-8')).hexdigest()


_MAX_CHAT_MESSAGES = 24
_MAX_CHAT_MESSAGE_CHARS = 8000

CHAT_SYSTEM_PROMPT = """You are an expert PX4 flight data analyst. The user is asking
questions about one specific flight log. Extracted ULog data is provided as context.

Rules:
- Answer from the provided log data and PX4 domain knowledge only.
- Be specific: cite parameters, values, timestamps, and evidence from the context.
- If the log data is insufficient, say so explicitly. Do not invent readings.
- Keep the answer focused on the question. Use markdown.
- Do not produce a full analysis report unless the user asks for one.
"""

_CHAT_CONTEXT_ACK = (
    'I have the extracted flight log. Ask questions about this flight; '
    'I will answer from the log data.'
)


def _sanitize_chat_messages(raw):
    """Return a safe, bounded list of {role, content} chat turns."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content')
        if role not in ('user', 'assistant'):
            continue
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        if len(content) > _MAX_CHAT_MESSAGE_CHARS:
            content = content[:_MAX_CHAT_MESSAGE_CHARS]
        out.append({'role': role, 'content': content})
        if len(out) >= _MAX_CHAT_MESSAGES:
            break
    return out


def _chat_history_payload(log_id, username):
    """Return the stored chat history dict (messages always a list)."""
    cached = _load_cached_analysis(log_id, kind=chat_kind(username))
    if not cached or not isinstance(cached, dict):
        return {'messages': []}
    messages = _sanitize_chat_messages(cached.get('messages'))
    return {
        'messages': messages,
        'model': cached.get('model'),
        'effort': cached.get('effort'),
    }


class AIAnalysisChatHandler(TornadoRequestHandlerBase):
    """Multi-turn Q&A about a single flight log."""

    @tornado.web.authenticated
    def get(self, *args, **kwargs):
        """GET request - return cached chat history if available."""
        log_id = _checked_log_id(self)
        if not log_id:
            return
        payload = _chat_history_payload(log_id, self.current_user)
        payload['cached'] = bool(payload['messages'])
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(payload))

    @tornado.web.authenticated
    def delete(self, *args, **kwargs):
        """DELETE request - clear cached chat history."""
        log_id = _checked_log_id(self)
        if not log_id:
            return
        cache_path = _get_cache_path(log_id, kind=chat_kind(self.current_user))
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
        except OSError:
            pass
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps({'cleared': True, 'messages': []}))

    @tornado.web.authenticated
    def post(self, *args, **kwargs):
        """Submit a chat job scoped to this user and flight log."""
        from .analysis_jobs import submit_analysis  # pylint: disable=import-outside-toplevel
        submit_analysis(self, 'chat')
