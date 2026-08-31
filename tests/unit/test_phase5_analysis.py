"""Phase 5: analysis runs once, is shared, and classifies provenance."""

import json

import pytest

from app.agents import analysis
from app.gateway import router
from app.graph.state import (
    AnalysisResult,
    Chunk,
    ContentObject,
    Parameters,
    Provenance,
    SourceType,
)
from app.prompts import loader


def _content(title: str = "Critical auth bypass") -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type=SourceType.PDF,
        title=title,
        text="A vulnerability rated 9.1 was fixed in 22.7R2.6. 14,000 instances affected.",
        chunks=[Chunk(id="c1", text="rated 9.1", source_id="s_1")],
    )


def _stub(monkeypatch, payload: dict, counter: list | None = None):
    async def fake(alias, messages, **kwargs):
        if counter is not None:
            counter.append(alias)
        return json.dumps(payload)

    monkeypatch.setattr(router, "complete", fake)


# --- Invariant 3: analysis runs once ----------------------------------------


@pytest.mark.p0
async def test_analysis_makes_exactly_one_model_call(monkeypatch):
    """TC-0402: input analysis runs exactly once per job.

    Seven selected formats must not cost seven analyses; that equivalence is
    the architectural claim the fan-out rests on.
    """
    calls: list[str] = []
    _stub(monkeypatch, {"objective": "inform", "audience": "public"}, calls)

    await analysis.analyse(_content(), Parameters(), job_id="j1", enrich=False)
    assert len(calls) == 1


@pytest.mark.p1
def test_analysis_result_is_frozen():
    """Shared by every generator, so it must not be mutable by any of them."""
    from pydantic import ValidationError

    result = AnalysisResult(objective="o", audience="a")
    with pytest.raises(ValidationError):
        result.objective = "changed"


# --- provenance -------------------------------------------------------------


@pytest.mark.p1
async def test_copyrighted_provenance_is_carried_through(monkeypatch):
    """Worked Example B: literary_copyrighted enables commentary mode."""
    _stub(
        monkeypatch,
        {
            "objective": "support close reading",
            "audience": "undergraduate students",
            "provenance": "literary_copyrighted",
        },
    )
    result = await analysis.analyse(_content(), Parameters(), enrich=False)
    assert result.provenance is Provenance.LITERARY_COPYRIGHTED
    assert result.commentary_mode is True


@pytest.mark.p1
async def test_unknown_provenance_falls_back_to_original(monkeypatch):
    _stub(monkeypatch, {"objective": "o", "audience": "a", "provenance": "nonsense"})
    result = await analysis.analyse(_content(), Parameters(), enrich=False)
    assert result.provenance is Provenance.ORIGINAL


# --- resilience -------------------------------------------------------------


@pytest.mark.p0
async def test_provider_error_does_not_fail_the_job(monkeypatch):
    """TC-0609 in spirit: a provider hiccup must not kill a healthy job.

    Analysis degrades to the operator's own parameters so every generator can
    still run.
    """

    async def boom(alias, messages, **kwargs):
        raise router.ProviderError("429 rate limited")

    monkeypatch.setattr(router, "complete", boom)

    params = Parameters(objective="drive awareness", audience="general public")
    result = await analysis.analyse(_content(), params, enrich=False)
    assert result.objective == "drive awareness"
    assert result.audience == "general public"


@pytest.mark.p1
async def test_malformed_json_falls_back_to_defaults(monkeypatch):
    async def junk(alias, messages, **kwargs):
        return "not json at all"

    monkeypatch.setattr(router, "complete", junk)
    result = await analysis.analyse(_content(), Parameters(), enrich=False)
    assert isinstance(result, AnalysisResult)


@pytest.mark.p1
async def test_fenced_json_is_parsed(monkeypatch):
    """Models wrap JSON in markdown fences despite json_object mode."""

    async def fenced(alias, messages, **kwargs):
        return '```json\n{"objective": "inform", "audience": "devs"}\n```'

    monkeypatch.setattr(router, "complete", fenced)
    result = await analysis.analyse(_content(), Parameters(), enrich=False)
    assert result.audience == "devs"


# --- alias, not provider ----------------------------------------------------


@pytest.mark.p0
async def test_analysis_requests_an_alias(monkeypatch):
    """Invariant 5: agents request "fast" or "long", never a provider."""
    calls: list[str] = []
    _stub(monkeypatch, {"objective": "o", "audience": "a"}, calls)
    await analysis.analyse(_content(), Parameters(), enrich=False)
    assert calls == [router.FAST]


# --- prompt versioning ------------------------------------------------------


@pytest.mark.p0
def test_prompts_are_files_not_literals():
    """Invariant 9: prompts live in versioned files."""
    assert loader.versions("input_analysis") == [1]
    assert loader.version_tag("input_analysis") == "input_analysis@v1"


@pytest.mark.p1
def test_prompt_renders_with_parameters():
    """TC-0203: the rendered prompt contains the selected audience and tone."""
    rendered = loader.render(
        "input_analysis",
        content=_content(),
        parameters=Parameters(audience="security leaders", tone="urgent"),
        excerpt="body text",
        enrichment=[],
    )
    assert "security leaders" in rendered
    assert "urgent" in rendered
    assert "body text" in rendered


@pytest.mark.p1
def test_missing_template_variable_fails_loudly():
    """StrictUndefined: a typo must not silently produce an empty prompt slot."""
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        loader.render("input_analysis", content=_content(), parameters=Parameters())


@pytest.mark.p1
def test_unknown_prompt_name_raises():
    with pytest.raises(FileNotFoundError):
        loader.latest_version("no_such_prompt")
