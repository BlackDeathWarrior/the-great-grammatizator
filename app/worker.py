"""arq worker entrypoint. `arq app.worker.WorkerSettings`.

The queue exists because the HTTP request cannot wait 30-90s for generation,
and because rate limiting and backoff live here (ARCHITECTURE.md §2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from arq.connections import RedisSettings

from app.config import get_settings


async def ping(ctx: dict) -> dict[str, str]:
    """Liveness probe for the queue itself.

    Also the reason the worker can boot before Phase 7: arq refuses to start
    with no registered functions.
    """
    return {"pong": datetime.now(UTC).isoformat()}


async def startup(ctx: dict) -> None:
    ctx["settings"] = get_settings()


class WorkerSettings:
    functions = [ping]  # run_job joins this in Phase 7
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
