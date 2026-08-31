"""The verdict policy. A POLICY, not a vote.

"Three of four passed" is meaningless when the failure is safety
(ARCHITECTURE.md sec.8).

  safety fail          -> block unconditionally, do not deliver   [TC-0602]
  grounding fail       -> retry with fix notes                    [TC-0603]
  tone below threshold -> retry once, then pass with a warning    [TC-0605]
  format fail          -> retry with the violated constraint
  source reuse         -> flag; long verbatim spans block         [TC-0510/0511]
  3 QA failures        -> stop the job, tell the operator         [TC-0607]
"""

from __future__ import annotations

from app.config import get_settings
from app.graph.state import (
    Artefact,
    ArtefactStatus,
    CheckerName,
    QAResult,
    Verdict,
)

RESTART_MESSAGE = (
    "Quality checks failed three times for {output_type}. The job has been "
    "stopped. Review the findings below, adjust the parameters or the source, "
    "and start a new job."
)


def decide(qa: QAResult, artefact: Artefact) -> tuple[Verdict, str]:
    """Apply the policy. Returns (verdict, operator_message).

    Order matters: safety is evaluated first and unconditionally, so no
    combination of other passes can rescue an unsafe artefact.
    """
    settings = get_settings()

    safety = qa.by_checker(CheckerName.SAFETY)
    if safety and not safety.passed:
        return Verdict.BLOCK, ""

    reuse = qa.by_checker(CheckerName.SOURCE_REUSE)
    if reuse and not reuse.passed:
        # A long verbatim span is a reproduction, not a transformation.
        return Verdict.BLOCK, ""

    hard_failures = [
        r
        for r in qa.results
        if not r.passed and r.checker in (CheckerName.GROUNDING, CheckerName.FORMAT)
    ]

    tone = qa.by_checker(CheckerName.TONE)
    tone_failed = tone is not None and not tone.passed

    if not hard_failures and not tone_failed:
        return Verdict.PASS, ""

    # Would this retry exceed the budget? retry_count is the number already
    # spent, so the next attempt is retry_count + 1.
    if artefact.retry_count + 1 > settings.qa_max_retries:
        return (
            Verdict.BLOCK,
            RESTART_MESSAGE.format(output_type=artefact.output_type),
        )

    if hard_failures:
        return Verdict.RETRY, ""

    # Tone alone: retry once, then pass flagged. Tone is advisory - it must not
    # be able to consume the whole budget on its own.
    if artefact.tone_retried:
        return Verdict.PASS_FLAGGED, ""
    return Verdict.RETRY, ""


def apply(qa: QAResult, artefact: Artefact) -> tuple[Artefact, str]:
    """Apply the verdict to the artefact, returning the updated copy."""
    verdict, message = decide(qa, artefact)
    updated = artefact.model_copy(deep=True)
    updated.qa_result = qa.model_copy(update={"verdict": verdict})

    if verdict is Verdict.PASS:
        updated.status = ArtefactStatus.PASSED
    elif verdict is Verdict.PASS_FLAGGED:
        updated.status = ArtefactStatus.PASSED_FLAGGED
    elif verdict is Verdict.BLOCK:
        updated.status = ArtefactStatus.BLOCKED
    else:
        updated.status = ArtefactStatus.QA
        # Only a QA verdict failure increments this counter (Invariant 8).
        updated.retry_count += 1
        tone = qa.by_checker(CheckerName.TONE)
        if tone is not None and not tone.passed:
            updated.tone_retried = True

    return updated, message
