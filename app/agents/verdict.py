"""The verdict policy. A POLICY, not a vote.

"Three of four passed" is meaningless when the failure is safety
(ARCHITECTURE.md sec.8).

  hard checker errored -> block: unverified, never "passed by default"
  safety fail          -> block unconditionally, do not deliver   [TC-0602]
  grounding fail       -> retry with fix notes                    [TC-0603]
  editorial fail       -> retry with the failing dimension named
  tone below the floor -> block: too poor to ship even flagged
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

TONE_FLOOR_MESSAGE = (
    "The {output_type} scored {score:.2f} for tone, below the {floor:.2f} floor. "
    "It has been withheld rather than delivered with a warning. Adjust the "
    "audience or style and start a new job."
)

UNVERIFIED_MESSAGE = (
    "The {checker} check could not run for {output_type}, so the artefact was "
    "never verified. It has been withheld rather than delivered unchecked. "
    "This is an infrastructure fault, not a quality one - retrying the job is "
    "the right response."
)


# Checkers whose verdict gates delivery. Kept in step with runner._FAIL_CLOSED.
_HARD_CHECKERS = frozenset({CheckerName.GROUNDING, CheckerName.SAFETY})


def decide(qa: QAResult, artefact: Artefact) -> tuple[Verdict, str]:
    """Apply the policy. Returns (verdict, operator_message).

    Order matters: safety is evaluated first and unconditionally, so no
    combination of other passes can rescue an unsafe artefact.
    """
    settings = get_settings()

    # A hard checker that could not run leaves the artefact UNVERIFIED. Block,
    # and say so in the operator's own terms - this is not a quality failure,
    # and a retry would only crash the same way, so it must not spend the QA
    # budget (Invariant 8).
    unverified = next(
        (r for r in qa.results if r.checker_error and r.checker in _HARD_CHECKERS),
        None,
    )
    if unverified is not None:
        return Verdict.BLOCK, UNVERIFIED_MESSAGE.format(
            checker=unverified.checker, output_type=artefact.output_type
        )

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
        if not r.passed
        and r.checker in (CheckerName.GROUNDING, CheckerName.FORMAT, CheckerName.EDITORIAL)
    ]

    tone = qa.by_checker(CheckerName.TONE)
    tone_failed = tone is not None and not tone.passed

    # Tone is advisory: it retries once, then passes flagged, so on its own it
    # can never stop anything. That leaves nothing between a 0.30 artefact and
    # the operator, so a floor blocks rather than flags.
    #
    # But only AFTER the retries have been spent. Blocking on the first
    # attempt denied the generator the fix note that would have corrected it -
    # a seven-format run blocked two artefacts at retry_count 0, so the
    # operator saw a refusal where a second attempt was very likely to pass.
    below_floor = tone is not None and tone.score is not None and tone.score < settings.tone_floor
    if below_floor and artefact.retry_count >= settings.qa_max_retries:
        return (
            Verdict.BLOCK,
            TONE_FLOOR_MESSAGE.format(
                output_type=artefact.output_type,
                score=tone.score,
                floor=settings.tone_floor,
            ),
        )

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
    if artefact.tone_retried and not below_floor:
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
