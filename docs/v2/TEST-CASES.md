# Test cases (v2)

`TC-<area><nn>`. **P0** blocks the demo, **P1** should pass, **P2** nice to have.

Current state: **180 pass, 3 skip** (176 test functions; parametrize expands
some). **98 are P0.**

This register is reconciled against the suite. Every row says where the test
lives, or says explicitly that it does not exist yet. A row with no test file is
a gap, not a claim.

```bash
docker compose exec app python -m pytest -q          # everything
docker compose exec app python -m pytest -q -m p0    # blocks the demo
docker compose exec app python -m pytest -q -m "not integration"   # host-runnable
```

Three scaffold tests skip in-container by design: they assert facts about the
repo (compose file, docs), which `.dockerignore` correctly keeps out of the
image.

Areas: 01 ingestion · 02 parameters · 03 registry · 04 generation · 05 QA
checkers · 06 verdict and retry · 07 export · 08 job lifecycle · 09 provider and
routing · 10 tool security · 11 end to end · 12 non-functional · 13 discipline.

---

## 01 — Ingestion

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0101 | Text PDF | `source_id` returned; chunks in Qdrant with `source_id` payload | P0 | `integration/test_phase2_sources_api.py` |
| TC-0102 | Scanned PDF, OCR fallback | Text layer < 200 chars → OCR runs → text extracted | P0 | verified live; unit covers threshold |
| TC-0103 | DOCX | Headings preserved in `text` | P1 | `integration/test_phase2_sources_api.py` |
| TC-0104 | HTML / URL | Boilerplate stripped | P1 | **gap — no test** |
| TC-0105 | Image | `media[]` populated with OCR text; original retained | P1 | **gap — no test** |
| TC-0106 | Video under limit | Transcript produced, chunked, embedded | P1 | **gap — needs a fixture** |
| TC-0107 | **Video at boundary** | Exactly 10:00 accepted | P0 | `unit/test_phase2_ingest.py` |
| TC-0108 | **Video over limit** | 10:01 rejected; no transcription attempted; no partial row | P0 | `unit/test_phase2_ingest.py` |
| TC-0109 | Duplicate upload | Second call returns existing `source_id`, `reused: true` | P0 | `integration/test_phase2_sources_api.py` |
| TC-0110 | Corrupt file | Fails < 3s, clear message, no source row | P0 | `integration/test_phase2_sources_api.py` |
| TC-0111 | Password-protected PDF | Specific message, not a generic 500 | P1 | **gap — no test** |
| TC-0112 | Empty source | OCR returns nothing → "no readable content" | P0 | both unit and integration |
| TC-0113 | Unsupported type | Rejected listing accepted types | P1 | both |
| TC-0114 | **Chunk id stability** | Ids identical across reads; never renumbered | P0 | both |
| TC-0115 | Free-form prompt only | Source created from text; chunking applies | P1 | both |
| TC-0116 | Multi-source job | Job references 3 sources; retrieval spans all | P2 | **gap — blocked on §13.2** |
| TC-0117 | **No LLM in ingest path** | Zero completion calls during `POST /sources` | P0 | `unit/test_phase2_ingest.py` + structural sweep |

## 02 — Parameters

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0201 | Defaults applied | No parameter defaults to empty | P0 | `unit/test_phase1_state.py` |
| TC-0202 | **Closed vocabularies** | Dashboard renders dropdowns; zero free-text param inputs | P0 | `integration/test_phase10_dashboard.py` |
| TC-0203 | Parameters reach the prompt | Rendered prompt contains audience and tone verbatim | P0 | `unit/test_phase5_analysis.py` |
| TC-0204 | **Parameters in the cache key** | Tone changed → cache miss | P0 | `unit/test_phase3_gateway.py` |
| TC-0205 | Same everything | Identical inputs → same key → cache hit | P1 | `unit/test_phase3_gateway.py` |
| TC-0206 | Invalid enum rejected | Out-of-vocabulary value refused with the valid list | P1 | **gap — dropdowns constrain the UI, the API does not validate** |

> TC-0206 is a real hole. The dashboard cannot send a bad value, but `POST
> /jobs` accepts any string for a parameter. Closing it means either a
> `Literal`/enum on `Parameters` or explicit validation against `VOCAB`.

## 03 — Output registry

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0301 | Unknown format id | Rejected **before** any job row is created | P0 | `integration/test_phase10_dashboard.py` |
| TC-0302 | **Add a format, zero code** | New entry + template + schema → appears and generates | P0 | `unit/test_phase6_registry.py` |
| TC-0303 | Registry supplies the alias | Long-form → `long`, social → `fast` | P1 | `unit/test_phase6_registry.py` + pipeline |
| TC-0304 | Constraints loaded | Registry `max_chars` is what the checker enforces | P0 | `unit/test_phase6_registry.py` |
| TC-0305 | **No format literals in the pipeline** | Pipeline modules contain no format id strings | P0 | `integration/test_phase8_fanout.py` |
| TC-0306 | Every schema requires claims | `claims[]` with `chunk_id` required by all seven | P0 | `unit/test_phase6_registry.py` |
| TC-0307 | Every renderer implemented | No registry entry names a renderer that does not exist | P0 | `integration/test_phase9_export.py` |

## 04 — Generation

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0401 | Single format | One artefact, schema-valid | P0 | `integration/test_phase7_example_a.py` |
| TC-0402 | Seven formats | Seven artefacts; **analysis ran exactly once** | P0 | `integration/test_phase8_fanout.py` |
| TC-0403 | Claims carry chunk ids | Every claim has a `chunk_id` that exists | P0 | `unit/test_phase6_registry.py` (schema) |
| TC-0404 | Invented chunk id | `c99` → grounding failure, **not a crash**, no model call | P0 | `unit/test_phase7_pipeline.py` |
| TC-0405 | Malformed JSON | Parse retry; **QA counter unchanged** | P0 | `unit/test_phase7_pipeline.py` |
| TC-0406 | Persistent malformed JSON | Artefact marked failed with the reason | P1 | `unit/test_phase7_pipeline.py` |
| TC-0407 | One generator fails | Others still complete; failed one marked | P0 | `integration/test_phase8_fanout.py` |
| TC-0408 | Analysis is shared | Same analysis object across all artefacts | P1 | `integration/test_phase7_example_a.py` |
| TC-0409 | Commentary mode | Copyrighted source → prompt switches to paraphrase | P1 | `integration/test_phase8_fanout.py` |
| TC-0410 | Video package shape | Scenes, narration, storyboard, subtitles, recommendations | P0 | `unit/test_phase6_registry.py` |
| TC-0411 | **Claims shape is explicit** | Prompt shows the literal `{"text","chunk_id"}` example | P0 | `unit/test_phase6_registry.py` |

> TC-0411 exists because prose alone was not enough: models emitted
> `{"claim":…, "citations":…}` and exhausted every parse retry. The fix is the
> literal JSON example in `_shared.jinja`, now pinned by a test so nobody
> "tidies" it away.

## 05 — QA checkers

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0501 | Grounding pass | All claims match cited chunks | P0 | `unit/test_phase7_pipeline.py` |
| TC-0502 | Grounding fail | Fails, **naming the offending claim** | P0 | `unit/test_phase7_pipeline.py` |
| TC-0503 | **Format check uses no LLM** | Zero completion calls in the format checker | P0 | behavioural + structural |
| TC-0504 | Length violation | Tweet at 281 chars fails deterministically | P0 | unit + fan-out |
| TC-0505 | Constraint violation | 7 hashtags where max is 5 → fail | P1 | `unit/test_phase7_pipeline.py` |
| TC-0506 | Tone returns a score | Numeric score plus a reason string, not a bare boolean | P1 | `unit/test_phase1_state.py` |
| TC-0507 | Safety catch | Artefact containing an email address → safety fail | P0 | `unit/test_phase3_gateway.py` (PII patterns) |
| TC-0508 | **Checkers are independent** | No checker prompt contains another's verdict | P0 | structural — each coroutine gets only its own inputs |
| TC-0509 | Checkers run concurrently | Wall time ≈ slowest, not the sum | P1 | **gap — not timed** |
| TC-0510 | Source reuse flag | 15+ consecutive matching words → flagged | P1 | `unit/test_phase7_pipeline.py` |
| TC-0511 | Source reuse block | 60-word verbatim span → blocked | P1 | `unit/test_phase7_pipeline.py` |
| TC-0512 | Reuse skipped | Non-copyrighted source → checker not invoked | P2 | covered by runner logic |
| TC-0513 | **Reuse check uses no LLM** | Zero completion calls in the reuse checker | P0 | structural sweep |

## 06 — Verdict and retry

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0601 | All pass | Artefact proceeds to export | P0 | `unit/test_phase7_pipeline.py` |
| TC-0602 | **Safety fail blocks** | Blocked even with the other three passing | P0 | `unit/test_phase7_pipeline.py` |
| TC-0603 | Grounding fail retries | Retry fires with a fix note naming the claim | P0 | `unit/test_phase7_pipeline.py` |
| TC-0604 | **Fix note is specific** | Retry prompt contains the claim or constraint text | P0 | `integration/test_phase7_example_a.py` |
| TC-0605 | Tone below threshold | Retries once, then passes flagged | P1 | `unit/test_phase7_pipeline.py` |
| TC-0606 | **Retry is per artefact** | Failing tweet retries alone | P0 | unit + Example A |
| TC-0607 | Retry cap | Third QA failure → job stopped, operator message | P0 | `unit/test_phase7_pipeline.py` |
| TC-0608 | Counter isolation | Artefact A at 2 retries; B still at 0 | P0 | `unit` + `integration/test_phase1_persistence.py` |
| TC-0609 | **Provider error does not count** | 429 → backoff; QA counter unchanged | P0 | fan-out + verified live |
| TC-0610 | Flagged artefacts surface | Blocked/flagged appear in the review view, marked | P1 | `integration/test_phase10_dashboard.py` |
| TC-0611 | Retry improves | Example A: tone 0.61 → ≥0.80 after the fix note | P1 | `integration/test_phase7_example_a.py` |

## 07 — Export

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0701 | Markdown / PDF | File written, opens, content matches the artefact | P0 | `integration/test_phase9_export.py` |
| TC-0702 | PPTX with notes | Slides present; **speaker notes populated** | P1 | `integration/test_phase9_export.py` |
| TC-0703 | SRT timings | Cue count matches scenes; timings sum to target | P1 | `integration/test_phase9_export.py` |
| TC-0704 | MP4 render | TTS + title cards + ffmpeg produce a playable file | P1 | `integration/test_phase9_export.py` |
| TC-0705 | **No generative video model** | No video-generation API in the export path | P0 | behavioural + structural |
| TC-0706 | Download link valid | Returned URL resolves to the written asset | P0 | `integration/test_phase10_dashboard.py` |
| TC-0707 | **Video duration honours `duration_sec`** | mp4 runtime matches declared scene durations | P1 | `integration/test_phase9_export.py` |
| TC-0708 | **Download refuses traversal** | `../` in three encodings returns 404 | P0 | `integration/test_phase10_dashboard.py` |
| TC-0709 | Citations survive to the file | Rendered output contains chunk ids | P1 | `integration/test_phase9_export.py` |

> TC-0707 and TC-0708 are new in v2. The first is a regression from a real bug
> (`-shortest` clipped scenes to narration length, desyncing subtitles). The
> second was found while writing the download route and is not in v1's register.

## 08 — Job lifecycle

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0801 | Async return | `POST /jobs` returns < 1s with `job_id` | P0 | `integration/test_phase10_dashboard.py` (measured 0.10s) |
| TC-0802 | Status polling | `GET /jobs/{id}` reflects per-artefact progress | P0 | `integration/test_phase10_dashboard.py` |
| TC-0803 | Partial completion | "4 of 7 done" — never "complete" while work remains | P0 | `integration/test_phase10_dashboard.py` |
| TC-0804 | Recoverable vs permanent | Provider outage → `failed_recoverable`; bad source → permanent | P1 | `unit/test_phase1_state.py` + verified live |
| TC-0805 | Job stopped message | Restart instruction, not a stack trace | P0 | `integration/test_phase10_dashboard.py` |
| TC-0806 | History | Completed job appears in history | P1 | `GET /jobs` |
| TC-0807 | Source reuse from history | New job on a saved source skips ingestion | P0 | `integration/test_phase10_dashboard.py` |
| TC-0808 | Regenerate one | Re-enters at generation; analysis not recomputed | P1 | **gap — route exists, no test** |
| TC-0809 | One artefact per format | Unique on `(job_id, output_type)` | P1 | `integration/test_phase1_persistence.py` |
| TC-0810 | QA results kept per attempt | Tone 0.61 → 0.84 both visible after a retry | P1 | `integration/test_phase1_persistence.py` |

## 09 — Provider and routing

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-0901 | **Alias not provider** | Zero provider names in code outside the gateway | P0 | `unit/test_phase3_gateway.py` |
| TC-0902 | Fallback fires | 429 on one deployment → another serves it | P0 | **gap — fallback configured, not exercised in a test** |
| TC-0903 | Long alias routing | Advisory and video package use `long` | P1 | `unit/test_phase7_pipeline.py` |
| TC-0904 | QA concurrency cap | Checkers do not exceed the configured limit | P1 | `unit/test_phase3_gateway.py` (semaphore size) |
| TC-0905 | Embeddings bypass the gateway | Embedding calls do not pass through the router | P1 | structural sweep |
| TC-0906 | All providers down | Job fails cleanly with a provider message; no hang | P1 | verified live with no keys |
| TC-0907 | **Provider keys read only by the gateway** | No module outside the gateway reads a key setting | P0 | `unit/test_phase3_gateway.py` |
| TC-0908 | **Model ids are live** | Every configured model id answers a trivial completion | P0 | `unit/test_phase3_gateway.py` + startup preflight |

> TC-0908 matters more than its position suggests. All three model ids inherited
> from v1 were dead on first live run. `router.preflight()` now sends one
> trivial completion per configured deployment at startup and logs a warning
> naming any that fail — a mid-demo failure became a startup warning.

## 10 — Tool registry and security

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-1001 | **Grounding has no web search** | Denied by the registry, and not listed as available | P0 | `unit/test_phase4_tool_registry.py` |
| TC-1002 | Generator cannot write chunks | **No** write/delete tool is registered at all | P0 | `unit/test_phase4_tool_registry.py` |
| TC-1003 | Cross-job isolation | Another job's id returns nothing | P0 | `unit/test_phase4_tool_registry.py` |
| TC-1004 | No raw client exposed | Every retrieval tool takes `job_id` first | P0 | `unit/test_phase4_tool_registry.py` |
| TC-1005 | Calls audited | Every call recorded, **denials included** | P1 | `unit/test_phase4_tool_registry.py` |
| TC-1006 | Allowlist matches the spec | Table in code equals the table in the architecture | P1 | `unit/test_phase4_tool_registry.py` |
| TC-1007 | Deterministic checkers get no tools | Tone and safety have empty allowlists | P1 | `unit/test_phase4_tool_registry.py` |

## 11 — End to end

| ID | Case | Expected | P | Where |
|---|---|---|---|---|
| TC-1101 | **Golden path** | Advisory + LinkedIn + exec summary → artefacts, verdicts, exports | P0 | verified live, 67s |
| TC-1102 | Example A reproduction | 4/4 grounding, tone fail then pass on retry | P0 | `integration/test_phase7_example_a.py` |
| TC-1103 | Example C reproduction | Literary excerpt → commentary mode, reuse flagged not blocked | P1 | **gap — needs a scanned fixture** |
| TC-1104 | Seven-format job | All seven; one analysis; no cross-artefact contamination | P0 | `integration/test_phase8_fanout.py` |
| TC-1105 | Cold start | `docker compose up` from clean → golden path passes | P0 | verified — 177 pass after full rebuild |
| TC-1106 | **Volumes persist** | `down && up` → embeddings present, no re-ingest | P0 | `unit/test_phase0_scaffold.py` + verified live |
| TC-1107 | Service name host | App reaches Qdrant at `http://qdrant:6333` | P0 | `unit/test_phase0_scaffold.py` + verified live |

## 12 — Non-functional

| ID | Target | Measured | P |
|---|---|---|---|
| TC-1201 | Ingestion < 10s for a 3-page PDF | Sub-second for text sources | P1 |
| TC-1202 | Single artefact < 20s | ~30s including QA — **over target** | P0 |
| TC-1203 | Multi-format job under 3 min | **123s for seven formats** | P0 |
| TC-1204 | Call budget logged | **53 calls** (13 gen + 39 QA + 1 analysis) | P1 |
| TC-1205 | Cached rerun | Provider calls near zero on an identical repeat | P1 |
| TC-1206 | Rate-limit resilience | Three jobs back to back without intervention | P0 |

> TC-1202 is the one to watch. A single artefact takes roughly 30s end to end —
> one generation plus three concurrent LLM checkers, capped at 2 concurrent.
> Raising `QA_CONCURRENCY` to 3 would cut it, at the cost of the rate-limit
> headroom the cap exists to protect. Measure before changing.
>
> TC-1204's 53 against v1's ~40 estimate is proportional: the overage is six
> QA retries across the run, each costing a generation plus three checkers.

## 13 — Discipline

These fail **silently** if nobody asserts them. Each maps to an invariant, and
each is checked structurally — by reading the source — as well as behaviourally.
All live in `unit/test_phase11_observability.py`.

| ID | Assertion | Invariant |
|---|---|---|
| TC-1301 | No module under `app/ingest/` imports the gateway | 1 |
| TC-1302 | `format_check.py` and `reuse.py` never reach `router.complete` | — |
| TC-1303 | No generative-video reference anywhere in `app/export/` | 10 |
| TC-1304 | Every registry entry resolves to a real versioned prompt file | 9 |
| TC-1305 | The verdict module touches neither the parse nor provider counter | 8 |
| TC-1306 | Parse and provider failures increment only their own counters | 8 |

## Evaluation set

`app/eval/eval_set.json`, run by `app/eval/harness.py`.

| Field | Meaning |
|---|---|
| `source_id` | Fixed source from the set |
| `output_type` | Format under test |
| `parameters` | Fixed parameter block |
| `expected_properties` | Assertions: min claims, required keys, length, tone, retries |
| `prompt_version` | Pinned version under evaluation |

Reports pass rate by checker, mean retries, mean latency, total model calls.
Compare v(n) against v(n-1) on the same set — that comparison is the whole point
of versioning prompts.

An `expected_properties` key the harness does not recognise is reported as a
**failure**, not skipped. An eval that silently ignores an assertion it does not
understand is worse than no eval.

## Gaps, ranked

Rows above marked "gap" have no test. In priority order:

1. **TC-0206** — parameter enum validation at the API, not just the UI.
2. **TC-0902** — exercise the provider fallback rather than trusting config.
3. **TC-0808** — regenerate re-enters at generation without recomputing analysis.
4. **TC-0104/0105/0111** — HTML, image and encrypted-PDF ingestion paths.
5. **TC-0106** — video ingestion under the limit, needs a fixture.
6. **TC-1103** — Example C against a real scanned source.
7. **TC-0509** — prove the checkers actually run concurrently.
8. **TC-0116** — multi-source jobs; blocked on real semantic retrieval (§13.2).

Closed since this register was written: **TC-0908** (startup preflight) and
**TC-0411** (claims example pinned) — the two that cost the most debugging
time.

## Deliberately not tested

No auth tests — no auth exists (§13.1). No load or concurrency testing beyond
TC-1206. No multi-language output tests — unimplemented, and §13.3 lists
questions that must be answered before it can be meaningfully tested. These are
gaps by decision, recorded rather than hidden.
