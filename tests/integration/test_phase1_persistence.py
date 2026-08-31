"""Phase 1 done-criterion: the schema round-trips and enforces its invariants.

Requires the compose data services. Run:  docker compose up -d postgres qdrant redis
"""

import os

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Artefact, Base, Job, JobSource, QAResultRow, Source
from app.graph.state import ArtefactStatus, JobStatus, SourceType

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres:dev@localhost:5432/app")


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine

    eng = create_engine(DB_URL, future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import sessionmaker

    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield s
    # Leave no residue between tests.
    s.rollback()
    for model in (QAResultRow, Artefact, JobSource, Job, Source):
        s.query(model).delete()
    s.commit()
    s.close()


def _source(session, hash_="sha256:aaa") -> Source:
    src = Source(
        source_hash=hash_,
        source_type=SourceType.PDF,
        title="Critical auth bypass",
        text="full text",
        chunks=[{"id": "c1", "text": "rated 9.1", "page": 1}],
    )
    session.add(src)
    session.commit()
    return src


@pytest.mark.p0
def test_job_with_three_artefacts_round_trips_at_distinct_retry_counts(session):
    """TC-0608: retry counters are per artefact and survive persistence.

    This is the Phase 1 done-criterion from the plan.
    """
    src = _source(session)
    job = Job(status=JobStatus.RUNNING, formats=["linkedin_post", "twitter_x", "presentation"])
    job.sources.append(JobSource(source_id=src.id))
    job.artefacts.extend(
        [
            Artefact(output_type="linkedin_post", retry_count=2, status=ArtefactStatus.QA),
            Artefact(output_type="twitter_x", retry_count=0, status=ArtefactStatus.PASSED),
            Artefact(output_type="presentation", retry_count=1, parse_retry_count=2),
        ]
    )
    session.add(job)
    session.commit()
    session.expire_all()

    reloaded = session.get(Job, job.id)
    counts = {a.output_type: a.retry_count for a in reloaded.artefacts}
    assert counts == {"linkedin_post": 2, "twitter_x": 0, "presentation": 1}

    # Invariant 8: the parse counter is genuinely independent of the QA counter.
    deck = next(a for a in reloaded.artefacts if a.output_type == "presentation")
    assert deck.parse_retry_count == 2
    assert deck.retry_count == 1


@pytest.mark.p0
def test_duplicate_source_hash_is_rejected_by_the_database(session):
    """TC-0109: the same file is never ingested twice.

    Enforced by a unique constraint rather than an application-level check, so
    a concurrent second upload cannot slip past it.
    """
    _source(session, "sha256:dup")
    session.add(
        Source(source_hash="sha256:dup", source_type=SourceType.PDF, title="again", text="x")
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.p1
def test_one_source_many_jobs(session):
    """ARCHITECTURE.md §2: a source is ingested once; a job references it.

    The operator can return the next day and generate a new format without
    re-uploading (TC-0807).
    """
    src = _source(session)
    for fmt in ("linkedin_post", "exec_summary"):
        job = Job(formats=[fmt])
        job.sources.append(JobSource(source_id=src.id))
        session.add(job)
    session.commit()

    assert session.query(Job).count() == 2
    assert session.query(Source).count() == 1


@pytest.mark.p1
def test_qa_results_are_kept_per_attempt(session):
    """Worked Example A shows tone 0.61 -> 0.84 across a retry.

    Overwriting the row would lose the evidence the dashboard and eval runs need.
    """
    src = _source(session)
    job = Job(formats=["linkedin_post"])
    job.sources.append(JobSource(source_id=src.id))
    art = Artefact(output_type="linkedin_post", retry_count=1)
    art.qa_results.extend(
        [
            QAResultRow(attempt=0, checker="tone", passed=False, score=0.61, reason="alarmist"),
            QAResultRow(attempt=1, checker="tone", passed=True, score=0.84, reason="ok"),
        ]
    )
    job.artefacts.append(art)
    session.add(job)
    session.commit()
    session.expire_all()

    rows = sorted(session.get(Artefact, art.id).qa_results, key=lambda r: r.attempt)
    assert [r.score for r in rows] == [0.61, 0.84]


@pytest.mark.p1
def test_one_artefact_per_format_per_job(session):
    """UC-03: one artefact record is created per selected format."""
    src = _source(session)
    job = Job(formats=["linkedin_post"])
    job.sources.append(JobSource(source_id=src.id))
    job.artefacts.extend(
        [
            Artefact(output_type="linkedin_post"),
            Artefact(output_type="linkedin_post"),
        ]
    )
    session.add(job)
    with pytest.raises(IntegrityError):
        session.commit()
