"""Source reuse checker. DETERMINISTIC n-gram overlap - no LLM.

Only invoked for copyrighted sources (ARCHITECTURE.md sec.8, TC-0512). A short
fragment as a title card is fine; a long verbatim passage in narration is not.
Threshold, not a hard gate - except for long spans, which block (TC-0510/0511).
"""

from __future__ import annotations

import re

from app.graph.state import Artefact, CheckerName, CheckerResult, ContentObject

# 15+ consecutive words matching the source is flagged (TC-0510).
FLAG_NGRAM = 15
# A 60-word verbatim span blocks outright (TC-0511).
BLOCK_NGRAM = 60


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def longest_common_run(artefact_words: list[str], source_words: list[str]) -> int:
    """Longest run of consecutive words appearing in both.

    Binary search over run length: an n-gram set intersection is cheap, and if
    no run of length n matches then none of length n+1 can either.
    """
    low, high, best = 1, min(len(artefact_words), len(source_words)), 0
    while low <= high:
        mid = (low + high) // 2
        if mid and _ngrams(artefact_words, mid) & _ngrams(source_words, mid):
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best


def check(artefact: Artefact, content: ContentObject) -> CheckerResult:
    """Compare artefact prose against the source."""
    text = _prose(artefact.content or {})
    run = longest_common_run(_words(text), _words(content.text))

    if run >= BLOCK_NGRAM:
        return CheckerResult(
            checker=CheckerName.SOURCE_REUSE,
            passed=False,
            score=float(run),
            reason=f"{run}-word verbatim span reproduced from the source",
            fix_notes=[
                f"A {run}-word passage is copied verbatim. Paraphrase it. "
                "Quoted spans must be short fragments used illustratively."
            ],
        )

    if run >= FLAG_NGRAM:
        # Flagged, not failed: a short quote as a title card is legitimate.
        return CheckerResult(
            checker=CheckerName.SOURCE_REUSE,
            passed=True,
            score=float(run),
            reason=f"{run}-word span matches the source (flagged, under the block threshold)",
        )

    return CheckerResult(
        checker=CheckerName.SOURCE_REUSE,
        passed=True,
        score=float(run),
        reason=f"longest verbatim run is {run} words",
    )


def _prose(content: dict) -> str:
    """Human-visible text only. Machine fields cannot infringe."""
    skip = {"claims", "source_chunks", "icon_hint", "subtitles_srt"}
    out: list[str] = []

    def walk(value, key=""):
        if key in skip:
            return
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)

    walk(content)
    return " ".join(out)
