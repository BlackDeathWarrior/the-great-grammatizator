"""Tool registry with per-caller allowlists.

Two gateways, two concerns (ARCHITECTURE.md sec.5). LiteLLM governs MODEL calls:
which provider, is it safe, is it cached. This registry governs TOOL calls: who
may call what, with which credential, logged.

Shipped as plain Python per the documented fallback in sec.5 - "the registry is
the idea; MCP is one way to serve it. The security argument survives intact."
Signatures are kept MCP-shaped so a server can wrap this later.

The load-bearing rule: the grounding checker has no web search. With it, the
checker verifies claims against the internet instead of the source document and
silently defeats itself. Enforced HERE, in the registry, not in a prompt
(Invariant 6, TC-1001).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class ToolGroup(StrEnum):
    RETRIEVAL = "retrieval"
    WEB_SEARCH = "web_search"
    RENDER = "render"
    MEDIA = "media"


class Caller(StrEnum):
    """Every agent that may call a tool. Unknown callers get nothing."""

    INPUT_ANALYSIS = "input_analysis"
    OUTPUT_GENERATOR = "output_generator"
    GROUNDING_CHECKER = "grounding_checker"
    TONE_CHECKER = "tone_checker"
    SAFETY_CHECKER = "safety_checker"
    EDITORIAL_CHECKER = "editorial_checker"
    EXPORT = "export"


# The allowlist, straight from the table in ARCHITECTURE.md sec.5.
ALLOWLIST: dict[Caller, frozenset[ToolGroup]] = {
    Caller.INPUT_ANALYSIS: frozenset({ToolGroup.RETRIEVAL, ToolGroup.WEB_SEARCH}),
    Caller.OUTPUT_GENERATOR: frozenset({ToolGroup.RETRIEVAL}),
    # Retrieval only. NEVER web search - this line is the whole point.
    Caller.GROUNDING_CHECKER: frozenset({ToolGroup.RETRIEVAL}),
    Caller.TONE_CHECKER: frozenset(),
    Caller.SAFETY_CHECKER: frozenset(),
    # Judges the artefact as written. It is handed the source-overlap figure
    # it needs, so it has no reason to reach for anything (TC-1007).
    Caller.EDITORIAL_CHECKER: frozenset(),
    Caller.EXPORT: frozenset({ToolGroup.RENDER, ToolGroup.MEDIA}),
}


class ToolDenied(PermissionError):
    """A caller attempted a tool outside its allowlist.

    Raised by the registry, never by a prompt. An agent cannot talk its way past
    this.
    """

    def __init__(self, caller: str, tool: str, group: str):
        super().__init__(f"Caller {caller!r} is not permitted to call {tool!r} (group {group!r}).")
        self.caller = caller
        self.tool = tool
        self.group = group


@dataclass(frozen=True)
class Tool:
    name: str
    group: ToolGroup
    fn: Callable[..., Any]
    description: str = ""


_REGISTRY: dict[str, Tool] = {}
_AUDIT: list[dict[str, Any]] = []


def tool(name: str, group: ToolGroup, description: str = ""):
    """Register a tool. Purpose-built and scoped - never a raw client."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[name] = Tool(name=name, group=group, fn=fn, description=description)
        return fn

    return decorator


def available(caller: Caller) -> list[str]:
    """What this caller may see. A denied tool is not merely refused - it is
    invisible, so a model never learns it exists and never tries."""
    allowed = ALLOWLIST.get(caller, frozenset())
    return sorted(t.name for t in _REGISTRY.values() if t.group in allowed)


def _trace(caller: str, tool: str, *, allowed: bool) -> None:
    """Surface the call in the Langfuse trace. Never load-bearing."""
    try:
        from app.observability import record_tool_call

        record_tool_call(caller, tool, allowed)
    except Exception:  # noqa: BLE001 - tracing must not break a tool call
        log.debug("tool tracing failed", exc_info=True)


def call(caller: Caller, name: str, /, **kwargs: Any) -> Any:
    """Invoke a tool as a caller. The single entry point; there is no other.

    Every call is audited so it can be surfaced in the Langfuse trace (TC-1005).
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ToolDenied(str(caller), name, "unknown")

    allowed = ALLOWLIST.get(caller, frozenset())
    if entry.group not in allowed:
        _AUDIT.append(
            {"caller": str(caller), "tool": name, "group": str(entry.group), "allowed": False}
        )
        log.warning("DENIED %s -> %s (%s)", caller, name, entry.group)
        _trace(str(caller), name, allowed=False)
        raise ToolDenied(str(caller), name, str(entry.group))

    _AUDIT.append({"caller": str(caller), "tool": name, "group": str(entry.group), "allowed": True})
    _trace(str(caller), name, allowed=True)
    return entry.fn(**kwargs)


def audit_log() -> list[dict[str, Any]]:
    return list(_AUDIT)


def clear_audit() -> None:
    _AUDIT.clear()


def registered() -> list[str]:
    return sorted(_REGISTRY)
