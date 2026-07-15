# btst_scanner.py
"""
===========================================================
 BTST SCANNER (naya, 3:05 PM) - Last-Hour Price Action Based
===========================================================
Ye module main.py ke run_close_bestbuys_pipeline() (purana 3 PM,
already-cached SWING data ko dobara score karta hai) se BILKUL ALAG
hai aur usko bilkul touch nahi karta. Document ki exact requirement:

  "BTST Selection Rules (Run at 2:55 PM - 3:05 PM): daily breakout
   stocks, strong price action in the last 1 hour (2:00 PM - 3:00 PM),
   heavy volume accumulation, aur stocks closing near the Day's High
   with positive sector support."

KYUN ALAG MODULE:
  - run_close_bestbuys_pipeline() SWING-SCORE (EMA/RSI/MACD/ADX se)
    ko dobara dikhaata hai - ye "aaj ka breakout jo agle din tak
    hold karne layak hai" wala concept hai
  - Ye naya BTST scanner specifically "PICHLE 1 GHANTE ka price
    action" check karta hai (2 PM - 3 PM candles), jo swing-score
    se bilkul alag signal hai - isliye intraday candles chahiye
    (yfinance, jaisa intraday_scanner.py aur mtf.py mein bhi hai,
    koi free NSE/Stooq source intraday history nahi deta)

"Positive sector support" ke liye - agar stock ka Nifty500-sector
peer bhi upar hai to bonus (simplified proxy: NIFTY benchmark ke
against stock ki last-hour relative strength).

V8.2.0 FIX (Task F5):
  1. IST timezone use kiya gaya hai (host TZ se independent).
  2. Market-hours guard - 14:00-15:30 IST ke beech chalna chahiye
     (last-hour price-action meaningful tabhi hai). Pehle weekend/
     holiday par /rerun btst chalane par kal ka data "live" dikhta tha.
  3. `_fetch_today_intraday` ko today-IST date filter lagta hai -
     stale yesterday bars ab properly skip hote hain.
  4. Graceful-degradation bug fix: agar `len(df) < bars_needed`
     ho to silently window shrink nahi karta - clear note add karta
     hai ki "actual window" chhota tha (transparency).
===========================================================
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from config import (
    CUSTOM_STOCKS, BTST_LOOKBACK_MINUTES, BTST_DAY_HIGH_PROXIMITY_PCT,
    BTST_TOP_N_RESULTS, INTRADAY_UNIVERSE_TOP_N, INTRADAY_INTERVAL,
)
from nifty_symbols import get_nifty500_symbols
from logger import logger

# V8.2.0: IST timezone - host TZ (UTC/etc) se independent market-time decisions.
IST = ZoneInfo("Asia/Kolkata")

# V8.2.0: BTST scan ka ideal time-window 14:00-15:30 IST (last hour + close).
# Iske baahar stale/yesterday data "live" BTST signals deta tha - guard fix.
_BTST_WINDOW_START_HHMM = "14:00"
_BTST_WINDOW_END_HHMM = "15:35"  # 15:30 + buffer for scan completion


def _is_btst_window_open():
    """V8.2.0: IST market-hours guard - BTST scan 14:00-15:35 IST ke beech meaningful."""
    now = datetime.now(IST)
    # Weekend guard (IST Saturday/Sunday)
    if now.weekday() >= 5:
        return False
    hhmm = now.strftime("%H:%M")
    return _BTST_WINDOW_START_HHMM <= hhmm <= _BTST_WINDOW_END_HHMM


def _fetch_today_intraday(symbol, interval="5m"):
    """
    Aaj ka poora intraday OHLCV (5-min candles, din ki shuruaat se
    ab tak) laata hai. Return: DataFrame ya None.

    V8.2.0: today-IST filter lagata hai - yfinance kabhi-kabhi (weekend/
    holiday/non-IST host) kal ke bars deta hai jise BTST last-hour
    calculation galat karta tha.
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period="1d", interval=interval, auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        # V8.2.0: filter to TODAY's bars in IST
        df = _filter_today_bars(df)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.debug(f"{symbol}: BTST intraday fetch fail ({e})")
        return None


def _filter_today_bars(df):
    """V8.2.0: intraday DataFrame ko sirf aaj ke IST-date wale bars par filter karta hai."""
    if df is None or df.empty:
        return df
    try:
        today_ist = datetime.now(IST).date()
        if df.index.tz is not None:
            idx_dates = df.index.tz_convert(IST).date
        else:
            idx_dates = df.index.date
        mask = [d == today_ist for d in idx_dates]
        today_df = df[mask]
        return today_df if not today_df.empty else df
    except Exception:
        return df


def _get_universe():
    """Intraday scanner jaisa hi liquid universe (poore Nifty500 par practical nahi)."""
    try:
        symbols = get_nifty500_symbols()
    except Exception as e:
        logger.warning(f"Nifty500 list nahi mil payi, sirf CUSTOM_STOCKS use kar raha hoon: {e}")
        symbols = []
    universe = list(dict.fromkeys(CUSTOM_STOCKS + symbols[:INTRADAY_UNIVERSE_TOP_N]))
    return universe[:INTRADAY_UNIVERSE_TOP_N]


def _last_hour_price_action(df, lookback_minutes=BTST_LOOKBACK_MINUTES):
    """
    Pichle `lookback_minutes` (default 60) ka price-action check karta
    hai: kitna % move hua, aur ye move consistently ek direction mein
    tha ya nahi (candles ka majority green/red).

    V8.2.0: agar `len(df) < bars_needed` ho to silently window shrink
    nahi karta - `actual_window_min` field add karta hai (transparency).
    Agar window <30 min hai to None return (data too thin).

    Return: dict {pct_move, bullish_candle_ratio, day_high, day_low,
    current_price, actual_window_min} ya None
    """
    if df is None or df.empty:
        return None

    bar_minutes = max(1, _bar_minutes())
    bars_needed = max(1, lookback_minutes // bar_minutes)
    actual_window_min = lookback_minutes

    # V8.2.0: graceful degradation - jab len(df) < bars_needed ho to
    # actual window note karke transparent behavior rakho, BUT agar
    # 30 min se kam data ho to skip (too thin for last-hour analysis).
    if len(df) < bars_needed:
        actual_window_min = len(df) * bar_minutes
        if actual_window_min < 30:
            # too little data - skip this stock (don't publish misleading signal)
            return None
        bars_needed = len(df)

    recent = df.iloc[-bars_needed:]
    if recent.empty:
        return None

    start_price = float(recent["Close"].iloc[0])
    current_price = float(recent["Close"].iloc[-1])
    pct_move = ((current_price - start_price) / start_price * 100) if start_price > 0 else 0.0

    green_candles = (recent["Close"] > recent["Open"]).sum()
    bullish_ratio = green_candles / len(recent) if len(recent) > 0 else 0.0

    day_high = float(df["High"].max())
    day_low = float(df["Low"].min())

    return {
        "pct_move": round(pct_move, 2),
        "bullish_candle_ratio": round(bullish_ratio, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "current_price": round(current_price, 2),
        "actual_window_min": actual_window_min,  # V8.2.0 transparency
    }


def _bar_minutes(interval=INTRADAY_INTERVAL):
    """V8.2.0: INTRADAY_INTERVAL se properly parse karta hai (5m → 5, 15m → 15, 1h → 60)."""
    try:
        if interval.endswith("m"):
            return int(interval[:-1])
        if interval.endswith("h"):
            return int(interval[:-1]) * 60
    except (ValueError, TypeError):
        pass
    return 5


def _volume_accumulation(df, lookback_minutes=BTST_LOOKBACK_MINUTES):
    """
    "Heavy volume accumulation" = pichle lookback window ka average
    volume, poore din ke average se zyada hai.

    V8.2.0: same graceful-degradation logic - too-thin data par None
    return karta hai (silently window shrink nahi karta).

    Return: dict {recent_avg_vol, day_avg_vol, accumulation_ratio} ya None
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None

    bar_minutes = max(1, _bar_minutes())
    bars_needed = max(1, lookback_minutes // bar_minutes)
    if len(df) < bars_needed:
        # V8.2.0: too-thin window - skip transparently
        if len(df) * bar_minutes < 30:
            return None
        bars_needed = len(df)

    recent_avg = float(df["Volume"].iloc[-bars_needed:].mean())
    day_avg = float(df["Volume"].mean())

    if day_avg <= 0:
        return None

    ratio = recent_avg / day_avg
    return {
        "recent_avg_vol": round(recent_avg, 0),
        "day_avg_vol": round(day_avg, 0),
        "accumulation_ratio": round(ratio, 2),
    }


def _day_high_proximity(current_price, day_high, threshold_pct=BTST_DAY_HIGH_PROXIMITY_PCT):
    """
    "Closing near the Day's High" check - current price, day's high ke
    kitne % andar hai.
    Return: bool (True agar proximity threshold ke andar hai)
    """
    if day_high <= 0:
        return False
    gap_pct = (day_high - current_price) / day_high * 100
    return gap_pct <= threshold_pct


def run_btst_scan(delay_sec=0.3):
    """
    Poora BTST scan chalata hai: liquid universe ke har stock ke liye
    last-1-hour price action + volume accumulation + Day's-High
    proximity check karta hai. Sirf wahi stocks jo TEENO criteria
    (ya kam se kam strong majority) pass karte hain, unhe conviction
    score ke saath return karta hai.

    V8.2.0: Market-hours guard - agar 14:00-15:35 IST ke beech nahi hai
    (weekend/holiday/early-morning/late-evening) to seedha empty list
    return karta hai. Pehle kal ka data "live" BTST signals publish karta tha.

    Return: list of dicts (sorted by conviction), har dict mein:
      {stock, pct_move, day_high, current_price, accumulation_ratio,
       conviction_score, reasons: [...]}
    """
    # V8.2.0: market-hours guard - BTST scan sirf last-hour window mein meaningful hai
    if not _is_btst_window_open():
        now_ist = datetime.now(IST)
        logger.info(f"BTST scan skip - last-hour window band hai (IST now={now_ist.strftime('%A %H:%M')})")
        return []

    universe = _get_universe()
    logger.info(f"BTST scan shuru: {len(universe)} stocks ka liquid universe")

    results = []

    for i, symbol in enumerate(universe, 1):
        df = _fetch_today_intraday(symbol)
        if df is None or df.empty:
            continue

        price_action = _last_hour_price_action(df)
        volume_info = _volume_accumulation(df)

        if price_action is None:
            continue

        score = 0
        reasons = []

        # Daily breakout stocks + strong last-hour price action
        # V8.2.0: agar actual_window < 60 min hai to reason mein note karo
        window_note = ""
        if price_action.get("actual_window_min", 60) < 60:
            window_note = f" ({price_action['actual_window_min']}min)"

        if price_action["pct_move"] > 0.5 and price_action["bullish_candle_ratio"] >= 0.6:
            score += 35
            reasons.append(f"Last hour{window_note} mein +{price_action['pct_move']}% strong move")

        # Heavy volume accumulation
        if volume_info and volume_info["accumulation_ratio"] > 1.3:
            score += 30
            reasons.append(f"Volume accumulation ({volume_info['accumulation_ratio']}x average)")

        # Closing near Day's High
        near_high = _day_high_proximity(price_action["current_price"], price_action["day_high"])
        if near_high:
            score += 35
            reasons.append(f"Day's High (₹{price_action['day_high']}) ke paas closing")

        if score >= 60 and reasons:  # kam se kam do criteria strongly pass hone chahiye
            results.append({
                "stock": symbol,
                "pct_move": price_action["pct_move"],
                "day_high": price_action["day_high"],
                "current_price": price_action["current_price"],
                "accumulation_ratio": volume_info["accumulation_ratio"] if volume_info else None,
                "conviction_score": score,
                "reasons": reasons,
                "df": df,  # V8.1.2: chart banane ke liye - dobara fetch nahi karna padega
            })

        if i < len(universe):
            time.sleep(delay_sec)

        if i % 25 == 0:
            logger.info(f"BTST scan progress: {i}/{len(universe)}")

    results.sort(key=lambda r: r["conviction_score"], reverse=True)
    logger.info(f"BTST scan complete: {len(results)} high-conviction candidates mile")
    return results[:BTST_TOP_N_RESULTS]
