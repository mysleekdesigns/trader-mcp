"""Tests for the Phase 3 guided strategy-design MCP prompts.

The prompts are pure guidance text (no code execution, no order/arming surface).
They must register, render, and -- crucially -- reference the LIVE template ids and
indicator whitelist so the guidance can never drift from the implementation, and
they must steer the model to validate before creating and to use only whitelisted
indicators/operators.
"""

from __future__ import annotations

from typing import Any

from trader_mcp.indicators import list_indicators
from trader_mcp.server.app import build_app
from trader_mcp.strategy import list_templates

PROMPT_NAMES = ("design_strategy", "pick_template", "explain_indicators")


def _rendered_text(result: Any) -> str:
    """Concatenate the text of every message in a FastMCP ``get_prompt`` result."""
    parts: list[str] = []
    for msg in result.messages:
        content = msg.content
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
async def test_prompts_registered() -> None:
    app = build_app()
    names = {p.name for p in await app.list_prompts()}
    assert set(PROMPT_NAMES) <= names


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
async def test_design_strategy_renders_and_references_real_templates() -> None:
    app = build_app()
    rendered = await app.get_prompt("design_strategy", {"goal": "trend following"})
    text = _rendered_text(rendered)
    assert text.strip()
    # Every live template id must appear in the guidance.
    for template in list_templates():
        assert template.id in text, f"template {template.id} missing from design_strategy prompt"
    # And it must steer toward validate-before-create + the whitelist.
    assert "validate_strategy" in text
    assert "create_strategy" in text
    assert "whitelist" in text.lower()


async def test_design_strategy_references_whitelisted_indicators() -> None:
    app = build_app()
    text = _rendered_text(await app.get_prompt("design_strategy", {}))
    kinds = {i.kind for i in list_indicators()}
    # A representative sample of the whitelist must be named.
    for kind in ("ema", "rsi", "macd"):
        assert kind in kinds  # guard: the kind really is whitelisted
        assert kind in text, f"indicator {kind} missing from design_strategy prompt"


async def test_pick_template_renders() -> None:
    app = build_app()
    text = _rendered_text(await app.get_prompt("pick_template", {}))
    for template in list_templates():
        assert template.id in text


async def test_explain_indicators_renders_whitelist() -> None:
    app = build_app()
    text = _rendered_text(await app.get_prompt("explain_indicators", {}))
    for kind in ("ema", "rsi", "macd"):
        assert kind in text
    assert "crossover" in text  # whitelisted helper documented
