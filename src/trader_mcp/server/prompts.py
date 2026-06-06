"""Phase 3 guided strategy-design MCP prompts (PRD §5.2 "guided design flows").

Three data-only prompts that steer an AI client through authoring a *declarative*
strategy spec safely:

    * ``design_strategy`` -- the end-to-end guided flow: pick a template, fill
      typed fields within the whitelist, validate, then create.
    * ``pick_template`` -- a focused helper summarizing the starter templates.
    * ``explain_indicators`` -- the closed indicator whitelist (the legal rule
      namespace) with each kind's outputs.

These prompts are **pure guidance text** -- they execute no code and place no
orders (Phase 3 has no execution path). They reference the *actual* template ids
from :func:`trader_mcp.strategy.list_templates` and the *actual* indicator kinds
from :func:`trader_mcp.indicators.list_indicators` so the guidance can never drift
from the implementation, and they explicitly instruct the model to use ONLY
whitelisted indicators/operators and to call ``validate_strategy`` before
``create_strategy``.

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from trader_mcp.indicators import list_indicators
from trader_mcp.server._sdk import FastMCP
from trader_mcp.strategy import list_templates


def _template_lines() -> str:
    """Render the live template registry as a bullet list for prompt text."""
    return "\n".join(
        f"  - {t.id} ({t.strategy_type}): {t.title} -- {t.summary}" for t in list_templates()
    )


def _indicator_lines() -> str:
    """Render the live indicator whitelist as a bullet list for prompt text."""
    lines: list[str] = []
    for ind in list_indicators():
        params = ", ".join(f"{name}={default}" for name, default in ind.params.items())
        suffixes = ", ".join(s or "(primary)" for s in ind.output_suffixes)
        lines.append(
            f"  - {ind.kind}: {ind.summary} | params: {params or 'none'} | outputs: {suffixes}"
        )
    return "\n".join(lines)


def register_strategy_prompts(app: FastMCP) -> None:
    """Register the Phase 3 guided strategy-design prompts on ``app``.

    This is the only place these prompts are registered; ``build_app`` invokes it.

    Args:
        app: The FastMCP application to register the prompts on.
    """

    @app.prompt(
        name="design_strategy",
        title="Design a declarative strategy",
        description=(
            "Guided flow to author a safe, declarative trader-mcp strategy spec: pick a "
            "template, fill typed fields within the indicator/operator whitelist, "
            "validate, then create. References the live templates and indicator whitelist."
        ),
    )
    def design_strategy(goal: str = "", symbol: str = "BTC/USD") -> str:
        """Return the guided strategy-design instructions for the client/model."""
        return (
            "You are authoring a declarative trader-mcp strategy spec. A strategy is "
            "DATA, never code -- you will produce a JSON spec, not a program.\n\n"
            f"Author goal: {goal or '(unspecified -- ask the user what edge they want)'}\n"
            f"Target symbol: {symbol} (US venues: Coinbase is the reference exchange; "
            "quote in USD/USDC).\n\n"
            "Follow these steps IN ORDER:\n"
            "1. Pick a starting template (call list_strategy_templates, or use one below). "
            "Templates are pre-validated, conservative starting points:\n"
            f"{_template_lines()}\n\n"
            "2. Decide the indicators. You may use ONLY these whitelisted indicator kinds "
            "(call list_indicators for the authoritative list). Rules may reference an "
            "indicator's outputs (the bare id for the primary line, id_<suffix> for "
            "extra lines) plus the OHLCV columns open/high/low/close/volume:\n"
            f"{_indicator_lines()}\n\n"
            "3. Write entry/exit rules as boolean expressions over those names. Allowed "
            "operators are comparisons (<, <=, >, >=, ==, !=), boolean and/or/not, and the "
            "whitelisted helpers crossover(a, b) / crossunder(a, b). Do NOT invent "
            "indicators, functions, or operators -- anything off the whitelist is rejected.\n\n"
            "4. Set position_sizing (percent_equity <= 100, or fixed_quote/fixed_base), "
            "risk (stop_loss_pct / take_profit_pct / max_leverage), and fees as needed. "
            "Keep risk conservative by default.\n\n"
            "5. ALWAYS call validate_strategy on your spec FIRST. It never raises -- it "
            "returns {ok, issues:[{location, problem, fix}]}. Apply each fix and re-validate "
            "until ok is true.\n\n"
            "6. ONLY THEN call create_strategy to persist it. The 'name' is the identity "
            "key (used by get_strategy / update_strategy / delete_strategy and the "
            "strategy://{name} resource).\n\n"
            "There is NO live execution in this phase -- authoring is pure local CRUD. "
            "Do not attempt to place orders, arm trading, or supply credentials."
        )

    @app.prompt(
        name="pick_template",
        title="Pick a strategy template",
        description=(
            "Helper that lists the live starter strategy templates so the client can "
            "choose one to build from with build_from_template / create_strategy."
        ),
    )
    def pick_template() -> str:
        """Return a concise summary of the available templates."""
        return (
            "Choose a starting template for your trader-mcp strategy. Each is a "
            "pre-validated, conservative spec you can override (name, symbol, timeframe, "
            "indicators, rules, sizing, risk, fees):\n\n"
            f"{_template_lines()}\n\n"
            "After picking one, fill in your overrides and call validate_strategy before "
            "create_strategy."
        )

    @app.prompt(
        name="explain_indicators",
        title="Explain the indicator whitelist",
        description=(
            "Helper that lists the closed indicator whitelist (the only names a strategy "
            "rule may reference) with each kind's params and output names."
        ),
    )
    def explain_indicators() -> str:
        """Return the whitelisted indicator catalog as guidance text."""
        return (
            "Strategy rules may reference ONLY these whitelisted indicator outputs (plus "
            "the OHLCV columns open/high/low/close/volume). For a single-output indicator, "
            "the rule name is the indicator's id; for multi-output indicators, use the "
            "bare id for the primary line and id_<suffix> for the extra lines:\n\n"
            f"{_indicator_lines()}\n\n"
            "Allowed operators: comparisons (<, <=, >, >=, ==, !=), boolean and/or/not, "
            "and the helpers crossover(a, b) / crossunder(a, b). Anything else is rejected "
            "by the safe evaluator."
        )
