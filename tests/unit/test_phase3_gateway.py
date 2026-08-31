"""Phase 3: the gateway is the only place a provider is named, and the cache
key is built from the right things."""

import pathlib
import re

import pytest

from app.gateway import cache, guardrails, router

ROOT = pathlib.Path(__file__).resolve().parents[2]

repo_only = pytest.mark.skipif(
    not (ROOT / "app").exists(), reason="repo not present (running inside the image)"
)


# --- Invariant 5: alias, not provider ---------------------------------------

# Names that must not appear outside the router. Deliberately includes the
# vendor names AND the litellm prefixes that encode them.
PROVIDER_TOKENS = [
    "groq",
    "gemini",
    "openrouter",
    "anthropic",
    "openai/",
    "llama-3",
    "gpt-4",
    "claude-",
]

# app/ingest/embed.py is exempt: ARCHITECTURE.md sec.2 requires embeddings to
# call the provider SDK DIRECTLY, bypassing the router (TC-0905).
#
# app/config.py is exempt from the NAME check only: it declares the key fields
# and cannot avoid spelling them. The separate test below enforces the part
# that actually matters - that nothing outside the gateway READS those fields.
EXEMPT = {"app/gateway/router.py", "app/ingest/embed.py", "app/config.py"}

# Settings fields that name a provider. Reading one of these outside the
# gateway means that module is making a provider decision.
PROVIDER_SETTINGS = ["groq_api_key", "gemini_api_key", "openrouter_api_key"]


@pytest.mark.p0
@repo_only
def test_no_provider_named_outside_the_router():
    """TC-0901: grep agent code for provider names; expect zero hits.

    Invariant 5. If an agent can name a provider it can pick one, and the whole
    routing/fallback story stops being enforceable.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXEMPT:
            continue
        text = path.read_text(encoding="utf-8").lower()
        # Strip comments and docstrings: naming a provider while explaining the
        # rule is not a violation of it.
        code = re.sub(r"#.*", "", text)
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        code = re.sub(r"'''.*?'''", "", code, flags=re.S)
        for token in PROVIDER_TOKENS:
            if token in code:
                offenders.append(f"{rel}: {token}")
    assert not offenders, "provider named outside the gateway: " + "; ".join(offenders)


@pytest.mark.p0
@repo_only
def test_provider_settings_are_read_only_by_the_gateway():
    """Invariant 5, the part that has teeth.

    config.py must spell the provider key names; that is unavoidable. What
    matters is that only the gateway reads them - a module that reads
    GROQ_API_KEY is choosing a provider no matter how the call is dressed up.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ("app/gateway/router.py", "app/config.py"):
            continue
        code = path.read_text(encoding="utf-8")
        for field in PROVIDER_SETTINGS:
            if field in code:
                offenders.append(f"{rel}: {field}")
    assert not offenders, "provider settings read outside the gateway: " + "; ".join(offenders)


@pytest.mark.p0
def test_only_two_aliases_exist():
    """Agents choose task shape, not capability tiers."""
    assert router.ALIASES == ("fast", "long")


@pytest.mark.p0
async def test_unknown_alias_is_rejected():
    with pytest.raises(ValueError, match="Unknown alias"):
        await router.complete("gpt-4", [{"role": "user", "content": "hi"}])


@pytest.mark.p1
async def test_missing_provider_raises_provider_error(monkeypatch):
    """No key configured is an infrastructure failure, not a quality failure."""
    router.reset()
    monkeypatch.setattr(router, "build_router", lambda: None)
    with pytest.raises(router.ProviderError, match="No provider configured"):
        await router.complete("fast", [{"role": "user", "content": "hi"}])
    router.reset()


# --- cache key --------------------------------------------------------------


@pytest.mark.p0
def test_cache_key_excludes_job_id():
    """TC-0205: a second job on the same source + settings must HIT cache.

    The key is derived only from source_hash, output_type and parameters, so
    two different jobs with identical inputs produce the same key.
    """
    params = {"tone": "urgent", "audience": "general public"}
    job_a = cache.cache_key("sha256:abc", "linkedin_post", params)
    job_b = cache.cache_key("sha256:abc", "linkedin_post", params)
    assert job_a == job_b


@pytest.mark.p0
def test_parameter_change_misses_cache():
    """TC-0204: same source + format, tone changed -> fresh generation."""
    base = cache.cache_key("sha256:abc", "linkedin_post", {"tone": "informative"})
    changed = cache.cache_key("sha256:abc", "linkedin_post", {"tone": "urgent"})
    assert base != changed


@pytest.mark.p0
def test_different_source_or_format_misses_cache():
    params = {"tone": "urgent"}
    base = cache.cache_key("sha256:abc", "linkedin_post", params)
    assert cache.cache_key("sha256:xyz", "linkedin_post", params) != base
    assert cache.cache_key("sha256:abc", "exec_summary", params) != base


@pytest.mark.p1
def test_parameter_order_does_not_affect_the_key():
    a = cache.cache_key("h", "t", {"tone": "urgent", "audience": "public"})
    b = cache.cache_key("h", "t", {"audience": "public", "tone": "urgent"})
    assert a == b


@pytest.mark.p0
def test_retry_bypasses_the_cache():
    """A retry must not be served the output that just failed QA.

    Without the salt, fix notes would be pointless: the cache would return the
    identical failing artefact.
    """
    params = {"tone": "urgent"}
    first = cache.cache_key("h", "linkedin_post", params, attempt_salt="0")
    retry = cache.cache_key("h", "linkedin_post", params, attempt_salt="1")
    assert first != retry


@pytest.mark.p1
def test_prompt_version_participates_in_the_key():
    """Invariant 9: a template change must invalidate cached output."""
    v1 = cache.cache_key("h", "t", {}, prompt_version="linkedin_post@v1")
    v2 = cache.cache_key("h", "t", {}, prompt_version="linkedin_post@v2")
    assert v1 != v2


# --- guardrails -------------------------------------------------------------


@pytest.mark.p1
def test_pii_is_detected_and_redacted():
    text = "Contact alice@example.com or call about SSN 123-45-6789."
    kinds = guardrails.find_pii(text)
    assert "email" in kinds
    assert "ssn" in kinds

    clean = guardrails.redact(text)
    assert "alice@example.com" not in clean
    assert "123-45-6789" not in clean


@pytest.mark.p1
def test_clean_text_passes_guardrails():
    text = "A vulnerability rated 9.1 was fixed in version 22.7R2.6."
    assert guardrails.find_pii(text) == []
    assert guardrails.redact(text) == text


@pytest.mark.p1
def test_oversized_prompt_is_rejected_before_sending():
    with pytest.raises(ValueError, match="over the"):
        guardrails.check_scope("x" * 500_000)


# --- concurrency cap --------------------------------------------------------


@pytest.mark.p1
def test_qa_semaphore_matches_configured_concurrency():
    """TC-0904: checkers must not exceed the configured concurrent-call limit."""
    from app.config import get_settings

    router.reset()
    sem = router.qa_semaphore()
    assert sem._value == get_settings().qa_concurrency
    router.reset()
