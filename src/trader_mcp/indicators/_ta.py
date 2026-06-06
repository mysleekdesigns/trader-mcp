"""Typed shim over ``pandas-ta`` (the single import seam for the indicator engine).

``pandas-ta`` 0.4.x ships a ``py.typed`` marker but its public functions are
re-exported in a way pyright flags (``reportPrivateImportUsage``) and its
parameter types are too narrow (e.g. ``std: DictLike``). Rather than scatter
``# type: ignore`` across every adapter, every call goes through this module, which
exposes the functions as plain ``Callable[..., Any]``. This keeps the registry and
tests clean while still importing the real implementation at runtime.

This is the *only* place ``pandas_ta`` is imported. Import is lazy at module load,
which is fine: the registry already only computes when asked.
"""

from __future__ import annotations

from typing import Any, cast

import pandas_ta as _ta

sma: Any = cast("Any", _ta).sma
ema: Any = cast("Any", _ta).ema
rsi: Any = cast("Any", _ta).rsi
macd: Any = cast("Any", _ta).macd
bbands: Any = cast("Any", _ta).bbands
donchian: Any = cast("Any", _ta).donchian
atr: Any = cast("Any", _ta).atr
stoch: Any = cast("Any", _ta).stoch
adx: Any = cast("Any", _ta).adx

__all__ = [
    "adx",
    "atr",
    "bbands",
    "donchian",
    "ema",
    "macd",
    "rsi",
    "sma",
    "stoch",
]
