"""Web search. Available to input analysis ONLY.

The grounding checker must never reach this. With web search it verifies claims
against the internet instead of the source document, silently defeating itself
(Invariant 6, TC-1001). That denial lives in the registry allowlist, not here
and not in any prompt - a tool cannot be trusted to refuse its own caller.
"""

from __future__ import annotations

import logging

from app.tools.registry import ToolGroup, tool

log = logging.getLogger(__name__)


@tool(
    "web_search",
    ToolGroup.WEB_SEARCH,
    "Search the web to enrich or corroborate source material. Input analysis only.",
)
def web_search(query: str, k: int = 5) -> list[dict]:
    """Return search results.

    Deliberately a stub in demo scope: no search provider is configured, and the
    architecture treats enrichment as optional (UC-06 is a "should"). Returning
    an empty list keeps analysis working offline. The security property under
    test - that grounding cannot reach this at all - holds regardless of whether
    the implementation is real, which is exactly why the denial belongs in the
    registry.
    """
    log.info("web_search stub called: %r", query)
    return []
