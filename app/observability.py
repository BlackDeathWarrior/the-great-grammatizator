"""Langfuse tracing.

ARCHITECTURE.md sec.10: traces, versioned prompts, eval runs. Prompt versioning
is what lets an eval run compare v1 against v2 on the same fixed test set;
without it every prompt change is a guess.

Tracing must never be load-bearing. If Langfuse is unconfigured or unreachable,
every function here degrades to a no-op rather than failing a job.
"""

from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

# Per-process counters. The docs ask for the call budget to be measured before
# the demo (TC-1204); counting here means the number is observed, not estimated.
_METRICS: dict[str, Any] = {"model_calls": 0, "by_alias": {}, "tool_calls": 0, "latency_ms": {}}


@functools.lru_cache(maxsize=1)
def _client():
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.info("langfuse not configured; tracing disabled")
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse unavailable: %s", exc)
        return None


@contextmanager
def trace(name: str, **metadata):
    """Record a span. Yields a dict the caller may add output to."""
    span: dict[str, Any] = {"name": name, "metadata": metadata}
    started = time.monotonic()
    client = _client()

    try:
        yield span
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        _METRICS["latency_ms"].setdefault(name, []).append(elapsed_ms)

        if client is not None:
            try:
                # Langfuse v4 replaced .trace() with create_event/observations.
                client.create_event(
                    name=name,
                    metadata={**metadata, "duration_ms": round(elapsed_ms)},
                    output=span.get("output"),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse trace failed: %s", exc)


def record_model_call(alias: str, prompt_version: str = "") -> None:
    """Count a completion. Feeds the call-budget report (TC-1204)."""
    _METRICS["model_calls"] += 1
    _METRICS["by_alias"][alias] = _METRICS["by_alias"].get(alias, 0) + 1


def record_tool_call(caller: str, tool: str, allowed: bool) -> None:
    """Every tool call appears in the trace, denials included (TC-1005)."""
    _METRICS["tool_calls"] += 1
    client = _client()
    if client is not None:
        try:
            client.create_event(
                name="tool_call",
                metadata={"caller": caller, "tool": tool, "allowed": allowed},
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse tool trace failed: %s", exc)


def metrics() -> dict[str, Any]:
    """Snapshot, with latency summarised rather than dumped raw."""
    latency = {
        name: {
            "count": len(samples),
            "mean_ms": round(sum(samples) / len(samples)),
            "max_ms": round(max(samples)),
        }
        for name, samples in _METRICS["latency_ms"].items()
        if samples
    }
    return {
        "model_calls": _METRICS["model_calls"],
        "by_alias": dict(_METRICS["by_alias"]),
        "tool_calls": _METRICS["tool_calls"],
        "latency": latency,
    }


def flush() -> None:
    """Push buffered traces. Worker jobs are short-lived, so an unflushed
    buffer would be discarded at process exit."""
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse flush failed: %s", exc)


def reset() -> None:
    _METRICS.update({"model_calls": 0, "by_alias": {}, "tool_calls": 0, "latency_ms": {}})
