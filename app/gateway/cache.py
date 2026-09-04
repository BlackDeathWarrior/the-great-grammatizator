"""Prompt cache.

Cache key is source_hash + output_type + parameters. NEVER job_id, or a second
job on the same source with the same settings misses the cache
(ARCHITECTURE.md sec.3, TC-0204, TC-0205).

Aggressive caching is one of the three named mitigations for free-tier rate
limits: a repeat demo run should hit cache rather than the provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

_PREFIX = "promptcache:"
_TTL_SECONDS = 60 * 60 * 24 * 7


def cache_key(
    source_hash: str,
    output_type: str,
    parameters: dict[str, Any],
    *,
    prompt_version: str = "",
    attempt_salt: str = "",
) -> str:
    """Build the key.

    prompt_version participates so a template change invalidates cleanly.
    attempt_salt lets a RETRY bypass the cache - a retry must produce different
    output than the attempt that just failed QA, or fix notes achieve nothing.
    """
    payload = json.dumps(
        {
            "source_hash": source_hash,
            "output_type": output_type,
            "parameters": dict(sorted(parameters.items())),
            "prompt_version": prompt_version,
            "attempt": attempt_salt,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checker_key(checker: str, artefact_content: Any, extra: str = "") -> str:
    """Key for a QA verdict.

    A checker is a pure function of the artefact it is shown: the same content
    judged by the same checker yields the same verdict, so re-asking costs
    tokens and latency for an answer already known. This matters most on a
    retry, where the artefact usually changes in one place and the checkers all
    run again from scratch.

    Deliberately NOT keyed on job_id or attempt: two jobs that generate
    identical content should share a verdict, exactly as TC-0205 requires of
    generation. `extra` carries anything outside the content that changes the
    judgement - a threshold, the operator's parameters for tone.
    """
    payload = json.dumps(
        {"checker": checker, "content": artefact_content, "extra": extra},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _PREFIX + "qa:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def get(key: str) -> str | None:
    try:
        return _redis().get(key)
    except Exception as exc:  # noqa: BLE001 - cache must never break generation
        log.warning("cache read failed: %s", exc)
        return None


def put(key: str, value: str, ttl: int = _TTL_SECONDS) -> None:
    try:
        _redis().setex(key, ttl, value)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache write failed: %s", exc)


def clear_prefix() -> int:
    """Test/demo hook: drop every cached prompt."""
    try:
        client = _redis()
        keys = list(client.scan_iter(match=_PREFIX + "*"))
        return client.delete(*keys) if keys else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("cache clear failed: %s", exc)
        return 0
