"""Output generation. One per selected format, driven entirely by the registry.

Prompt = template + content object + analysis + parameters + output schema
(ARCHITECTURE.md sec.7). Response is structured JSON with a claims[] array,
each claim carrying a chunk_id.

Malformed JSON is a PARSE retry on its own counter. It must never burn a QA
retry (Invariant 8, TC-0405).
"""

from __future__ import annotations

import json
import logging

from jsonschema import Draft202012Validator

from app.config import get_settings
from app.formats.registry import FormatSpec
from app.gateway import cache, router
from app.graph.state import AnalysisResult, Artefact, Claim, ContentObject, Parameters
from app.prompts import loader

log = logging.getLogger(__name__)


class ParseFailure(Exception):
    """The model returned unusable JSON.

    Distinct from a QA failure: this is the model failing to speak the protocol,
    not producing poor content. Separate counter (TC-0405, TC-0406).
    """


def _focus(content: ContentObject, analysis: AnalysisResult, *, job_id: str = "") -> ContentObject:
    """Narrow a long source to the chunks worth showing (top-k).

    Generators used to see every chunk inline in document order, which works
    for a 3-page advisory and does not for a 60-page report: the prompt either
    blows the context window or buries the relevant passage among boilerplate.

    Ordering is by relevance to what the operator actually asked for, which is
    only meaningful once embeddings are real (ARCHITECTURE §13.2). With the
    offline fallback active this degrades to document order - the same
    behaviour as before, so a degraded embedder never makes things worse.

    Chunk IDS ARE PRESERVED. Selection changes which chunks are shown, never
    what they are called, so a claim citing c37 still verifies against c37
    (Invariant 2).
    """
    total = len(content.chunks)
    if total <= loader.MAX_PROMPT_CHUNKS:
        return content

    query = " ".join(x for x in (analysis.objective, analysis.audience, content.title) if x).strip()

    ranked_ids: list[str] = []
    if job_id and query:
        try:
            from app.tools.registry import Caller, call

            hits = call(
                Caller.OUTPUT_GENERATOR,
                "search_chunks",
                job_id=job_id,
                query=query,
                k=loader.MAX_PROMPT_CHUNKS,
            )
            ranked_ids = [h["chunk_id"] for h in hits]
        except Exception as exc:  # noqa: BLE001 - retrieval must not fail a job
            log.warning("top-k selection unavailable, using document order: %s", exc)

    keep = {cid for cid in ranked_ids[: loader.MAX_PROMPT_CHUNKS]}
    if keep:
        # Document order among the selected chunks: the model reads better
        # prose than a relevance-shuffled sequence.
        selected = [c for c in content.chunks if c.id in keep]
    else:
        selected = list(content.chunks[: loader.MAX_PROMPT_CHUNKS])

    log.info("focused %d chunks to %d for %s", total, len(selected), content.source_id)
    return content.model_copy(update={"chunks": selected})


async def generate(
    spec: FormatSpec,
    content: ContentObject,
    analysis: AnalysisResult,
    parameters: Parameters,
    *,
    fix_notes: list[str] | None = None,
    attempt: int = 0,
    use_cache: bool = True,
    job_id: str = "",
    approach: str = "",
    style_notes: list[str] | None = None,
) -> Artefact:
    """Generate one artefact. Raises ProviderError or ParseFailure.

    Both are surfaced rather than swallowed so the caller can route them to the
    correct counter.
    """
    fix_notes = fix_notes or []
    version_tag = loader.version_tag(spec.prompt_template)

    key = cache.cache_key(
        content.source_hash,
        spec.id,
        parameters.model_dump(),
        prompt_version=version_tag,
        # A retry must never be served the output that just failed QA, or the
        # fix notes achieve nothing.
        # The angle and the operator's learned notes both change the output,
        # so a variant must never be served another variant's cached draft.
        attempt_salt=f"{attempt}|{approach}|{'|'.join(style_notes or [])}",
    )

    if use_cache and not fix_notes:
        hit = cache.get(key)
        if hit:
            log.info("cache hit for %s", spec.id)
            try:
                return _build(spec, json.loads(hit))
            except (json.JSONDecodeError, ParseFailure):
                log.warning("discarding malformed cache entry for %s", spec.id)

    prompt = loader.render(
        spec.prompt_template,
        content=_focus(content, analysis, job_id=job_id),
        analysis=analysis,
        parameters=parameters,
        constraints=spec.constraints,
        fix_notes=fix_notes,
        approach=approach,
        style_notes=style_notes or [],
    )
    system = loader.system(spec.prompt_template)

    settings = get_settings()
    last_error: str = ""
    for parse_attempt in range(settings.parse_max_retries + 1):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if last_error:
            # Tell the model exactly how it broke the protocol. A bare "try
            # again" reproduces the same malformed output.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was rejected: {last_error}\n"
                        "Return ONLY a JSON object matching the schema."
                    ),
                }
            )

        try:
            raw = await router.complete(
                spec.model_alias,
                messages,
                response_format={"type": "json_object"},
                temperature=0.4,
            )
        except router.MalformedOutput as exc:
            # The provider validated json_object mode server-side and rejected
            # the model's output before it reached us. Same fault as unparseable
            # text, so it takes the same cheap path - retry here with the error
            # fed back - rather than three rounds of provider backoff.
            last_error = f"provider rejected the JSON: {exc}"
            log.warning(
                "parse attempt %d/%d rejected upstream for %s",
                parse_attempt + 1,
                settings.parse_max_retries + 1,
                spec.id,
            )
            continue

        try:
            data = _parse(raw)
            _validate(spec, data)
            artefact = _build(spec, data)
        except ParseFailure as exc:
            last_error = str(exc)
            log.warning(
                "parse attempt %d/%d failed for %s: %s",
                parse_attempt + 1,
                settings.parse_max_retries + 1,
                spec.id,
                exc,
            )
            continue

        if use_cache:
            cache.put(key, json.dumps(data))
        return artefact

    raise ParseFailure(
        f"{spec.id}: no valid JSON after {settings.parse_max_retries + 1} attempts. {last_error}"
    )


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseFailure(f"not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise ParseFailure("top level must be a JSON object")
    return data


def _validate(spec: FormatSpec, data: dict) -> None:
    """Schema validation. A schema violation is a parse failure, not a QA one.

    The model produced the wrong SHAPE; the content was never assessable.
    """
    errors = sorted(
        Draft202012Validator(spec.schema()).iter_errors(data), key=lambda e: list(e.path)
    )
    if errors:
        detail = "; ".join(_error_line(e) for e in errors[:4])
        raise ParseFailure(f"schema violation - {detail}")


def _error_line(err) -> str:
    """One violation, said in a sentence the model can act on.

    jsonschema's own message embeds the entire offending value. For a rich
    object where a string was wanted that is several thousand tokens of the
    model's own output read back at it - which crowded out the instruction,
    cost a fortune per retry, and still did not say what to do differently.

    The common failures each get a specific repair instruction instead.
    """
    path = ".".join(str(p) for p in err.path) or "(root)"
    msg = err.message

    if "is not of type 'string'" in msg:
        return (
            f"{path} must be a plain STRING, not an object. Put the detail in the sentence itself."
        )
    if "does not match" in msg and "chunk_id" in path:
        return (
            f"{path} is not a real chunk id. Use one of the ids shown in the "
            "source (c1, c2, ...), or drop the claim."
        )
    if "is a required property" in msg:
        return f"{path}: {msg}"
    # Anything else: keep the message, but never the offending value.
    return f"{path}: {msg[:160]}"


def _build(spec: FormatSpec, data: dict) -> Artefact:
    claims = [Claim(text=c["text"], chunk_id=c["chunk_id"]) for c in data.get("claims", [])]
    return Artefact(output_type=spec.id, content=data, claims=claims)
