"""
===========================================================
 CHART PATTERN DETECTION
===========================================================
Swing high/low points nikaal kar unpar classic chart patterns
detect karta hai: Double Top, Double Bottom, Head & Shoulders,
Bull Flag, Bear Flag.

Ye rule-based / heuristic detection hai (koi ML nahi) - production
trading systems bhi isी tarah ke swing-point algorithms use karte
hain, lekin false positives/negatives dono ho sakte hain. Isko
ek "extra confirmation signal" ki tarah treat karo, akela decision
maker nahi.
===========================================================
"""

import numpy as np
import pandas as pd

from config import (
    SWING_WINDOW, PATTERN_TOLERANCE_PCT,
    FLAG_LOOKBACK, FLAG_POLE_MIN_MOVE_PCT,
)
from logger import logger


def find_swing_points(high, low, window=SWING_WINDOW):
    """
    Return: (swing_high_idx, swing_low_idx) - dono lists of integer
    positions (df ke index positions, not dates) jahan local
    swing high / low bane hain.

    Ek point "swing high" hai agar wo apne left aur right ke
    `window` bars se zyada high ho (aur vice versa for swing low).
    """
    n = len(high)
    swing_high_idx = []
    swing_low_idx = []

    # TODO: vectorize for perf - rolling max/min comparisons se O(n*window)
    # Python loop ko O(n) pandas/numpy ops mein convert kiya ja sakta hai.
    # Current implementation 500 stocks x 250 bars ke liye acceptable hai
    # but NIFTY500 universe mein bottleneck ban sakta hai.
    for i in range(window, n - window):
        left_h = high.iloc[i - window:i]
        right_h = high.iloc[i + 1:i + window + 1]
        if high.iloc[i] > left_h.max() and high.iloc[i] > right_h.max():
            swing_high_idx.append(i)

        left_l = low.iloc[i - window:i]
        right_l = low.iloc[i + 1:i + window + 1]
        if low.iloc[i] < left_l.min() and low.iloc[i] < right_l.min():
            swing_low_idx.append(i)

    return swing_high_idx, swing_low_idx


def _pct_diff(a, b):
    if b == 0:
        return 999
    return abs(a - b) / abs(b) * 100


def detect_double_top(df, swing_high_idx):
    """Do roughly-equal swing highs, beech mein ek dip - bearish reversal pattern."""
    if len(swing_high_idx) < 2:
        return False, None

    i1, i2 = swing_high_idx[-2], swing_high_idx[-1]
    h1, h2 = df["High"].iloc[i1], df["High"].iloc[i2]

    if _pct_diff(h1, h2) > PATTERN_TOLERANCE_PCT:
        return False, None

    # dono peaks ke beech ek meaningful dip hona chahiye (warna ye ek hi peak hai)
    between_low = df["Low"].iloc[i1:i2 + 1].min()
    if _pct_diff(between_low, min(h1, h2)) < PATTERN_TOLERANCE_PCT:
        return False, None

    # confirm hone ke liye price abhi neckline (between_low) ke aas paas/neeche ho
    last_close = df["Close"].iloc[-1]
    confirmed = last_close <= between_low * 1.02

    return confirmed, round(float(max(h1, h2)), 2)


def detect_double_bottom(df, swing_low_idx):
    """Do roughly-equal swing lows, beech mein ek bounce - bullish reversal pattern."""
    if len(swing_low_idx) < 2:
        return False, None

    i1, i2 = swing_low_idx[-2], swing_low_idx[-1]
    l1, l2 = df["Low"].iloc[i1], df["Low"].iloc[i2]

    if _pct_diff(l1, l2) > PATTERN_TOLERANCE_PCT:
        return False, None

    between_high = df["High"].iloc[i1:i2 + 1].max()
    if _pct_diff(between_high, min(l1, l2)) < PATTERN_TOLERANCE_PCT:
        return False, None

    last_close = df["Close"].iloc[-1]
    confirmed = last_close >= between_high * 0.98

    return confirmed, round(float(min(l1, l2)), 2)


def detect_head_shoulders(df, swing_high_idx):
    """
    3 swing highs: left shoulder < head > right shoulder (roughly equal
    shoulders). Bearish reversal pattern.
    """
    if len(swing_high_idx) < 3:
        return False

    i1, i2, i3 = swing_high_idx[-3], swing_high_idx[-2], swing_high_idx[-1]
    left, head, right = df["High"].iloc[i1], df["High"].iloc[i2], df["High"].iloc[i3]

    is_head_higher = head > left and head > right
    shoulders_similar = _pct_diff(left, right) <= PATTERN_TOLERANCE_PCT * 1.5

    return bool(is_head_higher and shoulders_similar)


def detect_flag(df, lookback=FLAG_LOOKBACK, pole_min_move=FLAG_POLE_MIN_MOVE_PCT):
    """
    Bull Flag: ek strong upward "pole" move, uske baad tight sideways/
    slight-down consolidation ("flag"). Continuation pattern.
    Bear Flag: iska ulta (strong down move + tight upward consolidation).
    Return: "BULL_FLAG" | "BEAR_FLAG" | None
    """
    if len(df) < lookback * 3:
        return None

    flag_zone = df.iloc[-lookback:]
    pole_zone = df.iloc[-lookback * 3:-lookback]

    # V8.2.0 BUGFIX: division-by-zero / NaN guards. Pehle bina check ke
    # divide kar rahe the - agar pole_zone ka first close 0 ya NaN ho
    # (corrupt/missing data), ya flag_zone ka Low.min() 0 ho, to ZeroDivisionError
    # ya NaN propagate ho jaata tha.
    pole_close_start = pole_zone["Close"].iloc[0]
    pole_close_end = pole_zone["Close"].iloc[-1]
    flag_high_max = flag_zone["High"].max()
    flag_low_min = flag_zone["Low"].min()

    if (pole_close_start is None or pd.isna(pole_close_start) or pole_close_start == 0
            or pd.isna(pole_close_end)
            or pd.isna(flag_high_max) or pd.isna(flag_low_min)
            or flag_low_min == 0):
        return None

    pole_move_pct = (pole_close_end - pole_close_start) / pole_close_start * 100
    flag_range_pct = (flag_high_max - flag_low_min) / flag_low_min * 100

    # flag ki range pole move ke mukable bahut tight honi chahiye
    tight_consolidation = flag_range_pct < abs(pole_move_pct) * 0.5

    if pole_move_pct >= pole_min_move and tight_consolidation:
        return "BULL_FLAG"
    if pole_move_pct <= -pole_min_move and tight_consolidation:
        return "BEAR_FLAG"
    return None


def detect_all_patterns(df):
    """
    Master function - df (indicators already added) leke saare
    patterns check karta hai.
    Return: dict {
        "patterns": [list of detected pattern names],
        "bullish": bool,  # koi bhi bullish pattern mila
        "bearish": bool,  # koi bhi bearish pattern mila
    }
    """
    detected = []

    try:
        swing_high_idx, swing_low_idx = find_swing_points(df["High"], df["Low"])

        dt_found, dt_level = detect_double_top(df, swing_high_idx)
        if dt_found:
            detected.append("Double Top")

        db_found, db_level = detect_double_bottom(df, swing_low_idx)
        if db_found:
            detected.append("Double Bottom")

        if detect_head_shoulders(df, swing_high_idx):
            detected.append("Head & Shoulders")

        flag = detect_flag(df)
        if flag == "BULL_FLAG":
            detected.append("Bull Flag")
        elif flag == "BEAR_FLAG":
            detected.append("Bear Flag")

    except Exception as e:
        # V8.2.0 BUGFIX: pehle bare `except Exception: pass` tha - ye real
        # programming bugs (KeyError/TypeError/IndexError) bhi silently
        # swallow kar deta tha. Ab graceful rehta hai (scan fail nahi
        # hota) lekin debug log jaata hai, taaki pattern-detection ke
        # actual bugs trace kiye ja sakein.
        logger.debug(f"Pattern detection error (graceful skip): {e}")

    bullish_patterns = {"Double Bottom", "Bull Flag"}
    bearish_patterns = {"Double Top", "Head & Shoulders", "Bear Flag"}

    return {
        "patterns": detected,
        "bullish": any(p in bullish_patterns for p in detected),
        "bearish": any(p in bearish_patterns for p in detected),
    }
