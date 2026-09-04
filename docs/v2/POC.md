# POC status and plan

**Audience: a future Claude Code session.** Read this first. It tells you what
exists, what it cost, what broke, and what to do next.

Companion documents in this folder: `ARCHITECTURE.md` (what is built, §13 for
what is not), `USE-CASES.md` (operator-facing behaviour and the demo scripts),
`TEST-CASES.md` (the register, reconciled against the suite, with a ranked gap
list).

---

## 1. What this is

One source in — article, report, advisory, image, video, or free-form prompt.
One or more communication artefacts out — LinkedIn post, tweet thread, executive
summary, advisory, infographic, presentation, video package.

The claim that makes it more than a prompt wrapper: **every factual assertion in
every artefact cites an immutable chunk of the source, and a checker verifies
each claim against the specific chunk it cites.**

## 2. Status

**Built, running, demonstrated against live providers.**

| | |
|---|---|
| Tests | 180 pass, 3 skip. 98 P0 |
| Formats | 7, all generating |
| Golden path | 67-93s over two runs — two artefacts, both passed |
| Seven-format job | 123s, 53 model calls, six passed, one legitimately blocked |
| Services | app, worker, postgres, qdrant, redis |
| Lint | clean |

Run it:

```bash
docker compose up -d
```

Dashboard at `http://127.0.0.1:8000`. Prefer `127.0.0.1` — some clients resolve
`localhost` to IPv6 `::1`, which Docker's port mapping does not answer on.

## 3. What the POC proves

Five things, each demonstrable in a single run.

**Grounding is real, not decorative.** Every claim carries a `chunk_id`. The
grounding checker fetches that chunk and asks whether it supports that claim.
An invented id is caught before any model call. This is why chunk ids are
immutable and why the content object is frozen.

**The QA split is principled.** A tweet over 280 characters is not a judgement
call — format and source-reuse are deterministic and provably make zero model
calls. Grounding, tone and safety are model calls because they are actually
judgements. Spending a completion on a length check would be slower, less
reliable and non-reproducible.

**The verdict is a policy, not a vote.** Safety blocks unconditionally, even
with three passes. Grounding retries with a note naming the failing claim. Tone
retries once then passes flagged. Three failures stop the job with an operator
message rather than a stack trace. Observed live: an advisory blocked after
three tone failures while six other artefacts passed.

**Formats are configuration.** Seven formats, zero per-format pipeline code. A
test asserts the pipeline modules contain no format id literals, so a switch
statement fails CI. Adding an eighth format is one JSON entry, one template, one
schema.

**Security is structural, not prompted.** The grounding checker cannot reach web
search — the allowlist denies it *and* the tool is not listed as available, so
the model never learns it exists. No write tool is registered anywhere, so no
allowlist mistake can expose one. Retrieval takes `job_id` as its first
parameter, making cross-job access inexpressible rather than merely discouraged.

## 4. What it cost

Twelve phases, thirteen commits, one session. Roughly:

| Phase | Work |
|---|---|
| 0–1 | Scaffold, compose, state objects, schema |
| 2 | Ingestion — the largest single phase |
| 3–5 | Gateway, tool registry, analysis |
| 6 | Seven formats as data — pure config |
| 7 | Pipeline, five checkers, verdict policy — the second largest |
| 8 | Fan-out — **tests only, no new pipeline code** |
| 9–10 | Export, dashboard |
| 11 | Observability, eval harness, discipline sweep |

Phase 8 is the one to note: adding six formats to a working one-format pipeline
required no pipeline code at all. That is the registry design paying off, and it
is the strongest evidence for Invariant 4.

## 5. What broke, and what to learn from it

Recorded because the same traps will recur.

**Every model id was dead.** All three inherited from v1's architecture: the
Groq model no longer existed, the Gemini model was retired, the OpenRouter
`:free` slug had become paid-only. The doc warned these strings drift; it was
right. Now mitigated: `router.preflight()` probes every configured deployment
at startup and logs a warning naming any that fail (TC-0908). It still cannot
stop an id dying between startup and a demo — re-probe if generation fails at
the first model call.

**A prose contract was not enough.** The shared prompt described the claims
shape in words. Models returned `{"claim":…, "citations":…}` and exhausted every
parse retry on every format. Adding the literal JSON example fixed it
immediately. For structured output, show the shape.

**Silent success is the worst failure mode.** Langfuse tracing degraded to a
no-op when the SDK changed its API — by design, since tracing must never break a
job. Nothing failed, no test caught it, and no trace reached the dashboard. If a
component is allowed to fail silently, something must periodically assert it is
actually working.

**Green tests are not a working feature.** The video renderer passed its suite
while producing an 11-second package as a 6-second video with subtitles running
past the end. `-shortest` clipped each scene to its narration. Only playing the
output caught it. For anything with a rendered artefact, inspect the artefact.

**Test bugs look like code bugs.** Several failures during the build were my
tests being wrong: a page-attribution fixture too small to ever split, a
loop-variable closure that would only ever report the last value, a hardcoded
`localhost` in a DB URL — the exact trap the architecture warns about. When a
test fails, suspect it as readily as the code.

## 6. What to do next

Ordered by value per unit of risk. Each item names its acceptance test.

### Tier 0 — done while writing these documents

Two gaps this register identified were cheap enough to close immediately:

- **TC-0908, model-id pre-flight.** `router.preflight()` sends one trivial
  completion per configured deployment; `app/main.py` calls it at startup and
  logs a warning naming any dead id. Never fatal — the API still serves
  `/health` so the operator can read the warning.
- **TC-0411, claims example pinned.** A test asserts the literal
  `{"text": …, "chunk_id": …}` example is present in `_shared.jinja` and reaches
  a rendered prompt, so the fix for a full debugging cycle cannot be tidied away.

### Tier 1 — before the next demo

**1. Parameter validation at the API (TC-0206).** The dashboard's dropdowns
constrain the UI; `POST /jobs` accepts any string. Validate against `VOCAB`.
*Done when:* `{"audience": "penguins"}` returns 400 listing valid values.

**2. Exercise the provider fallback (TC-0902).** Fallbacks are configured and
have never been tested. A forced 429 on one deployment should be served by
another without the job failing.
*Done when:* a test simulates a rate limit and the job still completes.

**3. Regenerate keeps the analysis (TC-0808).** The route exists and is
untested. UC-09's whole point is that a regenerate costs one artefact.
*Done when:* a test asserts `regenerate_task` does not recompute analysis.

### Tier 2 — makes the product real

**4. Real semantic retrieval (§13.2).** Configure an embedding provider; keep
the offline fallback for tests and air-gapped demos, but make it loud — surface
it in the job record, not only in logs. This unblocks two things that are
otherwise impossible: multi-source jobs (TC-0116) and long sources, since the
generator currently sees every chunk inline in the prompt.
*Done when:* `search_chunks(job_id, "what is the CVSS score")` returns the
severity chunk first on a document where it is not the first chunk.

**5. Top-k chunk selection.** Follows directly from (4). Today every chunk goes
into the prompt, which works for three pages and will not for sixty.
*Done when:* a 60-page source generates without exceeding the context limit, and
grounding still passes.

**6. Close the ingestion test gaps** (TC-0104, TC-0105, TC-0111, TC-0106).
Four extraction paths have no test. They work — they were exercised by hand —
but nothing guards them.
*Done when:* each has an integration test with a real fixture.

### Tier 3 — new capability, needs design first

**7. Authentication and multi-tenancy (§13.1).** The retrieval story is already
tenant-shaped. The invariant to preserve: cross-tenant access must be
**structurally impossible**, the way cross-job access already is. If it becomes
a filter an agent could omit, it has been done wrong.
*Done when:* a test proves tenant A's job cannot retrieve tenant B's chunks even
with a forged job id.

**8. Translation (§13.3).** Do not start by adding languages to `VOCAB`. Three
questions must be answered first, because the QA layer silently stops working
otherwise:

- Does the tone checker judge in the target language or in English?
- Grounding must compare a translated claim against an untranslated chunk — the
  source does not change. The grounding prompt needs the language pair.
- Does n-gram source-reuse matching mean anything across a translation? Probably
  not; it likely becomes inapplicable rather than merely weaker.

*Done when:* those three are answered in `ARCHITECTURE.md`, then implemented.

**9. Reviewer approval workflow.** UC-14 names a Reviewer actor. Nothing exists.
This is what would make QA verdicts consequential rather than advisory.
*Done when:* an artefact can be held pending approval and cannot be exported
until approved.

## 7. Rules for working on this

From `docs/v1/CLAUDE.md`, which remains the owner's working-style spec and has not
been superseded. The short version:

- **Plan, then act.** Anything touching more than one file gets a plan first.
- **Define done before starting.** "Ingestion works" is not a criterion.
  "`POST /sources` with the sample PDF returns `source_id` and 11 chunks appear
  in Qdrant" is.
- **Simplest thing that works.** No abstraction for one call site.
- **Stay in scope.** Notice adjacent problems, mention them, do not fix them.
- **Show evidence, not claims.** Run it. If you cannot run it, say so.
- **Ask before adding a dependency.**

Two more the build earned:

- **Tests in the container are authoritative.** The host venv is a convenience
  and drifts. `docker compose exec app python -m pytest -q` is the truth.
- **Restart the worker after editing `app/worker.py`.** arq registers task
  functions at boot; otherwise the queue reports "function not found" and the
  job silently never runs.

## 8. The invariants

Ten, unchanged from v1, all enforced by tests. **Do not relitigate these.** If
one genuinely blocks you, say why rather than working around it.

1. No LLM anywhere in the ingestion path
2. Chunk ids are immutable; agents read chunks, never write them
3. Analysis runs once per job, cached, shared by all generators
4. Formats live in a registry — config entry, template, schema, zero code
5. Agents request `"fast"` or `"long"`, never a provider
6. The grounding checker has no web search — enforced in the registry
7. Retry counters are per artefact
8. Provider errors and parse failures never touch the QA budget
9. Prompts live in versioned files, never string literals
10. Video is a package; MP4 rendering is deterministic TTS + ffmpeg

Limits: video input max 10 minutes; QA retries max 3 then stop the job.

## 9. Demo script

Fifteen minutes, in this order.

**1. Ingest** (1 min). Paste the advisory text. Point out: hashed, chunked,
embedded, no model call anywhere in that path. Re-upload the same file — it
returns the existing id instantly.

**2. Golden path** (2 min setup, 67s run). LinkedIn post + executive summary.
While it runs, explain that analysis happens once and is shared.

**3. Show the citations** (2 min). Expand the claims on each artefact. Every
factual statement points at a chunk id. This is the whole argument.

**4. All seven formats** (2 min setup, 123s run). Same source, seven outputs,
one analysis. This is the fan-out claim made concrete.

**5. The block** (3 min). The advisory fails tone three times and is blocked.
Show the operator message and the fix note naming exact phrases to replace.
Explain why the checker is right — an advisory written for "general public" is
too jargon-dense.

> **Lead with the failure, not the successes.** A demo where everything passes
> only proves the pipeline runs. The block proves the QA layer is doing
> something.

**6. Security** (2 min). Open `app/tools/registry.py`. The grounding checker
cannot reach web search — not by instruction, by allowlist, and it cannot even
see the tool. No write tool exists to be exposed.

**7. Add a format live** (3 min, optional). Add an entry to `registry.json`,
a template and a schema. Restart. It appears in the dashboard and generates.
No Python was touched.

**Before demoing:** run the model-id check. All three ids died once already.
