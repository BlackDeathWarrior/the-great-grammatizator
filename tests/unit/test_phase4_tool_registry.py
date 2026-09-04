"""Phase 4: the allowlist is structural.

Every denial here is enforced by the registry. None of it depends on a prompt
asking an agent nicely.
"""

import inspect

import pytest

import app.tools  # noqa: F401 - import registers every tool
from app.tools.registry import (
    ALLOWLIST,
    Caller,
    ToolDenied,
    ToolGroup,
    audit_log,
    available,
    call,
    clear_audit,
    registered,
)


@pytest.fixture(autouse=True)
def _clean_audit():
    clear_audit()
    yield
    clear_audit()


# --- Invariant 6: the load-bearing denial -----------------------------------


@pytest.mark.p0
def test_grounding_checker_cannot_call_web_search():
    """TC-1001. The security story of the whole demo.

    With web search the grounding checker verifies claims against the internet
    instead of the source document, silently defeating itself. The denial must
    come from the registry - a prompt instruction is not a control.
    """
    with pytest.raises(ToolDenied) as exc:
        call(Caller.GROUNDING_CHECKER, "web_search", query="CVE-2026-0001", k=3)

    assert exc.value.tool == "web_search"
    assert exc.value.caller == "grounding_checker"


@pytest.mark.p0
def test_web_search_is_invisible_to_the_grounding_checker():
    """Stronger than refusal: the checker never learns the tool exists.

    A model that cannot see a tool never tries to call it, so there is no
    refusal to argue with.
    """
    assert "web_search" not in available(Caller.GROUNDING_CHECKER)
    assert "web_search" in available(Caller.INPUT_ANALYSIS)


@pytest.mark.p0
def test_grounding_checker_still_has_retrieval():
    """Denying web search must not deny the checker its actual job."""
    assert "search_chunks" in available(Caller.GROUNDING_CHECKER)
    assert "get_chunk" in available(Caller.GROUNDING_CHECKER)


# --- generators are read-only -----------------------------------------------


@pytest.mark.p0
def test_no_write_or_delete_tool_is_registered_at_all():
    """TC-1002: agents read chunks; they never write them (Invariant 2).

    Enforced by absence: there is no write tool to allowlist, so no allowlist
    mistake can ever expose one.
    """
    forbidden = ("write", "delete", "upsert", "drop", "update", "insert")
    offenders = [n for n in registered() if any(f in n.lower() for f in forbidden)]
    assert not offenders, f"mutating tools registered: {offenders}"


@pytest.mark.p0
def test_output_generator_cannot_web_search():
    """Generators write from the source and the analysis, not from the web."""
    with pytest.raises(ToolDenied):
        call(Caller.OUTPUT_GENERATOR, "web_search", query="anything")


@pytest.mark.p0
def test_output_generator_cannot_render():
    """Rendering belongs to export; a generator producing files would bypass QA."""
    with pytest.raises(ToolDenied):
        call(
            Caller.OUTPUT_GENERATOR,
            "render_document",
            artefact={},
            renderer="pdf",
            out_dir="/tmp",
        )


# --- no raw client ----------------------------------------------------------


@pytest.mark.p0
def test_no_tool_exposes_a_raw_client():
    """TC-1004: agents cannot reach a bare Qdrant client.

    Every retrieval tool must take a job_id, which is what makes cross-job
    access structurally impossible rather than merely discouraged.
    """
    from app.tools import retrieval

    for name in ("search_chunks", "get_chunk"):
        fn = getattr(retrieval, name)
        params = list(inspect.signature(fn).parameters)
        assert params[0] == "job_id", f"{name} must be scoped by job_id, got {params}"


@pytest.mark.p0
def test_cross_job_retrieval_returns_nothing():
    """TC-1003: another job's id yields no data.

    Returning empty rather than raising also avoids leaking whether that job
    exists at all.
    """
    from app.tools import retrieval

    retrieval.bind_job("job_a", ["s_aaa"])
    try:
        assert call(Caller.OUTPUT_GENERATOR, "search_chunks", job_id="job_b", query="x") == []
        assert call(Caller.GROUNDING_CHECKER, "get_chunk", job_id="job_b", chunk_id="c1") is None
    finally:
        retrieval.unbind_job("job_a")


# --- allowlist shape --------------------------------------------------------


@pytest.mark.p1
def test_allowlist_matches_the_architecture_table():
    """ARCHITECTURE.md sec.5 states this table; the code must not drift from it. (TC-1006)"""
    assert ALLOWLIST[Caller.INPUT_ANALYSIS] == frozenset(
        {ToolGroup.RETRIEVAL, ToolGroup.WEB_SEARCH}
    )
    assert ALLOWLIST[Caller.OUTPUT_GENERATOR] == frozenset({ToolGroup.RETRIEVAL})
    assert ALLOWLIST[Caller.GROUNDING_CHECKER] == frozenset({ToolGroup.RETRIEVAL})
    assert ALLOWLIST[Caller.EXPORT] == frozenset({ToolGroup.RENDER, ToolGroup.MEDIA})


@pytest.mark.p1
def test_unknown_tool_is_denied_not_crashed():
    with pytest.raises(ToolDenied):
        call(Caller.INPUT_ANALYSIS, "definitely_not_a_tool")


@pytest.mark.p1
def test_deterministic_checkers_get_no_tools():
    """Tone and safety judge the artefact text itself; they need nothing. (TC-1007)"""
    assert available(Caller.TONE_CHECKER) == []
    assert available(Caller.SAFETY_CHECKER) == []


# --- audit ------------------------------------------------------------------


@pytest.mark.p1
@pytest.mark.integration  # exercises a real retrieval call
def test_every_call_is_audited_including_denials():
    """TC-1005: every tool call appears in the trace.

    Denials especially - an attempted breach that left no record would be the
    one worth knowing about.
    """
    from app.tools import retrieval

    retrieval.bind_job("job_x", ["s_x"])
    try:
        call(Caller.OUTPUT_GENERATOR, "search_chunks", job_id="job_x", query="q")
    finally:
        retrieval.unbind_job("job_x")

    with pytest.raises(ToolDenied):
        call(Caller.GROUNDING_CHECKER, "web_search", query="q")

    log = audit_log()
    assert {
        "caller": "output_generator",
        "tool": "search_chunks",
        "group": "retrieval",
        "allowed": True,
    } in log
    assert {
        "caller": "grounding_checker",
        "tool": "web_search",
        "group": "web_search",
        "allowed": False,
    } in log
