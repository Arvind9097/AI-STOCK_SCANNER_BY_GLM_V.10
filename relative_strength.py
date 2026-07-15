"""
===========================================================
 RELATIVE STRENGTH (vs Benchmark)
===========================================================
Stock ka return, benchmark index (NIFTY 50) ke return se compare
karta hai. Agar stock ne index se zyada return diya hai (same
period mein), to wo "market ko outperform" kar raha hai - ye
institutional traders ka ek popular filter hai (Relative Strength
/ RS Rating jaisa, simplified version).
===========================================================
"""

import math

from config import RELATIVE_STRENGTH_LOOKBACK
from logger import logger


def _is_invalid(val):
    """V8.2.0 FIX (bug #26): None, NaN, ya infinity check karta hai.
    Pehle NaN close silently propagate ho raha tha (NaN > 0 is False,
    NaN / NaN = NaN, waghera) - scanner.py ke if-checks manipulate
    hote the without clear error. Ab NaN/None explicitly detect."""
    if val is None:
        return True
    try:
        f = float(val)
    except (TypeError, ValueError):
        return True
    if math.isnan(f) or math.isinf(f):
        return True
    return False


def calc_benchmark_return(benchmark_df, lookback=RELATIVE_STRENGTH_LOOKBACK):
    if benchmark_df is None or len(benchmark_df) < lookback + 1:
        return None

    close = benchmark_df["Close"]
    if len(close) <= lookback:
        return None

    start = close.iloc[-lookback - 1]
    end = close.iloc[-1]
    # V8.2.0 FIX (bug #26): NaN/None close handle karo.
    if _is_invalid(start) or _is_invalid(end) or start == 0:
        return None

    return (end - start) / start * 100


def calc_stock_return(df, lookback=RELATIVE_STRENGTH_LOOKBACK):
    if df is None:
        return None
    close = df["Close"]
    if len(close) <= lookback:
        return None

    start = close.iloc[-lookback - 1]
    end = close.iloc[-1]
    # V8.2.0 FIX (bug #26): NaN/None close handle karo.
    if _is_invalid(start) or _is_invalid(end) or start == 0:
        return None

    return (end - start) / start * 100


def relative_strength(stock_return, benchmark_return):
    """
    Return: (rs_diff, outperforming)
    rs_diff = stock ka return - benchmark ka return (percentage points)
    outperforming = True agar stock ne index se behtar kiya
    """
    # V8.2.0 FIX (bug #26): NaN/None explicit check.
    if _is_invalid(stock_return) or _is_invalid(benchmark_return):
        return None, False

    rs_diff = round(stock_return - benchmark_return, 2)
    return rs_diff, rs_diff > 0
