"""Phase 11: observability, the eval harness, and the discipline sweep.

The final section re-asserts the P0s the plan flagged as "really assertions
about discipline" - the ones that fail silently if nobody checks them.
"""

import pathlib
import re

import pytest

from app import observability
from app.eval import harness
from app.formats import registry
from app.graph.state import Artefact, CheckerName, CheckerResult, Claim, QAResult

ROOT = pathlib.Path(__file__).resolve().parents[2]

repo_only = pytest.mark.skipif(
    not (ROOT / "app").exists(), reason="repo not present (running inside the image)"
)


@pytest.fixture(autouse=True)
def _reset():
    observability.reset()
    yield
    observability.reset()


# --- observability ----------------------------------------------------------


@pytest.mark.p1
def test_model_calls_are_counted_by_alias():
    """TC-1204: log the actual call count rather than estimating it."""
    observability.record_model_call("fast")
    observability.record_model_call("fast")
    observability.record_model_call("long")

    m = observability.metrics()
    assert m["model_calls"] == 3
    assert m["by_alias"] == {"fast": 2, "long": 1}


@pytest.mark.p1
def test_tracing_without_langfuse_configured_is_a_noop():
    """Tracing must never be load-bearing.

    An unconfigured or unreachable Langfuse must not fail a job.
    """
    with observability.trace("generation", output_type="linkedin_post") as span:
        span["output"] = {"ok": True}

    assert observability.metrics()["latency"]["generation"]["count"] == 1


@pytest.mark.p1
def test_latency_is_summarised_per_span():
    for _ in range(3):
        with observability.trace("qa"):
            pass
    latency = observability.metrics()["latency"]["qa"]
    assert latency["count"] == 3
    assert latency["mean_ms"] >= 0


@pytest.mark.p1
def test_tool_calls_including_denials_are_recorded():
    """TC-1005: every tool call appears in the trace, denials especially."""
    from app.tools.registry import Caller, ToolDenied, call

    with pytest.raises(ToolDenied):
        call(Caller.GROUNDING_CHECKER, "web_search", query="x")

    assert observability.metrics()["tool_calls"] >= 1


# --- eval harness -----------------------------------------------------------


@pytest.mark.p1
def test_eval_set_loads_and_names_real_formats():
    cases = harness.load_set()
    assert cases
    for case in cases:
        registry.get(case.output_type)  # raises if the format is unknown
        assert case.prompt_version, f"{case.output_type} has no pinned prompt version"


@pytest.mark.p1
def test_expected_properties_are_actually_asserted():
    """A failing artefact must be reported as failing."""
    artefact = Artefact(
        output_type="linkedin_post",
        content={"hook": "h"},
        claims=[Claim(text="t", chunk_id="c1")],
    )
    failures = harness._assert_properties(
        {"min_claims": 3, "required_keys": ["hook", "body", "call_to_action"]}, artefact
    )
    assert any("claims" in f for f in failures)
    assert any("missing keys" in f for f in failures)


@pytest.mark.p1
def test_unrecognised_assertion_is_a_failure_not_a_silent_pass():
    """An eval that ignores an assertion it does not understand is worse than none."""
    artefact = Artefact(output_type="linkedin_post", content={})
    failures = harness._assert_properties({"vibes": "immaculate"}, artefact)
    assert any("unrecognised" in f for f in failures)


@pytest.mark.p1
def test_report_summarises_pass_rate_by_checker():
    """TEST-CASES.md: report pass rate by checker, mean retries, latency, calls."""
    case = harness.EvalCase(source_id="s", output_type="linkedin_post")
    results = [
        harness.CaseResult(
            case=case,
            passed=True,
            retries=0,
            latency_ms=100.0,
            checker_passes={"grounding": True, "tone": True},
        ),
        harness.CaseResult(
            case=case,
            passed=False,
            retries=2,
            latency_ms=300.0,
            checker_passes={"grounding": True, "tone": False},
            failures=["tone 0.61, expected >= 0.75"],
        ),
    ]

    report = harness.report(results)
    assert report["cases"] == 2
    assert report["pass_rate"] == 0.5
    assert report["pass_rate_by_checker"]["grounding"] == 1.0
    assert report["pass_rate_by_checker"]["tone"] == 0.5
    assert report["mean_retries"] == 1.0
    assert report["mean_latency_ms"] == 200
    assert report["failures"][0]["reasons"]


@pytest.mark.p1
def test_tone_threshold_assertion_uses_the_recorded_score():
    """Worked Example A regression: 0.61 fails, 0.84 passes (TC-0611)."""

    def artefact_with_tone(score: float) -> Artefact:
        return Artefact(
            output_type="linkedin_post",
            content={"hook": "h"},
            qa_result=QAResult(
                results=[CheckerResult(checker=CheckerName.TONE, passed=True, score=score)]
            ),
        )

    assert harness._assert_properties({"min_tone": 0.75}, artefact_with_tone(0.61))
    assert harness._assert_properties({"min_tone": 0.75}, artefact_with_tone(0.84)) == []


# === the discipline sweep ===================================================
# These P0s fail silently if nobody asserts them. Each maps to an invariant.


@pytest.mark.p0
@repo_only
def test_no_llm_call_anywhere_in_the_ingestion_path():
    """TC-0117, Invariant 1.

    Checked structurally as well as behaviourally: no module under app/ingest
    may import the gateway at all. embed.py calls the provider SDK directly by
    design, which is a different thing and is asserted separately.
    """
    offenders = []
    for path in (ROOT / "app" / "ingest").rglob("*.py"):
        code = path.read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        if "app.gateway" in code or "from litellm" in code:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"ingestion imports the model gateway: {offenders}"


@pytest.mark.p0
@repo_only
def test_no_llm_call_in_the_deterministic_checkers():
    """TC-0503. Format and source-reuse are regex, length and n-gram work."""
    offenders = []
    for name in ("format_check.py", "reuse.py"):
        code = (ROOT / "app" / "agents" / "qa" / name).read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        if "router.complete" in code or "app.gateway" in code:
            offenders.append(name)
    assert not offenders, f"a deterministic checker calls a model: {offenders}"


@pytest.mark.p0
@repo_only
def test_no_generative_video_api_in_the_export_path():
    """TC-0705, Invariant 10. Video is TTS + ffmpeg, never generated frames."""
    banned = ("runway", "pika", "sora", "veo", "text-to-video", "video_generation")
    offenders = []
    for path in (ROOT / "app" / "export").rglob("*.py"):
        code = path.read_text(encoding="utf-8").lower()
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        for token in banned:
            if token in code:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"export references generative video: {offenders}"


@pytest.mark.p0
@repo_only
def test_prompts_are_files_not_string_literals():
    """Invariant 9. A prompt in a string literal cannot be versioned or evalled."""
    prompts = ROOT / "app" / "prompts" / "templates"
    assert list(prompts.glob("*.jinja")), "no prompt templates found"

    # Every registry entry must resolve to a real, versioned file.
    from app.prompts import loader

    for spec in registry.all_formats():
        name, version = loader.resolve(spec.prompt_template)
        assert (prompts / f"{name}@v{version}.jinja").exists(), spec.id


@pytest.mark.p0
@repo_only
def test_the_three_failure_counters_stay_distinct():
    """Invariant 8, TC-0405/0609.

    Only retry_count may gate the 3-strike stop. If any code path incremented
    them together the separation would be decorative.
    """
    build = (ROOT / "app" / "graph" / "build.py").read_text(encoding="utf-8")

    # Where parse and provider failures are handled, retry_count must not move.
    for marker in ("parse_retry_count += 1", "provider_error_count += 1"):
        assert marker in build, f"{marker} not found - counters may have merged"

    verdict = (ROOT / "app" / "agents" / "verdict.py").read_text(encoding="utf-8")
    assert "retry_count += 1" in verdict, "the QA counter is not incremented by the verdict"
    assert "parse_retry_count" not in verdict, "the verdict touches the parse counter"
    assert "provider_error_count" not in verdict, "the verdict touches the provider counter"
