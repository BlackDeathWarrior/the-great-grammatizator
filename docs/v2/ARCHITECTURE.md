# Architecture (v2)

Gen AI platform for automated content transformation. One source in, one or more
communication artefacts out, every factual claim cited back to an immutable
chunk of the source.

**This document describes what is built and running.** Where something is
planned but not built, it is in §13 under a heading that says so. Nothing in
§1–§12 is aspirational — if it is described here, there is code for it and a
test id beside it.

Supersedes `docs/v1/ARCHITECTURE.md`, which described an intended design. That file
stays as the record of original intent; code comments cite it by section number.
Where v1 and reality diverged, §12 says how and why.

---

## Design principle

**Deterministic preprocessing upstream, model calls downstream.**

Everything cheap and repeatable — extraction, normalisation, chunking, embedding
— happens once, before any agent exists. Everything expensive and
non-deterministic — analysis, generation, QA — happens after, against a frozen
content object.

The corollary that matters most: because the content object is frozen and its
chunk ids are immutable, a generated claim citing `c3` can be checked against
exactly the text the model was shown. That is the whole grounding story, and
every other decision defers to it.

---

## System shape

```
operator ──▶ Dashboard (Jinja + HTMX)          app/web/
                │
                ├── POST /sources ──▶ Ingestion (NO LLM)      app/ingest/
                │                      hash → dedup → extract → normalise
                │                      → chunk → embed → Qdrant
                │
                └── POST /jobs ────▶ Redis queue ──▶ arq worker    app/worker.py
                                                        │
                                                        ▼
                                          Orchestration            app/graph/build.py
                                                        │
                                    ┌───────────────────┼───────────────┐
                                    ▼                   ▼               ▼
                              analyse ONCE      generate per format   QA per artefact
                            app/agents/         app/agents/           app/agents/qa/
                            analysis.py         generator.py          (5 checkers)
                                    │                   │               │
                                    └───────────────────┴───────────────┘
                                                        ▼
                                                 verdict policy      app/agents/verdict.py
                                                        ▼
                                                 export to files     app/export/
```

Every model call goes through `app/gateway/router.py`. Every tool call goes
through `app/tools/registry.py`. Those two chokepoints are what make the
invariants enforceable rather than aspirational.

---

## 1. Dashboard — `app/web/`

Jinja templates served by FastAPI, HTMX for polling. No build step, no
`node_modules`, one process. `htmx.min.js` is vendored into `app/web/static/`
rather than pulled from a CDN: a demo should not depend on someone else's
uptime.

| Route | Purpose |
|---|---|
| `GET /` | Upload box, recent sources, parameters, format multi-select |
| `POST /ui/sources` | Ingest, redirect to `/?source_id=…` |
| `POST /ui/jobs` | Create job, redirect to its live view |
| `GET /jobs/{id}/view` | Job page; HTMX polls the partial below |
| `GET /ui/jobs/{id}/status` | Status partial, re-fetched every 2s |
| `POST /ui/jobs/{id}/artefacts/{type}/regenerate` | UC-09 |
| `GET /downloads/{job}/{type}/{file}` | Serve a rendered artefact |

Seven output types means **seven display partials**
(`app/web/templates/partials/artefact_<id>.html`). This is the single largest
share of frontend effort; budget it rather than treating it as one box.

Two behaviours that are load-bearing, not cosmetic:

- **Progress is reported honestly.** "4 of 7 done", never "complete" while work
  remains (TC-0803). The status partial computes `done_count` from artefacts
  that actually settled.
- **Blocked and flagged artefacts are rendered, marked.** Hiding them leaves the
  operator wondering where their deck went (TC-0610).

Parameters are **closed-vocabulary dropdowns** defined in `VOCAB` in
`app/web/routes.py` — zero free-text parameter inputs (TC-0202). Free text gives
the tone checker nothing concrete to compare against.

The download route builds a filesystem path from URL segments, so it resolves
the candidate and refuses anything outside the storage root. Path traversal in
three encodings returns 404 (tested).

## 2. Ingestion — `app/ingest/`, zero LLM

**Invariant 1.** Nothing in this path may call a completion endpoint. Enforced
two ways: a behavioural test that patches `litellm` and asserts zero calls
(TC-0117), and a structural test that no module under `app/ingest/` imports
`app.gateway` at all.

Pipeline, in `app/ingest/service.py`:

```
hash → dedup lookup → stage bytes → extract → normalise → chunk → embed → ready
```

| Endpoint | Behaviour |
|---|---|
| `POST /sources` | The pipeline above. Returns `source_id`, `chunk_count`, `reused` |
| `GET /sources` | Recent sources for reuse (UC-12) |

Extraction by type, `app/ingest/extract/`:

| Input | Handler | Notes |
|---|---|---|
| PDF | PyMuPDF | OCR fallback via tesseract when text layer < 200 chars |
| DOCX | python-docx | Headings preserved as `## ` so chunking splits on them |
| HTML / URL | trafilatura | Boilerplate stripped |
| Image | tesseract OCR | Original retained in `media[]`. See §12.1 |
| Video | faster-whisper | **10-minute gate checked before transcription** |

The video gate reads container metadata via `ffprobe` and raises before any
transcription work, so an over-limit upload costs nothing and leaves no partial
source row (TC-0108). Exactly 10:00 is accepted (TC-0107).

**Chunking** — `app/ingest/chunk.py`. `RecursiveCharacterTextSplitter`,
~3200 chars (~800 tokens) with 320-char overlap. Ids are `c1..cN` in document
order, assigned once, **never renumbered** (Invariant 2, TC-0114). Chunks carry
an optional page number so a citation can point at a page.

**Embedding** — `app/ingest/embed.py`. Calls the provider SDK **directly**,
bypassing the LiteLLM router: different API surface, no completion, incompatible
cache keys (TC-0905). One Qdrant collection for everything, filtered by a
`source_id` payload field — not one collection per job.

When no embedding key is configured, `embed_texts` falls back to a deterministic
hash-derived vector and logs a warning. Chunks still store and retrieve, and
citations remain exact because claims cite chunk ids directly — but ranking is
not semantic. See §13.2.

### Why this layer exists

- The HTTP request cannot wait 30–90s for generation
- OCR is CPU-bound and must not consume model-call concurrency
- Chunk ids must be created once and shared, or citations are meaningless
- Content hash enables reuse — the same file is never ingested twice
- Bad uploads fail in ~2s rather than mid-generation after spending tokens
- The queue is where rate limiting and backoff live

### Sources are separate from jobs

A source is ingested once; a job references it. `Source` has a unique constraint
on `source_hash`, so dedup is enforced by the database rather than application
logic — a concurrent second upload cannot slip past it (TC-0109).

## 3. Model gateway — `app/gateway/`

**The only module permitted to name a provider** is `router.py`.

Agents request an alias — `"fast"` or `"long"` — and never learn who served
them (Invariant 5). Enforced by two tests: no provider token appears in code
outside the gateway, and no module outside the gateway reads a provider key
setting from config. `config.py` must spell the key names and is exempt from the
first check; the second is the one with teeth.

| Alias | Used for | Rationale |
|---|---|---|
| `fast` | Short-form generation, all three LLM checkers | Demo latency depends on it |
| `long` | Long structured output (advisory, deck, video package) | Context size, reliable JSON |

A second entry under `fast` gives a separate rate-limit pool. Fallbacks run both
directions; `num_retries=2`, 30s cooldown on repeated failure.

> **Model ids drift.** Every id in v1's architecture was dead by the first live
> run — the Groq model no longer existed, the Gemini model was retired, and the
> OpenRouter `:free` slug had become paid-only. Re-probe before any demo. The
> current working set is pinned in `router.py` with a comment saying exactly
> this.

**`ProviderError`** is a distinct exception type. Everything reaching the router
and failing becomes one. That separation is what lets callers route
infrastructure failures away from the quality budget (Invariant 8).

**Cache** — `cache.py`. Key is `source_hash + output_type + parameters`, never
`job_id`, so a second job on the same source and settings hits cache
(TC-0204/0205). Two additions beyond v1:

- `prompt_version` participates, so editing a template invalidates cleanly
- a retry carries an `attempt_salt`, so a retry is never served the cached
  artefact that just failed QA — without it, fix notes achieve nothing

**Guardrails** — `guardrails.py`. Deterministic PII patterns and a prompt-size
check, run before a prompt leaves the process. Not a model call; LLM safety
judgement belongs in the QA safety checker, which sees a finished artefact.

**Concurrency cap** — `qa_semaphore()`, sized from `QA_CONCURRENCY`. The three
LLM checkers do not all fire at once, or a free tier rate-limits mid-demo
(TC-0904).

## 4. Orchestration — `app/graph/`

`state.py` defines the shape first; every node reads and writes it and nothing
else. `JobState` carries `content`, `analysis`, `parameters`, `artefacts` (keyed
by `output_type`) and `status`.

`build.py` has two entry points:

- **`run_artefact(format_id, content, analysis, parameters)`** — generate one
  artefact and drive it through QA until settled. Returns
  `(artefact, operator_message)`.
- **`run_job(job_id, content, parameters, format_ids)`** — analyse once, then
  fan out across formats with `asyncio.gather(..., return_exceptions=True)` so
  one failing generator cannot stop the others (TC-0407).

The per-artefact retry loop is an **explicit bounded loop**, not LangGraph
conditional edges. Retries are per artefact and artefacts are independent;
routing retries through shared graph edges would make one artefact's retry state
reachable from another, which Invariant 7 forbids. The fan-out is still
concurrent. See §12.3.

## 5. Tool registry — `app/tools/`

Plain Python with per-caller allowlists, MCP-shaped signatures. v1 offered this
as an acceptable fallback to a real MCP server; it is what shipped. The security
argument does not depend on the transport.

| Caller | Allowed groups |
|---|---|
| `INPUT_ANALYSIS` | retrieval, web_search |
| `OUTPUT_GENERATOR` | retrieval |
| `GROUNDING_CHECKER` | retrieval — **never** web_search |
| `TONE_CHECKER` | (none) |
| `SAFETY_CHECKER` | (none) |
| `EDITORIAL_CHECKER` | (none) |
| `EXPORT` | render, media |

**The grounding restriction is the load-bearing one** (Invariant 6, TC-1001).
With web search, the checker verifies claims against the internet instead of the
source document and silently defeats itself. It holds two ways: the allowlist
denies the call, and `available(GROUNDING_CHECKER)` does not list the tool at
all — a model that cannot see a tool never tries to call it, so there is no
refusal to argue with.

**No write tool exists anywhere.** Not denied — absent. A test asserts no
registered tool name contains write/delete/upsert/drop/update/insert, so no
future allowlist mistake can expose one (TC-1002, Invariant 2).

Retrieval tools take `job_id` as their **first parameter**, so cross-job access
is not expressible rather than merely discouraged (TC-1003/1004). A test asserts
that signature shape directly. An unbound job returns empty rather than raising,
which also avoids leaking whether that job exists.

Every call is audited, **denials included** — an attempted breach that left no
record would be the one worth knowing about (TC-1005).

## 6. Analysis — `app/agents/analysis.py`

Runs **once per job**, cached on the job, shared by every generator
(Invariant 3). This is what makes seven formats cost roughly one format's
analysis (TC-0402, TC-0408).

Produces one `AnalysisResult`: objective, audience, key facts, entities,
enrichment, and **provenance**. A `literary_copyrighted` source sets
`commentary_mode`, which switches generation to paraphrase with short quoted
fragments only.

A provider error here degrades to the operator's own parameters rather than
failing the job. Analysis is one call; losing it should not cost seven
generations that would otherwise succeed.

v1 described three subagents; this is one structured call. See §12.2.

## 7. Generation — `app/agents/generator.py`

Prompt = template + content object + analysis + parameters + constraints, all
supplied by the registry entry. Response is structured JSON containing a
`claims[]` array, each claim carrying `text` and `chunk_id`.

**Formats are data.** `app/formats/registry.json` plus a schema and a template
per format. Adding one is config-only (Invariant 4, TC-0302), and a test asserts
the pipeline modules contain **no format id literals** — a switch statement
fails CI.

| id | alias | renderer | key constraints |
|---|---|---|---|
| `video_package` | long | video | scenes 3–8, runtime target 240s ±45 |
| `linkedin_post` | fast | markdown | max 3000 chars, ≤5 hashtags |
| `twitter_x` | fast | markdown | 280 chars/tweet, ≤8 tweets |
| `advisory` | long | docx | max 8000 chars, ≥1 recommendation |
| `infographic` | fast | json | 3–8 panels, headline ≤90 chars |
| `exec_summary` | long | docx | max 4000 chars, 3–7 key points |
| `presentation` | long | pptx | 4–12 slides, ≤6 bullets/slide |

Every schema **requires** `claims[]` with `chunk_id` matching `^c\d+$`. A format
that could omit it would force the checker to search the whole document.

Templates share `_shared.jinja`, which carries the grounding contract, the chunk
list, the analysis, the commentary-mode conditional and the fix-notes block, so
the contract cannot drift between formats.

> The shared block shows the **literal JSON shape** of a claim. Describing it in
> prose was not enough: models emitted `{"claim":…, "citations":…}` and burned
> every parse retry. Keep the example.

**Malformed JSON or a schema violation is a PARSE failure** on its own counter,
never a QA retry (TC-0405). The model failed to speak the protocol; the content
was never assessable. The retry tells the model exactly how it broke the
protocol — a bare "try again" reproduces the same malformed output.

## 8. Quality assurance — `app/agents/qa/`

Five checkers per artefact, **independent**: each sees the artefact and its own
criteria, never another checker's verdict, or they anchor on each other
(TC-0508). Independence is structural — each coroutine is handed only what it
needs, so there is no channel through which one verdict could reach another.

| Checker | Kind | Gate |
|---|---|---|
| Grounding | LLM, per claim vs its **cited chunk** | hard, triggers retry |
| Format | **deterministic** — regex, length, registry constraints | hard |
| Tone | LLM score + reason string | threshold, advisory |
| Safety | deterministic PII pass, then LLM policy | hard, blocks outright |
| Source reuse | **deterministic** n-gram | copyrighted sources only |

A tweet over 280 characters is not a judgement call. `format_check.py` and
`reuse.py` make zero model calls, asserted behaviourally (TC-0503) and
structurally (neither imports the gateway).

A cited-but-nonexistent chunk id is caught **before any model call** — no
completion is needed to know `c99` is absent from the document (TC-0404).

### Verdict policy — a policy, not a vote

`app/agents/verdict.py`. "Three of four passed" is meaningless when the failure
is safety.

| Condition | Outcome |
|---|---|
| Safety fail | **Block unconditionally**, even with everything else passing (TC-0602) |
| Source reuse over block threshold | Block — reproduction, not transformation |
| Grounding or format fail | Retry with **specific** fix notes (TC-0603/0604) |
| Tone below threshold | Retry once, then pass **flagged** (TC-0605) |
| Budget exhausted | Stop the job, operator restart message (TC-0607/0805) |

Safety is evaluated first and unconditionally, so no combination of other passes
can rescue an unsafe artefact.

Fix notes must name the failing claim or the violated constraint. "Quality
insufficient" returns the same output. Observed live: a tone failure produced
*"replace 'unauthenticated attackers' with 'hackers' and 'CVSS scale' with 'a
widely-used measure of security risk'"* — that is what makes a retry converge.

### Three separate failure counters

Invariant 8. Only `retry_count` gates the 3-strike stop.

| Counter | Incremented by | Gates |
|---|---|---|
| `retry_count` | QA verdict failures only | the 3-strike job stop |
| `parse_retry_count` | malformed JSON / schema violation | its own small budget |
| `provider_error_count` | 429, timeout, outage | **nothing** — diagnostic only |

Otherwise a flaky free tier kills jobs that were generating fine. Verified on a
live job with no keys configured: provider error count 1, QA retry count 0.

## 9. Export — `app/export/`

Dispatch is on the registry's `renderer` field, not on format id, so seven
formats share five renderers and a new format naming an existing renderer needs
no change here.

| Renderer | Produces |
|---|---|
| `markdown` | `.md` + `.json` |
| `json` | `.json` |
| `docx` | `.docx` + `.pdf` |
| `pptx` | `.pptx` with **populated speaker notes** (TC-0702) |
| `video` | `.json`, `.srt`, per-scene `.png` + `.mp3`, stitched `.mp4` |

Renderers walk whatever shape the schema produced rather than knowing about
specific formats. Citations are rendered into every output — they are the point
of the pipeline.

Export runs **through the tool registry**, so it is subject to the same
allowlist as everything else and appears in the audit trail. Blocked artefacts
never reach it.

### Video is a package, not generated frames

Invariant 10. The model produced script, storyboard, scene descriptions,
narration, subtitles and visual direction — all text. Rendering is deterministic:

```
narration  → Edge TTS        → audio
subtitles  → SRT timed from duration_sec
scenes     → title cards drawn with PIL
all        → ffmpeg concat   → mp4
```

No generative video API is called anywhere; asserted by a test that scans the
export path for such references (TC-0705). Each stage degrades independently: no
ffmpeg still yields audio and stills, no TTS still yields SRT and cards.

> **Scene duration comes from `duration_sec`, not from TTS length.** The first
> implementation used `ffmpeg -shortest`, so scenes lasted as long as their
> narration — an 11s package rendered as a 6s video while the SRT still
> described 11s, and subtitles ran past the end. Narration shorter than the
> scene is padded with silence; longer extends the scene. A regression test
> probes the actual file duration.

## 10. Persistence — `app/db/`

| Table | Holds |
|---|---|
| `sources` | Unique on `source_hash`. Chunks stored as JSONB, canonical |
| `jobs` | Parameters, formats, the shared analysis, operator message |
| `job_sources` | Association — a job may reference several sources |
| `artefacts` | Content, claims, three counters, export paths, status |
| `qa_results` | **One row per checker per attempt** |

QA results are kept per attempt rather than overwritten, so a tone score moving
0.61 → 0.84 across a retry stays visible to the dashboard and to eval runs.

`JobStatus` distinguishes `failed_recoverable` from `failed_permanent` — v1
listed this as an open gap; it is closed (TC-0804). A provider outage is
recoverable and the operator should retry; a corrupt source is not.

## 11. Queue, observability, deployment

**Queue** — Redis + arq, `app/worker.py`. `POST /jobs` validates the whole
format selection, creates rows, enqueues, and returns in well under a second
(TC-0801, measured at 0.10s). Tasks: `run_job_task`, `regenerate_task`, `ping`.
`job_timeout` is 900s — the default 300s would kill a healthy seven-format job.

> arq registers task functions at boot. **After editing `app/worker.py`,
> `docker compose restart worker`** or the queue reports "function not found".

**Observability** — `app/observability.py`. Langfuse traces plus in-process
counters for model calls by alias, tool calls and per-span latency, so the call
budget is measured rather than estimated (TC-1204).

Tracing is never load-bearing: unconfigured or unreachable, every function
degrades to a no-op. That is correct behaviour, and it is also a trap — when
the Langfuse SDK changed its API, tracing silently stopped reaching the
dashboard while everything appeared healthy. Traces are flushed explicitly at
the end of each job because the buffer would otherwise be discarded.

**Eval harness** — `app/eval/`. A fixed case set with pinned prompt versions;
reports pass rate by checker, mean retries, mean latency, total model calls.
An `expected_properties` key the harness does not recognise is reported as a
**failure**, not skipped — an eval that silently ignores an assertion is worse
than no eval.

**Deployment** — five compose services: `qdrant:6333`, `postgres:5432`,
`redis:6379`, `app:8000`, `worker`.

Two things that cost time if missed:

1. Inside a container, address services **by name** (`http://qdrant:6333`), not
   `localhost` (TC-1107).
2. **Mount the volumes** or embeddings vanish on every `down` and you re-ingest
   before every run (TC-1106).

On the host, prefer `127.0.0.1` over `localhost` — some clients resolve
`localhost` to IPv6 `::1`, which Docker's port mapping does not answer on.

## 12. Where v1's design and reality diverged

Each documented at the code site. This list is the record a future session is
told to trust, so a divergence that is not here is a divergence nobody wrote
down - add to it rather than leaving one implicit.

### 12.1 Image ingestion is OCR only

`docs/v1/ARCHITECTURE.md:45` specifies "vision caption + OCR". A vision caption is
a model call, and Invariant 1 forbids model calls in the ingestion path. The
invariant wins: OCR runs at ingest, and richer visual description is deferred to
the analysis layer, which may call models and can read `media[]` from the frozen
content object. `app/ingest/extract/image.py` records this.

### 12.2 Analysis is one call, not three subagents

v1 §6 describes three subagents. Three calls would triple the cost of the one
step the architecture insists must be cheap, and the outputs are consumed as a
single object. Collapsed into one structured call that also classifies
provenance, since that needs the same document read.

### 12.3 The retry loop is explicit, not graph edges

v1 §4 implies LangGraph conditional edges throughout. Retries are per artefact
(Invariant 7) and artefacts are independent; shared graph edges would make one
artefact's retry state reachable from another. An explicit bounded loop inside
one fan-out branch says exactly what is meant. The fan-out remains concurrent.

### 12.4 Parameters are entered by interview, and validated by shape

v1's UC-02 mandates closed vocabularies, with a stated reason: free text gives
the tone checker nothing concrete to compare against. That reason is sound and
still holds, but the lists were also restrictive - five audiences, four styles,
no way to say "second-year CS students who have not seen memory safety before".

The interview (`app/agents/interview.py`) replaces the input METHOD, not the
data contract. It reads the source, proposes a brief, and takes corrections;
what it produces is the same six structured `Parameters`. The dashboard offers
both doors onto those fields - "Describe it" and "Pick from lists" - and the
interview's result lands *in* the dropdowns as an editable confirmation step,
so the operator always sees and can override what the model decided.

Three consequences worth stating:

- The selects remain in the DOM, so **TC-0202 passes unmodified**. A value the
  interview proposes that is not in `VOCAB` is added as a selected option
  rather than discarded.
- `Parameters` is now validated for **shape** rather than list membership -
  non-blank, single-line, under 200 characters (TC-0206). A richer audience
  description gives the tone and editorial checkers *more* to compare against,
  not less, which is the opposite of what the original constraint feared.
- The interview is a convenience and never a gate: a provider failure returns
  a message pointing at the lists, and the operator can always start a job.

### 12.5 A sixth checker judges the writing itself

Format, grounding, safety and source-reuse all check properties *of* an
artefact; tone judges fit and says so explicitly ("You judge fit only"). None
of them asked whether the writing was any good, and the first live run shipped
a LinkedIn post with 36 consecutive words copied from the source that every
checker passed.

`app/agents/qa/editorial.py` scores specificity, substance, originality and
structure, and measures verbatim overlap deterministically rather than trusting
the model to notice it. Editorial failures retry with the failing dimension
named and block when the budget is spent; tone gains a floor below which an
artefact is withheld rather than flagged.

## 13. Planned, not built

Everything below is **design intent**. There is no code and no test id. A future
session must not treat these as invariants.

### 13.1 Authentication and multi-tenancy

Currently absent by explicit decision. The retrieval story is already
tenant-shaped — `search_chunks(job_id, …)` is scoped by signature — but scoping
is bound in process memory (`_JOB_SOURCES`) rather than derived from an
authenticated principal.

Intended shape:

- A `tenant` table; `sources` and `jobs` gain a `tenant_id`
- Qdrant payload gains `tenant_id`; every retrieval filter requires it
- Tool calls carry a principal, and `bind_job` derives scope from it rather than
  trusting the caller
- The audit log records the principal, not just the caller role

The invariant to preserve: cross-tenant access must be **structurally
impossible**, the way cross-job access already is. If it becomes a filter an
agent could omit, this has been done wrong.

### 13.2 Real semantic retrieval

`embed_texts` currently falls back to deterministic hash-derived vectors when no
embedding key is set. Chunks store and retrieve, and citations are exact because
claims cite ids directly — but `search_chunks` cannot rank by meaning, so
retrieval returns arbitrary chunks rather than relevant ones.

This matters most for two things that are otherwise blocked:

- **Multi-source jobs.** A job referencing several sources merges chunks into
  one retrieval set; without semantic ranking, merging is not useful.
- **Long sources.** Today the generator sees every chunk inline in the prompt.
  That works for a 3-page advisory and will not for a 60-page report.

Intended: configure an embedding provider, keep the offline fallback for tests
and air-gapped demos, and make the fallback loud — a warning in the job record,
not only in logs.

### 13.3 Translation and multi-language output

`language` is already a parameter, is already in the cache key, and reaches the
prompt. Nothing acts on it beyond that: `VOCAB` offers only `en`.

Open questions a future session must answer before building:

- Does the tone checker judge in the target language or in English?
- Does grounding compare a translated claim against an English chunk? (It must —
  the source does not change — which means the grounding prompt needs the
  language pair.)
- Does source reuse n-gram matching still mean anything across a translation?
  (Probably not; it likely becomes inapplicable rather than merely weaker.)

Adding target languages to `VOCAB` without answering these ships a feature whose
QA layer silently stops working.

## 14. Known gaps — stated, not hidden

| Gap | Status |
|---|---|
| Authentication | Not built. §13.1 |
| Semantic retrieval | Offline fallback active. §13.2 |
| Translation | Parameter exists, unimplemented. §13.3 |
| Reviewer approval | UC-14 names the actor; no workflow built |
| Chunk selection | Generators see all chunks inline; no top-k selection yet |
| Rate-limit tuning | Fallbacks configured, backoff policy not tuned under load |
| Model id drift | Ids die without warning. Re-probe before every demo |
