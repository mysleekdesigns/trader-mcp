"""Shared pytest fixtures for the trader-mcp test suite.

``qa-parity-engineer`` expands this with the backtest<->live parity harness and
recorded-fixture integration in later phases.
"""

from __future__ import annotations

import pytest

from trader_mcp.config import get_settings


@pytest.fixture
def clean_settings_cache() -> None:
    """Clear the cached :func:`get_settings` result before a test.

    Lets a test mutate the environment and observe a fresh ``Settings`` load.
    """
    get_settings.cache_clear()
