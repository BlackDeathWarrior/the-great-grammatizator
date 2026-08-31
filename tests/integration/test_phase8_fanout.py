"""Phase 8: fan out to all seven formats.

The point of this phase is what it does NOT contain: no per-format pipeline
code was added between Phase 7 and here. Everything below is driven by the
registry entries written in Phase 6, which is itself the proof TC-0302 asks for.
"""

import json

import pytest

from app.agents import generator
from app.formats import registry
from app.gateway import router
from app.graph import build
from app.graph.state import (
    AnalysisResult,
    ArtefactStatus,
    Chunk,
    ContentObject,
    JobStatus,
    Parameters,
    Provenance,
)

pytestmark = pytest.mark.integration

ALL_SEVEN = [
    "video_package",
    "linkedin_post",
    "twitter_x",
    "advisory",
    "infographic",
    "exec_summary",
    "presentation",
]

CONTENT = ContentObject(
    source_id="s_fan",
    source_hash="sha256:fanout",
    source_type="pdf",
    title="Critical auth bypass",
    text="A vulnerability rated 9.1 allows bypass. Fixed in 22.7R2.6.",
    chunks=[
        Chunk(id="c1", text="rated 9.1 out of 10", source_id="s_fan"),
        Chunk(id="c2", text="fixed in 22.7R2.6", source_id="s_fan"),
    ],
)

ANALYSIS = AnalysisResult(objective="inform", audience="general public")
CLAIMS = [{"text": "rated 9.1", "chunk_id": "c1"}, {"text": "fixed in 22.7R2.6", "chunk_id": "c2"}]


def _valid_body(format_id: str) -> dict:
    """A schema-valid payload per format. Shapes come from the Phase 6 schemas."""
    bodies = {
        "linkedin_post": {
            "hook": "Check your version today.",
            "body": "A critical bypass is being exploited.",
            "call_to_action": "Upgrade to 22.7R2.6.",
            "hashtags": ["#CyberSecurity"],
        },
        "twitter_x": {"tweets": ["Critical bypass. Patch to 22.7R2.6 now."], "hashtags": ["#Sec"]},
        "exec_summary": {
            "title": "Critical bypass",
            "bottom_line": "Patch immediately.",
            "key_points": ["Rated 9.1", "Fix available", "Exploited"],
            "recommended_action": "Upgrade to 22.7R2.6.",
            "residual_risk": "Unpatched instances stay exposed.",
        },
        "advisory": {
            "title": "Critical authentication bypass",
            "severity": "critical",
            "summary": "Authentication can be bypassed.",
            "affected": ["NetGuard < 22.7R2.6"],
            "recommendations": ["Upgrade to 22.7R2.6."],
            "references": [],
        },
        "infographic": {
            "headline": "Critical bypass: patch now",
            "panels": [
                {
                    "heading": "Severity",
                    "stat": "9.1/10",
                    "caption": "CVSS",
                    "icon_hint": "alert",
                },
                {
                    "heading": "Fix",
                    "stat": "22.7R2.6",
                    "caption": "Available",
                    "icon_hint": "check",
                },
                {
                    "heading": "Exposure",
                    "stat": "14,000",
                    "caption": "Instances",
                    "icon_hint": "globe",
                },
            ],
            "layout_recommendation": "Three columns.",
            "key_messaging": ["Patch today"],
        },
        "presentation": {
            "title": "Critical bypass briefing",
            "slides": [
                {"heading": "What happened", "bullets": ["Bypass found"], "speaker_notes": "Open."},
                {"heading": "Severity", "bullets": ["9.1/10"], "speaker_notes": "Explain CVSS."},
                {"heading": "Fix", "bullets": ["22.7R2.6"], "speaker_notes": "Name the version."},
                {"heading": "Action", "bullets": ["Patch now"], "speaker_notes": "Close."},
            ],
        },
        "video_package": {
            "title": "Critical bypass explained",
            "scenes": [
                {
                    "narration": "A critical vulnerability was disclosed.",
                    "visual": "Title card",
                    "on_screen_text": "CVSS 9.1",
                    "duration_sec": 80,
                    "source_chunks": ["c1"],
                },
                {
                    "narration": "A fix is available now.",
                    "visual": "Version callout",
                    "on_screen_text": "22.7R2.6",
                    "duration_sec": 80,
                    "source_chunks": ["c2"],
                },
                {
                    "narration": "Patch today.",
                    "visual": "Closing card",
                    "on_screen_text": "Patch now",
                    "duration_sec": 80,
                    "source_chunks": ["c2"],
                },
            ],
            "storyboard_notes": "Three beats.",
            "subtitles_srt": "1\n00:00:00,000 --> 00:01:20,000\nA critical vulnerability.\n",
            "visual_recommendations": ["Avoid film stills - separately licensed."],
            "discussion_prompts": ["What is your patch window?"],
        },
    }
    body = bodies[format_id]
    return {**body, "claims": CLAIMS}


@pytest.fixture
def all_pass(monkeypatch):
    """Every checker passes; every format returns its valid shape."""
    generated: list[str] = []

    async def fake(alias, messages, **kwargs):
        system = messages[0]["content"]
        prompt = messages[-1]["content"]

        if "verify whether a claim is supported" in system:
            return json.dumps({"claims": [{"index": i, "supported": True} for i in (1, 2)]})
        if "rate how well" in system.lower():
            return json.dumps({"score": 0.9, "reason": "fits"})
        if "policy concerns" in system:
            return json.dumps({"safe": True, "reason": "clean"})

        # Identify the format from its prompt, since the registry drives which
        # template was rendered.
        for fid in ALL_SEVEN:
            if _marker(fid) in prompt:
                generated.append(fid)
                return json.dumps(_valid_body(fid))
        raise AssertionError(f"unrecognised generation prompt: {prompt[:200]}")

    monkeypatch.setattr(router, "complete", fake)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)
    return generated


def _marker(format_id: str) -> str:
    """A phrase unique to each format's template."""
    return {
        "linkedin_post": "Write a LinkedIn post",
        "twitter_x": "Write a tweet or short thread",
        "exec_summary": "Write an executive briefing",
        "advisory": "Write a structured advisory",
        "infographic": "Design infographic content",
        "presentation": "Build a slide deck",
        "video_package": "Produce a VIDEO PACKAGE",
    }[format_id]


@pytest.mark.p0
async def test_all_seven_formats_produce_artefacts(all_pass):
    """TC-1104: all seven artefacts, one analysis, no cross-contamination."""
    analyses = []

    async def counting(content, parameters, **kwargs):
        analyses.append(1)
        return ANALYSIS

    import app.agents.analysis as analysis_module

    original = analysis_module.analyse
    analysis_module.analyse = counting
    try:
        result = await build.run_job("j_seven", CONTENT, Parameters(), ALL_SEVEN)
    finally:
        analysis_module.analyse = original

    assert set(result["artefacts"]) == set(ALL_SEVEN)
    for fid, artefact in result["artefacts"].items():
        assert artefact.status is ArtefactStatus.PASSED, f"{fid}: {artefact.error}"

    # TC-0402: one analysis for seven formats.
    assert len(analyses) == 1

    # TC-0606/0608: no retry leaked between artefacts.
    assert all(a.retry_count == 0 for a in result["artefacts"].values())
    assert result["status"] is JobStatus.DONE


@pytest.mark.p0
async def test_one_generator_failing_does_not_stop_the_others(monkeypatch, all_pass):
    """TC-0407: the other six still complete; the failed one is marked."""
    original = router.complete

    async def fail_advisory(alias, messages, **kwargs):
        if _marker("advisory") in messages[-1]["content"]:
            raise router.ProviderError("simulated 429 for advisory only")
        return await original(alias, messages, **kwargs)

    monkeypatch.setattr(router, "complete", fail_advisory)

    result = await build.run_job("j_partial", CONTENT, Parameters(), ALL_SEVEN, analysis=ANALYSIS)

    assert result["artefacts"]["advisory"].status is ArtefactStatus.FAILED
    assert result["artefacts"]["advisory"].provider_error_count == 1
    # TC-0609: a provider error never touches the QA budget.
    assert result["artefacts"]["advisory"].retry_count == 0

    others = [a for f, a in result["artefacts"].items() if f != "advisory"]
    assert len(others) == 6
    assert all(a.status is ArtefactStatus.PASSED for a in others)


@pytest.mark.p0
async def test_tweet_over_280_fails_deterministically_in_the_fanout(monkeypatch, all_pass):
    """TC-0504 inside a real job: the deterministic checker still bites."""
    original = router.complete

    async def long_tweet(alias, messages, **kwargs):
        if _marker("twitter_x") in messages[-1]["content"]:
            body = _valid_body("twitter_x")
            body["tweets"] = ["x" * 281]
            return json.dumps(body)
        return await original(alias, messages, **kwargs)

    monkeypatch.setattr(router, "complete", long_tweet)

    artefact, _ = await build.run_artefact(
        "twitter_x", CONTENT, ANALYSIS, Parameters(), job_id="j_tweet"
    )

    # It retried on the format failure, exhausted the budget, and blocked.
    assert artefact.retry_count >= 1
    from app.graph.state import CheckerName

    fmt = artefact.qa_result.by_checker(CheckerName.FORMAT)
    assert fmt.passed is False
    assert "281" in fmt.fix_notes[0]


@pytest.mark.p1
async def test_commentary_mode_reaches_every_generator(monkeypatch):
    """TC-0409: a copyrighted source switches generation to paraphrase."""
    seen: list[str] = []

    async def capture(alias, messages, **kwargs):
        system = messages[0]["content"]
        prompt = messages[-1]["content"]
        if "communications writer" in system:
            seen.append(prompt)
            for fid in ALL_SEVEN:
                if _marker(fid) in prompt:
                    return json.dumps(_valid_body(fid))
        if "verify whether a claim is supported" in system:
            return json.dumps({"claims": [{"index": i, "supported": True} for i in (1, 2)]})
        if "rate how well" in system.lower():
            return json.dumps({"score": 0.9, "reason": "fits"})
        return json.dumps({"safe": True, "reason": "clean"})

    monkeypatch.setattr(router, "complete", capture)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    copyrighted = AnalysisResult(
        objective="support close reading",
        audience="undergraduate students",
        provenance=Provenance.LITERARY_COPYRIGHTED,
    )
    await build.run_artefact("video_package", CONTENT, copyrighted, Parameters(), job_id="j_lit")

    assert seen, "no generation prompt captured"
    assert "COMMENTARY MODE" in seen[0]
    assert "paraphrase" in seen[0].lower()


@pytest.mark.p0
def test_no_per_format_branching_in_the_pipeline():
    """Invariant 4: if you are editing a switch statement, you are doing it wrong.

    The pipeline modules must not name individual formats. Everything they need
    comes from the registry entry.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    pipeline = [
        "app/graph/build.py",
        "app/agents/generator.py",
        "app/agents/qa/runner.py",
        "app/agents/qa/format_check.py",
        "app/agents/verdict.py",
    ]
    offenders = []
    for rel in pipeline:
        code = (root / rel).read_text(encoding="utf-8")
        for fid in ALL_SEVEN:
            if f'"{fid}"' in code or f"'{fid}'" in code:
                offenders.append(f"{rel} names {fid}")
    assert not offenders, "pipeline branches on format id: " + "; ".join(offenders)


@pytest.mark.p1
def test_every_registered_format_has_a_working_body_fixture():
    """Guards this test file itself against registry drift."""
    assert set(ALL_SEVEN) == set(registry.ids())
