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
# tiers; verify against the provider before a demo.
#
# Verified 2026-09-01. The originals from the doc had ALL THREE gone dead:
#   llama-3.3-70b-versatile        -> "does not exist" on Groq
#   gemini-2.0-flash               -> retired, Gemini points at 3.6
#   llama-3.3-70b-instruct:free    -> the :free slug is now paid-only
# Re-probe these if a demo fails at the first model call.
_GROQ_FAST = "groq/openai/gpt-oss-20b"
_GEMINI_LONG = "gemini/gemini-3.6-flash"
_OPENROUTER_FAST = "openrouter/meta-llama/llama-3.3-70b-instruct"

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


async def preflight() -> list[dict[str, Any]]:
    """Send one trivial completion per configured deployment.

    Every model id inherited from the original architecture was dead on the
    first live run - the Groq model no longer existed, the Gemini model was
    retired, and the OpenRouter free slug had become paid-only. Free-tier ids
    drift without notice, and the failure surfaces as a job dying mid-demo.

    This turns that into a startup warning naming the dead id (TC-0908).
    """
    import litellm

    router_obj = get_router()
    if router_obj is None:
        return [{"alias": "-", "model": "-", "ok": False, "error": "no provider configured"}]

    results: list[dict[str, Any]] = []
    for entry in router_obj.model_list:
        params = entry["litellm_params"]
        row = {"alias": entry["model_name"], "model": params["model"], "ok": True, "error": ""}
        try:
            await litellm.acompletion(
                model=params["model"],
                api_key=params.get("api_key"),
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - any failure means unusable
            row["ok"] = False
            row["error"] = str(exc)[:160]
            log.warning(
                "PREFLIGHT FAILED %s (%s): %s",
                params["model"],
                entry["model_name"],
                row["error"],
            )
        results.append(row)

    if results and not any(r["ok"] for r in results):
        log.error("PREFLIGHT: no configured model answered. Generation will fail.")
    return results


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
