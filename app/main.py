"""FastAPI entrypoint. Layer 1 (web) + layer 2 (API) mount here."""

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import jobs, sources
from app.config import get_settings
from app.web import routes as web_routes

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the single shared qdrant collection exists before any ingest.
    # Best-effort: a cold qdrant must not stop the API from serving /health.
    try:
        from app.ingest.embed import ensure_collection

        ensure_collection()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("qdrant not ready at startup: %s", exc)
    yield


app = FastAPI(title="Content Transformation Platform", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(pathlib.Path(__file__).parent / "web" / "static")),
    name="static",
)

app.include_router(sources.router)
app.include_router(jobs.router)
app.include_router(web_routes.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Dependency probes arrive with the services that need them."""
    s = get_settings()
    return {"status": "ok", "qdrant_url": s.qdrant_url}
