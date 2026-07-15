"""
===========================================================
 TECHNICAL INDICATORS
===========================================================
Sab indicators yahan pandas/numpy se khud implement kiye
gaye hain (koi external 'ta' library dependency nahi - isse
version conflicts / install issues avoid hote hain).

Formulas standard hain (Wilder's smoothing jahan applicable):
EMA, RSI, MACD, ATR, ADX, Supertrend, Volume Spike,
Breakout, Consolidation, Support/Resistance.
===========================================================
"""

import numpy as np
import pandas as pd

from config import (
    EMA_FAST, EMA_MID, EMA_SLOW,
    RSI_PERIOD,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    ADX_PERIOD,
    ATR_PERIOD,
    SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER,
    VOLUME_AVG_PERIOD, VOLUME_SPIKE_MULTIPLIER,
    BREAKOUT_LOOKBACK,
    CONSOLIDATION_LOOKBACK, CONSOLIDATION_RANGE_PCT,
    SUPPORT_RESISTANCE_LOOKBACK,
)


# -----------------------------------------------------------
# BASIC BUILDING BLOCKS
# -----------------------------------------------------------

def ema(series, window):
    return series.ewm(span=window, adjust=False).mean()


def rsi(close, window=RSI_PERIOD):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's smoothing (RMA)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))

    # V8.2.0 BUGFIX: Pehle avg_loss==0 case ko universal RSI=100 set kar
    # rahe the, lekin flat market (avg_gain==0 AND avg_loss==0) mein bhi
    # ye RSI=100 de raha tha - galat (flat market neutral hota hai).
    # Ab 3 cases handle hote hain:
    #   - Pure uptrend (avg_gain>0, avg_loss==0): RSI = 100
    #   - Pure downtrend (avg_gain==0, avg_loss>0): RSI = 0 (rs=0 -> naturally)
    #   - Flat market (avg_gain==0, avg_loss==0): RSI = 50 (neutral)
    #   - Early-window NaN (min_periods se): RSI = 50 (safe fallback)
    rsi_val = rsi_val.fillna(50)
    pure_uptrend = (avg_loss == 0) & (avg_gain > 0)
    flat_market = (avg_loss == 0) & (avg_gain == 0)
    rsi_val[pure_uptrend] = 100
    rsi_val[flat_market] = 50  # flat case override (pure_uptrend ke baad)
    return rsi_val


def macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def true_range(high, low, close):
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr


def atr(high, low, close, window=ATR_PERIOD):
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def adx(high, low, close, window=ADX_PERIOD):
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    atr_val = tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()

    plus_di = 100 * (plus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / atr_val.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()

    return adx_val.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def supertrend(high, low, close, period=SUPERTREND_PERIOD, multiplier=SUPERTREND_MULTIPLIER):
    atr_val = atr(high, low, close, period)
    hl2 = (high + low) / 2

    upper_band = hl2 + multiplier * atr_val
    lower_band = hl2 - multiplier * atr_val

    final_upper = upper_band.copy()
    final_lower = lower_band.copy()
    trend = pd.Series(index=close.index, dtype="float64")
    # V8.2.0 BUGFIX: direction ko default 1 (bullish) set karna galat tha
    # agar stock pehle valid bar se hi bearish hai. Pehle valid bar par
    # direction close vs midpoint (hl2) se decide hogi (neeche).
    direction = pd.Series(1, index=close.index, dtype="int64")  # 1 = uptrend, -1 = downtrend

    # ATR ke shuru ke rows NaN hote hain (min_periods) - unse pehle
    # supertrend calculate karna meaningless hai, isliye pehla valid
    # index dhoondh kar wahin se shuru karte hain.
    valid_idx = atr_val.first_valid_index()
    if valid_idx is None:
        return trend, direction

    start_pos = close.index.get_loc(valid_idx)

    # TODO: vectorize for perf - band carry-forward recurrence ko
    # numba/cython se speed up kiya ja sakta hai (500 stocks x 250 bars
    # = 125k iterations per scan). Correctness pehle verify karni padegi.
    for i in range(len(close)):
        if i < start_pos:
            trend.iloc[i] = np.nan
            direction.iloc[i] = 1  # placeholder; trend NaN hai to direction irrelevant
            continue

        if i == start_pos:
            final_upper.iloc[i] = upper_band.iloc[i]
            final_lower.iloc[i] = lower_band.iloc[i]
            # V8.2.0 BUGFIX: pehla valid bar ka direction hardcoded 1
            # (bullish) tha - galat. Ab close vs midpoint (hl2) se
            # decide hota hai. Agar close midpoint se upar hai to bullish,
            # neeche hai to bearish.
            midpoint = (final_upper.iloc[i] + final_lower.iloc[i]) / 2
            direction.iloc[i] = 1 if close.iloc[i] >= midpoint else -1
            trend.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]
            continue

        # band carry-forward logic (standard supertrend algorithm)
        if upper_band.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = upper_band.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if lower_band.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = lower_band.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        trend.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]

    return trend, direction


# -----------------------------------------------------------
# VOLUME / PRICE STRUCTURE
# -----------------------------------------------------------

def volume_spike(volume, window=VOLUME_AVG_PERIOD, multiplier=VOLUME_SPIKE_MULTIPLIER):
    avg_vol = volume.rolling(window).mean()
    spike = volume > (avg_vol * multiplier)
    return spike.fillna(False), avg_vol


def relative_volume(volume, window=VOLUME_AVG_PERIOD):
    """RVOL = aaj ka volume / N-din ka average volume. 1.0 = normal, 2.0 = double."""
    avg_vol = volume.rolling(window).mean()
    rvol = volume / avg_vol.replace(0, np.nan)
    return rvol.fillna(1.0)


def volume_dryup(volume, window=VOLUME_AVG_PERIOD, threshold=0.6):
    """
    Volume Dry-up = aaj ka volume average se kaafi kam (< threshold * avg).
    Ye aksar ek badi move se pehle "quiet before the storm" hota hai -
    especially agar price consolidation mein bhi ho.
    """
    avg_vol = volume.rolling(window).mean()
    dryup = volume < (avg_vol * threshold)
    return dryup.fillna(False)


def breakout_flag(close, high, lookback=BREAKOUT_LOOKBACK):
    # pichle N din ka high (aaj ko chhod kar) todna = breakout
    prior_high = high.shift(1).rolling(lookback).max()
    return (close > prior_high).fillna(False), prior_high


def consolidation_flag(high, low, lookback=CONSOLIDATION_LOOKBACK, range_pct=CONSOLIDATION_RANGE_PCT):
    recent_high = high.rolling(lookback).max()
    recent_low = low.rolling(lookback).min()
    range_percent = ((recent_high - recent_low) / recent_low.replace(0, np.nan)) * 100
    is_consolidating = range_percent <= range_pct
    return is_consolidating.fillna(False), range_percent


def support_resistance(high, low, lookback=SUPPORT_RESISTANCE_LOOKBACK):
    # shift(1) zaroori hai - warna breakout wale din resistance = aaj ka
    # high hi ban jaata hai, jisse Risk:Reward hamesha artificially kam
    # aata hai. Yahan hum "pichle N din" (aaj ko chhod kar) ka high/low
    # nikaal rahe hain, jaisa ki asli support/resistance level hota hai.
    resistance = high.shift(1).rolling(lookback).max()
    support = low.shift(1).rolling(lookback).min()
    return support, resistance


def pivot_points(high, low, close):
    """
    Classic (Floor Trader) Pivot Points - pichle din ke H/L/C se
    calculate hote hain, aaj ke intraday support/resistance ke liye
    use hote hain.
    Return: dict of Series {PP, R1, R2, R3, S1, S2, S3}
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    pp = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * pp) - prev_low
    s1 = (2 * pp) - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2 * (pp - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)

    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3}


def fibonacci_levels(swing_high, swing_low):
    """
    Ek swing high aur swing low (numbers, Series nahi) leke standard
    Fibonacci retracement levels return karta hai. Uptrend mein
    "retracement support" ke roop mein use hote hain.
    """
    diff = swing_high - swing_low
    return {
        "0.0": swing_high,
        "23.6": swing_high - 0.236 * diff,
        "38.2": swing_high - 0.382 * diff,
        "50.0": swing_high - 0.5 * diff,
        "61.8": swing_high - 0.618 * diff,
        "78.6": swing_high - 0.786 * diff,
        "100.0": swing_low,
    }


def rolling_vwap(high, low, close, volume, window=20):
    """
    Rolling N-day VWAP (Volume Weighted Average Price).
    NOTE: asli VWAP intraday hota hai (ek din ke andar); daily data
    ke saath ye ek "N-din ka volume-weighted trend anchor" hai -
    approximation samjho, exact intraday VWAP nahi.
    """
    typical_price = (high + low + close) / 3
    pv = typical_price * volume
    vwap = pv.rolling(window).sum() / volume.rolling(window).sum().replace(0, np.nan)
    return vwap


# -----------------------------------------------------------
# MASTER FUNCTION
# -----------------------------------------------------------

def add_indicators(df):
    """
    Input: raw OHLCV dataframe (columns: Open, High, Low, Close, Volume)
    Output: same df with saare indicator columns add kiye hue (in-place)
    """
    close = pd.to_numeric(df["Close"], errors="coerce")
    high = pd.to_numeric(df["High"], errors="coerce")
    low = pd.to_numeric(df["Low"], errors="coerce")
    volume = pd.to_numeric(df["Volume"], errors="coerce")

    df["Close"] = close
    df["High"] = high
    df["Low"] = low
    df["Volume"] = volume

    df["EMA20"] = ema(close, EMA_FAST)
    df["EMA50"] = ema(close, EMA_MID)
    df["EMA200"] = ema(close, EMA_SLOW)

    # V8.2.0 BUGFIX: ewm(adjust=False) pehle hi row 1 se EMA values deta
    # hai (recursive seed = first close). Isliye ek 30-day stock ka
    # "EMA200" kabhi NaN nahi hota - lekin wo actually sirf 30-day
    # recency-weighted average hai, 200-day EMA nahi. scanner.py ka
    # dropna(subset=["EMA200"]) check phir short-history stocks ko miss
    # kar deta tha. Ab pehle N rows ko NaN mark karte hain taaki
    # dropna() short stocks ko sahi se filter kare.
    n = len(df)
    if n > EMA_FAST:
        df.loc[df.index[:EMA_FAST], "EMA20"] = np.nan
    if n > EMA_MID:
        df.loc[df.index[:EMA_MID], "EMA50"] = np.nan
    if n > EMA_SLOW:
        df.loc[df.index[:EMA_SLOW], "EMA200"] = np.nan

    df["RSI"] = rsi(close, RSI_PERIOD)

    macd_line, signal_line, hist = macd(close)
    df["MACD"] = macd_line
    df["MACD_SIGNAL"] = signal_line
    df["MACD_HIST"] = hist

    df["ATR"] = atr(high, low, close, ATR_PERIOD)

    adx_val, plus_di, minus_di = adx(high, low, close, ADX_PERIOD)
    df["ADX"] = adx_val
    df["PLUS_DI"] = plus_di
    df["MINUS_DI"] = minus_di

    st_trend, st_dir = supertrend(high, low, close)
    df["SUPERTREND"] = st_trend
    df["SUPERTREND_DIR"] = st_dir  # 1 = bullish, -1 = bearish

    spike, avg_vol = volume_spike(volume)
    df["VOLUME_AVG20"] = avg_vol
    df["VOLUME_SPIKE"] = spike
    df["RVOL"] = relative_volume(volume)
    df["VOLUME_DRYUP"] = volume_dryup(volume)

    brk, prior_high = breakout_flag(close, high)
    df["PRIOR_HIGH_20D"] = prior_high
    df["BREAKOUT"] = brk

    consol, range_pct = consolidation_flag(high, low)
    df["RANGE_PCT_15D"] = range_pct
    df["CONSOLIDATING"] = consol

    support, resistance = support_resistance(high, low)
    df["SUPPORT"] = support
    df["RESISTANCE"] = resistance

    pivots = pivot_points(high, low, close)
    for name, series in pivots.items():
        df[f"PIVOT_{name}"] = series

    df["VWAP20"] = rolling_vwap(high, low, close, volume, window=20)

    return df
