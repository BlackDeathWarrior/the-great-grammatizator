"""Phase 6: the registry is data. Adding a format touches no code."""

import json

import pytest
from jsonschema import Draft202012Validator

from app.formats import registry
from app.graph.state import AnalysisResult, Chunk, ContentObject, Parameters, SourceType
from app.prompts import loader

EXPECTED = {
    "video_package",
    "linkedin_post",
    "twitter_x",
    "advisory",
    "infographic",
    "exec_summary",
    "presentation",
}


def _content() -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type=SourceType.PDF,
        title="Critical auth bypass",
        text="body",
        chunks=[
            Chunk(id="c1", text="rated 9.1 out of 10", source_id="s_1"),
            Chunk(id="c2", text="fixed in 22.7R2.6", source_id="s_1"),
        ],
    )


# --- registry shape ---------------------------------------------------------


@pytest.mark.p0
def test_all_seven_formats_are_registered():
    assert set(registry.ids()) == EXPECTED


@pytest.mark.p0
def test_unknown_format_is_rejected_before_a_job_exists():
    """TC-0301: rejected before any job is created."""
    with pytest.raises(registry.UnknownFormat) as exc:
        registry.get("telepathy")
    assert "linkedin_post" in str(exc.value)


@pytest.mark.p0
def test_a_bad_id_rejects_the_whole_selection():
    """The operator must not silently get six artefacts having asked for seven."""
    with pytest.raises(registry.UnknownFormat):
        registry.validate_selection(["linkedin_post", "nonsense"])


@pytest.mark.p1
def test_empty_selection_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        registry.validate_selection([])


# --- Invariant 4: config, not code ------------------------------------------


@pytest.mark.p0
def test_every_format_supplies_everything_the_pipeline_needs():
    """Invariant 4: the registry entry alone drives generation.

    If any of these came from code, adding a format would mean a code change.
    """
    for spec in registry.all_formats():
        assert spec.prompt_template, f"{spec.id} has no template"
        assert spec.output_schema, f"{spec.id} has no schema"
        assert spec.model_alias in ("fast", "long"), f"{spec.id}: {spec.model_alias}"
        assert spec.renderer, f"{spec.id} has no renderer"
        assert spec.label, f"{spec.id} has no label"


@pytest.mark.p0
def test_adding_a_format_requires_no_code_change(tmp_path, monkeypatch):
    """TC-0302: a new JSON entry + template + schema is enough.

    Simulated by injecting an eighth format into the registry file and asserting
    it becomes fully usable without touching a single .py file.
    """
    original = json.loads(registry.REGISTRY_PATH.read_text(encoding="utf-8"))
    original["formats"].append(
        {
            "id": "press_release",
            "label": "Press Release",
            "prompt_template": "linkedin_post@v1",
            "output_schema": "linkedin_post.schema.json",
            "constraints": {"max_chars": 2000, "hashtags_max": 3},
            "model_alias": "fast",
            "renderer": "markdown",
        }
    )
    patched = tmp_path / "registry.json"
    patched.write_text(json.dumps(original), encoding="utf-8")

    monkeypatch.setattr(registry, "REGISTRY_PATH", patched)
    registry.reset_cache()
    try:
        assert "press_release" in registry.ids()
        spec = registry.get("press_release")
        assert spec.constraints["max_chars"] == 2000
        assert spec.schema()["required"]  # schema resolves
    finally:
        registry.reset_cache()


@pytest.mark.p1
def test_registry_supplies_the_model_alias():
    """TC-0303: long-form routes to `long`, social routes to `fast`."""
    assert registry.get("advisory").model_alias == "long"
    assert registry.get("exec_summary").model_alias == "long"
    assert registry.get("video_package").model_alias == "long"
    assert registry.get("linkedin_post").model_alias == "fast"
    assert registry.get("twitter_x").model_alias == "fast"


@pytest.mark.p0
def test_constraints_come_from_the_registry():
    """TC-0304: max_chars from the registry is what the checker will enforce."""
    assert registry.get("linkedin_post").constraints["max_chars"] == 3000
    assert registry.get("linkedin_post").constraints["hashtags_max"] == 5
    assert registry.get("twitter_x").constraints["max_chars_per_tweet"] == 280


# --- schemas ----------------------------------------------------------------


@pytest.mark.p0
def test_every_schema_is_valid_json_schema():
    for spec in registry.all_formats():
        Draft202012Validator.check_schema(spec.schema())


@pytest.mark.p0
def test_every_schema_requires_claims_with_chunk_ids():
    """TC-0403: this is what makes grounding cheap. (TC-0306)

    Without a required claims[] carrying chunk_ids, the checker would have to
    search the whole document instead of verifying against the cited chunk.
    """
    for spec in registry.all_formats():
        schema = spec.schema()
        assert "claims" in schema["required"], f"{spec.id} does not require claims"
        claim = schema["properties"]["claims"]["items"]
        assert set(claim["required"]) == {"text", "chunk_id"}, spec.id


@pytest.mark.p1
def test_worked_example_shapes_match_the_docs():
    """USE-CASES.md Example A gives these shapes verbatim."""
    linkedin = registry.get("linkedin_post").schema()["properties"]
    assert {"hook", "body", "call_to_action", "hashtags"} <= set(linkedin)

    exec_summary = registry.get("exec_summary").schema()["properties"]
    assert {
        "title",
        "bottom_line",
        "key_points",
        "recommended_action",
        "residual_risk",
    } <= set(exec_summary)


@pytest.mark.p1
def test_video_package_shape_is_a_package_not_a_video():
    """TC-0410, Invariant 10. Script, storyboard, narration, subtitles - all text."""
    props = registry.get("video_package").schema()["properties"]
    assert {"scenes", "storyboard_notes", "subtitles_srt", "visual_recommendations"} <= set(props)
    scene = props["scenes"]["items"]["properties"]
    assert {"narration", "visual", "on_screen_text", "duration_sec"} <= set(scene)


# --- templates --------------------------------------------------------------


@pytest.mark.p0
def test_every_format_has_a_versioned_template_that_renders():
    """Invariant 9: prompts are versioned files, and every one must actually work."""
    for spec in registry.all_formats():
        rendered = loader.render(
            spec.prompt_template,
            content=_content(),
            analysis=AnalysisResult(objective="inform", audience="public"),
            parameters=Parameters(),
            constraints=spec.constraints,
            fix_notes=[],
        )
        assert rendered, f"{spec.id} rendered empty"
        # The grounding contract must reach every generator.
        assert "chunk_id" in rendered, f"{spec.id} omits the grounding contract"
        assert "[c1]" in rendered, f"{spec.id} does not list available chunks"


@pytest.mark.p0
def test_fix_notes_reach_the_retry_prompt():
    """TC-0604: the retry prompt carries the specific failing claim."""
    rendered = loader.render(
        "linkedin_post@v1",
        content=_content(),
        analysis=AnalysisResult(objective="inform", audience="public"),
        parameters=Parameters(),
        constraints=registry.get("linkedin_post").constraints,
        fix_notes=["Soften the closing line. Keep urgency factual."],
    )
    assert "Soften the closing line" in rendered


@pytest.mark.p1
def test_commentary_mode_changes_the_prompt():
    """Worked Example B: copyrighted source must switch to paraphrase."""
    from app.graph.state import Provenance

    kwargs = dict(
        content=_content(),
        parameters=Parameters(),
        constraints=registry.get("video_package").constraints,
        fix_notes=[],
    )
    plain = loader.render(
        "video_package@v1",
        analysis=AnalysisResult(objective="o", audience="a"),
        **kwargs,
    )
    commentary = loader.render(
        "video_package@v1",
        analysis=AnalysisResult(
            objective="o", audience="a", provenance=Provenance.LITERARY_COPYRIGHTED
        ),
        **kwargs,
    )
    assert "COMMENTARY MODE" not in plain
    assert "COMMENTARY MODE" in commentary
    assert "paraphrase" in commentary.lower()


@pytest.mark.p1
def test_constraints_are_interpolated_into_the_prompt():
    """The model must be told the limit the deterministic checker will enforce."""
    rendered = loader.render(
        "twitter_x@v1",
        content=_content(),
        analysis=AnalysisResult(objective="o", audience="a"),
        parameters=Parameters(),
        constraints=registry.get("twitter_x").constraints,
        fix_notes=[],
    )
    assert "280" in rendered


@pytest.mark.p0
def test_shared_prompt_shows_the_literal_claims_shape():
    """TC-0411: the claims contract must show its JSON shape, not just describe it.

    Regression. Prose alone was not enough: models returned
    {"claim":..., "citations":...} and exhausted every parse retry on every
    format. The literal example fixed it. This test exists so nobody tidies the
    example away.
    """
    shared = (loader.PROMPTS_DIR / "_shared.jinja").read_text(encoding="utf-8")

    assert '"text"' in shared and '"chunk_id"' in shared, "claims keys not shown literally"
    assert '"claims": [' in shared, "no example claims array in the shared block"

    # And it must actually reach a rendered generator prompt.
    rendered = loader.render(
        "linkedin_post@v1",
        content=_content(),
        analysis=AnalysisResult(objective="inform", audience="public"),
        parameters=Parameters(),
        constraints=registry.get("linkedin_post").constraints,
        fix_notes=[],
    )
    assert '"chunk_id": "c1"' in rendered
