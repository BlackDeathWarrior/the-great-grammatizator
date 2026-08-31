"""LiteLLM gateway. THE ONLY MODULE PERMITTED TO NAME A PROVIDER.

Invariant 5: agents request the alias "fast" or "long", never a provider.
LiteLLM routes. TC-0901 greps agent code for provider names and expects zero
hits outside this file.

Routing is by task shape, not preference (ARCHITECTURE.md sec.3):
    fast -> Groq        short-form generation, QA checkers; demo latency
    long -> Gemini      long structured output; context size, reliable JSON
    overflow -> OpenRouter, a separate rate-limit pool

Free tiers WILL rate-limit during the demo, so: fallbacks, a concurrency cap on
QA checkers, and aggressive caching.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

FAST = "fast"
LONG = "long"
ALIASES = (FAST, LONG)

# Model identifiers. ARCHITECTURE.md sec.3 warns these strings change on free
# tiers; verify against the provider's current list before a demo.
_GROQ_FAST = "groq/llama-3.3-70b-versatile"
_GEMINI_LONG = "gemini/gemini-2.0-flash"
_OPENROUTER_FAST = "openrouter/meta-llama/llama-3.3-70b-instruct:free"

_router = None
_qa_semaphore: asyncio.Semaphore | None = None


class ProviderError(RuntimeError):
    """A provider-side failure: 429, timeout, outage.

    Distinct from a QA verdict failure. These retry with backoff and must NEVER
    increment the 3-strike quality counter (Invariant 8, TC-0609).
    """


def build_router():
    """Construct the Router. Only entries with a configured key are included."""
    from litellm import Router

    s = get_settings()
    model_list: list[dict[str, Any]] = []

    if s.groq_api_key:
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {"model": _GROQ_FAST, "api_key": s.groq_api_key},
            }
        )
    if s.openrouter_api_key:
        # Second entry under the same alias: a separate rate-limit pool that
        # LiteLLM can fall back to when Groq returns 429 (TC-0902).
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {
                    "model": _OPENROUTER_FAST,
                    "api_key": s.openrouter_api_key,
                },
            }
        )
    if s.gemini_api_key:
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {"model": _GEMINI_LONG, "api_key": s.gemini_api_key},
            }
        )

    if not model_list:
        return None

    return Router(
        model_list=model_list,
        fallbacks=[{FAST: [LONG]}, {LONG: [FAST]}],
        num_retries=2,
        retry_after=2,
        allowed_fails=3,
        cooldown_time=30,
    )


def get_router():
    global _router
    if _router is None:
        _router = build_router()
    return _router


def qa_semaphore() -> asyncio.Semaphore:
    """Concurrency cap for QA checkers.

    ARCHITECTURE.md sec.3: cap the checkers rather than firing all four at once,
    or a free tier rate-limits mid-demo (TC-0904).
    """
    global _qa_semaphore
    if _qa_semaphore is None:
        _qa_semaphore = asyncio.Semaphore(get_settings().qa_concurrency)
    return _qa_semaphore


def reset() -> None:
    """Test hook: drop the cached router and semaphore."""
    global _router, _qa_semaphore
    _router = None
    _qa_semaphore = None


async def complete(
    alias: str,
    messages: list[dict[str, str]],
    *,
    response_format: dict | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """Make a completion call by ALIAS.

    Callers pass "fast" or "long" and never learn which provider served them.
    Raises ProviderError so callers can distinguish infrastructure failure from
    a quality failure.
    """
    if alias not in ALIASES:
        raise ValueError(f"Unknown alias {alias!r}; expected one of {ALIASES}")

    router = get_router()
    if router is None:
        raise ProviderError(
            "No provider configured. Set GROQ_API_KEY, GEMINI_API_KEY or "
            "OPENROUTER_API_KEY in .env."
        )

    kwargs: dict[str, Any] = {
        "model": alias,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    from app.observability import record_model_call

    record_model_call(alias)

    try:
        resp = await router.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - litellm raises many provider types
        # Everything reaching here is infrastructure, not content quality.
        raise ProviderError(str(exc)) from exc

    return resp.choices[0].message.content or ""
