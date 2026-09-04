# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Implemented and running. All 11 architecture layers are built; **180 tests pass,
3 skip (98 P0)** against the live compose stack. Provider keys are configured and
jobs generate real artefacts end to end.

Measured, not estimated. Before the editorial checker: 67s and 93s for the
two-format golden path, 123s for seven formats, 53 model calls (TC-1203/1204).

**TC-1202 is still missed, but by less than before.** A single artefact takes
~29s against a 20s target, measured with six checkers rather than four. It was
30-45s with four and no editorial checker, and briefly ~80s once editorial
landed - the editorial contract in the generator prompt cut the retries, and
caching QA verdicts cut the rest. An identical rerun is ~3s (TC-1205: provider
calls near zero on a repeat).

Retrieval is semantic. `EMBEDDING_MODEL=gemini-embedding-001` reuses the Gemini
key; `search_chunks(job_id, "what is the CVSS score")` returns the severity
chunk first on a document where it is third (§13.2's own acceptance test). A
source over `MAX_PROMPT_CHUNKS` is narrowed by top-k relevance, preserving chunk
ids. The offline hash-vector fallback remains for air-gapped runs and announces
itself on the job record.

Known gaps, stated not hidden. No auth (§13.1) - the operator profile is a name
in a cookie, not an identity. `language` is a parameter that nothing acts on
(§13.3). Gemini's free tier 429s routinely, so both aliases carry two
deployments; re-probe model ids before a demo, they drift.

## Documents (read at session start, not every turn)

**`docs/v2/` is the current spec.** It describes what is built and running; §13
marks design intent that has no code, and a claim without a test id beside it is
a gap, not a fact. `docs/v1/` is the original design, kept for history - where the
two disagree, v2 wins.

| File | Contents |
|---|---|
| `docs/v2/ARCHITECTURE.md` | The primary spec. The 11 layers as built, §12 divergences from v1, §13 planned-not-built, §14 known gaps. |
| `docs/v2/USE-CASES.md` | UC-01..UC-16, each marked Built / Partial / Stub / Not built. Three worked examples, two observed live. |
| `docs/v2/TEST-CASES.md` | TC-<area><nn> reconciled against the suite, P0 = blocks the demo, plus a ranked list of the ten gap ids. |
| `docs/v2/POC.md` | Status, five failure post-mortems worth reading, tiered backlog, demo script. |
| `docs/v1/CLAUDE.md` | Working-style expectations from the project owner (plan-then-act, define done, stay in scope, show evidence, ask before adding a dependency). **Not superseded.** |

## What this is

One source in (article, report, advisory, image, video, free-form prompt), one or more
communication artefacts out (LinkedIn post, exec summary, video package, deck, …).
Operator selects source + parameters + formats; the platform ingests once, analyses
once, generates per format, QAs per artefact, exports. Hackathon / demo scope.

## Architecture in one pass

Layers, each knowing only the one below:

1. **Dashboard** — upload, params, format multi-select, job polling, per-artefact QA
   view, download. ~40% of build effort (seven output types = seven display partials).
2. **API ingestion** — `POST /sources` (hash → extract → normalise → chunk → embed),
   `POST /jobs` (references `source_id`, enqueues), `GET /jobs/{id}` (polled). Fully
   deterministic, **no LLM**.
3. **Model gateway** (`app/gateway/`) — deterministic guardrails → router + fallback →
   prompt cache. Two aliases only, `fast` and `long`. Cache key is
   `source_hash + output_type + parameters + prompt_version + attempt_salt`; the salt
   means a retry is never served the artefact that just failed QA.
4. **LangGraph orchestration** — one `JobState` TypedDict, every node writes into it.
5. **Tool registry** (`app/tools/`) — plain Python with per-caller allowlists and
   MCP-shaped signatures; v1 offered this as a fallback to a real MCP server and it is
   what shipped. Purpose-built tools (`search_chunks(job_id, query, k)`), never a raw
   Qdrant client. No write/delete tool is registered anywhere.
6. **Input analysis** — **one** structured call (not three subagents; see Deviations),
   run once per job, cached on the job row, shared by every generator. Classifies source
   provenance here. Web enrichment is allowlisted but the provider is a stub.
7. **Output subagents** — one per selected format, driven by the output registry.
8. **QA subagents** — six independent checkers per artefact, capped at
   `qa_concurrency`: grounding, format, tone, safety, editorial, and source-reuse (the
   last only for copyrighted provenance). Format and source-reuse are deterministic and
   provably make zero model calls; safety is a deterministic PII pass *then* an LLM
   policy call; editorial measures verbatim source overlap deterministically and feeds
   that figure to its model call. Grounding, safety and editorial fail **closed**: a
   checker that could not run withholds the artefact as unverified rather than passing
   it.
9. **Export** — structured JSON → files. Video = TTS + SRT + title cards + ffmpeg.
10. **Langfuse** — traces, versioned prompts, eval runs.
11. **Docker Compose** — five services: qdrant `:6333`, postgres `:5432`, redis `:6379`,
   app `:8000`, and the arq `worker`.

## Invariants — decided, do not relitigate (from `docs/v1/CLAUDE.md` §Project invariants)

1. No LLM anywhere in the ingestion path. Never model-clean source text.
2. Chunk ids are immutable, created once at ingest. Every factual claim in an artefact
   cites a `chunk_id`. Agents read chunks, never write them.
3. Analysis runs once per job, cached, shared by all generators.
4. Output formats live in a registry: new format = config entry + prompt template +
   JSON schema, zero code change. Editing a switch statement = doing it wrong.
5. Agents request model alias `"fast"` or `"long"`, never a provider. LiteLLM routes.
6. The grounding checker must not have web search — enforce in the tool registry, not a
   prompt.
7. Retry counters are per artefact.
8. Two separate failure counters: provider errors (429, timeout) retry with backoff and
   don't count; only QA verdict failures hit the 3-strike job stop.
9. Prompts live in versioned files, never string literals in Python.
10. Video is a *package* (script, storyboard, narration, subtitles). MP4 render is
    deterministic TTS + ffmpeg. Never call it video generation.

Limits: video input max 10 min (else blocked); QA retries max 3 then stop the job. No
auth — deliberate demo scope, documented not hidden.

## Key implementation details easy to get wrong

- Embeddings call the provider **directly**, bypassing the LiteLLM router (different
  API surface, incompatible cache keys). See `ARCHITECTURE.md` §2.
- One Qdrant collection, filtered by a `source_id` payload field — not one collection
  per job.
- Cache key never contains `job_id` (a second job on the same source + settings must
  hit cache).
- Malformed generation JSON → a **parse** retry on its own counter, never a QA retry.
- Format and source-reuse checkers are deterministic (regex + length, n-gram) — assert
  zero completion calls in them.
- QA verdict is a policy, not a vote: safety fail blocks unconditionally; grounding
  fail retries with a *specific* fix note; tone below threshold retries once then
  passes flagged.
- Inside the app container, reach services by name (`http://qdrant:6333`), not
  `localhost`. Mount the Compose volumes or embeddings vanish on `down`.
- `literary_copyrighted` provenance switches generation to commentary mode (paraphrase,
  short quoted fragments only).

## Stack

Python 3.11 · FastAPI · LangGraph (+ LangChain adapters only) · LiteLLM Router ·
Groq / Gemini Flash / OpenRouter (free tiers) · Qdrant · Postgres · Langfuse ·
Docker Compose. Verify exact free-tier model identifiers against the provider before
wiring — the strings change.

## Commands

Bring up the stack (qdrant, postgres, redis, app, worker):

```bash
docker compose up -d
```

Dashboard at http://127.0.0.1:8000 — upload, pick parameters and formats, watch
(prefer `127.0.0.1`; some clients resolve `localhost` to IPv6 `::1`, which Docker's
port mapping does not answer on)
the job. Tests run in-container, which is the authoritative environment:

```bash
docker compose exec app python -m pytest -q
```

Just the ones that block the demo:

```bash
docker compose exec app python -m pytest -q -m p0
```

A single test:

```bash
docker compose exec app python -m pytest -q tests/integration/test_phase7_example_a.py -k tone
```

Lint and format (run on the host):

```bash
ruff check . && ruff format .
```

After editing `app/worker.py`, restart the worker — arq registers task
functions at boot:

```bash
docker compose restart worker
```

Tests marked `integration` need the stack; `pytest -m "not integration"` runs
on the host. Three scaffold tests skip in-container by design — they assert
facts about the repo, which `.dockerignore` correctly keeps out of the image.

## Where things live

| Concern | Path |
|---|---|
| Ingestion (zero LLM) | `app/ingest/` — `service.py` is the entry point |
| Model gateway | `app/gateway/router.py` — the ONLY file that may name a provider |
| Tool allowlist | `app/tools/registry.py` — where TC-1001 is enforced |
| Formats | `app/formats/registry.json` + `schemas/` + `app/prompts/templates/` |
| Pipeline | `app/graph/build.py` — generate → QA → verdict, per artefact |
| Verdict policy | `app/agents/verdict.py` |
| Dashboard | `app/web/` — Jinja + HTMX, seven per-format partials |

## Adding a format

Config only, no code (Invariant 4, TC-0302): add an entry to
`app/formats/registry.json`, a `<id>@v1.jinja` template, and a
`<id>.schema.json` requiring `claims[]`. It then appears in the dashboard and
generates. A test asserts the pipeline modules contain no format id literals,
so a switch statement will fail CI.

## Deviations from the docs, and why

Three places where the implementation departs from a literal reading. Each is
documented at the site.

1. **Image ingestion** (`app/ingest/extract/image.py`) — `docs/v1/ARCHITECTURE.md:45`
   specifies "vision caption + OCR", but a vision caption is a model call and
   Invariant 1 forbids those in the ingest path. Resolved as OCR at ingest,
   richer visual description deferred to the analysis layer.
2. **Analysis subagents** (`app/agents/analysis.py`) — §6 describes three
   subagents; they are one structured call. Three calls would triple the cost
   of the step the architecture insists must be cheap, and the outputs are
   consumed as one object.
3. **Retry loop** (`app/graph/build.py`) — an explicit bounded loop rather than
   LangGraph conditional edges. Routing retries through shared graph edges
   would make one artefact's retry state reachable from another, which
   Invariant 7 forbids. The fan-out is still concurrent.
