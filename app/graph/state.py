"""Job state. Defined FIRST, before any node exists (ARCHITECTURE.md §4).

Every node reads and writes this one shape. Nodes passing bespoke payloads to
each other is where these graphs get tangled.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field, field_validator

# --- enums -----------------------------------------------------------------


class SourceType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    HTML = "html"
    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"


class Provenance(StrEnum):
    """Classified during input analysis (ARCHITECTURE.md §6).

    LITERARY_COPYRIGHTED switches generation into commentary mode: narration
    analyses and paraphrases, quoted spans capped at short fragments.
    """

    ORIGINAL = "original"
    PUBLIC_RECORD = "public_record"
    LITERARY_COPYRIGHTED = "literary_copyrighted"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    # TC-0804. ARCHITECTURE.md lists the recoverable/permanent split as an open
    # gap; closing it here. Provider outage is recoverable, bad source is not.
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_PERMANENT = "failed_permanent"
    # 3 QA strikes: stop the job and tell the operator to restart (TC-0607).
    STOPPED_QA_BUDGET = "stopped_qa_budget"


class ArtefactStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    QA = "qa"
    PASSED = "passed"
    PASSED_FLAGGED = "passed_flagged"  # tone below threshold after one retry
    BLOCKED = "blocked"  # safety fail: never delivered (TC-0602)
    FAILED = "failed"


class Verdict(StrEnum):
    PASS = "pass"
    PASS_FLAGGED = "pass_flagged"
    RETRY = "retry"
    BLOCK = "block"


class CheckerName(StrEnum):
    GROUNDING = "grounding"
    FORMAT = "format"
    TONE = "tone"
    SAFETY = "safety"
    SOURCE_REUSE = "source_reuse"
    # Is the writing any good? Nothing else asks (see app/agents/qa/editorial.py).
    EDITORIAL = "editorial"


# --- frozen content --------------------------------------------------------


class Chunk(BaseModel):
    """Immutable. Created once at ingest, never renumbered (Invariant 2, TC-0114).

    Every factual claim in a generated artefact cites one of these by id.
    """

    model_config = {"frozen": True}

    id: str
    text: str
    page: int | None = None
    source_id: str


class Media(BaseModel):
    model_config = {"frozen": True}

    kind: str  # "image" | "audio" | "video"
    path: str
    caption: str | None = None
    ocr_text: str | None = None


class ContentObject(BaseModel):
    """The only thing downstream reads. Frozen after ingest (ARCHITECTURE.md §2).

    Shape is fixed by the spec; do not add fields without changing the doc.
    """

    model_config = {"frozen": True}

    source_id: str
    source_hash: str
    source_type: SourceType
    title: str
    text: str
    chunks: list[Chunk] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)

    def chunk_ids(self) -> set[str]:
        return {c.id for c in self.chunks}

    def chunk_by_id(self, chunk_id: str) -> Chunk | None:
        """A cited-but-nonexistent id is a grounding failure, not a crash (TC-0404)."""
        return next((c for c in self.chunks if c.id == chunk_id), None)


# --- analysis --------------------------------------------------------------


class AnalysisResult(BaseModel):
    """Written ONCE per job, cached, shared by every generator (Invariant 3).

    This is what makes selecting seven formats cost roughly the same analysis
    as selecting one (TC-0402, TC-0408).
    """

    model_config = {"frozen": True}

    objective: str
    audience: str
    key_facts: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    enrichment: list[str] = Field(default_factory=list)
    provenance: Provenance = Provenance.ORIGINAL

    @property
    def commentary_mode(self) -> bool:
        """Copyrighted source means paraphrase, short quoted fragments only."""
        return self.provenance is Provenance.LITERARY_COPYRIGHTED


# --- generation parameters -------------------------------------------------


# A parameter describes an audience or an intent; it is not a document. The
# cap is generous enough for "second-year CS students who have not seen memory
# safety before" and small enough that no parameter can carry a prompt.
MAX_PARAMETER_CHARS = 200


class Parameters(BaseModel):
    """The generation brief: who it is for, how it should read (UC-02).

    Validated for SHAPE, not membership of a fixed list. The dashboard offers
    closed vocabularies because a dropdown gives the tone checker something
    concrete to compare against (TC-0202), but POST /jobs used to accept any
    string at all - the docs call this "a real hole" (TC-0206). A value that is
    blank, or long enough to be a smuggled instruction, is refused everywhere:
    on the API, in the dashboard form, and from the interview.

    Part of the cache key: changing tone must produce a fresh generation
    (TC-0204).
    """

    audience: str = "general public"
    tone: str = "informative"
    language: str = "en"
    detail: str = "brief"
    objective: str = "inform"
    style: str = "plain"

    @field_validator("audience", "tone", "language", "detail", "objective", "style")
    @classmethod
    def _usable(cls, v: str, info) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError(f"{info.field_name} cannot be blank")
        if len(v) > MAX_PARAMETER_CHARS:
            raise ValueError(
                f"{info.field_name} is {len(v)} characters; keep it under "
                f"{MAX_PARAMETER_CHARS}. Parameters describe an audience or an "
                "intent, not a document."
            )
        if "\n" in v:
            raise ValueError(f"{info.field_name} must be a single line")
        return v

    def cache_fragment(self) -> str:
        return "|".join(f"{k}={v}" for k, v in sorted(self.model_dump().items()))


# --- QA --------------------------------------------------------------------


class Claim(BaseModel):
    """Every factual claim cites a chunk.

    This is what makes grounding cheap: the checker verifies against the cited
    chunk instead of searching the whole document (ARCHITECTURE.md §7).
    """

    text: str
    chunk_id: str


class CheckerResult(BaseModel):
    checker: CheckerName
    passed: bool
    # Tone returns a numeric score plus a reason string, never a bare boolean
    # (TC-0506). Deterministic checkers leave score None.
    score: float | None = None
    reason: str = ""
    # Fix notes must be SPECIFIC - the failing claim or the violated
    # constraint (§8, TC-0604). "Quality insufficient" returns the same output.
    fix_notes: list[str] = Field(default_factory=list)
    # The checker itself broke - a bad response shape, a template error, a bug.
    # This is NOT a quality judgement: the artefact was never actually assessed.
    # A hard checker that could not run must fail CLOSED (the verdict blocks)
    # rather than reporting a pass nobody verified.
    checker_error: bool = False


class QAResult(BaseModel):
    """Checkers are independent: none sees another verdict (TC-0508)."""

    results: list[CheckerResult] = Field(default_factory=list)
    verdict: Verdict = Verdict.PASS

    def by_checker(self, name: CheckerName) -> CheckerResult | None:
        return next((r for r in self.results if r.checker is name), None)

    def all_fix_notes(self) -> list[str]:
        return [n for r in self.results for n in r.fix_notes]


# --- artefact --------------------------------------------------------------


class Artefact(BaseModel):
    """One per selected output format. Carries its OWN counters.

    Retry is per artefact: a failing tweet must not regenerate the deck
    (Invariant 7, TC-0606, TC-0608).
    """

    output_type: str
    status: ArtefactStatus = ArtefactStatus.PENDING
    content: dict[str, Any] | None = None
    claims: list[Claim] = Field(default_factory=list)
    qa_result: QAResult | None = None

    # Two SEPARATE counters (Invariant 8).
    #   retry_count          - QA verdict failures only. 3 strikes stops the job.
    #   parse_retry_count    - malformed JSON. Never burns a QA retry (TC-0405).
    #   provider_error_count - 429/timeout. Diagnostic only, gates nothing
    #                          (TC-0609); a flaky free tier must not kill a job.
    retry_count: int = 0
    parse_retry_count: int = 0
    provider_error_count: int = 0

    tone_retried: bool = False  # tone retries once, then passes flagged (TC-0605)
    export_paths: list[str] = Field(default_factory=list)
    error: str | None = None


def merge_artefacts(left: dict[str, Artefact], right: dict[str, Artefact]) -> dict[str, Artefact]:
    """Reducer for the generate/QA fan-out.

    Artefacts are keyed by output_type and each branch only ever touches its own
    key, so a plain merge is safe and keeps concurrent branches from clobbering
    one another.
    """
    return {**left, **right}


class JobState(TypedDict, total=False):
    """The one shape every node writes into (ARCHITECTURE.md §4)."""

    job_id: str
    content: ContentObject  # frozen after ingest
    analysis: AnalysisResult | None  # written once
    parameters: Parameters
    artefacts: Annotated[dict[str, Artefact], merge_artefacts]  # keyed by output_type
    status: JobStatus
    operator_message: str | None  # e.g. the restart instruction after 3 strikes
