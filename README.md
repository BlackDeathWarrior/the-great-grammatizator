# The Great Grammatizator

One source in — an advisory, a report, an article, an image, a video — and one
or more finished communication artefacts out: a LinkedIn post, an executive
summary, a video package, a deck.

Every factual claim in an artefact cites the chunk of the source it came from,
and six independent checkers judge each artefact before an operator ever sees
it.

> Hackathon / demo scope. This README states what is built and measured, and
> what is not. Claims carry test ids where a test backs them.

---

## What it does

An operator picks a source, sets parameters and audience, and selects one or
more output formats. The platform ingests once, analyses once, generates per
format, QAs per artefact, and exports.

Seven formats ship in the registry:

| Format | Id |
|---|---|
| LinkedIn Post | `linkedin_post` |
| Twitter / X | `twitter_x` |
| Executive Summary | `exec_summary` |
| Advisory | `advisory` |
| Presentation | `presentation` |
| Infographic | `infographic` |
| Video Package | `video_package` |

A video package is a *package* — script, storyboard, narration, subtitles. The
MP4 render is deterministic TTS + ffmpeg. It is never video generation.

---

## Running it

Requires Docker and a provider API key. Bring up the stack — qdrant, postgres,
redis, app, worker:

```bash
docker compose up -d
```

Copy `.env.example` to `.env` and fill in at least one provider key before
running a job. Keys can also be pasted at runtime on the Settings tab, where
they are encrypted at rest.

The dashboard is at <http://127.0.0.1:8000>.

Prefer `127.0.0.1` over `localhost` — some clients resolve `localhost` to IPv6
`::1`, which Docker's port mapping does not answer on.

### Tests

The container is the authoritative environment:

```bash
docker compose exec app python -m pytest -q
```

Just the ones that block the demo:

```bash
docker compose exec app python -m pytest -q -m p0
```

Unit tests alone — fast, no provider calls, 295 pass and 3 skip:

```bash
docker compose exec app python -m pytest -q tests/unit
```

The three skips are by design: they assert facts about the repo, which
`.dockerignore` correctly keeps out of the image.

### After editing

`app/worker.py` — restart the worker; arq registers task functions at boot:

```bash
docker compose restart worker
```

A dependency in `pyproject.toml` — rebuild **both** services. `app` and
`worker` are separate images from the same Dockerfile, so building only `app`
leaves the worker on its old image, and the missing module surfaces as a job
dying rather than as a failed build:

```bash
docker compose build app worker && docker compose up -d --force-recreate app worker
```

---

## Architecture

Eleven layers, each knowing only the one below.

1. **Dashboard** — upload, parameters, format multi-select, job polling,
   per-artefact QA view, download.
2. **API ingestion** — `POST /sources` (hash → extract → normalise → chunk →
   embed), `POST /jobs`, `GET /jobs/{id}`. Fully deterministic, **no LLM**.
3. **Model gateway** (`app/gateway/`) — deterministic guardrails → router +
   fallback → prompt cache. Two aliases only, `fast` and `long`.
4. **LangGraph orchestration** — one `JobState` TypedDict; every node writes
   into it.
5. **Tool registry** (`app/tools/`) — per-caller allowlists, MCP-shaped
   signatures. Purpose-built tools such as `search_chunks(job_id, query, k)`,
   never a raw Qdrant client. No write or delete tool is registered anywhere.
6. **Input analysis** — one structured call, run once per job, cached on the
   job row, shared by every generator.
7. **Output subagents** — one per selected format, driven by the registry.
8. **QA subagents** — six independent checkers per artefact: grounding,
   format, tone, safety, editorial, and source-reuse. Format and source-reuse
   are deterministic and provably make zero model calls. Grounding, safety and
   editorial fail **closed** — a checker that could not run withholds the
   artefact as unverified rather than passing it.
9. **Export** — structured JSON → files.
10. **Langfuse** — traces, versioned prompts, eval runs.
11. **Docker Compose** — five services.

The prompt cache key is
`source_hash + output_type + parameters + prompt_version + attempt_salt`. The
salt means a retry is never served the artefact that just failed QA. The key
never contains `job_id`, so a second job on the same source and settings hits
cache.

### Adding a format

Config only, no code (TC-0302): add an entry to `app/formats/registry.json`, a
`<id>@v1.jinja` template, and a `<id>.schema.json` requiring `claims[]`. It
then appears in the dashboard and generates. A test asserts the pipeline
modules contain no format id literals, so a switch statement fails CI.

### Where things live

| Concern | Path |
|---|---|
| Ingestion (zero LLM) | `app/ingest/` — `service.py` is the entry point |
| Model gateway | `app/gateway/router.py` — the only file that may name a provider |
| Tool allowlist | `app/tools/registry.py` |
| Formats | `app/formats/registry.json` + `schemas/` + `app/prompts/templates/` |
| Pipeline | `app/graph/build.py` |
| Verdict policy | `app/agents/verdict.py` |
| Dashboard | `app/web/` — Jinja + HTMX |

---

## Invariants

Decided, and not relitigated:

1. No LLM anywhere in the ingestion path. Never model-clean source text.
2. Chunk ids are immutable, created once at ingest. Agents read chunks, never
   write them.
3. Analysis runs once per job, cached, shared by all generators.
4. Output formats live in a registry. Editing a switch statement is doing it
   wrong.
5. Agents request alias `"fast"` or `"long"`, never a provider.
6. The grounding checker must not have web search — enforced in the tool
   registry, not in a prompt.
7. Retry counters are per artefact.
8. Two separate failure counters: provider errors (429, timeout) retry with
   backoff and do not count; only QA verdict failures hit the 3-strike stop.
9. Prompts live in versioned files, never string literals in Python.
10. Video is a package. The MP4 render is deterministic.

---

## Performance, measured

Not estimated. Measured against the live compose stack:

- Two-format golden path: 67s and 93s (TC-1203/1204).
- Seven formats: 123s, 53 model calls.
- A single artefact: ~29s against a 20s target, with six checkers. **TC-1202
  is still missed**, by less than before — it was 30–45s with four checkers
  and no editorial, and briefly ~80s once editorial landed. The editorial
  contract in the generator prompt cut the retries; caching QA verdicts cut
  the rest.
- An identical rerun: ~3s, provider calls near zero (TC-1205).

Retrieval is semantic. `EMBEDDING_MODEL=gemini-embedding-001` reuses the Gemini
key. An offline hash-vector fallback remains for air-gapped runs and announces
itself on the job record.

---

## Known gaps

Stated, not hidden.

- **No authentication** (§13.1). The operator profile is a name in a cookie,
  not an identity. The sign-in screen is explicitly a mock and says so on its
  face.
- **`language` is a parameter that nothing acts on** (§13.3).
- **Gemini's free tier returns 429 routinely.** Both aliases carry two
  deployments. Re-probe model ids before a demo — they drift.
- **Integration-test teardown** hits a foreign key violation from `feedback`
  to `artefacts` (`feedback_artefact_id_fkey` has no `ondelete`). It affects
  cleanup between integration tests, not application behaviour. Unit tests are
  unaffected.

---

## Documentation

`docs/v2/` is the current spec. `docs/v1/` is the original design, kept for
history — where the two disagree, v2 wins.

| File | Contents |
|---|---|
| `docs/v2/ARCHITECTURE.md` | The primary spec. Eleven layers as built, §12 divergences, §13 planned-not-built, §14 known gaps. |
| `docs/v2/USE-CASES.md` | UC-01..UC-16, each marked Built / Partial / Stub / Not built. |
| `docs/v2/TEST-CASES.md` | TC-`<area><nn>` reconciled against the suite; P0 blocks the demo. |
| `docs/v2/POC.md` | Status, five failure post-mortems, tiered backlog, demo script. |
| `docs/v2/PIPELINE-REVIEW.md` | Pipeline latency review. |

Three places where the implementation departs from a literal reading of the
docs, each documented at the site:

1. **Image ingestion** — v1 specifies "vision caption + OCR", but a vision
   caption is a model call and Invariant 1 forbids those at ingest. Resolved
   as OCR at ingest; richer description deferred to analysis.
2. **Analysis subagents** — §6 describes three; they are one structured call.
   Three would triple the cost of the step the architecture insists must be
   cheap, and the outputs are consumed as one object.
3. **Retry loop** — an explicit bounded loop rather than LangGraph conditional
   edges, because shared graph edges would make one artefact's retry state
   reachable from another, which Invariant 7 forbids.

---

## Stack

Python 3.11 · FastAPI · LangGraph · LiteLLM Router · Groq / Gemini Flash /
OpenRouter / Mistral / NVIDIA NIM · Qdrant · Postgres · Redis · Langfuse ·
Docker Compose.
