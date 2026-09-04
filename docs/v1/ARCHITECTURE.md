# Architecture

Gen AI Platform for Automated Content Transformation.

One source in, one or more communication artefacts out. Layers top to bottom; each
layer only knows about the one below it.

![Implementation architecture](architecture.svg)

---

## Design principle

**Deterministic preprocessing upstream, model calls downstream.**

Everything cheap and repeatable (extraction, normalisation, chunking, embedding)
happens once, before any agent exists. Everything expensive and non-deterministic
(generation, QA) happens after, against a frozen content object.

---

## 1. Dashboard

Upload box · recent-sources list · parameter controls · format multi-select · job
status polling · per-artefact results with QA findings · download.

Six output types means six display components. This is roughly 40% of total build
effort — budget it rather than treating it as one box on a diagram.

## 2. API ingestion — deterministic, no LLM

| Endpoint | Behaviour |
|---|---|
| `POST /sources` | hash → store → extract → normalise → chunk → embed → ready |
| `POST /jobs` | references `source_id` + parameters + formats, enqueues, returns `job_id` |
| `GET /jobs/{id}` | status + per-artefact state, polled by the dashboard |

Extraction by type:

| Input | Handler | Notes |
|---|---|---|
| PDF | PyMuPDF | OCR fallback if extraction < ~200 chars (scanned) |
| DOCX | python-docx | |
| HTML / URL | trafilatura | |
| Image | vision caption + OCR | kept as attachment too |
| Video | Whisper transcript | **max 10 min**, else blocked with paywall stub |

Normalised content object — the only thing downstream reads:

```json
{
  "source_id": "s_8f2a",
  "source_hash": "sha256:...",
  "source_type": "pdf",
  "title": "Critical auth bypass in ...",
  "text": "full extracted text",
  "chunks": [{"id": "c1", "text": "...", "page": 1}],
  "media": []
}
```

Chunking: `RecursiveCharacterTextSplitter`, ~800 tokens with overlap. Embeddings go
to Qdrant in **one collection**, filtered by a `source_id` payload field — not one
collection per job.

Embeddings call the provider **directly, not through LiteLLM**. Different API surface,
no guardrails, no completion, incompatible cache keys.

### Why this layer exists

- The HTTP request cannot wait 30–90s for generation
- OCR is CPU-bound and must not consume model-call concurrency
- Chunk ids must be created once and shared, or grounding citations are meaningless
- Content hash enables source reuse — the same file is never ingested twice
- Bad uploads fail in ~2s rather than mid-generation after spending tokens
- The queue is where rate limiting and backoff live

### Sources are separate from jobs

A source is ingested once. A job references it. One source, many jobs — the operator
can return the next day and generate a new format without re-uploading.

Three input modes: new upload · existing source (skip ingestion) · multiple sources
(job references several `source_id`s; chunks merge into one retrieval set).

## 3. LLM gateway — LiteLLM

Guardrails (safety, PII, scope) → Router + fallback → prompt cache.

Cache key is `source_hash + output_type + parameters`. Never `job_id`, or a second job
on the same source with the same settings misses the cache.

Routing by task shape, not preference:

| Task | Alias | Provider | Why |
|---|---|---|---|
| Short-form generation | `fast` | Groq | fastest tokens/sec; demo latency depends on it |
| Long structured output | `long` | Gemini Flash | context size, reliable JSON |
| QA checkers (4 parallel) | `fast` | Groq | speed dominates |
| Overflow when rate-limited | — | OpenRouter | separate rate-limit pool |

```python
router = Router(
    model_list=[
        {"model_name": "fast", "litellm_params": {"model": "groq/llama-3.3-70b-versatile"}},
        {"model_name": "fast", "litellm_params": {"model": "openrouter/..."}},
        {"model_name": "long", "litellm_params": {"model": "gemini/gemini-2.5-flash"}},
    ],
    fallbacks=[{"fast": ["long"]}],
    num_retries=2,
)
```

Verify exact model identifiers against the provider's current model list before
wiring — free-tier model strings change.

Free tiers **will** rate-limit during the demo. Mitigations: LiteLLM `fallbacks`, a
concurrency cap on QA checkers rather than firing all four at once, and aggressive
caching so a repeat demo run hits cache.

## 4. Agent orchestration — LangGraph

Define the state object **first** and keep every node writing into that one shape:

```python
class JobState(TypedDict):
    content: ContentObject          # frozen after ingest
    analysis: AnalysisResult | None  # written once
    artefacts: dict[str, Artefact]   # keyed by output_type
    # each Artefact carries its own status, qa_result, retry_count
```

Nodes passing bespoke payloads to each other is where these graphs get tangled.

Langflow is optional, for visual prototyping and the demo only. Not in the runtime
path. Nothing depends on it.

## 5. MCP gateway + tool registry

Two gateways, two concerns. LiteLLM governs *model* calls: which provider, is it safe,
is it cached. MCP governs *tool* calls: who may call what, with which credential,
logged.

Expose purpose-built tools — `search_chunks(job_id, query, k)` — never a raw Qdrant
client. Scoped signatures make cross-job access structurally impossible.

| Agent | Allowed tools |
|---|---|
| Input analysis | retrieval, web search |
| Output generators | retrieval only |
| Grounding checker | retrieval only — **never** web search |
| Export | render, media |

The grounding restriction is load-bearing. With web search, the checker verifies
claims against the internet instead of the source document, silently defeating itself.

Tool groups: Retrieval (Qdrant) · Web search · Render (docx/pptx/pdf) · Media (TTS,
ffmpeg).

**Fallback if MCP costs too much time:** ship the registry as plain Python functions
with the same allowlist enforcement. The registry is the idea; MCP is one way to serve
it. The security argument survives intact.

## 6. Input analysis subagents — once per job

Web search (enrich, verify) · Content analysis (facts, entities) · Input analysis
(objective, audience).

Result is cached on the job and shared by every generator. This is what makes
selecting six formats cost roughly the same analysis as selecting one.

Source provenance is classified here. A `literary_copyrighted` source switches
generation into **commentary mode**: narration analyses and paraphrases, quoted spans
capped at short fragments.

## 7. Output subagents — one per selected format

Registry entry per format:

```json
{
  "id": "linkedin_post",
  "label": "LinkedIn Post",
  "prompt_template": "linkedin_post@v3",
  "output_schema": "linkedin_post.schema.json",
  "constraints": {"max_chars": 3000, "hashtags_max": 5},
  "model_alias": "fast",
  "renderer": "markdown"
}
```

Adding a format = one config entry + one template + one schema. No code change.

Prompt = template + content object + analysis + parameters + output schema.
Response is **structured JSON** with a `claims[]` array, each claim carrying a
`chunk_id`. That single decision is what makes grounding cheap — the checker verifies
against the cited chunk instead of searching the whole document.

Malformed JSON is a **parse** retry, handled separately. Never burn a QA retry on it.

## 8. Content QA subagents — per artefact

Four checkers, parallel, **independent**. Each sees the artefact and its own criteria,
never another checker's verdict — otherwise they anchor on each other.

| Checker | Kind | Gate |
|---|---|---|
| Grounding — claims vs cited chunks | LLM | hard, triggers retry |
| Format — schema, length, constraints | **deterministic, no LLM** | hard |
| Tone fit — audience, style | LLM score | threshold, advisory |
| Safety — PII, policy | LLM | hard, blocks outright |
| Source reuse — n-gram vs source | **deterministic** | threshold; only for copyrighted sources |

Format is a regex and a length check. A tweet over 280 characters is not a judgement
call — do not spend an LLM call on it.

### Verdict policy — a policy, not a vote

"Three of four passed" is meaningless when the failure is safety.

- safety fail → block unconditionally, do not deliver
- grounding fail → retry with fix notes
- tone below threshold → retry once, then pass with a warning flag
- 3 QA failures → **stop the job**, tell the operator to restart
- source reuse over threshold → flag; long verbatim spans block

Fix notes must be **specific** — the failing claim, or the violated constraint — and
are injected into the retry prompt. "Quality insufficient" returns the same output.
If the note is specific, two attempts usually suffice; if it isn't, twenty wouldn't.

### Two separate failure counters

Provider errors (429, timeout) retry with backoff and do **not** count against the
quality budget. Only QA verdict failures increment the 3-strike counter. Otherwise a
flaky free tier kills jobs that were generating fine.

## 9. Export

Renders structured JSON to files: pdf/docx/md · pptx + notes · mp4/srt/json.

**Video is a package, not generated frames.** The model produces script, storyboard,
scene descriptions, narration, subtitles and visual recommendations — all text.
Rendering is deterministic: narration → TTS (Edge TTS) → audio; subtitles → SRT timed
from `duration_sec`; scenes → title cards or stock slots; stitched with ffmpeg.
~90 seconds, no generative video model, fully explainable.

Never describe this as video generation.

## 10. Observability — Langfuse

Traces, versioned prompts, eval runs. Prompt versioning is what lets an eval run
compare v3 against v4 on the same fixed test set. Without it, every prompt change is
a guess.

## 11. Deployment — Docker Compose

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
    volumes: ["./qdrant_storage:/qdrant/storage"]

  postgres:
    image: postgres:16
    environment: { POSTGRES_PASSWORD: dev }
    volumes: ["./pg_data:/var/lib/postgresql/data"]

  app:
    build: .
    ports: ["8000:8000"]
    depends_on: [qdrant, postgres]
    environment:
      QDRANT_URL: http://qdrant:6333
      DATABASE_URL: postgresql://postgres:dev@postgres:5432/app
```

Two things that cost time if missed: use the **service name** as host from inside the
app container (`http://qdrant:6333`, not `localhost`), and mount the volumes or
embeddings vanish on every `docker compose down` and you re-ingest before every run.

---

## Known gaps — stated, not hidden

| Gap | Status |
|---|---|
| Authentication | Deliberately out of scope for the demo |
| Translation | `language` is a parameter; multi-language output not yet designed |
| Cost / latency budget | ~40 model calls per 6-format job. Measure before the demo |
| Job failure states | Need `failed_recoverable` vs `failed_permanent` beyond queued/running/done |
| Rate-limit handling | LiteLLM fallbacks configured; backoff policy not yet tuned |
