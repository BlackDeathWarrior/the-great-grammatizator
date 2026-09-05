"""Reading and writing runtime provider keys.

Resolution order is DB first, then env. A key pasted in the dashboard wins over
.env so that saving one visibly does something; deleting it falls back to .env
rather than to nothing, so the stack that worked before the operator
experimented still works after.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.db.session import session_scope
from app.secrets_store.crypto import UndecryptableKey, decrypt, encrypt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    """One credential the platform can use.

    Deliberately does NOT name the Settings field holding the .env fallback.
    Spelling GROQ_API_KEY here would be choosing a provider, which Invariant 5
    reserves for the gateway (TC-0907) - so this module asks the gateway for
    both the field name and its value.
    """

    id: str
    label: str
    # What stops working without it, in the operator's terms rather than the
    # architecture's.
    role: str


def _load_providers() -> tuple[ProviderSpec, ...]:
    """Build the roster from the gateway rather than restating it.

    The list of providers is itself provider knowledge, and Invariant 5 keeps
    that in one file (TC-0901). Asking the gateway also means a provider added
    there appears in the settings page without a second edit - and, more to the
    point, that the two can never disagree about which credentials exist.
    """
    from app.gateway.router import PROVIDER_IDS, PROVIDER_LABELS, PROVIDER_ROLES

    return tuple(
        ProviderSpec(
            id=pid,
            label=PROVIDER_LABELS.get(pid, pid.title()),
            role=PROVIDER_ROLES.get(pid, ""),
        )
        for pid in PROVIDER_IDS
    )


PROVIDERS: tuple[ProviderSpec, ...] = _load_providers()

_BY_ID = {p.id: p for p in PROVIDERS}


def spec(provider: str) -> ProviderSpec:
    try:
        return _BY_ID[provider]
    except KeyError:
        raise ValueError(f"Unknown provider {provider!r}") from None


def _env_value(provider: str) -> str:
    """The .env fallback, asked of the gateway rather than read directly."""
    from app.gateway.router import env_key_for

    return env_key_for(provider)


def resolve_key(provider: str) -> str:
    """The key the gateway should actually use. Saved key first, else .env.

    Returns "" when neither exists, which the gateway reads as "omit this
    provider from the Router" - the same behaviour as an unset env var.
    """
    from app.db.models import ProviderKey

    spec(provider)  # validate

    try:
        with session_scope() as session:
            row = session.query(ProviderKey).filter_by(provider=provider).one_or_none()
            ciphertext = row.ciphertext if row else None
    except Exception:
        # No database yet, or it is down. The env fallback is the whole point:
        # a broken key store must never take the platform offline.
        log.warning(
            "Could not read stored key for %s; falling back to env.", provider, exc_info=True
        )
        return _env_value(provider)

    if ciphertext:
        try:
            return decrypt(ciphertext)
        except UndecryptableKey:
            log.warning("Stored key for %s is unreadable; falling back to env.", provider)

    return _env_value(provider)


def save_key(provider: str, plaintext: str) -> None:
    """Store a pasted key and make every process notice."""
    from app.db.models import ProviderKey

    spec(provider)
    plaintext = plaintext.strip()
    if not plaintext:
        raise ValueError("An empty key is not a key.")

    with session_scope() as session:
        row = session.query(ProviderKey).filter_by(provider=provider).one_or_none()
        if row is None:
            row = ProviderKey(provider=provider)
            session.add(row)
        row.ciphertext = encrypt(plaintext)
        row.hint = plaintext[-4:]

    bump_version()


def delete_key(provider: str) -> bool:
    """Forget a saved key. Reverts to .env, not to nothing."""
    from app.db.models import ProviderKey

    spec(provider)
    with session_scope() as session:
        row = session.query(ProviderKey).filter_by(provider=provider).one_or_none()
        if row is None:
            return False
        session.delete(row)

    bump_version()
    return True


def key_status() -> list[dict]:
    """What the Settings page renders. Never includes a plaintext key."""
    from app.db.models import ProviderKey
    from app.gateway.router import env_field_for

    with session_scope() as session:
        rows = {r.provider: r for r in session.query(ProviderKey).all()}
        saved = {p: {"hint": r.hint, "updated_at": r.updated_at} for p, r in rows.items()}

    out = []
    for p in PROVIDERS:
        entry = saved.get(p.id)
        env = _env_value(p.id)
        out.append(
            {
                "id": p.id,
                "label": p.label,
                "role": p.role,
                "env_field": (env_field_for(p.id) or "").upper(),
                # Where the key in force right now came from. The operator
                # needs this to explain a surprise: a stale saved key silently
                # overriding a corrected .env is otherwise invisible.
                "source": "saved" if entry else ("env" if env else "none"),
                "configured": bool(entry or env),
                "hint": entry["hint"] if entry else (env[-4:] if env else ""),
                "updated_at": entry["updated_at"] if entry else None,
            }
        )
    return out


def current_version() -> int:
    """The key generation. Changes whenever any key is saved or deleted."""
    from app.db.models import ProviderKeyVersion

    try:
        with session_scope() as session:
            row = session.get(ProviderKeyVersion, 1)
            return row.version if row else 0
    except Exception:
        return 0


def bump_version() -> int:
    from app.db.models import ProviderKeyVersion

    with session_scope() as session:
        row = session.get(ProviderKeyVersion, 1)
        if row is None:
            row = ProviderKeyVersion(id=1, version=0)
            session.add(row)
        row.version = (row.version or 0) + 1
        return row.version


async def probe(provider: str) -> dict:
    """Spend one trivial call proving a key actually works.

    Configured and working are different things. A revoked, mistyped or
    quota-exhausted key is indistinguishable from a good one in the settings
    table, and the difference otherwise surfaces as a job dying mid-demo - the
    same reasoning behind the startup preflight (TC-0908).

    Never raises: a probe reports a failure, it does not become one.
    """
    try:
        spec(provider)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}

    key = resolve_key(provider)
    if not key:
        return {"ok": False, "detail": "No key configured, in the database or in .env."}

    try:
        if provider == "embedding":
            return await _probe_embedding(key)
        return await _probe_completion(provider, key)
    except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
        return {"ok": False, "detail": _tidy(exc)}


async def _probe_completion(provider: str, key: str) -> dict:
    """One-token completion against the model this provider actually serves.

    The model ids live in the gateway, which is the only module permitted to
    name a provider (Invariant 5). Asking it rather than repeating the strings
    here is what keeps that true - and means a probe cannot silently test a
    different model from the one a job would use.
    """
    import litellm

    from app.gateway.router import probe_model_for

    model = probe_model_for(provider)
    if model is None:
        return {"ok": False, "detail": "No deployment uses this provider."}

    await litellm.acompletion(
        model=model,
        api_key=key,
        messages=[{"role": "user", "content": "ok"}],
        max_tokens=1,
        timeout=20,
    )
    return {"ok": True, "detail": f"{model} answered."}


async def _probe_embedding(key: str) -> dict:
    """Embed one word. Runs off-thread: both SDK paths are synchronous."""
    import asyncio

    s = get_settings()

    def _call() -> int:
        from app.ingest.embed import probe_embedding

        return probe_embedding(key)

    dim = await asyncio.to_thread(_call)
    detail = f"{s.embedding_model} returned a {dim}-dimension vector."
    if dim != s.embedding_dim:
        # A working key that returns the wrong width is worse than no key: the
        # vectors upsert and retrieval silently ranks nothing.
        return {
            "ok": False,
            "detail": f"{detail} EMBEDDING_DIM is {s.embedding_dim} - they must match.",
        }
    return {"ok": True, "detail": detail}


# What each failure means for the operator, rather than what the SDK called it.
# A probe exists to answer "can I run a job now"; a raw provider payload makes
# the reader do that translation themselves, at the moment they are least able
# to (POC.md sec.5: a message nobody can act on is a message nobody reads).
_FAILURES = {
    "AuthenticationError": "The provider rejected this key.",
    "RateLimitError": "Key works, but the quota is exhausted right now.",
    "NotFoundError": "Key works, but that model id is gone - re-probe the id.",
    "PermissionDeniedError": "The key is valid but not allowed to use this model.",
    "Timeout": "The provider did not answer in time.",
    "APIConnectionError": "Could not reach the provider.",
}


def _tidy(exc: Exception) -> str:
    """Say what happened in a sentence, then the detail if there is room.

    Provider SDKs raise walls of JSON. The name of the exception carries almost
    all the signal - the body is one long quota policy - so the name is
    translated and the body is trimmed hard rather than dumped into the page.
    """
    name = type(exc).__name__
    for marker, sentence in _FAILURES.items():
        if marker in name:
            return sentence

    # Unrecognised: the raw text is all there is, so keep it short and readable.
    text = " ".join(str(exc).split())
    return text[:160] or name
