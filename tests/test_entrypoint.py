"""Tests for the console entry point wiring.

The real ``main()`` blocks on the stdio event loop, so we patch ``build_app`` and
``app.run`` to assert the wiring (build the app, serve over the locked ``stdio``
transport) without actually serving.

Note: ``trader_mcp.server`` re-exports the ``main`` *function*, which shadows the
``trader_mcp.server.main`` submodule on attribute access. We therefore reach the
submodule via :mod:`importlib` and patch it by its dotted path.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from trader_mcp.server.app import TRANSPORT

_main_module = importlib.import_module("trader_mcp.server.main")


def test_main_builds_app_and_runs_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls: list[dict[str, Any]] = []

    class _FakeApp:
        def run(self, *, transport: str) -> None:
            run_calls.append({"transport": transport})

    monkeypatch.setattr(_main_module, "build_app", lambda: _FakeApp())
    _main_module.main()

    assert run_calls == [{"transport": TRANSPORT}]
    assert TRANSPORT == "stdio"
