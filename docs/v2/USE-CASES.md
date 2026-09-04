# Use cases (v2)

What the platform does, from the operator's side. Every use case below is
either **built** (there is code and a test id) or marked **not built**.

Supersedes `docs/USE-CASES.md`. The two worked examples at the end are the
important part of this document — they are the demo scripts, and Example A has
been observed end to end against live providers.

---

## Actors

| Actor | Role | Status |
|---|---|---|
| Operator | Submits sources, selects outputs, reviews, exports | Built |
| Administrator | Manages prompt versions, runs evals | Partially — eval harness exists, no UI |
| Reviewer | Approves artefacts before publication | **Not built** |
| External systems | LLM providers, web sources | Built |

There is no auth, so "actor" is a role description, not an identity. See
`ARCHITECTURE.md` §13.1.

## Register

| ID | Use case | Status |
|---|---|---|
| UC-01 | Submit source content | Built |
| UC-02 | Set generation parameters | Built |
| UC-03 | Select one or more output formats | Built |
| UC-04 | Generate a single artefact | Built |
| UC-05 | Generate multiple artefacts from one source | Built |
| UC-06 | Enrich source with web-retrieved context | Stub — allowlist enforced, no provider |
| UC-07 | Run automated quality checks per artefact | Built |
| UC-08 | View QA findings and confidence per artefact | Built |
| UC-09 | Regenerate one artefact without rerunning the job | Built |
| UC-10 | Edit generated content in place | Not built |
| UC-11 | Export artefacts | Built |
| UC-12 | View job history; reuse a saved source | Built |
| UC-13 | Manage prompt templates and versions | Partial — versioned files, no UI |
| UC-14 | View traces, run evaluations | Built (harness + Langfuse), no UI |
| UC-15 | Multi-tenant isolation | **Not built** — §13.1 |
| UC-16 | Multi-language output | **Not built** — §13.3 |

---

## UC-01 — Submit source content

**Precondition:** none. No auth in current scope.

1. Operator uploads a file, pastes a URL, or types free-form text
2. System hashes the bytes; a matching hash returns the existing `source_id`
   immediately, with `reused: true`
3. Route to extractor by extension, falling back to mimetype
4. Build the normalised content object
5. Chunk (~800 tokens, overlap) and embed into Qdrant with a `source_id` payload
6. Return `source_id`; status `ingested`

**Alternates**

| Situation | Behaviour |
|---|---|
| Unsupported type | Rejected, listing accepted extensions (TC-0113) |
| Scanned PDF | Text layer < 200 chars triggers OCR (TC-0102) |
| Extraction and OCR both empty | Fail: "no readable content" (TC-0112) |
| Truncated / corrupt file | Fails in < 3s with a specific message, no source row (TC-0110) |
| Password-protected PDF | Specific message, not a generic 500 (TC-0111) |
| Video over 10 minutes | Blocked with a paywall stub **before** transcription (TC-0108) |
| Duplicate hash | Existing `source_id`, no re-extraction, no new embeddings (TC-0109) |

Hashing the **bytes** rather than the extracted text is deliberate: extraction is
library-version-dependent, so text-hashing would miss dedup after an upgrade.

## UC-02 — Set generation parameters

```json
{
  "audience":  "general public",
  "tone":      "informative, urgent",
  "language":  "en",
  "detail":    "brief",
  "objective": "drive awareness and patching",
  "style":     "plain, no jargon"
}
```

**Closed vocabularies, never free text.** The lists live in `VOCAB` in
`app/web/routes.py`; the dashboard renders them as dropdowns and a test asserts
there are zero free-text parameter inputs (TC-0202). Predictable templating, and
the tone checker gets something concrete to compare against.

Every parameter has a default — an unset parameter means a sensible default,
never an empty prompt slot (TC-0201).

Parameters are part of the cache key: changing tone produces a fresh generation
(TC-0204). `language` is in the key and reaches the prompt but nothing acts on
it yet (§13.3).

## UC-03 — Select output formats

Each selection is validated against the registry, which supplies the prompt
template, output schema, constraints, model alias and renderer. **The whole
selection is validated before anything is created** — a bad id rejects the
request rather than silently dropping one format, so the operator never gets six
artefacts having asked for seven (TC-0301).

One artefact record per selected format, unique on `(job_id, output_type)`.

## UC-04 / UC-05 — Generate

**The same code path.** One format or seven is only the width of the fan-out.

1. Input analysis runs **once**, cached on the job
2. Orchestrator fans out one generation task per artefact record
3. Each builds prompt = template + content + analysis + parameters + constraints
4. Call via the gateway using the registry's `model_alias`
5. Validate the returned JSON against the schema
6. Persist, hand to QA

**Alternates**

| Situation | Behaviour |
|---|---|
| Malformed JSON | Parse retry on a **separate** counter (TC-0405) |
| Schema violation | Also a parse failure — wrong shape, content never assessable |
| Parse retries exhausted | Artefact marked failed with the reason (TC-0406) |
| One generator fails | Other artefacts still complete (TC-0407) |
| Provider 429 | Fallback; **does not** count against QA retries (TC-0609) |
| Copyrighted source | Commentary mode: paraphrase, short fragments only (TC-0409) |

## UC-06 — Web enrichment

The allowlist is real and enforced: input analysis may call `web_search`, and
the grounding checker structurally cannot (TC-1001). The tool itself is a stub
returning an empty list — no search provider is configured.

This is worth understanding precisely: **the security property is built and
tested; the capability is not.** Wiring a real provider changes no allowlist
code.

## UC-07 / UC-08 — QA and review

Five checkers run per artefact, independent, concurrency-capped. The verdict
policy in `ARCHITECTURE.md` §8 decides the outcome.

The operator sees, per artefact:

- a status badge — passed / passed-flagged / blocked / failed
- one chip per checker with pass/fail and, where applicable, a score
- every fix note, verbatim
- the cited claims with their chunk ids
- retry count, and separately the provider-error count

**Flagged and blocked artefacts still appear, marked** (TC-0610). Hiding them
leaves the operator wondering where their deck went.

## UC-09 — Regenerate one artefact

Re-enters at **generation**, not ingestion. The source and the cached analysis
are untouched, so a regenerate costs one artefact rather than a whole job
(TC-0808).

Carries **operator instructions**, not machine fix notes. The counters reset,
because the operator is asking for something different rather than retrying the
same request.

## UC-11 — Export

Renders per the registry's `renderer`. Assets land under
`{STORAGE_DIR}/{job_id}/{output_type}/` and download links resolve through a
path-contained route (TC-0706).

The video package produces `.json`, `.srt`, per-scene `.png` and `.mp3`, and a
stitched `.mp4` — all deterministic, no generative video (TC-0705).

## UC-12 — History and source reuse

Recent sources on the dashboard; selecting one skips ingestion entirely
(TC-0807). Recent jobs link to their live view. One source, many jobs — the
operator can return the next day and generate a new format without re-uploading.

## UC-13 / UC-14 — Prompt versions and evaluation

Prompts are versioned files: `<name>@v<N>.jinja` plus an optional
`.system.txt`. `version_tag()` feeds the cache key, so editing a template
invalidates cleanly.

The eval harness runs a fixed case set and reports pass rate by checker, mean
retries, mean latency and total model calls. Comparing v(n) against v(n-1) on
the same set is the entire reason prompts are versioned.

Neither has a UI. Both are exercised from tests or a shell.

## UC-15 / UC-16 — Not built

Multi-tenancy and multi-language output. Design intent in `ARCHITECTURE.md`
§13.1 and §13.3. Do not implement from this register alone — §13.3 in particular
lists three questions that must be answered first, because adding languages
without answering them ships a feature whose QA layer silently stops working.

---

# Worked example A — security advisory → LinkedIn post + exec summary

**This has been run end to end against live providers.** The numbers below are
observed, not projected.

**Source.** A vendor bulletin: authentication bypass in a VPN appliance, CVSS
9.1, confirmed exploitation in the wild, fixed in 22.7R2.6 released 28 Aug 2026,
~14,000 internet-facing instances affected.

**Parameters.** general public · informative, urgent · en · brief · drive
awareness and patching · plain, no jargon.

**Formats.** `linkedin_post` + `exec_summary`.

**Observed result — 67s and 93s across two runs:**

| Artefact | Status | QA retries | Grounding | Tone |
|---|---|---|---|---|
| LinkedIn post | passed | 0 | 3/3 | 0.80 |
| Executive summary | passed | 1 | 3/3 | 0.80 |

Both cite every factual claim to a chunk id. The exec summary's `residual_risk`
came back as *"Systems targeted by attackers between August 21, 2026, and the
date of patch deployment may already be compromised"* — inference from the
source, not restatement of it. That is the transformation the platform exists to
perform.

**Analysis ran once** and was shared by both generators (TC-0402/0408).

## The scripted variant — an honest QA catch

`tests/integration/test_phase7_example_a.py` pins the version of this example
worth demonstrating, with the provider stubbed so the pipeline behaviour is the
assertion:

1. LinkedIn post generates with an alarmist closing line
2. Grounding passes 4/4; format passes; safety passes
3. **Tone fails at 0.61** — "closing line alarmist for the stated audience"
4. Fix note: *"Soften the closing line. Keep urgency factual — reference active
   exploitation rather than predicting consequences."*
5. The note reaches the retry prompt (asserted, not assumed)
6. Retry rewrites one sentence → **0.84**, passes
7. The exec summary is untouched throughout — retries are per artefact

> **This is the demo moment.** One source, two artefacts, no re-analysis, and
> one honest QA catch with a targeted fix. A demo where everything passes first
> time only proves the pipeline runs.

---

# Worked example B — all seven formats, and a real block

**Also observed live.** This is the stronger demo, because something failed.

**Source.** The same advisory. **Formats.** All seven.

**Observed result — 123 seconds, 53 model calls:**

| Artefact | Status | QA retries | Notes |
|---|---|---|---|
| linkedin_post | passed | 0 | |
| twitter_x | passed | 0 | Every tweet under 280, checked deterministically |
| infographic | passed | 0 | |
| exec_summary | passed | 1 | |
| presentation | passed | 1 | Speaker notes populated |
| video_package | passed | 1 | Script, storyboard, narration, subtitles |
| **advisory** | **blocked** | **3** | Tone never reached threshold |

**Job status: `stopped_qa_budget`.** The operator saw:

> Quality checks failed three times for advisory. The job has been stopped.
> Review the findings below, adjust the parameters or the source, and start a
> new job.

Not a stack trace (TC-0805). The advisory's final tone verdict scored 0.60 with:

> *"Simplify the language in the summary and claims sections — replace
> 'unauthenticated attackers' with 'hackers' and 'CVSS scale' with 'a
> widely-used measure of security risk'."*

**The catch was correct.** An advisory document written for an audience of
"general public" genuinely is too jargon-dense. The right operator response is
to change the audience parameter to "security leaders" and re-run — which is
exactly what the message tells them to do.

What this demonstrates, in one run:

- seven formats from **one** analysis
- deterministic checks catching what deterministic checks should
- an LLM checker catching a real quality problem
- the 3-strike policy stopping the job rather than shipping something bad
- per-artefact isolation — six artefacts unaffected by the seventh's failure
- an operator message that says what to do next

---

# Worked example C — literary excerpt → video package

**Designed and unit-tested; not yet run against a real scanned source.**

**Source.** A scanned chapter of a copyrighted novel. OCR fallback path.

**Branch this triggers.** Analysis classifies provenance as
`literary_copyrighted`, which sets `commentary_mode`. The shared prompt block
switches to: analyse and paraphrase, quoted spans capped at short fragments.
Without this, "make a video from this excerpt" produces the excerpt read aloud —
reproduction, not transformation.

**Additional checker.** Source reuse activates for copyrighted provenance only
(TC-0512). Deterministic n-gram matching: 15+ consecutive matching words is
flagged; 60+ blocks.

**Expected verdict.** A five-word quoted fragment on a title card is flagged,
not blocked — a threshold, not a hard gate. A 60-word verbatim passage in
narration blocks.

**Output shape.** `scenes[]` each with `narration`, `visual`, `on_screen_text`,
`duration_sec`, `source_chunks[]`; plus `storyboard_notes`, `subtitles_srt`,
`visual_recommendations` (including what to avoid — film stills are separately
licensed) and `discussion_prompts`.

**To run this for real**, a future session needs: a scanned PDF of
public-domain-adjacent literary text, confirmation that analysis classifies it
as `literary_copyrighted` (it is a model judgement and may need prompt work),
and a check that the reuse checker flags rather than blocks. Do not assume the
provenance classification is reliable until it has been observed.
