# Gen AI Content Transformation Platform

Turns one source (article, report, advisory, image, video, prompt) into one or more
communication artefacts the operator selects. Hackathon timeline.

Detail lives in `docs/ARCHITECTURE.md`, `docs/USE-CASES.md` and `docs/TEST-CASES.md`.
start of a session, not every turn.

---

## How you work

**Don't assume.** If the request has more than one reading, say so and pick with me.
State assumptions out loud before coding, not after. If something in the docs
contradicts what I just asked, stop and name the contradiction.

**Plan, then act.** For anything touching more than one file: propose the plan first
— files, functions, order — and wait. Read-only exploration is free; edits are not.
Small single-file changes don't need this.

**Define done before starting.** Every task gets an explicit success criterion I can
check. "Ingestion works" is not one. "`POST /sources` with the sample PDF returns
`source_id` and 11 chunks appear in Qdrant" is. State it, build to it, verify it,
show me the evidence.

**Simplest thing that works.** No abstraction for one call site. No config option I
didn't ask for. No "you'll probably want this later." If it's 200 lines and could be
50, rewrite it before showing me.

**Stay in scope.** Touch only files the task requires. Notice adjacent problems, tell
me about them, don't fix them. Never refactor working code as a side effect.

**Show evidence, not claims.** After edits: what changed, what you ran, what it
output. Don't say "this should work" — run it. If you can't run it, say that.

**Ask before adding a dependency.** Every time.

---

## Project invariants

These are decided. Don't relitigate them; if one blocks you, tell me why.

1. **No LLM in the ingestion path.** Extract, normalise, chunk, embed are plain code.
   Never model-clean source text — grounding must check against the true source.
2. **Chunk ids are immutable**, created once at ingest. Every factual claim in a
   generated artefact cites a `chunk_id`. Agents read chunks; they never write them.
3. **Analysis runs once per job**, cached, shared by all generators. Not per format.
4. **Output formats live in a registry.** New format = config entry + prompt template
   + JSON schema. If you're editing a switch statement, you're doing it wrong.
5. **Agents request `"fast"` or `"long"`, never a provider.** LiteLLM routes.
6. **The grounding checker must not have web search.** With it, it verifies claims
   against the internet instead of the source and silently defeats itself. Enforce
   in the tool registry, not in a prompt.
7. **Retry counters are per artefact.** A failing tweet must not regenerate the deck.
8. **Two separate failure counters.** Provider errors (429, timeout) retry with
   backoff and don't count. Only QA verdict failures hit the 3-strike job stop.
9. **Prompts live in versioned files**, never as string literals in Python.
10. **We generate a video *package*** — script, storyboard, narration, subtitles.
    MP4 rendering is TTS + ffmpeg, deterministic. Never call it video generation.

## Limits

Video input max 10 min (else blocked). QA retries max 3, then stop the job and tell
the operator to restart. No auth — deliberate demo scope, documented not hidden.

---

## Stack

Python 3.11 · FastAPI · LangGraph (+ LangChain adapters only) · LiteLLM Router
· Groq / Gemini Flash / OpenRouter, free tiers · Qdrant `:6333` · Postgres · Langfuse
· Docker Compose.

## Commands

```
docker compose up -d          # qdrant + postgres
uvicorn app.main:app --reload
pytest -q
ruff check . && ruff format .
```

## Build order

One output format end to end before the second. Format two is then ~an hour.
Three formats half-built is how the weekend disappears.
