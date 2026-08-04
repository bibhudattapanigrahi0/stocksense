"""
Shared utilities — NaN/inf/numpy sanitization.

yfinance and pandas routinely produce NaN cells and numpy scalar types.
Both break JSON serialization (json.dumps rejects NaN and numpy scalars),
so data crossing into API/MCP responses passes through deep_sanitize.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def is_bad_float(value: Any) -> bool:
    """True for NaN or ±inf floats (native or numpy)."""
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return math.isnan(f) or math.isinf(f)
    return False


def round_price(x: Any) -> Any:
    """Round a price keeping enough significant digits for its magnitude.

    Plain round(x, 2) collapses sub-dollar assets (a $0.0123 coin → 0.01), which
    destroys stop/target/P&L math for cheap crypto. Scale the precision instead.
    """
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    a = abs(x)
    if a >= 100:
        return round(x, 2)
    if a >= 1:
        return round(x, 4)
    if a >= 0.01:
        return round(x, 6)
    return round(x, 8)


def deep_sanitize(obj: Any) -> Any:
    """
    Recursively convert a structure to strict-JSON-safe values:
    NaN/inf → None, numpy scalars → native Python types.
    """
    if isinstance(obj, dict):
        return {k: deep_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [deep_sanitize(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj
