# pylint: disable=line-too-long
"""Production startup invariants and session configuration."""
import os
import secrets


def cookie_secret(local=False):
    """Refuse predictable signing keys; local file viewing gets an ephemeral key."""
    value = os.environ.get('COOKIE_SECRET', '')
    if not value and local:
        return secrets.token_urlsafe(48)
    if len(value) < 32 or value == 'change_me_to_a_random_string':
        raise RuntimeError('Set COOKIE_SECRET to a cryptographically random secret (32+ characters)')
    return value


def require_persistent_storage():
    """Fail closed for production if the operator has not mounted durable storage."""
    if os.environ.get('FLIGHT_REVIEW_ENV') != 'production':
        return
    path = os.environ.get('STORAGE_PATH', '')
    if not path or not os.path.ismount(path):
        raise RuntimeError('Production STORAGE_PATH must be a persistent volume mount')
