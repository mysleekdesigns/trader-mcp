"""Engine self-tests (owned by ``backtest-engine-engineer``).

Focused unit tests for the parts only the engine author fully understands: broker
fill math, native metric formulas vs hand-computed fixtures, determinism, the
no-look-ahead fill convention, and grid/DCA mechanics. The cross-cutting
backtest<->live parity suite and the backtesting.py cross-validation live
alongside these and are owned by ``qa-parity-engineer``.
"""
