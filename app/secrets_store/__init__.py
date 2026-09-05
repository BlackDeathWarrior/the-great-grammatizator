"""Runtime provider credentials, encrypted at rest.

Deliberately narrow. This package owns exactly two things: turning a pasted
secret into ciphertext, and handing the plaintext back to app/gateway/router.py
when it builds a Router. It does NOT name a provider's model strings, does not
call a provider, and is not imported by any agent - Invariant 5 stays intact.

What this protects against, honestly stated: a dumped Postgres volume, and a
key sitting readable on a shared screen. What it does not protect against:
anyone who can reach the dashboard, because the app has no auth (sec.13.1). A
runtime key store is a convenience for a demo, not a security boundary, and
saying otherwise would be worse than not having it.
"""

from app.secrets_store.store import (
    PROVIDERS,
    bump_version,
    current_version,
    delete_key,
    key_status,
    probe,
    resolve_key,
    save_key,
)

__all__ = [
    "PROVIDERS",
    "bump_version",
    "current_version",
    "delete_key",
    "key_status",
    "probe",
    "resolve_key",
    "save_key",
]
