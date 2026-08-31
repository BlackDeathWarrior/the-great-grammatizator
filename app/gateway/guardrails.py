"""Guardrails ahead of the router: safety, PII, scope (ARCHITECTURE.md sec.3).

Deterministic checks only. These run on every prompt before it reaches a
provider and on every completion before it reaches an agent. They are cheap
pattern checks, not a model call - an LLM safety judgement belongs in the QA
safety checker, which sees the finished artefact.
"""

from __future__ import annotations

import re

# Conservative patterns. False positives here are cheap (a redaction); false
# negatives leak PII into a provider request.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|api|key)[-_][A-Za-z0-9]{16,}\b", re.I)),
]

_REDACTION = "[redacted]"


def find_pii(text: str) -> list[str]:
    """Return the kinds of PII present, if any."""
    return [kind for kind, pattern in _PII_PATTERNS if pattern.search(text or "")]


def redact(text: str) -> str:
    """Mask PII. Used on outbound prompts, never on stored source text.

    Source text stays pristine because grounding verifies claims against it
    (Invariant 1).
    """
    out = text or ""
    for _kind, pattern in _PII_PATTERNS:
        out = pattern.sub(_REDACTION, out)
    return out


def check_scope(text: str, max_chars: int = 400_000) -> None:
    """Reject prompts that would blow the context window before sending."""
    if len(text or "") > max_chars:
        raise ValueError(
            f"Prompt is {len(text)} chars, over the {max_chars} limit. "
            "Reduce retrieved chunks or detail level."
        )
