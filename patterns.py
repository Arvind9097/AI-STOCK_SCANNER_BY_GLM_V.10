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


# ───────────────────────────────────────────────────────────────────
# V9.3: CUP AND HANDLE PATTERN + BREAKOUT RETEST + HIGH BREAKOUT
# ───────────────────────────────────────────────────────────────────

def detect_cup_and_handle(df, lookback=30, cup_depth_pct=12.0, handle_depth_pct=5.0):
    """
    V9.3: Cup and Handle pattern detect karta hai.

    CUP: U-shaped bottom (left rim → bottom → right rim)
    HANDLE: Small pullback after right rim (tight consolidation)
    BREAKOUT: Close above rim = pattern complete (BUY signal)
    """
    try:
        import numpy as np
        if len(df) < lookback:
            return None

        recent = df.tail(lookback).reset_index(drop=True)
        highs = recent["High"].values
        lows = recent["Low"].values
        closes = recent["Close"].values

        third = len(recent) // 3
        if third < 3:
            return None

        left_rim_idx = int(np.argmax(highs[:third]))
        left_rim = highs[left_rim_idx]

        cup_bottom_idx = left_rim_idx + int(np.argmin(lows[left_rim_idx:2*third]))
        cup_bottom = lows[cup_bottom_idx]

        if left_rim > 0:
            cup_depth = (left_rim - cup_bottom) / left_rim * 100
            if cup_depth < cup_depth_pct:
                return {"found": False, "rim_level": round(left_rim, 2),
                        "cup_bottom": round(cup_bottom, 2)}
        else:
            return None

        right_section = closes[cup_bottom_idx:]
        right_rim_idx = None
        for i, c in enumerate(right_section):
            if c >= left_rim * 0.97:
                right_rim_idx = cup_bottom_idx + i
                break

        if right_rim_idx is None:
            return {"found": False, "rim_level": round(left_rim, 2),
                    "cup_bottom": round(cup_bottom, 2)}

        right_rim = highs[right_rim_idx]
        rim_level = (left_rim + right_rim) / 2

        handle_section = recent.iloc[right_rim_idx:]
        if len(handle_section) < 2:
            return {"found": False, "rim_level": round(rim_level, 2),
                    "cup_bottom": round(cup_bottom, 2)}

        handle_low = float(handle_section["Low"].min())
        handle_high = float(handle_section["High"].max())

        if rim_level > 0:
            handle_depth = (rim_level - handle_low) / rim_level * 100
            if handle_depth > handle_depth_pct:
                return {"found": False, "rim_level": round(rim_level, 2),
                        "cup_bottom": round(cup_bottom, 2)}
        else:
            handle_depth = 0

        current_close = closes[-1]
        breakout = current_close > rim_level

        retest_count = 0
        for i in range(right_rim_idx, len(recent)):
            if abs(recent["High"].iloc[i] - rim_level) / rim_level * 100 < 1.5:
                retest_count += 1

        found = breakout or handle_depth < handle_depth_pct

        return {
            "found": found,
            "rim_level": round(rim_level, 2),
            "cup_bottom": round(cup_bottom, 2),
            "handle_low": round(handle_low, 2),
            "breakout": breakout,
            "retest_count": retest_count,
            "cup_depth_pct": round((left_rim - cup_bottom) / left_rim * 100, 1) if left_rim > 0 else 0,
            "handle_depth_pct": round(handle_depth, 1),
        }

    except Exception as e:
        logger.debug(f"Cup and Handle detection error: {e}")
        return None


def check_breakout_levels(df):
    """
    V9.3: Check if current price is breaking 6-month / 52-week / all-time high.
    Also checks retest count + tight range at resistance.
    """
    try:
        if len(df) < 20:
            return None

        current_close = float(df["Close"].iloc[-1])

        six_month_data = df.tail(125) if len(df) >= 125 else df
        six_month_high = float(six_month_data["High"].max())
        breaks_6mo = current_close >= six_month_high * 0.99

        if len(df) >= 250:
            week52_high = float(df.tail(250)["High"].max())
        else:
            week52_high = float(df["High"].max())
        breaks_52wk = current_close >= week52_high * 0.99

        alltime_high = float(df["High"].max())
        breaks_alltime = current_close >= alltime_high * 0.99

        retest_count = 0
        six_month_section = six_month_data.tail(30)
        for _, row in six_month_section.iterrows():
            if abs(row["High"] - six_month_high) / six_month_high * 100 < 1.5:
                retest_count += 1

        last_5 = df.tail(5)
        range_pct = (last_5["High"].max() - last_5["Low"].min()) / current_close * 100
        tight_range = range_pct < 3.0

        return {
            "breaks_6month_high": breaks_6mo,
            "breaks_52week_high": breaks_52wk,
            "breaks_alltime_high": breaks_alltime,
            "6month_high": round(six_month_high, 2),
            "52week_high": round(week52_high, 2),
            "alltime_high": round(alltime_high, 2),
            "retest_count": retest_count,
            "tight_range_at_resistance": tight_range,
            "range_pct": round(range_pct, 2),
        }

    except Exception as e:
        logger.debug(f"Breakout level check error: {e}")
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

    bullish_patterns = {"Double Bottom", "Bull Flag", "Cup and Handle"}
    bearish_patterns = {"Double Top", "Head & Shoulders", "Bear Flag"}

    # V9.3: Add Cup and Handle detection
    try:
        ch = detect_cup_and_handle(df)
        if ch and ch.get("found"):
            detected.append("Cup and Handle")
    except Exception as e:
        logger.debug(f"Cup&Handle detection error: {e}")

    # V9.3: Add breakout level info (6-month / 52-week / all-time high)
    breakout_info = None
    try:
        breakout_info = check_breakout_levels(df)
    except Exception as e:
        logger.debug(f"Breakout check error: {e}")

    return {
        "patterns": detected,
        "bullish": any(p in bullish_patterns for p in detected),
        "bearish": any(p in bearish_patterns for p in detected),
        "cup_and_handle": ch if ch else None,
        "breakout_levels": breakout_info,
    }
