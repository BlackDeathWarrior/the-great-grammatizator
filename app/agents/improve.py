"""The self-improvement loop: notice what keeps going wrong, and stop doing it.

The platform already collects three kinds of signal and, until now, acted on
none of them beyond the immediate artefact:

  - a thumb down from an operator, on work that had already cleared every
    automated check. This is the strongest signal in the system, because it is
    a person disagreeing with a passing verdict.
  - QA fix notes, which say precisely what a checker objected to.
  - the variant choices already handled by app/agents/preferences.py.

This module closes the loop: it reads a window of recent signals for one
operator, asks what RECURS, and turns each recurring pattern into a standing
rule that joins their style notes and reaches every later prompt.

Three properties keep an autonomous loop from going wrong:

1. **It only ever writes prompt text.** It cannot change a threshold, a
   constraint, or code. The worst it can do is give the generator a bad
   instruction, which is visible in the dashboard and deletable in one click.
2. **It requires repetition.** A pattern seen once is an anecdote, and a rule
   invented from an anecdote steers every future job for no reason. The prompt
   says so and the caller enforces a minimum signal count.
3. **Its output is legible.** Rules are sentences, carry the evidence that
   produced them, and are capped by the same MAX_NOTES as everything else.

It runs after feedback rather than on a timer: the operator has just told us
something, which is the moment the signal is worth acting on.
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from app.agents import preferences
from app.gateway import router
from app.prompts import loader

log = logging.getLogger(__name__)

# Below this there is nothing to generalise from, and asking anyway invites the
# model to invent a pattern in order to be helpful.
MIN_SIGNALS = 2

# One pass considers a window, not all history. Taste moves, and a fault fixed
# months ago should not still be steering prompts.
WINDOW = 20

# A fix note has to recur to count as a habit rather than an accident.
MIN_REPEATS = 2

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "is",
    "are",
    "it",
    "this",
    "that",
    "with",
    "for",
    "from",
    "at",
    "by",
    "on",
    "as",
    "be",
    "not",
    "but",
    "your",
    "you",
    "its",
    "into",
    "than",
    "then",
    "more",
}


def recurring_fix_notes(notes: list[str], *, min_repeats: int = MIN_REPEATS) -> list[str]:
    """Fix notes that appear more than once, most frequent first.

    Matched on a coarse fingerprint rather than exact text: the same objection
    is phrased differently every time it is raised, so exact matching would
    find no repeats at all and the loop would never learn anything.
    """
    first_seen: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for note in notes:
        key = _fingerprint(note)
        if not key:
            continue
        counts[key] += 1
        first_seen.setdefault(key, note)

    return [first_seen[k] for k, n in counts.most_common() if n >= min_repeats]


def _fingerprint(note: str) -> str:
    """The content words of a note, so rephrasings collapse together."""
    words = "".join(c.lower() if c.isalnum() else " " for c in note or "").split()
    content = [w for w in words if w not in _STOP and not w.isdigit() and len(w) > 3]
    return " ".join(sorted(set(content))[:6])


async def derive_rules(
    *,
    dislikes: list[dict],
    liked: list[dict],
    fix_notes: list[str],
) -> list[str]:
    """Ask what recurs. Returns [] when nothing does, which is common.

    An empty answer is right far more often than it looks, and a great deal
    better than a confident rule built from one bad afternoon.
    """
    recurring = recurring_fix_notes(fix_notes)

    if len(dislikes) + len(recurring) < MIN_SIGNALS:
        log.info("not enough recurring signal to learn from; leaving prompts alone")
        return []

    try:
        raw = await router.complete(
            router.FAST,
            [
                {"role": "system", "content": loader.system("improve@v1")},
                {
                    "role": "user",
                    "content": loader.render(
                        "improve@v1",
                        dislikes=dislikes[:WINDOW],
                        liked=liked[:WINDOW],
                        fix_notes=recurring[:WINDOW],
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=400,
        )
    except router.ProviderError as exc:
        # The signals are already stored; only this pass over them is lost, and
        # the next one will see the same evidence.
        log.warning("improvement pass unavailable: %s", exc)
        return []

    rules = _parse(raw).get("rules") or []
    if not isinstance(rules, list):
        return []

    return [r for r in (preferences._clean(x) for x in rules[:3]) if r]


def apply_rules(style_notes: list[dict], rules: list[str], evidence: dict) -> list[dict]:
    """Fold new rules into the operator's notes, bounded and deduplicated."""
    merged = list(style_notes or [])
    for rule in rules:
        merged = preferences.merge_note(merged, rule, evidence)
    return merged


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("improvement pass returned non-JSON; learning nothing")
        return {}
    return data if isinstance(data, dict) else {}
