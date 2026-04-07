"""Unified MCP error shape: stable ``code``, ``retryable``, ``suggested_next_steps``.

Agent-facing strings are English. See docs/mcp_metadata_and_errors.md.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# --- Stable error codes (UPPER_SNAKE; do not rename lightly) ---

CONFIG_INVALID = "CONFIG_INVALID"
"""Invalid deployment config (e.g. missing HYBRID_DB or file missing)."""

INPUT_VALIDATION = "INPUT_VALIDATION"
"""Missing required fields, invalid type/value, or unparseable input."""

UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
"""Operation name or enum not in the allowed set (e.g. symbol_graph ``op``, ``graph_mode``)."""

RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
"""Reserved; most queries return empty lists instead of this error."""

PERMISSION_DENIED = "PERMISSION_DENIED"
"""Unauthorized (rare for this read-only MCP; HTTP /admin is separate)."""

TIMEOUT = "TIMEOUT"

RATE_LIMITED = "RATE_LIMITED"

UPSTREAM_ERROR = "UPSTREAM_ERROR"
"""Network, connection failure, or upstream 5xx — often retryable."""

INTERNAL_ERROR = "INTERNAL_ERROR"
"""Unclassified server error."""


def sanitize_for_client(message: str, db_path: str | None) -> str:
    text = str(message)
    if db_path:
        text = text.replace(db_path, "<db>")
    return text[:4000]


def mcp_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    suggested_next_steps: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if suggested_next_steps:
        err["suggested_next_steps"] = suggested_next_steps
    if details:
        err["details"] = details
    return err


def exception_to_mcp_error(exc: BaseException, db_path: str | None) -> dict[str, Any]:
    """Map unexpected exceptions to a stable error object."""
    raw = str(exc)
    msg = sanitize_for_client(raw, db_path)
    name = type(exc).__name__
    lower = raw.lower()

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
        "timeout" in lower or "timed out" in lower
    ):
        return mcp_error(
            TIMEOUT,
            msg,
            retryable=True,
            suggested_next_steps=[
                "Retry later; if it persists, check network and embedding endpoint reachability.",
            ],
            details={"exception_type": name},
        )

    if isinstance(exc, ConnectionError) or any(
        x in lower for x in ("connection refused", "name resolution", "network is unreachable", "connection reset")
    ):
        return mcp_error(
            UPSTREAM_ERROR,
            msg,
            retryable=True,
            suggested_next_steps=[
                "Check outbound network and DNS from this host; confirm embedding/upstream services are up, then retry.",
            ],
            details={"exception_type": name},
        )

    if "429" in raw or "rate limit" in lower or "too many requests" in lower:
        return mcp_error(
            RATE_LIMITED,
            msg,
            retryable=True,
            suggested_next_steps=[
                "Reduce concurrency or wait before retrying; verify API quota and credentials.",
            ],
            details={"exception_type": name},
        )

    if any(x in lower for x in ("503", "502", "504", "bad gateway", "service unavailable")):
        return mcp_error(
            UPSTREAM_ERROR,
            msg,
            retryable=True,
            suggested_next_steps=["Upstream service is temporarily unavailable; retry later."],
            details={"exception_type": name},
        )

    return mcp_error(
        INTERNAL_ERROR,
        msg,
        retryable=False,
        suggested_next_steps=[
            "If reproducible, report error.details.exception_type and error.message to operators.",
        ],
        details={"exception_type": name},
    )


def tool_result_config_error(message: str) -> str:
    """JSON string when HYBRID_DB is missing or invalid (``tool`` is null)."""
    return json.dumps(
        {
            "ok": False,
            "tool": None,
            "error": mcp_error(
                CONFIG_INVALID,
                message,
                retryable=False,
                suggested_next_steps=[
                    "Set environment variable HYBRID_DB to an absolute path of an existing SQLite index file.",
                    "Optionally set HYBRID_CONFIG to your JSON config path.",
                    "If the agent cannot spawn this process, use the REST ``serve`` API documented for remote agents.",
                ],
            ),
        },
        ensure_ascii=False,
    )
