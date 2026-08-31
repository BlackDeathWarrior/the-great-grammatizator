"""Tool registry. Importing this package registers every tool.

Import for side effects so the allowlist is complete before any agent runs.
"""

from app.tools import render, retrieval, websearch  # noqa: F401
from app.tools.registry import (  # noqa: F401
    ALLOWLIST,
    Caller,
    ToolDenied,
    ToolGroup,
    audit_log,
    available,
    call,
    registered,
)
