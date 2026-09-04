"""Guardrails ahead of the router: PII detection and scope (ARCHITECTURE.md sec.3).

Deterministic checks only - cheap pattern matching, never a model call. An LLM
safety judgement belongs in the QA safety checker, which sees the finished
artefact.

What actually runs where, because this docstring used to overstate it:

  check_scope  - called by router.complete() on every outbound prompt. Rejects
                 one that would blow the context window, before dispatch.
  find_pii     - called by the QA safety checker on the finished artefact. It
                 is the deterministic half of that hard gate.
  redact       - NOT applied to prompts. Source text must reach the model
                 verbatim or grounding breaks: a claim cites a chunk id and is
                 verified against exactly the text the model was shown, so
                 masking it would make every citation unverifiable. Kept for
                 redacting text on the way OUT - logs, traces, error messages -
                 where the original is not needed.
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
    """Mask PII on the way OUT: operator messages, logs, traces.

    Never applied to prompts or stored source text - grounding verifies a claim
    against exactly the chunk the model was shown, so masking that text would
    make every citation unverifiable (Invariant 1, Invariant 2).
    """
    out = text or ""
    for _kind, pattern in _PII_PATTERNS:
        out = pattern.sub(_REDACTION, out)
    return out


class PromptTooLarge(ValueError):
    """A prompt that would blow the context window, caught before dispatch.

    Deliberately not a ProviderError: nothing is wrong with the provider, and
    retrying with backoff would pay for the same rejection three times.
    """


def check_scope(text: str, max_chars: int = 400_000) -> None:
    """Reject prompts that would blow the context window before sending."""
    if len(text or "") > max_chars:
        raise PromptTooLarge(
            f"Prompt is {len(text)} chars, over the {max_chars} limit. "
            "Reduce retrieved chunks or detail level."
        )
