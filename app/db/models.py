"""Persistence. Sources are separate from jobs (ARCHITECTURE.md §2).

A source is ingested once. A job references it. One source, many jobs - the
operator can return the next day and generate a new format without re-uploading.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.graph.state import ArtefactStatus, JobStatus, Provenance, SourceType


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class Source(Base):
    """Ingested once, reused forever.

    The unique constraint on source_hash is what enforces dedup (TC-0109): the
    same file is never ingested twice, and the second upload returns the
    existing source_id immediately.
    """

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("source_hash", name="uq_sources_hash"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_hash: Mapped[str] = mapped_column(String(80), index=True)
    source_type: Mapped[SourceType] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    # Chunks live here as the canonical record AND in qdrant as vectors. Ids are
    # assigned once at ingest and never renumbered (Invariant 2, TC-0114).
    chunks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    jobs: Mapped[list[JobSource]] = relationship(back_populates="source")


class Job(Base):
    """References one or more sources plus parameters plus selected formats."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    status: Mapped[JobStatus] = mapped_column(String(24), default=JobStatus.QUEUED)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    formats: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # Analysis is written ONCE and shared by every generator (Invariant 3).
    # Storing it on the job - not per artefact - is what makes that structural.
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[Provenance] = mapped_column(String(24), default=Provenance.ORIGINAL)

    # After 3 QA strikes the operator gets a restart instruction, not a stack
    # trace (TC-0805).
    operator_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    sources: Mapped[list[JobSource]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artefacts: Mapped[list[Artefact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobSource(Base):
    """Association: a job may reference several sources; chunks merge into one
    retrieval set (ARCHITECTURE.md §2, TC-0116).
    """

    __tablename__ = "job_sources"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)

    job: Mapped[Job] = relationship(back_populates="sources")
    source: Mapped[Source] = relationship(back_populates="jobs")


class Artefact(Base):
    """One row per selected format. Owns its own retry counters.

    A failing tweet must not regenerate the deck (Invariant 7, TC-0606), which
    is why the counters live here and not on the job.
    """

    __tablename__ = "artefacts"
    __table_args__ = (UniqueConstraint("job_id", "output_type", name="uq_artefact_job_type"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    output_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[ArtefactStatus] = mapped_column(String(24), default=ArtefactStatus.PENDING)

    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    export_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # Two independent counters (Invariant 8). Only retry_count gates the
    # 3-strike stop; parse and provider failures must never consume it
    # (TC-0405, TC-0609).
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_error_count: Mapped[int] = mapped_column(Integer, default=0)
    tone_retried: Mapped[bool] = mapped_column(default=False)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="artefacts")
    variants: Mapped[list[Variant]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    qa_results: Mapped[list[QAResultRow]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )


class QAResultRow(Base):
    """One row per checker per attempt.

    Kept per attempt rather than overwritten so the dashboard can show that
    tone went 0.61 -> 0.84 across a retry (worked Example A), and so an eval run
    can report mean retries per artefact.
    """

    __tablename__ = "qa_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    artefact_id: Mapped[str] = mapped_column(ForeignKey("artefacts.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)

    checker: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column()
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    fix_notes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    artefact: Mapped[Artefact] = relationship(back_populates="qa_results")


class OperatorProfile(Base):
    """Who is asking. Deliberately NOT authentication (§13.1).

    Preferences have to hang off something, and this platform has no identity
    layer. A profile is a name the operator types once and a cookie remembers -
    enough to keep two operators' tastes apart and to carry a preference from
    one job to the next, and honest about being no more than that.

    Nothing here is a security boundary. If auth is ever built, this table is
    what a real principal would replace.
    """

    __tablename__ = "operator_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # What we have learned this operator likes, newest last. Plain sentences,
    # not weights: they are injected into the generator prompt, shown back in
    # the UI, and deletable one by one. A preference the operator cannot read
    # is a preference they cannot correct.
    style_notes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    preferences: Mapped[list[Preference]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class Variant(Base):
    """One candidate for an artefact, when the operator asked to choose.

    Variants share an artefact row: they are alternative CONTENT for the same
    (job, output_type), each independently QA'd, so choosing between them is a
    choice between things that have all passed the same bar.
    """

    __tablename__ = "variants"
    __table_args__ = (UniqueConstraint("artefact_id", "label", name="uq_variant_artefact_label"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    artefact_id: Mapped[str] = mapped_column(ForeignKey("artefacts.id"), index=True)

    # "A", "B", "C" - stable within an artefact so a recorded choice stays
    # meaningful after a reload.
    label: Mapped[str] = mapped_column(String(8))
    # What made this one different, in one phrase: the thing the operator is
    # really choosing between ("data-led opener", "question opener").
    approach: Mapped[str] = mapped_column(String(120), default="")

    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default=ArtefactStatus.PENDING)
    qa: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    export_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    chosen: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    artefact: Mapped[Artefact] = relationship(back_populates="variants")


class Preference(Base):
    """A recorded choice: this variant, over those, for this format.

    Kept as evidence rather than a score. The style note derived from a choice
    can be wrong, and when it is, the operator needs to see what it was derived
    FROM in order to disagree with it.
    """

    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("operator_profiles.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    output_type: Mapped[str] = mapped_column(String(64))

    chosen_approach: Mapped[str] = mapped_column(String(120), default="")
    rejected_approaches: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # The note this choice produced, so a note can be traced to its evidence.
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    profile: Mapped[OperatorProfile] = relationship(back_populates="preferences")
