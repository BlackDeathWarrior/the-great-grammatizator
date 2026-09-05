"""FastAPI entrypoint. Layer 1 (web) + layer 2 (API) mount here."""

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import jobs, sources
from app.config import get_settings
from app.web import routes as web_routes
from app.web import settings_routes

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

    # Probe every configured model id. Free-tier ids drift and the failure
    # otherwise surfaces as a job dying mid-demo (TC-0908). Never fatal: the
    # API must still serve /health so the operator can see the warning.
    try:
        from app.gateway.router import preflight

        rows = await preflight()
        dead = [r for r in rows if not r["ok"]]
        if dead:
            logging.getLogger(__name__).warning(
                "%d of %d model deployments unusable: %s",
                len(dead),
                len(rows),
                ", ".join(f"{r['model']} ({r['error'][:60]})" for r in dead),
            )
        else:
            logging.getLogger(__name__).info("preflight: %d model(s) OK", len(rows))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("preflight skipped: %s", exc)

    yield


app = FastAPI(title="The Great Grammatizator", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(pathlib.Path(__file__).parent / "web" / "static")),
    name="static",
)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError):
    """Report bad input as 400 with a readable message.

    FastAPI's default is 422 with a nested error structure. The rest of this
    API answers bad input with 400 and a sentence, and TC-0206 specifies 400,
    so a rejected parameter should not be the one place that differs.
    """
    problems = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        problems.append(f"{field}: {msg}" if field else msg)

    return JSONResponse(status_code=400, content={"detail": "; ".join(problems)})


app.include_router(sources.router)
app.include_router(jobs.router)
app.include_router(web_routes.router)
app.include_router(settings_routes.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Dependency probes arrive with the services that need them."""
    s = get_settings()
    return {"status": "ok", "qdrant_url": s.qdrant_url}
