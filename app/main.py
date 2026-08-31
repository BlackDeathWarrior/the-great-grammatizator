"""FastAPI entrypoint. Layer 1 (web) + layer 2 (API) mount here."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Routers, db engine and qdrant collection bootstrap land here in later
    # phases. Kept empty so Phase 0's health check has nothing to fail on.
    yield


app = FastAPI(title="Content Transformation Platform", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Dependency probes arrive with the services that need them."""
    s = get_settings()
    return {"status": "ok", "qdrant_url": s.qdrant_url}
