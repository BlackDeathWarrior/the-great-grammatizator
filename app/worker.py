"""arq worker entrypoint. `arq app.worker.WorkerSettings`.

The queue exists because the HTTP request cannot wait 30-90s for generation,
and because rate limiting and backoff live here (ARCHITECTURE.md §2).
"""

from arq.connections import RedisSettings

from app.config import get_settings


async def startup(ctx: dict) -> None:
    ctx["settings"] = get_settings()


class WorkerSettings:
    functions: list = []  # run_job registered in Phase 7
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
