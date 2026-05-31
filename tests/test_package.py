"""Smoke tests for package import and metadata."""

from __future__ import annotations

import trader_mcp


def test_package_exposes_version() -> None:
    assert isinstance(trader_mcp.__version__, str)
    assert trader_mcp.__version__ != ""


def test_package_all_exports_version() -> None:
    assert "__version__" in trader_mcp.__all__


def test_server_main_is_importable() -> None:
    from trader_mcp.server import build_app, main

    assert callable(main)
    assert callable(build_app)
