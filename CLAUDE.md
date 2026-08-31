# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Implemented and running. All 11 architecture layers are built; 177 tests pass
(96 P0) against the live compose stack.

Not yet done: no provider keys are configured, so generation raises a clean
`ProviderError` until `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY`
are set in `.env`. Everything up to the model call — ingestion, chunking,
embedding, retrieval, the registry, QA plumbing, export, dashboard — runs
without them. Latency and call-budget numbers (TC-1203/1204) still need
measuring against a real provider before the demo.

## Documents (read at session start, not every turn)

| File | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | The 11 layers, per-layer design rationale, Docker Compose, known gaps. The primary spec. |
| `docs/USE-CASES.md` | UC-01..UC-14, parameter schema, two fully worked end-to-end examples (A: advisory → LinkedIn + exec summary; B: literary excerpt → video package). |
| `docs/TEST-CASES.md` | TC-<area><nn>, P0 = blocks the demo. Build to the P0s. |
| `docs/CLAUDE.md` | Working-style expectations from the project owner (plan-then-act, define done, stay in scope, show evidence, ask before adding a dependency). |

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
3. **LiteLLM gateway** — guardrails → Router + fallback → prompt cache. Cache key is
   `source_hash + output_type + parameters`.
4. **LangGraph orchestration** — one `JobState` TypedDict, every node writes into it.
5. **MCP gateway + tool registry** — per-agent tool allowlist. Purpose-built tools
   (`search_chunks(job_id, query, k)`), never a raw Qdrant client.
6. **Input analysis subagents** — web search, content analysis, input analysis. Runs
   once per job, cached, shared by every generator. Classifies source provenance here.
7. **Output subagents** — one per selected format, driven by the output registry.
8. **QA subagents** — 4 parallel independent checkers per artefact (grounding, format,
   tone, safety) + source-reuse for copyrighted sources. Format and source-reuse are
   deterministic; the other three are model calls.
9. **Export** — structured JSON → files. Video = TTS + SRT + title cards + ffmpeg.
10. **Langfuse** — traces, versioned prompts, eval runs.
11. **Docker Compose** — qdrant `:6333`, postgres `:5432`, app `:8000`.

## Invariants — decided, do not relitigate (from `docs/CLAUDE.md` §Project invariants)

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

Dashboard at http://localhost:8000 — upload, pick parameters and formats, watch
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

1. **Image ingestion** (`app/ingest/extract/image.py`) — `ARCHITECTURE.md:45`
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
