# Pipeline review — 2026-09-09

A pre-demo review of the agentic path: gateway → generation → QA → verdict →
export. Four defects found and fixed, all of them in the same class — **a
content fault wearing an infrastructure fault's clothes**, or the reverse.

Measured before: a seven-format job hit the 900s worker timeout with **0 of 7**
artefacts and no error recorded against any of them.
Measured after: the same job completes in **~430s with 6 of 7 passed**, the
seventh correctly blocked by safety and grounding.

---

## What was already right

Worth stating, because it shaped how much was worth changing:

- **Verdict is a policy, not a vote** (`app/agents/verdict.py`). Safety is
  evaluated first and unconditionally; no combination of passes rescues an
  unsafe artefact.
- **Hard checkers fail closed.** Grounding, safety and editorial that cannot run
  withhold the artefact as *unverified* rather than passing it. The comments
  record two earlier silent-failure bugs — safety returning `{}` read as safe,
  grounding returning `[]` certifying every claim — both already fixed.
- **Two failure counters are genuinely separate** (Invariant 8). Provider errors
  retry with backoff and never touch the 3-strike QA budget.
- **Grounding has no web search**, enforced in the tool allowlist rather than a
  prompt (`app/tools/registry.py`, Invariant 6).
- **QA sees the full source; the generator sees a focused subset.** `_focus()`
  narrows what the model reads, but grounding validates against every chunk id,
  so a claim citing a chunk that was not shown still verifies. The asymmetry is
  correct and worth not "fixing".

---

## Defect 1 — a dead deployment on the `long` alias

`_OPENROUTER_LONG` pointed at `openrouter/minimax/minimax-m3:free`. The `:free`
slug now 404s, and OpenRouter's own error names the replacement.

**Why it cost so much.** A *missing* deployment is skipped. A *dead* one is
tried: every `long` call paid three attempts with 12s backoff before falling
back. Across seven formats that is most of a 900s budget spent on a model that
was never going to answer.

Fixed to `openrouter/minimax/minimax-m3`. Startup now logs `preflight: 5
model(s) OK` for the first time.

## Defect 2 — unbounded `max_tokens`

`router.complete()` only sent `max_tokens` when a caller passed one, and no
caller did. Providers read the absence as *reserve the whole context window*,
so OpenRouter's free tier refused outright:

> requested up to 131072 tokens, but can only afford 11858

That arrives as a 402, is classified as a provider error, and is retried three
times with backoff — per artefact.

Fixed with `_DEFAULT_MAX_TOKENS = 8000` applied in the gateway. The ceiling
belongs there because that file is the only one allowed to know a provider
exists (Invariant 5); a ceiling at each call site is one an agent eventually
forgets.

## Defect 3 — `json_validate_failed` misclassified as infrastructure

Groq validates `json_object` mode server-side and fails the **request** when the
model emits bad JSON. Every exception out of `acompletion` became a
`ProviderError`, so this bought three retries and 36s of backoff for a fault a
parse retry fixes in one cheap attempt with the error fed back to the model.

Added `MalformedOutput`, raised when the provider's message carries
`json_validate_failed` / `failed to validate json` / `failed_generation`.
Matched on text because the exception class is a generic `BadRequestError`.

- `generator.generate()` converts it to a parse retry — its own counter, never
  the QA budget.
- `qa/runner.py` converts it to a checker error, so a hard checker still fails
  closed rather than triggering provider backoff.

## Defect 4 — the editorial checker starved itself

`editorial.check()` capped its reply at `max_tokens=400` to avoid truncation.
Too tight: the model returned empty strings, or output the provider rejected as
invalid JSON. Because editorial is a **hard** checker, that withheld artefacts
as unverified for what was really a token budget.

Two changes:

1. Ceiling raised to 1200.
2. `_salvage()` recovers a truncated reply. This checker writes its four scores
   *before* its prose suggestion, so a response cut mid-suggestion still carries
   everything the verdict needs. The salvage closes the object at the last
   complete key/value pair and re-parses; it returns `None` when nothing usable
   survives, so **fail-closed still holds** — the point is to rescue a real
   judgement, never to manufacture one.

## Defect 5 — provider backoff too short to outlast a rate-limit window

Found in the run data rather than by reading: every failed artefact showed
`retries=1, provider_err=3`. The QA budget allows **three** retries, but each
artefact only ever got **one** — the retry itself hit provider errors and gave
up with two-thirds of its self-correction budget unused.

The arithmetic: backoff doubles from 12s, so three attempts wait 12s + 24s =
36s. A free-tier rate-limit window is typically 60s or more, so all three
attempts landed inside the same window and reported a dead provider that was
merely throttled.

`_PROVIDER_ATTEMPTS` raised from 3 to 4, which pushes the total past 84s and
clears a typical window. It costs nothing when the provider is healthy: the
first attempt succeeds and the rest are never scheduled.

**Why this matters more than it looks.** The retry loop is where quality
actually comes from — the format checker writes precise fix notes ("Panel 1's
caption is only 93 characters; at least 110 is expected. Say what the number
means.") and the model acts on them. A truncated retry budget does not just
lose one attempt; it disables the correction mechanism the whole QA design
rests on.

---

## Not changed, deliberately

- **`qa_concurrency: 2`.** Slower than it could be, but the cap exists to keep
  free tiers from rate-limiting mid-demo, and the 429s seen earlier in the
  session are evidence it is doing real work. Raising it trades demo
  reliability for speed.
- **`job_timeout: 900`.** With the four fixes a seven-format job lands near
  430s. The timeout is a genuine backstop, not the constraint.
- **Run demo jobs sequentially.** Three concurrent seven-format jobs starve the
  QA semaphore and drive artefacts to `r=3` blocked with no provider errors.
  This is a demo-operations note, not a code defect.

- **Never run the test suite against a live demo job.** The integration tests
  reset the database, which deletes in-flight jobs and every job record on the
  box - the exported *files* survive on disk, but the API returns
  `Unknown job`. This cost two demo runs during this review. Finish the tests
  first, then generate the demo artefacts, and do not rebuild the containers
  while a job is running (`app` and `worker` have no bind mount, so a rebuild
  restarts the worker and kills the job mid-flight).

## Verification

- `pytest -q` — **441 passed, 3 skipped**.
- `ruff check .` — clean apart from five pre-existing alembic import-order
  warnings, untouched.
- End-to-end: seven formats, real DOCX/PDF/PPTX/MP4 exports, claims citing real
  chunk ids, checkers genuinely discriminating (safety blocked exploit
  instructions; grounding caught an invented chunk id; format caught narration
  shorter than its scene duration).
