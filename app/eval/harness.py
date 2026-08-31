"""Evaluation harness.

TEST-CASES.md "Evaluation set": run a fixed test set against versioned prompts
so a change can be compared rather than guessed at. Reports pass rate by
checker, mean retries per artefact, mean latency and total model calls.

Comparing v(n) against v(n-1) on the same set is the whole point of versioning
prompts (Invariant 9).
"""

from __future__ import annotations

import json
import logging
import pathlib
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from app.graph.state import CheckerName, Parameters

log = logging.getLogger(__name__)

DEFAULT_SET = pathlib.Path(__file__).parent / "eval_set.json"


@dataclass
class EvalCase:
    """One row of the fixed test set."""

    source_id: str
    output_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    expected_properties: dict[str, Any] = field(default_factory=dict)
    prompt_version: str = ""


@dataclass
class CaseResult:
    case: EvalCase
    passed: bool
    retries: int
    latency_ms: float
    checker_passes: dict[str, bool]
    failures: list[str] = field(default_factory=list)


def load_set(path: pathlib.Path | None = None) -> list[EvalCase]:
    raw = json.loads((path or DEFAULT_SET).read_text(encoding="utf-8"))
    return [EvalCase(**case) for case in raw["cases"]]


async def run_case(case: EvalCase, content, analysis) -> CaseResult:
    """Run one case end to end and assert its expected properties."""
    from app.graph import build

    started = time.monotonic()
    artefact, _message = await build.run_artefact(
        case.output_type,
        content,
        analysis,
        Parameters(**case.parameters),
        job_id=f"eval_{case.source_id}_{case.output_type}",
    )
    latency_ms = (time.monotonic() - started) * 1000

    checker_passes = {}
    if artefact.qa_result:
        checker_passes = {str(r.checker): r.passed for r in artefact.qa_result.results}

    failures = _assert_properties(case.expected_properties, artefact)

    return CaseResult(
        case=case,
        passed=not failures,
        retries=artefact.retry_count,
        latency_ms=latency_ms,
        checker_passes=checker_passes,
        failures=failures,
    )


def _assert_properties(expected: dict[str, Any], artefact) -> list[str]:
    """Check the assertions the eval set declares.

    Deliberately explicit rather than a generic matcher: an eval that silently
    ignores an assertion it does not understand is worse than no eval.
    """
    failures: list[str] = []
    content = artefact.content or {}

    if "min_claims" in expected and len(artefact.claims) < expected["min_claims"]:
        failures.append(f"{len(artefact.claims)} claims, expected >= {expected['min_claims']}")

    if "required_keys" in expected:
        missing = [k for k in expected["required_keys"] if k not in content]
        if missing:
            failures.append(f"missing keys: {', '.join(missing)}")

    if "max_chars" in expected:
        length = len(json.dumps(content))
        if length > expected["max_chars"]:
            failures.append(f"{length} chars, expected <= {expected['max_chars']}")

    if "min_tone" in expected and artefact.qa_result:
        tone = artefact.qa_result.by_checker(CheckerName.TONE)
        if tone and tone.score is not None and tone.score < expected["min_tone"]:
            failures.append(f"tone {tone.score:.2f}, expected >= {expected['min_tone']}")

    if expected.get("must_pass_grounding") and artefact.qa_result:
        grounding = artefact.qa_result.by_checker(CheckerName.GROUNDING)
        if grounding and not grounding.passed:
            failures.append("grounding failed")

    if "max_retries" in expected and artefact.retry_count > expected["max_retries"]:
        failures.append(f"{artefact.retry_count} retries, expected <= {expected['max_retries']}")

    unknown = set(expected) - {
        "min_claims",
        "required_keys",
        "max_chars",
        "min_tone",
        "must_pass_grounding",
        "max_retries",
    }
    if unknown:
        # Surfaced as a failure, not ignored: an unrecognised assertion means
        # the eval set expects something this harness never checked.
        failures.append(f"unrecognised expected_properties: {', '.join(sorted(unknown))}")

    return failures


def report(results: list[CaseResult]) -> dict[str, Any]:
    """Per-run report. Compare against the previous run on the same set."""
    from app.observability import metrics

    if not results:
        return {"cases": 0}

    checkers: dict[str, list[bool]] = {}
    for result in results:
        for checker, passed in result.checker_passes.items():
            checkers.setdefault(checker, []).append(passed)

    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r.passed),
        "pass_rate": round(sum(1 for r in results if r.passed) / len(results), 3),
        "pass_rate_by_checker": {
            name: round(sum(values) / len(values), 3) for name, values in checkers.items()
        },
        "mean_retries": round(statistics.mean(r.retries for r in results), 2),
        "mean_latency_ms": round(statistics.mean(r.latency_ms for r in results)),
        "total_model_calls": metrics()["model_calls"],
        "failures": [
            {"case": f"{r.case.output_type}", "reasons": r.failures} for r in results if r.failures
        ],
    }
