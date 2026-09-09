"""Operator-requested cancellation.

A seven-format job runs for minutes. An operator who has spotted the wrong
source, the wrong audience, or simply changed their mind had no way to stop it
and had to watch their own rate limits burn.

The flag lives in Redis rather than on the job row for two reasons. It must be
visible to the worker *immediately* - the artefact loop polls it between units
of work, and a Postgres round trip per artefact is a cost the run should not
carry. And it is disposable by nature: losing it loses a cancellation, never a
result, which is the same rule `progress` keeps.

**Cancellation is cooperative, and deliberately so.** Killing the coroutine
mid-flight would abandon a half-written artefact and leave the export directory
inconsistent. Instead the flag is checked at the boundary between artefacts:
work already finished is kept and persisted, work not yet started never begins.
An operator who stops a job at five of seven keeps those five.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_PREFIX = "jobcancel:"
# Long enough to outlive any job (the worker's own timeout is 900s), short
# enough that a stale flag cannot haunt a job id that gets reused.
_TTL_SECONDS = 3600


class JobCancelled(Exception):
    """Raised inside the run when the operator has asked for a stop.

    Distinct from asyncio.CancelledError, which the event loop owns and which
    means the coroutine is being torn down. This one is a decision, not a
    fault: the caller records what finished and reports it as stopped, never as
    failed.
    """


def _redis():
    import redis

    from app.config import get_settings

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def request(job_id: str) -> None:
    """Ask a running job to stop at its next artefact boundary."""
    try:
        _redis().setex(_PREFIX + job_id, _TTL_SECONDS, "1")
    except Exception as exc:  # noqa: BLE001 - never break the request path
        log.warning("could not record cancellation for %s: %s", job_id, exc)


def is_requested(job_id: str) -> bool:
    """Has a stop been asked for?

    Fails OPEN: if Redis cannot be reached the job continues. A cancellation
    that silently did not happen is a nuisance; a job that stopped because the
    cache blinked would be a bug, and the operator can always ask again.
    """
    try:
        return _redis().get(_PREFIX + job_id) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read cancellation for %s: %s", job_id, exc)
        return False


def clear(job_id: str) -> None:
    """Drop the flag once the job has stopped, so a rerun is not born cancelled."""
    try:
        _redis().delete(_PREFIX + job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not clear cancellation for %s: %s", job_id, exc)
