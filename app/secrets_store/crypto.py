"""Fernet encryption for stored provider keys.

The secret comes from SECRET_KEY. When it is absent - the ordinary case for a
fresh clone - one is generated and written to a file beside the storage dir so
saved keys survive a restart. Generating rather than refusing keeps the stack
bootable with no setup step; persisting rather than holding it in memory is
what stops every restart silently invalidating the keys the operator saved.
"""

from __future__ import annotations

import contextlib
import logging
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

log = logging.getLogger(__name__)

_KEY_FILENAME = "secret.key"


class UndecryptableKey(RuntimeError):
    """Ciphertext that will not open with the current SECRET_KEY.

    Almost always means the secret changed (a regenerated key file, a new
    SECRET_KEY in .env) rather than tampering. The saved credential is simply
    gone and the operator must paste it again, so this surfaces as "re-enter
    this key" and never as a stack trace.
    """


def _key_path() -> Path:
    return Path(get_settings().storage_dir) / _KEY_FILENAME


@lru_cache
def _fernet() -> Fernet:
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return Fernet(_coerce(configured))

    path = _key_path()
    if path.exists():
        return Fernet(path.read_bytes().strip())

    generated = Fernet.generate_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(generated)
        # Owner-only where the platform honours it; a no-op on Windows bind
        # mounts, which is why this is best-effort rather than load-bearing.
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        log.warning(
            "SECRET_KEY was not set; generated one at %s. Set SECRET_KEY in .env "
            "to control it yourself.",
            path,
        )
    except OSError:
        # An unwritable storage dir must not stop the app booting. Keys saved
        # in this process still work; they just will not survive a restart.
        log.warning(
            "Could not persist a generated SECRET_KEY; saved keys will not survive restart."
        )
    return Fernet(generated)


def _coerce(value: str) -> bytes:
    """Accept either a real Fernet key or any passphrase.

    A urlsafe-base64 32-byte key is used as-is. Anything else is stretched, so
    an operator who puts a memorable string in .env gets a working install
    instead of a ValueError they have to decode.
    """
    import base64
    import hashlib

    raw = value.encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except Exception:
        pass
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise UndecryptableKey(
            "This saved key cannot be read with the current SECRET_KEY. Enter it again."
        ) from exc


def reset_cache() -> None:
    """Forget the derived Fernet. Tests only."""
    _fernet.cache_clear()
