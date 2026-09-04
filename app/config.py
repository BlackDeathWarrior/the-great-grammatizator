"""Settings. Env only, no literals (docs/CLAUDE.md: no config option I didn't ask for)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Providers. Read ONLY by app/gateway/router.py (Invariant 5, TC-0901),
    # except EMBEDDING_* which app/ingest/embed.py reads directly by design
    # (ARCHITECTURE.md §2: embeddings bypass the router, TC-0905).
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""

    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # Services. Compose service names, not localhost (TC-1107).
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "chunks"
    database_url: str = "postgresql+psycopg://postgres:dev@postgres:5432/app"
    redis_url: str = "redis://redis:6379"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Limits (ARCHITECTURE.md "Limits").
    video_max_seconds: int = 600  # 10 min hard gate, TC-0107/TC-0108
    qa_max_retries: int = 3  # 3 strikes then stop the job, TC-0607
    parse_max_retries: int = 2  # separate counter, never burns a QA retry, TC-0405
    qa_concurrency: int = 2  # cap so the checkers don't fire at once, TC-0904
    tone_threshold: float = 0.80
    # Below this an artefact reads as unpublishable, whatever tone thinks of
    # the fit. Tone alone can never block (it is advisory by policy), so a
    # floor is the only thing stopping a 0.30 artefact shipping flagged.
    tone_floor: float = 0.55
    # Calibrated against this model, not chosen a priori. Scored against
    # known-quality drafts on the free tier, the editorial checker RANKS
    # reliably (good 0.45 > mediocre 0.25 > filler 0.05) but its absolute
    # scale sits far below where the wording implies. At 0.75 it blocked
    # everything including drafts a professional would ship, which teaches the
    # operator nothing and wastes three retries proving it.
    #
    # Measured spread on IDENTICAL input is ~0.10 (0.32-0.42 over five runs),
    # so a threshold must sit below that band rather than inside it, or the
    # same draft passes or fails at random. 0.30 clears every good draft
    # measured and still blocks filler, which scored 0.05.
    #
    # This gates the FLOOR. Quality above it is carried by the fix notes and
    # by the variant comparison, which rank reliably even where the absolute
    # numbers do not. Raise it when a stronger model is configured - and
    # re-measure rather than guessing.
    editorial_threshold: float = 0.30

    # Timeouts. Without these a hung connection is indistinguishable from slow
    # work, and the only thing that eventually notices is the arq job timeout
    # 900s later - by which point the operator has watched a job sit at
    # "running" for fifteen minutes with no way to tell why.
    request_timeout: int = 60  # one model call
    embedding_timeout: int = 30  # one embeddings batch
    fanout_concurrency: int = 3  # generators in flight at once, TC-0904

    storage_dir: str = "/data/storage"


@lru_cache
def get_settings() -> Settings:
    return Settings()
