"""File-based backtest-report persistence (one JSON per report).

Mirrors :class:`trader_mcp.strategy.store.StrategyStore`: cheap to construct,
defaults its root to ``{data_dir}/backtests`` (gitignored), creates the directory
lazily on first write, and stores one human-readable JSON document per report. A
report is addressed by its deterministic :attr:`BacktestReport.report_id`, so
re-running an identical backtest overwrites the same file (idempotent).

The MCP-server engineer wraps this for ``get_backtest_report`` /
``compare_backtests`` and exposes saved reports as ``backtest://`` resources.
"""

from __future__ import annotations

import json
from pathlib import Path

from trader_mcp.config import get_settings
from trader_mcp.engine.models import (
    BacktestComparison,
    BacktestComparisonRow,
    BacktestReport,
)
from trader_mcp.errors import ValidationError

#: Objectives a comparison can rank by; drawdown ranks ascending (lower is better).
_ASCENDING_OBJECTIVES: frozenset[str] = frozenset({"max_drawdown_pct", "volatility_pct"})


class BacktestStore:
    """File-based store for :class:`BacktestReport` documents (one JSON per id)."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        """Create a store rooted at ``{data_dir}/backtests`` (defaults from settings).

        Construction touches no filesystem; the directory is created on first save.
        """
        if data_dir is None:
            data_dir = get_settings().data_dir
        self.root: Path = Path(data_dir) / "backtests"

    def path_for(self, report_id: str) -> Path:
        """Return the JSON path for ``report_id`` (no I/O; may not exist)."""
        return self.root / f"{report_id}.json"

    def exists(self, report_id: str) -> bool:
        """Return whether a saved report exists for ``report_id``."""
        return self.path_for(report_id).is_file()

    def save(self, report: BacktestReport) -> Path:
        """Persist ``report`` as JSON (atomic via a temp file) and return its path.

        Serialized via the stdlib ``json`` module (not ``model_dump_json``) so a
        ``profit_factor`` of ``inf`` -- a documented, meaningful value (wins, no
        losses) -- round-trips as the ``Infinity`` literal. Pydantic's JSON writer
        would emit ``null`` for inf, which then fails to re-parse as a float; the
        stdlib path keeps the value loadable.
        """
        path = self.path_for(report.report_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        document = report.model_dump(mode="json")
        tmp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, report_id: str) -> BacktestReport:
        """Load and re-validate the report saved under ``report_id``.

        Reads via stdlib ``json`` (which parses the ``Infinity`` literal back to a
        float) then re-validates against the current schema.

        Raises:
            trader_mcp.errors.ValidationError: if no such report exists or the
                stored JSON no longer validates against the current schema.
        """
        path = self.path_for(report_id)
        if not path.is_file():
            raise ValidationError(
                f"No saved backtest report with id {report_id!r}.",
                details={"kind": "report_not_found", "report_id": report_id},
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        return BacktestReport.model_validate(document)

    def delete(self, report_id: str) -> bool:
        """Delete the report saved under ``report_id``; return whether it existed."""
        path = self.path_for(report_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_ids(self) -> list[str]:
        """Return the ids of every saved report, sorted."""
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))


def compare_reports(
    reports: list[BacktestReport],
    *,
    objective: str = "sharpe",
) -> BacktestComparison:
    """Build a side-by-side comparison of ``reports`` ranked by ``objective``.

    ``objective`` is a :class:`BacktestMetrics` field. Return/ratio metrics rank
    descending (higher is better); ``max_drawdown_pct``/``volatility_pct`` rank
    ascending. The ``best`` field is the winning ``report_id`` (``None`` for an
    empty list).

    Raises:
        trader_mcp.errors.ValidationError: for an unknown ``objective``.
    """
    if not reports:
        return BacktestComparison(objective=objective, best=None, reports=[])

    sample = reports[0].metrics
    if not hasattr(sample, objective):
        raise ValidationError(
            f"Unknown comparison objective {objective!r}.",
            details={"kind": "bad_objective", "objective": objective},
        )

    rows: list[BacktestComparisonRow] = []
    for r in reports:
        rows.append(
            BacktestComparisonRow(
                report_id=r.report_id,
                strategy_name=r.strategy_name,
                symbol=r.symbol,
                timeframe=r.timeframe,
                total_return_pct=r.metrics.total_return_pct,
                cagr_pct=r.metrics.cagr_pct,
                sharpe=r.metrics.sharpe,
                max_drawdown_pct=r.metrics.max_drawdown_pct,
                win_rate_pct=r.metrics.win_rate_pct,
                trade_count=r.metrics.trade_count,
                objective_value=float(getattr(r.metrics, objective)),
            )
        )

    ascending = objective in _ASCENDING_OBJECTIVES
    best_row = (min if ascending else max)(rows, key=lambda row: row.objective_value)
    return BacktestComparison(objective=objective, best=best_row.report_id, reports=rows)
