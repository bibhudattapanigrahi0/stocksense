"""
Authentication primitives — password hashing and session tokens.

Deliberately **stdlib-only** (``hashlib.scrypt`` + ``secrets``): no bcrypt/argon2/
passlib/jose dependency to install or compile, so the app authenticates anywhere
CPython runs. scrypt is a memory-hard KDF; the parameters below cost tens of
milliseconds per hash — enough to make offline guessing expensive, cheap enough
for an interactive login.

Password hashes are stored self-describing as ``scrypt$<salt_b64>$<dk_b64>`` so the
salt (and, later, tunable parameters) travel with the hash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# ~16 MB, ~tens of ms per hash. maxmem is set explicitly because CPython's default
# (0) rejects n this large on some OpenSSL builds.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash for ``password`` (random per-call salt)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored scrypt hash.

    Returns False for empty passwords, unclaimed accounts (no hash), or any
    malformed/foreign hash string — never raises.
    """
    if not password or not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, b_salt, b_dk = stored.split("$", 2)
        salt = base64.b64decode(b_salt)
        expected = base64.b64decode(b_dk)
    except Exception:
        return False
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected), maxmem=_MAXMEM)
    return hmac.compare_digest(dk, expected)


def new_token(nbytes: int = 32) -> str:
    """A URL-safe, unguessable opaque session token."""
    return secrets.token_urlsafe(nbytes)


def normalize_email(email: str) -> str:
    """Canonical form for lookups and the UNIQUE constraint (trim + lowercase)."""
    return (email or "").strip().lower()
