"""Provider errors, said in words an operator can act on.

A raw LiteLLM exception is a wall of JSON with three nested provider payloads
in it. It tells an operator that something went wrong and nothing whatsoever
about what to do next, which on a free tier is nearly always one of two things:
wait, or change a key.

This module is deliberately pattern-matching on message text rather than on
exception classes. The text is what reaches the artefact row - by the time it
is stored the exception is a string - and provider wording changes far less
often than provider model ids do.
"""

from __future__ import annotations

# The gateway is the only module allowed to name a provider (Invariant 5), so
# the question "who failed, and where do I send the operator" is asked of it
# rather than answered here.
from app.gateway.router import console_for, provider_in

# Ordered: the first pattern that matches wins, so the specific cases sit above
# the general ones. Each entry is (needles, kind, headline, what to do).
_RULES: list[tuple[tuple[str, ...], str, str, str]] = [
    (
        ("ratelimit", "rate limit", "429", "quota", "resource_exhausted", "too many requests"),
        "rate_limit",
        "The provider's rate limit was reached.",
        "Free tiers reset on a timer - often daily. Wait and run it again, or "
        "use a key with more headroom.",
    ),
    (
        ("insufficient_quota", "billing", "payment", "credit", "insufficient funds"),
        "billing",
        "The provider account is out of credit.",
        "The key works, but the account behind it cannot pay for the call.",
    ),
    (
        (
            "invalid_api_key",
            "unauthorized",
            "401",
            "authentication",
            "invalid x-api-key",
            "no auth credentials",
        ),
        "auth",
        "The provider rejected the API key.",
        "The key is missing, expired, or wrong for this provider.",
    ),
    (
        ("notfounderror", "does not exist", "404", "model_not_found"),
        "model_gone",
        "The model this alias points at no longer exists.",
        "Free-tier model names change without notice. The identifier needs "
        "re-probing against the provider.",
    ),
    (
        ("timeout", "timed out", "deadline"),
        "timeout",
        "The provider did not answer in time.",
        "Usually load at the provider. Running it again often just works.",
    ),
    (
        ("json_validate_failed", "failed to validate json", "failed_generation"),
        "malformed",
        "The model returned something that was not valid JSON.",
        "The generator retried and could not get a clean structure back. A "
        "different model for this alias usually fixes it.",
    ),
    (
        ("overloaded", "503", "502", "unavailable", "capacity"),
        "outage",
        "The provider is over capacity.",
        "Nothing is wrong with this job. Try again shortly.",
    ),
]


def explain(raw: str | None) -> dict | None:
    """Turn a stored provider error into something worth showing.

    Returns None for a falsy error so a template can test it directly. The raw
    text is always carried through under `detail`: an operator who wants the
    stack trace must still be able to reach it, because a friendly summary that
    hides the only diagnostic detail is worse than the jargon it replaced.
    """
    if not raw:
        return None

    text = str(raw)
    low = text.lower()

    kind, headline, advice = (
        "unknown",
        "The provider call did not succeed.",
        "The message below is what the provider sent back.",
    )
    for needles, k, head, adv in _RULES:
        if any(n in low for n in needles):
            kind, headline, advice = k, head, adv
            break

    provider = provider_in(text)
    return {
        "kind": kind,
        "headline": headline,
        "advice": advice,
        "provider": provider,
        # Only offer an upgrade link where paying actually fixes it.
        "console": console_for(provider)
        if kind in ("rate_limit", "billing", "auth", "model_gone")
        else "",
        "detail": text,
    }
