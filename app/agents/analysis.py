"""Input analysis. Runs ONCE per job, cached on the job, shared by every generator.

Invariant 3. This is what makes selecting seven formats cost roughly the same
analysis as selecting one (TC-0402, TC-0408).

Three subagents per ARCHITECTURE.md sec.6 - web search enrichment, content
analysis (facts, entities), input analysis (objective, audience) - collapsed
into a single structured call. Three separate calls would triple the cost of
the one step the architecture insists must be cheap, and the outputs are read
as one object anyway.

Source provenance is classified here. A literary_copyrighted source switches
generation into commentary mode downstream.
"""

from __future__ import annotations

import json
import logging

from app.gateway import router
from app.graph.state import AnalysisResult, ContentObject, Parameters, Provenance
from app.prompts import loader
from app.tools.registry import Caller, ToolDenied, call

log = logging.getLogger(__name__)

# Cap the text sent for analysis. The whole document is rarely needed to
# establish objective, audience and provenance, and long sources would blow the
# context on the one call every job pays for.
_ANALYSIS_CHARS = 12_000


async def analyse(
    content: ContentObject,
    parameters: Parameters,
    *,
    job_id: str = "",
    enrich: bool = True,
) -> AnalysisResult:
    """Produce the single AnalysisResult for a job."""
    enrichment: list[str] = []
    if enrich and job_id:
        enrichment = _enrich(job_id, content.title)

    prompt = loader.render(
        "input_analysis",
        content=content,
        parameters=parameters,
        excerpt=content.text[:_ANALYSIS_CHARS],
        enrichment=enrichment,
    )

    try:
        raw = await router.complete(
            router.FAST,
            [
                {"role": "system", "content": loader.system("input_analysis")},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        data = _parse(raw)
    except router.ProviderError as exc:
        # Analysis must not be able to fail a whole job on a provider hiccup.
        # A neutral analysis still lets every generator run.
        log.warning("analysis provider error, falling back to defaults: %s", exc)
        data = {}

    return AnalysisResult(
        objective=data.get("objective") or parameters.objective,
        audience=data.get("audience") or parameters.audience,
        key_facts=list(data.get("key_facts") or [])[:12],
        entities=list(data.get("entities") or [])[:20],
        enrichment=enrichment,
        provenance=_provenance(data.get("provenance")),
    )


def _enrich(job_id: str, query: str) -> list[str]:
    """Optional web enrichment (UC-06, a "should").

    Input analysis is the only caller permitted web search. Going through the
    registry rather than importing the tool directly is what keeps that true.
    """
    try:
        results = call(Caller.INPUT_ANALYSIS, "web_search", query=query, k=3)
    except ToolDenied:  # pragma: no cover - would mean the allowlist changed
        log.warning("web search denied to input analysis; check the allowlist")
        return []
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        log.warning("web search failed: %s", exc)
        return []
    return [r.get("snippet", "") for r in results if r.get("snippet")]


def _parse(raw: str) -> dict:
    try:
        data = json.loads(_strip_fence(raw))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log.warning("analysis returned non-JSON; using defaults")
        return {}


def _strip_fence(raw: str) -> str:
    """Some models wrap JSON in a markdown fence despite json_object mode."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _provenance(value: str | None) -> Provenance:
    """Unknown values fall back to ORIGINAL.

    Deliberately conservative in the safe direction: mislabelling a copyrighted
    source as original would skip commentary mode, so the model is given an
    explicit enum and anything unrecognised is treated as the ordinary case
    while the copyrighted label is honoured whenever it is returned.
    """
    try:
        return Provenance(value) if value else Provenance.ORIGINAL
    except ValueError:
        log.warning("unknown provenance %r; treating as original", value)
        return Provenance.ORIGINAL
