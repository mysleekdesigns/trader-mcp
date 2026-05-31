"""Event-driven interpreter (owned by ``backtest-engine-engineer``).

INVARIANT: one interpreter is used for BOTH backtest and live. The same
event-driven spec interpreter consumes historical bars (backtest) and streaming
bars (paper/live) to guarantee backtest<->live parity. Never fork strategy-
execution logic between the two paths. Placeholder for Phase 4.
"""

from __future__ import annotations
