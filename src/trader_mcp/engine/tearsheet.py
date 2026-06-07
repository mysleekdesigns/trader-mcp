"""quantstats tear-sheet generation (the ONE place quantstats is imported).

INVARIANT: quantstats is imported **lazily, only inside this code path**, so the
rest of the engine stays light and quantstats-free. ALL reported metrics are
computed natively (:mod:`trader_mcp.engine.metrics`); quantstats is used solely to
render the optional HTML tear sheet from the equity curve.

GRACEFUL DEGRADATION: if quantstats (or its plotting stack) cannot be imported or
fails to render, this returns the native metrics plus an explanatory note and does
NOT raise -- a backtest must never fail because the optional tear sheet could not
be drawn. The HTML is written under ``{data_dir}/reports/`` (gitignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trader_mcp.engine.models import TearsheetResult

if TYPE_CHECKING:
    from trader_mcp.engine.models import BacktestReport


def _reports_dir(data_dir: Path | str | None) -> Path:
    from trader_mcp.config import get_settings

    root = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return root / "reports"


def generate_tearsheet(
    report: BacktestReport,
    *,
    data_dir: Path | str | None = None,
    title: str | None = None,
) -> TearsheetResult:
    """Render an HTML tear sheet for ``report`` and return a structured result.

    Builds a per-bar returns series from the report's equity curve and hands it to
    quantstats' HTML reporter. On any import/render failure the function degrades:
    it returns the native :class:`BacktestMetrics` with a ``note`` and a ``None``
    ``html_path`` rather than raising.

    Args:
        report: A completed :class:`BacktestReport`.
        data_dir: Override the cache root (defaults to settings). The HTML lands in
            ``{data_dir}/reports/{report_id}.html``.
        title: Optional tear-sheet title (defaults to the strategy name + symbol).

    Returns:
        A :class:`TearsheetResult` (``html_path`` is ``None`` on graceful failure).
    """
    if len(report.equity_curve) < 2:
        return TearsheetResult(
            report_id=report.report_id,
            html_path=None,
            metrics=report.metrics,
            note="Equity curve too short to render a tear sheet (need >= 2 points).",
        )

    try:
        return _render(report, data_dir=data_dir, title=title)
    except Exception as exc:
        return TearsheetResult(
            report_id=report.report_id,
            html_path=None,
            metrics=report.metrics,
            note=f"Tear sheet unavailable (quantstats render failed: {type(exc).__name__}).",
        )


def _render(
    report: BacktestReport,
    *,
    data_dir: Path | str | None,
    title: str | None,
) -> TearsheetResult:
    """Do the actual quantstats render (separated so the caller can catch failures)."""
    import pandas as pd
    import quantstats as qs

    # quantstats prefers a tz-naive index; drop the tzinfo when building it.
    timestamps = [pt.timestamp.replace(tzinfo=None) for pt in report.equity_curve]
    equities = [pt.equity for pt in report.equity_curve]
    index = pd.DatetimeIndex(timestamps)
    equity = pd.Series(equities, index=index, dtype="float64")
    returns = equity.pct_change().fillna(0.0)

    out_dir = _reports_dir(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{report.report_id}.html"

    qs.reports.html(
        returns,
        title=title or f"{report.strategy_name} — {report.symbol} {report.timeframe}",
        output=str(html_path),
        download_filename=str(html_path),
    )
    return TearsheetResult(
        report_id=report.report_id,
        html_path=str(html_path),
        metrics=report.metrics,
        note=None,
    )
