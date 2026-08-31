# Test cases

Numbering: `TC-<area><nn>`. Priority **P0** = blocks the demo, **P1** = should pass,
**P2** = nice to have.

Areas: 01 ingestion · 02 parameters · 03 registry · 04 generation · 05 QA checkers ·
06 verdict and retry · 07 export · 08 job lifecycle · 09 provider and routing ·
10 tool security · 11 end to end · 12 non-functional.

---

## 01 — Ingestion

| ID | Case | Input | Expected | P |
|---|---|---|---|---|
| TC-0101 | Text PDF | 3-page digital PDF | `source_id` returned; text length > 0; chunks written to Qdrant with `source_id` payload | P0 |
| TC-0102 | Scanned PDF, OCR fallback | 12-page scan | Extraction returns < 200 chars → OCR path runs → text extracted; `source_type` still `pdf` | P0 |
| TC-0103 | DOCX | .docx with headings | Text extracted, headings preserved in `text` | P1 |
| TC-0104 | HTML / URL | news article URL | Boilerplate stripped, article body extracted | P1 |
| TC-0105 | Image | photo of a poster | `media[]` populated with caption + OCR text; original retained as attachment | P1 |
| TC-0106 | Video under limit | 4-minute MP4 | Transcript produced, chunked, embedded | P1 |
| TC-0107 | **Video at boundary** | exactly 10:00 | Accepted | P0 |
| TC-0108 | **Video over limit** | 10:01 | Rejected with paywall stub; no transcription attempted; no partial source row | P0 |
| TC-0109 | Duplicate upload | same file twice | Second call returns the **existing** `source_id`; no re-extraction, no new embeddings | P0 |
| TC-0110 | Corrupt file | truncated PDF | Fails in < 3s with a clear message; no job created; no tokens spent | P0 |
| TC-0111 | Password-protected PDF | encrypted PDF | Rejected with a specific message, not a generic 500 | P1 |
| TC-0112 | Empty source | blank page scan | OCR returns nothing → fail with "no readable content" | P0 |
| TC-0113 | Unsupported type | .exe upload | Rejected listing accepted types | P1 |
| TC-0114 | Chunk id stability | ingest, then re-read source | Chunk ids identical across reads; ids never renumbered | P0 |
| TC-0115 | Free-form prompt only | text, no file | Source created from text; chunking still applies | P1 |
| TC-0116 | Multi-source job | 3 reports | Job references 3 `source_id`s; retrieval spans all three | P2 |
| TC-0117 | **No LLM in ingest path** | any ingest | Assert zero LiteLLM completion calls during `POST /sources` (embeddings excepted) | P0 |

## 02 — Parameters

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0201 | Defaults applied | Job with no parameters set generates using documented defaults, not empty slots | P0 |
| TC-0202 | Invalid enum | `audience: "penguins"` rejected with the valid list | P1 |
| TC-0203 | Parameters reach the prompt | Rendered prompt contains the selected audience and tone verbatim | P0 |
| TC-0204 | **Parameters are in the cache key** | Same source + format, tone changed → cache **miss**, fresh generation | P0 |
| TC-0205 | Same everything | Same source + format + parameters → cache **hit**, no provider call | P1 |

## 03 — Output registry

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0301 | Unknown format id | Rejected before any job is created | P1 |
| TC-0302 | **Add a format with zero code change** | New JSON entry + template + schema → format appears in dashboard and generates | P0 |
| TC-0303 | Registry supplies model alias | Advisory routes to `long`, social routes to `fast` | P1 |
| TC-0304 | Constraints loaded | `max_chars` from registry is what the format checker enforces | P0 |

## 04 — Generation

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0401 | Single format | One artefact produced, schema-valid | P0 |
| TC-0402 | Six formats | Six artefacts; **input analysis ran exactly once** | P0 |
| TC-0403 | Claims carry chunk ids | Every entry in `claims[]` has a `chunk_id` that exists in the source | P0 |
| TC-0404 | Invented chunk id | Model cites `c99` (nonexistent) → caught, treated as grounding failure not a crash | P0 |
| TC-0405 | Malformed JSON | Parse retry fires; **QA retry counter unchanged** | P0 |
| TC-0406 | Persistent malformed JSON | After parse retries exhausted, artefact marked failed with reason | P1 |
| TC-0407 | One generator fails | Other five artefacts still complete; failed one marked retryable | P0 |
| TC-0408 | Analysis is shared | Analysis object identical across all six artefacts (same object id / hash) | P1 |
| TC-0409 | Commentary mode | `literary_copyrighted` source → narration paraphrases; no long verbatim spans | P1 |
| TC-0410 | Video package shape | Contains scenes, narration, storyboard notes, subtitles, visual recommendations | P0 |

## 05 — QA checkers

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0501 | Grounding pass | All claims match cited chunks → pass | P0 |
| TC-0502 | Grounding fail | Inject a claim absent from the source → fail, with the offending claim named | P0 |
| TC-0503 | **Format check uses no LLM** | Assert zero completion calls in the format checker | P0 |
| TC-0504 | Length violation | Tweet at 281 chars → fail deterministically | P0 |
| TC-0505 | Constraint violation | 7 hashtags where max is 5 → fail | P1 |
| TC-0506 | Tone score returned | Numeric score plus a reason string, not a bare boolean | P1 |
| TC-0507 | Safety catch | Artefact containing a personal email address → safety fail | P0 |
| TC-0508 | **Checkers are independent** | Checker prompts contain no other checker's verdict | P0 |
| TC-0509 | Checkers run in parallel | Wall time ≈ slowest checker, not the sum | P1 |
| TC-0510 | Source reuse flag | 15+ consecutive words matching source → flagged | P1 |
| TC-0511 | Source reuse block | 60-word verbatim span in narration → blocked | P1 |
| TC-0512 | Reuse check skipped | Non-copyrighted source → checker not invoked | P2 |

## 06 — Verdict and retry

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0601 | All pass | Artefact proceeds to export | P0 |
| TC-0602 | **Safety fail blocks** | Even with other three passing, artefact is blocked, not delivered | P0 |
| TC-0603 | Grounding fail retries | Retry fires with a fix note naming the failing claim | P0 |
| TC-0604 | **Fix note is specific** | Retry prompt contains the claim or constraint text, not "quality insufficient" | P0 |
| TC-0605 | Tone below threshold | Retries once, then passes with a warning flag on the artefact | P1 |
| TC-0606 | **Retry is per artefact** | Failing tweet retries alone; presentation is not regenerated | P0 |
| TC-0607 | Retry cap | Third QA failure → job stopped, operator message shown | P0 |
| TC-0608 | Retry counter isolation | Artefact A at 2 retries; artefact B still starts at 0 | P0 |
| TC-0609 | **Provider error does not count** | Simulated 429 → backoff retry; QA counter unchanged | P0 |
| TC-0610 | Flagged artefacts surface | Blocked/flagged artefacts appear in the review view, marked | P1 |
| TC-0611 | Retry improves | Example A regression: tone 0.61 → ≥ 0.80 after fix note | P1 |

## 07 — Export

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0701 | Markdown / PDF | File written, opens, content matches artefact JSON | P0 |
| TC-0702 | PPTX with notes | Slides present; speaker notes populated | P1 |
| TC-0703 | SRT timings | Cue count matches scenes; timings sum to `runtime_target_sec` ± tolerance | P1 |
| TC-0704 | MP4 render | TTS + title cards + ffmpeg produce a playable file | P1 |
| TC-0705 | **No generative video model called** | Assert no video-generation API in the export path | P0 |
| TC-0706 | Download link valid | Returned URL resolves to the written asset | P0 |

## 08 — Job lifecycle

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0801 | Async return | `POST /jobs` returns in < 1s with `job_id` | P0 |
| TC-0802 | Status polling | `GET /jobs/{id}` reflects per-artefact progress | P0 |
| TC-0803 | Partial completion | 4 of 6 done → status shows partial honestly, not "complete" | P0 |
| TC-0804 | Recoverable vs permanent | Provider outage → `failed_recoverable`; bad source → `failed_permanent` | P1 |
| TC-0805 | Job stopped message | After 3 strikes, operator sees a restart instruction, not a stack trace | P0 |
| TC-0806 | History | Completed job appears in history with artefacts and QA verdicts | P1 |
| TC-0807 | Source reuse from history | New job on saved source skips ingestion entirely | P0 |
| TC-0808 | Regenerate one | UC-09 re-enters at generation; analysis not recomputed | P1 |

## 09 — Provider and routing

| ID | Case | Expected | P |
|---|---|---|---|
| TC-0901 | **Alias not provider** | Grep agent code for provider names → zero hits outside router config | P0 |
| TC-0902 | Fallback fires | Groq returns 429 → OpenRouter serves the call, job continues | P0 |
| TC-0903 | Long alias routing | Advisory and video package use `long` | P1 |
| TC-0904 | QA concurrency cap | Four checkers do not exceed the configured concurrent-call limit | P1 |
| TC-0905 | Embeddings bypass gateway | Embedding calls do not pass through LiteLLM router | P1 |
| TC-0906 | All providers down | Job fails cleanly with a provider-error message; no silent hang | P1 |

## 10 — Tool registry and security

| ID | Case | Expected | P |
|---|---|---|---|
| TC-1001 | **Grounding checker has no web search** | Attempted web-search call from grounding checker is denied by the registry | P0 |
| TC-1002 | Generator cannot write chunks | No write/delete tool exposed to output subagents | P0 |
| TC-1003 | Cross-job isolation | `search_chunks` with another job's id returns nothing / is rejected | P0 |
| TC-1004 | No raw client exposed | Agents cannot reach a bare Qdrant client | P0 |
| TC-1005 | Calls audited | Every tool call appears in the Langfuse trace | P1 |

## 11 — End to end

| ID | Case | Expected | P |
|---|---|---|---|
| TC-1101 | **Golden path** | Advisory PDF + LinkedIn + exec summary → both artefacts, QA verdicts, exports | P0 |
| TC-1102 | Example A reproduction | Matches `USE-CASES.md` Example A: 4/4 grounding, tone fail then pass on retry | P0 |
| TC-1103 | Example B reproduction | Literary excerpt → video package, commentary mode, reuse flag not block | P1 |
| TC-1104 | Six-format job | All six artefacts produced; one analysis; no cross-artefact retry contamination | P0 |
| TC-1105 | Cold start | `docker compose up` from clean checkout → golden path passes | P0 |
| TC-1106 | **Volumes persist** | `docker compose down && up` → embeddings still present, no re-ingest | P0 |
| TC-1107 | Service name host | App reaches Qdrant at `http://qdrant:6333` from inside the container | P0 |

## 12 — Non-functional

| ID | Case | Target | P |
|---|---|---|---|
| TC-1201 | Ingestion latency | 3-page PDF ingested in < 10s | P1 |
| TC-1202 | Single artefact latency | Generation + QA in < 20s | P0 |
| TC-1203 | Six-artefact job latency | Under 3 minutes end to end — **measure before the demo** | P0 |
| TC-1204 | Call budget | Six-format job ≈ 40 model calls; log the actual count | P1 |
| TC-1205 | Cached rerun | Repeat identical job hits cache; provider calls near zero | P1 |
| TC-1206 | Rate-limit resilience | Three jobs back to back complete without manual intervention | P0 |

---

## Evaluation set (UC-14)

A fixed test set is provided. Run it against versioned prompts so a change can be
compared rather than guessed at.

| Field | Meaning |
|---|---|
| `source_id` | Fixed source from the set |
| `output_type` | Format under test |
| `parameters` | Fixed parameter block |
| `expected_properties` | Assertions: claim count ≥ n, length band, required keys, tone ≥ threshold |
| `prompt_version` | Version under evaluation |

Report per run: pass rate by checker, mean retries per artefact, mean latency, total
model calls. Compare v(n) against v(n-1) on the same set — that comparison is the
whole point of versioning prompts.

## What is deliberately not tested

No auth tests — no auth exists in demo scope. No load or concurrency tests beyond
TC-1206. No multi-language output tests — translation is not yet designed. These are
gaps by decision, recorded in `ARCHITECTURE.md`.
