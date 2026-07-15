"""
===========================================================
 MULTI-TIMEFRAME CONFLUENCE (v5)
===========================================================
Swing trading ke liye sirf daily chart kaafi nahi hota - professional
approach ye hai:
  Weekly  -> bada trend direction confirm karta hai (bull ya bear market
             mein stock kis taraf hai)
  Daily   -> actual setup/pattern select karta hai (scanner.py already
             karta hai)
  1 Hour  -> "abhi entry lene layak hai ya thoda wait karna chahiye"
             confirm karta hai

IMPORTANT DESIGN CHOICE: Weekly trend daily data ko RESAMPLE karke
nikalta hai - koi extra Yahoo download nahi lagta (free hai, sab
stocks ke liye chalta hai). 1H confirmation ke liye EXTRA download
lagta hai, isliye ye sirf top-scoring candidates ke liye hi chalta
hai (rate-limit safe rehne ke liye) - ye scanner.py ke scan() ke
BAAD, sirf shortlisted stocks par chalta hai.
===========================================================
"""

import pandas as pd

from indicators import ema, rsi
from config import (
    WEEKLY_EMA_FAST, WEEKLY_EMA_SLOW,
    MTF_1H_PERIOD, MTF_1H_INTERVAL, MTF_1H_EMA,
    SIGNAL_THRESHOLDS,
)
from market_data_fetcher import fetch_intraday
from logger import logger

# V8.1.2 NOTE: 1H/intraday data koi bhi free NSE/Stooq source nahi deta
# (dono sirf EOD daily history serve karte hain) - isliye 1H confirmation
# hamesha yfinance (secondary) se hi aata hai. fetch_intraday() ye
# centralize karta hai taaki future mein koi free intraday source mile
# to sirf ek jagah badalna pade.


def _signal_from_score(score):
    """
    Score se Signal label nikalta hai (scanner._get_signal ka mirror).
    Local copy rakhi hai taaki mtf.py -> scanner.py import na karna
    pade (scanner.py mtf.py se get_weekly_trend import karta hai,
    circular import se bachne ke liye yahan define kiya hai).
    """
    if score >= SIGNAL_THRESHOLDS["STRONG BUY"]:
        return "STRONG BUY"
    elif score >= SIGNAL_THRESHOLDS["BUY"]:
        return "BUY"
    elif score >= SIGNAL_THRESHOLDS["WATCH"]:
        return "WATCH"
    return "SELL / AVOID"


def get_weekly_trend(daily_df):
    """
    daily_df: raw ya indicator-added daily OHLCV dataframe (Date column zaroori)
    Return: dict {trend: 'BULLISH'/'BEARISH'/'NEUTRAL', weekly_close, weekly_ema_fast, weekly_ema_slow}
    ya None (agar kaafi data nahi hai)
    """
    if daily_df is None or "Date" not in daily_df.columns or len(daily_df) < WEEKLY_EMA_SLOW * 5:
        return None

    try:
        df = daily_df.copy()
        df["Date"] = pd.to_datetime(df["Date"])
        # V8.2.0 BUGFIX: "W" default week-ending-Sunday deta hai, lekin
        # NSE trading week Mon-Fri hota hai - conventional trading-week
        # resample "W-FRI" hai (Friday ko week end). Isse weekly candles
        # ki alignment sahi hoti hai aur Friday ki candle ek complete
        # week banati hai (Monday-Friday) instead of partial.
        weekly = df.set_index("Date").resample("W-FRI").agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
        }).dropna()

        if len(weekly) < WEEKLY_EMA_SLOW:
            return None

        weekly_close = weekly["Close"]
        wema_fast = ema(weekly_close, WEEKLY_EMA_FAST).iloc[-1]
        wema_slow = ema(weekly_close, WEEKLY_EMA_SLOW).iloc[-1]
        last_close = weekly_close.iloc[-1]

        if last_close > wema_fast > wema_slow:
            trend = "BULLISH"
        elif last_close < wema_fast < wema_slow:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

        return {
            "trend": trend,
            "weekly_close": round(float(last_close), 2),
            "weekly_ema_fast": round(float(wema_fast), 2),
            "weekly_ema_slow": round(float(wema_slow), 2),
        }
    except Exception as e:
        logger.warning(f"Weekly trend calculate nahi ho paya: {e}")
        return None


def fetch_1h_confirmation(symbol):
    """
    Symbol ke liye 1H data download karke "entry confirmed hai ya
    pending" check karta hai. YE EXTRA API CALL LAGTA HAI - sirf
    shortlisted top candidates ke liye call karo, sab stocks ke
    liye nahi (rate-limit risk).

    Return: dict {confirmed: bool, last_close, ema20_1h, rsi_1h} ya None
    """
    try:
        raw = fetch_intraday(symbol, period=MTF_1H_PERIOD, interval=MTF_1H_INTERVAL)
        if raw is None or raw.empty or len(raw) < MTF_1H_EMA + 5:
            return None

        close = raw["Close"]
        ema_1h = ema(close, MTF_1H_EMA)
        rsi_1h = rsi(close, 14)

        last_close = float(close.iloc[-1])
        last_ema = float(ema_1h.iloc[-1])
        last_rsi = float(rsi_1h.iloc[-1])

        # "Confirmed" = 1H close apne EMA20 se upar hai (short-term
        # momentum bhi supportive hai), aur RSI bahut overbought nahi
        # hai (>85 to chase karna risky hota hai).
        # V8.2.0 BUGFIX: pehle sirf upper bound (<85) tha, neeche koi
        # floor nahi tha - ek crashing stock (RSI=10) bhi "confirmed"
        # ban jaata tha. Ab RSI >= 40 bhi check karte hain taaki
        # short-term momentum genuinely supportive ho.
        confirmed = last_close > last_ema and 40 <= last_rsi < 85

        return {
            "confirmed": bool(confirmed),
            "last_close": round(last_close, 2),
            "ema20_1h": round(last_ema, 2),
            "rsi_1h": round(last_rsi, 2),
        }
    except Exception as e:
        logger.warning(f"{symbol}: 1H confirmation fetch fail ({e})")
        return None


def enrich_top_candidates_with_mtf(ranked_result, top_n):
    """
    scanner.py ke scan() output (Score se sorted) leke, top_n
    candidates ke liye 1H confirmation add karta hai (in-place update
    on each row dict) + score/signal adjust karta hai.

    Isko main.py se scan() ke BAAD call karo - scan() ke andar nahi,
    kyunki ye extra network calls karta hai jo sirf shortlisted
    stocks ke liye chalne chahiye.
    """
    from config import MTF_1H_ENABLED, MTF_1H_CONFIRM_BONUS_POINTS

    if not MTF_1H_ENABLED:
        return ranked_result

    checked = 0
    for row in ranked_result:
        if checked >= top_n:
            # V8.2.0 NOTE: scanner.py already har row ko "NOT_CHECKED"
            # set karta hai, isliye yahan dobara set karna dead code tha.
            # Harmless tha lekin redundant - skip karte hain (row already
            # "NOT_CHECKED" hai).
            continue

        # Sirf un stocks ke liye check karo jo already BUY/STRONG BUY/WATCH ke kaabil hain
        if row.get("Signal") not in ("STRONG BUY", "BUY", "WATCH"):
            continue

        result_1h = fetch_1h_confirmation(row["Stock"])
        checked += 1

        if result_1h is None:
            row["MTF_1H_Status"] = "UNAVAILABLE"
            continue

        if result_1h["confirmed"]:
            row["MTF_1H_Status"] = "CONFIRMED"
            # V8.2.0 BUGFIX: Score +5 karne ke baad Signal recompute
            # karna padta hai - warna ek BUY (score 60) stock +5 milke
            # 65 ho jaata tha but Signal abhi bhi BUY (>=60) hi tha,
            # aur ek WATCH (score 40) stock +5 milke 45 ho jaata tha
            # but Signal abhi bhi WATCH hi rehta tha - dashboard par
            # inconsistency dikhti thi. Ab Score update ke baad Signal
            # hamesha fresh recompute hota hai.
            row["Score"] = min(100, row["Score"] + MTF_1H_CONFIRM_BONUS_POINTS)
            row["Signal"] = _signal_from_score(row["Score"])
        else:
            row["MTF_1H_Status"] = "PENDING"
            # Agar signal BUY tha (STRONG BUY nahi) aur 1H confirm nahi hua,
            # to WATCH kar do - "abhi entry lene ki jagah wait karo" jaisा
            if row.get("Signal") == "BUY":
                # Score bhi WATCH range mein cap karo (SIGNAL_THRESHOLDS["BUY"]-1)
                # taaki Score aur Signal consistent rahein.
                row["Score"] = min(row["Score"], SIGNAL_THRESHOLDS["BUY"] - 1)
                row["Signal"] = "WATCH"
                row.setdefault("Reasons", []).append("1H confirmation pending -> BUY se WATCH")

    ranked_result.sort(key=lambda r: r["Score"], reverse=True)
    return ranked_result
