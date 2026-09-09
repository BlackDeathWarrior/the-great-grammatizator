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
import weakref
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
# Re-probed 2026-09-09: Gemini's daily free quota was exhausted and BOTH
# OpenRouter slugs had gone (llama-3.3-70b-instruct now errors, minimax-m3:free
# is 404). That left `long` with no reachable deployment at all. This is a Groq
# model on the same key as `fast`, so it shares a rate-limit pool - not ideal -
# but a working alias beats a correct one that 404s.
_GROQ_LONG = "groq/openai/gpt-oss-120b"
_GEMINI_LONG = "gemini/gemini-3.6-flash"
_OPENROUTER_FAST = "openrouter/meta-llama/llama-3.3-70b-instruct"
# Mistral, added as a fourth provider. Its free tier is a separate rate-limit
# pool from Groq and Gemini, which is the whole point: the failure mode this
# platform actually hits on a demo day is every deployment for an alias sitting
# behind the same exhausted quota.
# Probed 2026-09-09 against the demo key: mistral-small/medium/large and
# open-mixtral all return 429 or "not available" on this tier, so the aliases
# point at the models the key can actually reach. ministral-8b is the small
# fast one; open-mistral-nemo carries a 128k context, which is what `long`
# needs. Re-probe if the tier changes - these strings drift like every other
# free-tier slug.
# NVIDIA NIM, a fifth pool. Probed 2026-09-09 against the demo key: most of the
# catalogue 404s (listed but not deployed for this key) and llama-3.3-70b is
# 410 Gone - end of life 2026-08-26. Of what does answer, muse-glimmer-30b
# returns EMPTY content under json_object mode, which would fail the pipeline
# silently, so it is deliberately not used. These two return clean JSON.
_NIM_FAST = "nvidia_nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
_NIM_LONG = "nvidia_nim/moonshotai/kimi-k3"
_MISTRAL_FAST = "mistral/ministral-8b-latest"
_MISTRAL_LONG = "mistral/open-mistral-nemo"
# Long-context second pool for the `long` alias.
# Re-probed 2026-09-09: the ":free" slug now 404s and OpenRouter's own error
# names the replacement. A dead deployment is worse than a missing one -
# every `long` call paid three retries and 12s of backoff before falling
# back, which is what pushed a seven-format job past the 900s job timeout.
_OPENROUTER_LONG = "openrouter/minimax/minimax-m3"

# Where an operator goes when a provider says no. This lives here because this
# file is the only one allowed to know a provider's name (Invariant 5): the web
# layer asks "who failed, and where do I send them", and never spells out an
# answer of its own.
_PROVIDER_CONSOLES = {
    "groq": "https://console.groq.com/settings/billing",
    "gemini": "https://aistudio.google.com/app/apikey",
    "google": "https://aistudio.google.com/app/apikey",
    "openrouter": "https://openrouter.ai/credits",
}


def provider_in(text: str) -> str:
    """Name the provider a raw error came from, or "" if it is not one of ours.

    The caller has an exception string and no idea which deployment produced
    it. Answering that here keeps the provider vocabulary in this file.
    """
    low = (text or "").lower()
    for name in _PROVIDER_CONSOLES:
        if name in low:
            return name
    return ""


def console_for(provider: str) -> str:
    """The page where an operator checks limits or tops up a provider account."""
    return _PROVIDER_CONSOLES.get(provider, "")


# Enough for the longest artefact this platform produces - a seven-slide deck
# with speaker notes measured ~3.4k tokens - with headroom, and far below the
# free-tier ceilings that reject an unbounded request.
_DEFAULT_MAX_TOKENS = 8000


_router = None
# The key generation the cached router was built from; see get_router().
_router_key_version = -1
# One semaphore per event loop; see qa_semaphore(). Weak keys so a finished
# loop does not keep its semaphore (or itself) alive.
_qa_semaphores: weakref.WeakKeyDictionary[object, asyncio.Semaphore] = weakref.WeakKeyDictionary()


class MalformedOutput(RuntimeError):
    """The model emitted output the provider rejected as invalid JSON.

    A content fault wearing a provider error's clothes: some providers validate
    json_object mode server-side and fail the request rather than returning the
    bad text. Callers route this to their PARSE counter, never the QA one and
    never the provider-backoff path (Invariant 8).
    """


# Provider wording for "the model did not produce the JSON it was asked for".
# Matched on text because the exception CLASS is a generic BadRequestError.
_MALFORMED_MARKERS = (
    "json_validate_failed",
    "failed to validate json",
    "failed_generation",
)


def _is_malformed_output(exc: Exception) -> bool:
    low = str(exc).lower()
    return any(m in low for m in _MALFORMED_MARKERS)


class ProviderError(RuntimeError):
    """A provider-side failure: 429, timeout, outage.

    Distinct from a QA verdict failure. These retry with backoff and must NEVER
    increment the 3-strike quality counter (Invariant 8, TC-0609).
    """


def build_router():
    """Construct the Router. Only entries with a configured key are included."""
    from litellm import Router

    from app.secrets_store import resolve_key

    # Keys come from the store, which layers a key saved in the dashboard over
    # the .env fallback. This file still owns every provider NAME (Invariant 5);
    # the store only decides which credential to hand back.
    groq_key = resolve_key("groq")
    gemini_key = resolve_key("gemini")
    openrouter_key = resolve_key("openrouter")
    mistral_key = resolve_key("mistral")
    nim_key = resolve_key("nvidia_nim")

    model_list: list[dict[str, Any]] = []

    if groq_key:
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {"model": _GROQ_FAST, "api_key": groq_key},
            }
        )
    if openrouter_key:
        # Second entry under the same alias: a separate rate-limit pool that
        # LiteLLM can fall back to when Groq returns 429 (TC-0902).
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {
                    "model": _OPENROUTER_FAST,
                    "api_key": openrouter_key,
                },
            }
        )
    if gemini_key:
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {"model": _GEMINI_LONG, "api_key": gemini_key},
            }
        )
    if groq_key:
        # Third `long` deployment, and on the free tiers usually the only one
        # actually reachable. Ordered after Gemini so the genuinely
        # long-context provider is still preferred when its quota allows.
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {"model": _GROQ_LONG, "api_key": groq_key},
            }
        )
    if openrouter_key:
        # `long` had exactly ONE deployment, so a Gemini 429 - routine on the
        # free tier - had no same-alias alternative and fell across to `fast`,
        # losing the long context the alias exists to provide. Four of seven
        # formats ask for `long`, so this was the likeliest way to degrade a
        # demo (TC-0902, TC-0904).
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {
                    "model": _OPENROUTER_LONG,
                    "api_key": openrouter_key,
                },
            }
        )

    if mistral_key:
        # Both aliases: a fourth pool is worth most where the others are
        # thinnest, and `long` has historically been the alias that ran out of
        # places to go.
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {"model": _MISTRAL_FAST, "api_key": mistral_key},
            }
        )
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {"model": _MISTRAL_LONG, "api_key": mistral_key},
            }
        )

    if nim_key:
        model_list.append(
            {
                "model_name": FAST,
                "litellm_params": {"model": _NIM_FAST, "api_key": nim_key},
            }
        )
        model_list.append(
            {
                "model_name": LONG,
                "litellm_params": {"model": _NIM_LONG, "api_key": nim_key},
            }
        )

    if not model_list:
        return None

    return Router(
        model_list=model_list,
        fallbacks=[{FAST: [LONG]}, {LONG: [FAST]}],
        num_retries=2,
        retry_after=2,
        # A free tier rate-limits routinely, so benching a deployment after
        # three failures for a full 30 seconds took the `long` alias out for
        # most of a seven-format run. More tolerance, shorter bench.
        allowed_fails=8,
        cooldown_time=10,
    )


def get_router():
    """The Router, rebuilt whenever a provider key changes anywhere.

    A key saved in the browser lands in Postgres, not in this process's memory,
    so the worker would otherwise keep using the credential it booted with
    until someone restarted it. Comparing a cheap counter before each call is
    what makes "no restart" true in the container that actually runs the job,
    rather than only in the one that served the form.
    """
    global _router, _router_key_version

    from app.secrets_store import current_version

    version = current_version()
    if _router is None or version != _router_key_version:
        _router = build_router()
        _router_key_version = version
    return _router


def qa_semaphore() -> asyncio.Semaphore:
    """Concurrency cap for QA checkers.

    ARCHITECTURE.md sec.3: cap the checkers rather than firing all of them at
    once, or a free tier rate-limits mid-demo (TC-0904).

    Kept per event loop. An asyncio.Semaphore binds to the loop that first
    awaits it and raises "bound to a different event loop" on every other one,
    so a single module global would break the moment a second loop touched it -
    the API process and the worker, or a worker whose loop is replaced after a
    restart. The failure is not subtle but it is remote from its cause, and it
    would surface as every QA checker erroring at once.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (a synchronous caller inspecting the cap). Hand back
        # a fresh one rather than caching under a key we cannot weakly hold.
        return asyncio.Semaphore(get_settings().qa_concurrency)

    existing = _qa_semaphores.get(loop)
    if existing is None:
        existing = asyncio.Semaphore(get_settings().qa_concurrency)
        _qa_semaphores[loop] = existing
    return existing


def reset() -> None:
    """Test hook: drop the cached router and semaphores."""
    global _router, _router_key_version
    _router = None
    _router_key_version = -1
    _qa_semaphores.clear()


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
        # Without this a hung connection stalls the artefact until arq's job
        # timeout 900s later. preflight() always passed one; the real call
        # path did not.
        "timeout": get_settings().request_timeout,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    # An unset max_tokens is not "no limit" - providers read it as "reserve the
    # whole context window". OpenRouter's free tier then refuses the call
    # outright ("you requested up to 131072 tokens, but can only afford 11858"),
    # which arrives as a 402 and is retried three times with backoff before
    # falling back. Seven formats of that is what pushed a job past the 900s
    # timeout with nothing to show for it.
    #
    # A ceiling belongs here rather than at each call site: this file is the
    # only one that knows a provider exists, and an agent that had to remember
    # a token budget would eventually forget.
    kwargs["max_tokens"] = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS

    # Guardrail before dispatch. Without this an oversized prompt reaches the
    # provider, comes back as a context-length error, is classified as a
    # ProviderError and is then retried - paying for the same rejection three
    # times before failing with a message that does not say what went wrong.
    from app.gateway import guardrails

    guardrails.check_scope("\n".join(m.get("content") or "" for m in messages))

    from app.observability import record_model_call

    record_model_call(alias)

    try:
        resp = await router.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - litellm raises many provider types
        # Not everything reaching here is infrastructure. A provider that
        # enforces json_object mode server-side rejects the CALL when the model
        # emits invalid JSON, and that is the model failing to speak the
        # protocol - the same condition generator._parse raises ParseFailure
        # for. Classified as a ProviderError it bought three retries with 12s
        # of backoff each, per artefact, for a fault that a parse retry fixes
        # in one cheap attempt with the error fed back to the model.
        if _is_malformed_output(exc):
            raise MalformedOutput(str(exc)) from exc
        raise ProviderError(str(exc)) from exc

    return resp.choices[0].message.content or ""


# The credentials this platform can hold, in the operator's terms. Named here
# rather than in the settings page or the key store because a list of providers
# IS provider knowledge, and Invariant 5 keeps that in one file (TC-0901). The
# dashboard asks for this roster; it does not maintain its own.
PROVIDER_IDS = ("groq", "gemini", "openrouter", "mistral", "nvidia_nim", "embedding")

PROVIDER_LABELS = {
    "groq": "Groq",
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "mistral": "Mistral",
    "nvidia_nim": "NVIDIA NIM",
    "embedding": "Embeddings",
}

PROVIDER_ROLES = {
    "groq": "Short-form generation and the QA checkers (the 'fast' alias).",
    "gemini": "Long structured output - decks, video packages (the 'long' alias).",
    "openrouter": "Fallback pool. Without it a rate-limited job has nowhere to go.",
    "mistral": "A second fallback pool, on both aliases. Separate quota from the rest.",
    "nvidia_nim": "A third fallback pool, on both aliases. Hosted NVIDIA endpoints.",
    "embedding": "Semantic retrieval at ingest. Without it search falls back to hash vectors.",
}


# Which Settings field holds each provider's .env fallback. This lives here
# rather than in the key store because naming GROQ_API_KEY is choosing a
# provider, and Invariant 5 puts that decision in exactly one file (TC-0907).
_ENV_FIELDS = {
    "groq": "groq_api_key",
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "mistral": "mistral_api_key",
    "nvidia_nim": "nvidia_nim_api_key",
    # Embeddings bypass this router by design (ARCHITECTURE.md sec.2) but still
    # need a credential resolved, and the store must not spell the field name
    # any more than it spells the others.
    "embedding": "embedding_api_key",
}


def env_field_for(provider: str) -> str | None:
    """The Settings attribute holding this provider's .env fallback."""
    return _ENV_FIELDS.get(provider)


def env_key_for(provider: str) -> str:
    """The .env credential for a provider, or "" when unset."""
    field = _ENV_FIELDS.get(provider)
    if field is None:
        return ""
    return (getattr(get_settings(), field, "") or "").strip()


def probe_model_for(provider: str) -> str | None:
    """The model id a settings probe should test for this provider.

    Exists so app/secrets_store never has to name a model. Invariant 5 is about
    where provider strings may live, not only about who calls LiteLLM, and a
    probe that tested a hardcoded id would drift from the one jobs actually use
    - reporting a healthy key for a dead deployment.
    """
    return {
        "groq": _GROQ_FAST,
        "gemini": _GEMINI_LONG,
        "openrouter": _OPENROUTER_FAST,
        "mistral": _MISTRAL_FAST,
        "nvidia_nim": _NIM_FAST,
    }.get(provider)
