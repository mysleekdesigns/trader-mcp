"""Phase 3 strategy-authoring MCP tools (PRD §5.2 "strategy authoring", §6 Phase 3).

Wraps the declarative-strategy public API (:mod:`trader_mcp.strategy`) as a typed,
structured-output MCP tool surface for authoring, validating, persisting, and
managing strategy specs in the local file store.

These tools are SAFE by construction: Phase 3 has **no execution path**. A strategy
spec is *data, never code* -- authoring is pure CRUD over the local
``{data_dir}/strategies`` directory plus the spec's own static validation (which runs
every rule expression through the safe-evaluator whitelist at construction time).
There is no order, arming, or credential surface here, so the safe-by-default
invariant holds trivially -- there is nothing to gate.

Every tool takes typed Pydantic-validated arguments and returns a Pydantic v2 model
with ``structured_output=True`` so FastMCP emits an ``outputSchema``. The write tools
(``create_strategy`` / ``update_strategy``) accept a typed :class:`StrategySpec`
input, so FastMCP also emits a real ``inputSchema`` documenting every spec field
(including the nested optional grid/dca blocks) -- upholding the typed-Pydantic-I/O
invariant. The strategy layer already returns typed models (``StrategySpec`` /
``StrategyInfo`` / ``ValidationReport`` / ``TemplateInfo``); the only wrappers added
here are the list/result objects in :mod:`trader_mcp.server.schemas`.

Error model: a write tool's ``spec`` is validated into a :class:`StrategySpec` at the
MCP boundary (FastMCP runs the spec's full cross-field/whitelist validators), so an
invalid payload is rejected there before the tool body runs. For an AI-actionable
"what's wrong + how to fix" report on a draft, clients call ``validate_strategy``
first: it takes a loose dict, never raises, and returns a structured
``ValidationReport`` of ``{location, problem, fix}`` issues so the client can iterate
to a valid spec before committing. Other caller mistakes raise redacted
``TraderMCPError`` subclasses -- ``get_strategy`` / ``update_strategy`` raise a
redacted not-found ``ValidationError`` -- which propagate to the MCP boundary where
FastMCP surfaces them (the strategy layer never leaks raw input or secrets).

The MCP SDK stays isolated: registration goes through the ``FastMCP`` instance
re-exported from :mod:`trader_mcp.server._sdk`; this module never ``import mcp``.
"""

from __future__ import annotations

from typing import Any

from trader_mcp.errors import ValidationError
from trader_mcp.indicators import list_indicators
from trader_mcp.server._sdk import FastMCP
from trader_mcp.server.schemas import (
    CreateStrategyResult,
    DeleteResult,
    IndicatorsResult,
    StrategiesResult,
    TemplatesResult,
)
from trader_mcp.strategy import (
    StrategySpec,
    StrategyStore,
    ValidationReport,
    list_templates,
)
from trader_mcp.strategy import (
    validate_strategy as _validate_strategy,
)


def register_strategy_tools(app: FastMCP, store: StrategyStore) -> None:
    """Register the Phase 3 strategy-authoring tools on ``app``.

    The tool callables close over the single process-wide ``store`` (the local
    strategy file cache). This is the only place these tools are registered;
    ``build_app`` is the single registration point that invokes it.

    Args:
        app: The FastMCP application to register the tools on.
        store: The shared local strategy store the tools read from / write to.
    """

    @app.tool(
        name="list_strategy_templates",
        title="List strategy templates",
        description=(
            "List the starter strategy templates an author can build from (id, title, "
            "summary, strategy_type, and the top-level fields each template lets you "
            "override). No network call -- the templates are built-in and validated."
        ),
        structured_output=True,
    )
    def list_strategy_templates() -> TemplatesResult:
        """Return the built-in strategy template library."""
        templates = list_templates()
        return TemplatesResult(templates=templates, count=len(templates))

    @app.tool(
        name="list_indicators",
        title="List whitelisted indicators",
        description=(
            "List the closed whitelist of indicator kinds a strategy rule may use: "
            "each kind's tunable params (with defaults) and the output-name suffixes it "
            "contributes (e.g. macd -> id, id_signal, id_hist). Rules may reference ONLY "
            "these outputs (plus OHLCV columns). No network call."
        ),
        structured_output=True,
    )
    def list_indicators_tool() -> IndicatorsResult:
        """Return the whitelisted indicator library (the legal rule namespace)."""
        indicators = list_indicators()
        return IndicatorsResult(indicators=indicators, count=len(indicators))

    @app.tool(
        name="validate_strategy",
        title="Validate a strategy spec",
        description=(
            "Validate a strategy spec payload and return a structured report (ok + a "
            "list of issues, each with location/problem/fix). NEVER raises -- the "
            "AI-friendly surface to iterate on a spec before calling create_strategy. "
            "Rules are checked against the indicator/operator whitelist. No network call."
        ),
        structured_output=True,
    )
    def validate_strategy(spec: dict[str, Any]) -> ValidationReport:
        """Return a structured validation report for ``spec`` (never raises)."""
        return _validate_strategy(spec)

    @app.tool(
        name="create_strategy",
        title="Create & save a strategy",
        description=(
            "Persist a typed StrategySpec to the local store (one JSON file per strategy, "
            "keyed by name) and return the saved spec plus its listing info. The spec is "
            "validated at the MCP boundary against the full StrategySpec schema (rules are "
            "checked against the indicator/operator whitelist). For an AI-actionable "
            "'what's wrong + how to fix' report on a draft, call validate_strategy first "
            "(it returns structured issues instead of rejecting). No order/arming path -- "
            "pure local CRUD."
        ),
        structured_output=True,
    )
    def create_strategy(spec: StrategySpec) -> CreateStrategyResult:
        """Persist an already-validated ``spec`` and return it with its listing info.

        FastMCP validates the inbound payload into a :class:`StrategySpec` (running the
        spec's full cross-field/whitelist validators) before this runs, so ``spec`` is
        guaranteed valid here -- the typed input gives clients a real ``inputSchema``.
        Use ``validate_strategy`` (loose dict) for iterative, non-raising feedback.
        """
        info = store.save(spec)
        return CreateStrategyResult(spec=spec, info=info)

    @app.tool(
        name="get_strategy",
        title="Get a saved strategy",
        description=(
            "Load and return the full saved StrategySpec for ``name`` (re-validated on "
            "load). Raises a redacted not-found error if no such strategy exists. No "
            "network call -- reads the local store only."
        ),
        structured_output=True,
    )
    def get_strategy(name: str) -> StrategySpec:
        """Return the saved :class:`StrategySpec` for ``name`` (raises if absent)."""
        return store.load(name)

    @app.tool(
        name="list_strategies",
        title="List saved strategies",
        description=(
            "List every saved strategy with its listing summary (name, exchange, symbol, "
            "timeframe, strategy_type, schema_version, created/updated). No network call."
        ),
        structured_output=True,
    )
    def list_strategies() -> StrategiesResult:
        """Return the saved-strategy catalog."""
        strategies = store.list()
        return StrategiesResult(strategies=strategies, count=len(strategies))

    @app.tool(
        name="update_strategy",
        title="Update a saved strategy",
        description=(
            "Overwrite an existing strategy with a new typed StrategySpec. The ``name`` is "
            "the identity key: the spec's ``name`` must match an existing saved strategy "
            "(the store preserves its original 'created' timestamp and refreshes "
            "'updated'). To rename, create a new strategy and delete the old one. The spec "
            "is validated at the MCP boundary; a not-found error is raised if no strategy "
            "with that name exists. Returns the updated spec + info. No order/arming path "
            "-- pure local CRUD."
        ),
        structured_output=True,
    )
    def update_strategy(name: str, spec: StrategySpec) -> CreateStrategyResult:
        """Overwrite the strategy saved under ``name`` with an already-validated ``spec``.

        FastMCP validates ``spec`` into a :class:`StrategySpec` at the boundary, so it is
        guaranteed valid here. ``name`` is the identity key (no rename) -- the spec's
        ``name`` must match the target.
        """
        if not store.exists(name):
            raise ValidationError(
                f"No saved strategy named {name!r} to update.",
                details={"kind": "strategy_not_found", "name": name},
            )
        if spec.name != name:
            raise ValidationError(
                "update_strategy does not rename: the spec's 'name' "
                f"({spec.name!r}) must match the target ({name!r}). "
                "To rename, create_strategy the new name then delete_strategy the old.",
                details={
                    "kind": "strategy_rename_not_allowed",
                    "target": name,
                    "spec_name": spec.name,
                },
            )
        info = store.save(spec)
        return CreateStrategyResult(spec=spec, info=info)

    @app.tool(
        name="delete_strategy",
        title="Delete a saved strategy",
        description=(
            "Delete the strategy saved under ``name``. Returns whether a strategy "
            "existed and was removed (idempotent: deleting a missing strategy returns "
            "deleted=false, not an error). No network call -- local store only."
        ),
        structured_output=True,
    )
    def delete_strategy(name: str) -> DeleteResult:
        """Delete the strategy saved under ``name``; report whether it existed."""
        return DeleteResult(name=name, deleted=store.delete(name))
