"""The guardrails must actually run, not merely exist.

guardrails.py claimed in its own docstring that these "run on every prompt
before it reaches a provider". They did not: check_scope() and redact() had
zero call sites anywhere in the app. Nothing asserted they ran, which is
exactly how they drifted into dead code while the docstring kept promising
otherwise.

These tests exist so that cannot happen silently again.
"""

import pytest

from app.gateway import guardrails, router
from app.prompts import loader


@pytest.mark.p0
async def test_an_oversized_prompt_is_refused_before_dispatch(monkeypatch):
    """It must never reach the provider, be billed, and come back as a 400."""
    dispatched = []

    class _FakeRouter:
        async def acompletion(self, **kwargs):
            dispatched.append(kwargs)
            raise AssertionError("an oversized prompt was sent to the provider")

    monkeypatch.setattr(router, "get_router", lambda: _FakeRouter())

    huge = "x" * 500_000
    with pytest.raises(guardrails.PromptTooLarge, match="over the"):
        await router.complete(router.FAST, [{"role": "user", "content": huge}])

    assert dispatched == []


@pytest.mark.p0
async def test_prompt_too_large_is_not_a_provider_error(monkeypatch):
    """Nothing is wrong with the provider, so it must not retry with backoff."""

    class _FakeRouter:
        async def acompletion(self, **kwargs):
            raise AssertionError("should not dispatch")

    monkeypatch.setattr(router, "get_router", lambda: _FakeRouter())

    with pytest.raises(guardrails.PromptTooLarge):
        await router.complete(router.FAST, [{"role": "user", "content": "x" * 500_000}])

    assert not issubclass(guardrails.PromptTooLarge, router.ProviderError)


@pytest.mark.p1
async def test_a_normal_prompt_still_passes_the_guardrail(monkeypatch):
    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    class _FakeRouter:
        async def acompletion(self, **kwargs):
            return _Resp()

    monkeypatch.setattr(router, "get_router", lambda: _FakeRouter())

    assert await router.complete(router.FAST, [{"role": "user", "content": "hello"}]) == "ok"


# === the chunk listing is capped ============================================


@pytest.mark.p0
def test_the_shared_prompt_caps_the_chunk_listing():
    """Every chunk at 400 chars, uncapped, is how prompts got too large."""
    from app.graph.state import Chunk, ContentObject

    chunks = [
        Chunk(id=f"c{i}", source_id="s_1", text=f"chunk {i} " + "y" * 300) for i in range(1, 200)
    ]
    content = ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Long",
        text="...",
        chunks=chunks,
    )

    from app.formats import registry
    from app.graph.state import AnalysisResult, Parameters

    spec = registry.get("linkedin_post")
    rendered = loader.render(
        spec.prompt_template,
        content=content,
        analysis=AnalysisResult(objective="inform", audience="general public"),
        parameters=Parameters(),
        constraints=spec.constraints,
        fix_notes=[],
    )

    assert "[c60]" in rendered, "the cap should not be lower than MAX_PROMPT_CHUNKS"
    assert "[c61]" not in rendered, "the chunk listing is not capped"
    # Silently dropping chunks would break grounding: a claim can only cite
    # what the model was shown, so the omission must be stated.
    assert "further chunks omitted" in rendered
    assert "will fail the grounding check" in rendered


@pytest.mark.p1
def test_a_short_source_lists_every_chunk_with_no_omission_notice():
    from app.formats import registry
    from app.graph.state import AnalysisResult, Chunk, ContentObject, Parameters

    content = ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Short",
        text="...",
        chunks=[Chunk(id="c1", source_id="s_1", text="only chunk")],
    )
    spec = registry.get("linkedin_post")
    rendered = loader.render(
        spec.prompt_template,
        content=content,
        analysis=AnalysisResult(objective="inform", audience="general public"),
        parameters=Parameters(),
        constraints=spec.constraints,
        fix_notes=[],
    )

    assert "[c1]" in rendered
    assert "further chunks omitted" not in rendered


# === redact is used where raw exception text reaches the operator ===========


@pytest.mark.p1
def test_redact_masks_the_patterns_it_claims_to():
    dirty = "failed for alice@example.com with key sk-abcdefghijklmnop1234"
    clean = guardrails.redact(dirty)

    assert "alice@example.com" not in clean
    assert "sk-abcdefghijklmnop1234" not in clean
    assert "[redacted]" in clean


@pytest.mark.p1
def test_the_worker_redacts_the_operator_message():
    """The failure string is rendered straight into the dashboard."""
    import inspect

    from app import worker

    source = inspect.getsource(worker.run_job_task)
    assert "guardrails.redact" in source, "raw exception text reaches the operator unredacted"
