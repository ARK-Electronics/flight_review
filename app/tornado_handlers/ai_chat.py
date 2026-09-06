"""Tornado handler for asking follow-up questions about a flight log."""
from __future__ import print_function
import json
import os
import traceback

import tornado.web
import tornado.gen

# pylint: disable=relative-beyond-top-level,invalid-name,line-too-long
from .common import TornadoRequestHandlerBase
from .ai_analysis import (
    _begin_analysis_request, _build_analysis_prompt, _call_grok,
    _checked_log_id, _extracted_flight_data, _get_cache_path,
    _json_error, _load_cached_analysis, _request_json,
    _save_cached_analysis,
)

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


def _chat_history_payload(log_id):
    """Return the stored chat history dict (messages always a list)."""
    cached = _load_cached_analysis(log_id, kind='chat')
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
        payload = _chat_history_payload(log_id)
        payload['cached'] = bool(payload['messages'])
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(payload))

    @tornado.web.authenticated
    def delete(self, *args, **kwargs):
        """DELETE request - clear cached chat history."""
        log_id = _checked_log_id(self)
        if not log_id:
            return
        cache_path = _get_cache_path(log_id, kind='chat')
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
        except OSError:
            pass
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps({'cleared': True, 'messages': []}))

    @tornado.web.authenticated
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        """POST request - ask a question about the log."""
        log_id, api_key, model, effort = _begin_analysis_request(self)
        if not log_id:
            return

        parsed = _request_json(self)
        user_message = parsed.get('message')
        if not isinstance(user_message, str) or not user_message.strip():
            _json_error(self, 400, 'message is required')
            return
        user_message = user_message.strip()
        if len(user_message) > _MAX_CHAT_MESSAGE_CHARS:
            user_message = user_message[:_MAX_CHAT_MESSAGE_CHARS]

        history = _sanitize_chat_messages(
            parsed.get('history') or _chat_history_payload(log_id).get('messages'))
        # Drop a trailing duplicate of this user turn if the client already appended it.
        if history and history[-1]['role'] == 'user' and history[-1]['content'] == user_message:
            history = history[:-1]
        history.append({'role': 'user', 'content': user_message})
        if len(history) > _MAX_CHAT_MESSAGES:
            history = history[-_MAX_CHAT_MESSAGES:]

        try:
            data = _extracted_flight_data(log_id)
            log_context = _build_analysis_prompt(
                data['flight_summary'], data['pid_data'], data['ekf_data'],
                data['vehicle_status'], data['parameters'],
                data['logged_messages'], data['motor_failure'],
                for_chat=True
            )
            extra_messages = [
                {'role': 'user', 'content': log_context},
                {'role': 'assistant', 'content': _CHAT_CONTEXT_ACK},
            ]
            extra_messages.extend(history)

            ok, payload, status = yield _call_grok(
                api_key, model, CHAT_SYSTEM_PROMPT, effort=effort,
                extra_messages=extra_messages)
            if not ok:
                _json_error(self, status, payload)
                return

            reply = payload.get('analysis') or ''
            history.append({'role': 'assistant', 'content': reply})
            stored = {
                'messages': history,
                'model': model,
                'effort': effort,
            }
            _save_cached_analysis(log_id, stored, kind='chat')
            self.set_header('Content-Type', 'application/json')
            self.write(json.dumps({
                'reply': reply,
                'reasoning': payload.get('reasoning'),
                'model': model,
                'effort': effort,
                'messages': history,
            }))
        except Exception as e:  # pylint: disable=broad-except
            traceback.print_exc()
            _json_error(self, 500, 'Chat failed: {}'.format(str(e)))
