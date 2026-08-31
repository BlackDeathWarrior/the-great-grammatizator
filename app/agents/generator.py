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


async def generate(
    spec: FormatSpec,
    content: ContentObject,
    analysis: AnalysisResult,
    parameters: Parameters,
    *,
    fix_notes: list[str] | None = None,
    attempt: int = 0,
    use_cache: bool = True,
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
        attempt_salt=str(attempt),
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
        content=content,
        analysis=analysis,
        parameters=parameters,
        constraints=spec.constraints,
        fix_notes=fix_notes,
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

        raw = await router.complete(
            spec.model_alias,
            messages,
            response_format={"type": "json_object"},
            temperature=0.4,
        )

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
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors[:4]
        )
        raise ParseFailure(f"schema violation - {detail}")


def _build(spec: FormatSpec, data: dict) -> Artefact:
    claims = [Claim(text=c["text"], chunk_id=c["chunk_id"]) for c in data.get("claims", [])]
    return Artefact(output_type=spec.id, content=data, claims=claims)
