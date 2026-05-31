"""Tests for the typed Pydantic v2 I/O models behind the MCP tool surface.

The Phase 0 admin tools take no input, so the typed-I/O contract is enforced by
the strict output models: unexpected fields are rejected, wrong types are
rejected, and assignment is validated. These are the boundary guarantees
``mcp-server-engineer`` relies on when adding real tool inputs in Phase 1.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trader_mcp.server.schemas import HealthCheckResult, ServerStatusResult


def _valid_status_kwargs() -> dict[str, object]:
    return {
        "name": "trader-mcp",
        "version": "0.0.0",
        "transport": "stdio",
        "started_at": datetime.now(UTC),
        "uptime_seconds": 1.5,
        "tool_count": 2,
    }


def test_health_check_accepts_valid_payload() -> None:
    result = HealthCheckResult(status="ok", version="0.0.0", timestamp=datetime.now(UTC))
    assert result.status == "ok"


def test_server_status_accepts_valid_payload() -> None:
    result = ServerStatusResult(**_valid_status_kwargs())  # type: ignore[arg-type]
    assert result.tool_count == 2


def test_models_forbid_unexpected_fields() -> None:
    """``extra='forbid'`` rejects bad input at the model boundary."""
    with pytest.raises(ValidationError):
        HealthCheckResult(
            status="ok",
            version="0.0.0",
            timestamp=datetime.now(UTC),
            surprise="leak",  # type: ignore[call-arg]
        )


def test_server_status_rejects_wrong_types() -> None:
    kwargs = _valid_status_kwargs()
    kwargs["uptime_seconds"] = "not-a-float"
    kwargs["tool_count"] = "not-an-int"
    with pytest.raises(ValidationError):
        ServerStatusResult(**kwargs)  # type: ignore[arg-type]


def test_models_validate_on_assignment() -> None:
    """``validate_assignment=True`` keeps invariants after construction."""
    result = HealthCheckResult(status="ok", version="0.0.0", timestamp=datetime.now(UTC))
    with pytest.raises(ValidationError):
        result.timestamp = "not-a-datetime"  # type: ignore[assignment]


def test_output_schema_marks_all_fields_required() -> None:
    """No Optional fields in Phase 0: the JSON Schema requires every field."""
    schema = ServerStatusResult.model_json_schema()
    assert set(schema["required"]) == set(ServerStatusResult.model_fields)
