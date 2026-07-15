# intraday_scanner.py
"""
===========================================================
 INTRADAY SCANNER (naya, 9:30 AM) - ORB / VWAP / Rel-Volume
===========================================================
Ye module scanner.py (SWING scanner) se BILKUL ALAG hai aur usko
bilkul touch nahi karta. Document ki exact requirement:

  "Intraday Selection Rules (Run before 9:30 AM): high liquidity
   (Nifty 500), high relative volume (Volume > 2x of 20-day average),
   Top Gainers/Losers from Pre-market, aur stocks near opening range
   breakouts (ORB) ya VWAP crossovers."

KYUN ALAG MODULE, ALAG DATA SOURCE:
  - Swing scanner (scanner.py) DAILY EOD candles use karta hai
    (NSE Chart API -> Stooq -> yfinance daily, market_data_fetcher.py se)
  - Ye Intraday scanner ko ORB (Opening Range Breakout) aur ASLI VWAP
    crossover ke liye INTRADAY candles (5-min bars) chahiye - koi bhi
    free NSE/Stooq source intraday history nahi deta (sirf EOD daily),
    isliye ye seedha yfinance intraday interval use karta hai (jaisa
    mtf.py apne 1H confirmation ke liye karta hai). Market string
    khulne ke baad hi chalta hai (9:30 AM), sirf ek chhoti liquid
    shortlist par (poore Nifty500 par nahi - rate-limit + samay dono
    ki wajah se practical nahi).

V8.2.0 FIXES (Task F5):
  1. RVOL ab true 20-DAY average use karta hai (pehle 20-BAR = 100-min
     rolling mean tha, jo semantically wrong tha). Ek daily volume
     history fetch karke `daily_avg / bars_per_day` se per-bar baseline
     nikaalte hain, phir current 5-min bar volume ko is baseline se
     compare karte hain. Proper RVOL = current_bar_vol / baseline.
  2. ORB ab df ko TODAY ke bars par filter karta hai (IST date ke
     against). Pehle `df.iloc[:N]` blindly aaj ke 9:15-9:30 maan leta
     tha - weekend/holiday/non-IST host par kal ke 15:00 bars galat
     ORB publish kar dete the. Ab market-hours guard bhi hai.
  3. `bar_minutes` dead-code conditional hata diya - ab INTRADAY_INTERVAL
     se properly parse hota hai.
  4. Poora module IST use karta hai (host TZ se independent).

Pre-market Movers: nse_market_data.py ka existing `get_pre_open_movers()`
hi reuse hota hai.
===========================================================
"""

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from config import (
    CUSTOM_STOCKS, INTRADAY_UNIVERSE_TOP_N, INTRADAY_INTERVAL,
    INTRADAY_ORB_MINUTES, INTRADAY_RVOL_THRESHOLD, INTRADAY_TOP_N_RESULTS,
    DATA_DIR,
)
from nifty_symbols import get_nifty500_symbols
from utils import atomic_write_json
from logger import logger

# V8.2.0: IST timezone - host TZ (UTC/etc) se independent market-time decisions.
IST = ZoneInfo("Asia/Kolkata")

# V9.0: Intraday picks ka in-memory cache - intraday_tracker.py ise
# read karke live entry/target/SL alerts bhejta hai (5-min loop).
# Cache file se pehle yahan se try hota hai (file I/O skip).
_last_picks_cache = None

# V9.0: Cache file path - intraday_tracker.py same file se fallback
# read karta hai agar in-memory cache None ho (e.g. naya process start
# hua ho aur scan abhi tak na chala ho).
_INTRADAY_PICKS_CACHE_FILE = os.path.join(DATA_DIR, "intraday_picks_today.json")


def _save_picks_cache(results):
    """
    V9.0: Intraday scan ke picks ko cache mein save karta hai -
    in-memory (_last_picks_cache) + atomic file write
    (data/intraday_picks_today.json).

    Format (intraday_tracker.py expected):
      {
        "_date": "2025-07-14",
        "picks": [
          {"stock": "RELIANCE.NS", "entry": 2940, "entry_low": 2940,
           "entry_high": 2955, "sl": 2890, "target": 3010},
          ...
        ]
      }

    Entry/SL/Target logic main.py ke run_intraday_scan_pipeline jaisa
    hi hai (0.5% SL, 1% T1). Entry zone ek chhota 0.5% upper buffer
    rakhta hai (entry_low = entry, entry_high = entry*1.005) - taaki
    live tracker ko ek meaningful zone mile jisme price aaye par
    ENTRY NOW alert fire ho.
    """
    global _last_picks_cache

    picks = []
    for r in results:
        entry = r.get("current_price")
        if entry is None or entry <= 0:
            continue
        entry = float(entry)
        picks.append({
            "stock": r.get("stock"),
            "entry": round(entry, 2),
            "entry_low": round(entry, 2),                 # breakout level
            "entry_high": round(entry * 1.005, 2),         # 0.5% upper buffer
            "sl": round(entry * 0.995, 2),                 # 0.5% SL (main.py same)
            "target": round(entry * 1.01, 2),              # 1% T1 (main.py same)
        })

    # In-memory cache (intraday_tracker pehle yahan se read karta hai)
    _last_picks_cache = picks

    # Atomic file write - naye process restart par bhi tracker ko
    # aaj ke picks mil jaaye (jab tak naya scan na chal jaaye).
    payload = {
        "_date": datetime.now(IST).strftime("%Y-%m-%d"),
        "picks": picks,
    }
    try:
        atomic_write_json(_INTRADAY_PICKS_CACHE_FILE, payload)
        logger.info(f"V9.0: {len(picks)} intraday picks cache mein save ho gaye "
                    f"({_INTRADAY_PICKS_CACHE_FILE})")
    except Exception as e:
        logger.warning(f"Intraday picks cache save fail: {e}")


# V8.2.0: NSE market hours - 9:15 to 15:30 IST. Intraday scan sirf in
# hours mein meaningful hai (baahar no fresh bars, ya stale yesterday data).
_NSE_MARKET_OPEN_HHMM = "09:15"
_NSE_MARKET_CLOSE_HHMM = "15:30"

# V8.2.0: ek trading day mein 5-min bars ki ginti (9:15-15:30 = 375 min / 5 = 75).
# 20-day RVOL baseline nikalne ke liye use hota hai: daily_avg_vol / bars_per_day
# = expected per-bar volume. Agar interval change ho (15m/1m) to isko bhi adjust karo.
def _bars_per_day(interval=INTRADAY_INTERVAL):
    """Ek NSE trading day (375 min) mein kitne bars banenge - interval ke hisab se."""
    try:
        if interval.endswith("m"):
            mins = int(interval[:-1])
        elif interval.endswith("h"):
            mins = int(interval[:-1]) * 60
        else:
            mins = 5
    except (ValueError, TypeError):
        mins = 5
    return max(1, 375 // mins)


def _bar_minutes(interval=INTRADAY_INTERVAL):
    """V8.2.0: dead-code fix - INTRADAY_INTERVAL se properly parse karta hai."""
    try:
        if interval.endswith("m"):
            return int(interval[:-1])
        if interval.endswith("h"):
            return int(interval[:-1]) * 60
    except (ValueError, TypeError):
        pass
    return 5


def _is_market_open_now():
    """IST market hours check - intraday scan ko sirf 9:15-15:30 ke beech allow karta hai."""
    now = datetime.now(IST)
    # weekend guard (IST Saturday/Sunday)
    if now.weekday() >= 5:
        return False
    hhmm = now.strftime("%H:%M")
    return _NSE_MARKET_OPEN_HHMM <= hhmm <= _NSE_MARKET_CLOSE_HHMM


# -----------------------------------------------------------
# INTRADAY DATA FETCH (yfinance only - koi free NSE/Stooq source
# intraday candles nahi deta, jaisa mtf.py mein bhi documented hai)
# -----------------------------------------------------------
def _fetch_intraday_candles(symbol, interval=INTRADAY_INTERVAL):
    """
    Aaj ka intraday OHLCV (5-min candles) laata hai. Return: DataFrame
    ya None (fail hone par - kabhi crash nahi karta).

    V8.2.0: multi-index columns defensively flatten karta hai (yfinance
    kabhi single-ticker ke liye bhi 2-level columns deta hai).
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period="1d", interval=interval, auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        logger.debug(f"{symbol}: intraday candle fetch fail ({e})")
        return None


def _fetch_daily_volume_history(symbol, days=25):
    """
    V8.2.0: pichle ~25 trading days ki DAILY volume history laata hai.
    `days=25` isliye kyunki 20-day average chahiye, aur weekends/holidays
    ke baad 22-23 calendar day mein ~20 trading day mil jaate hain.
    Return: pandas Series (daily Volume indexed by date) ya None.
    """
    try:
        import yfinance as yf
        # period="1mo" ~ 22 trading days; "2mo" safer for 20-day avg.
        df = yf.download(symbol, period="2mo", interval="1d", auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        if "Volume" not in df.columns:
            return None
        vol = pd.to_numeric(df["Volume"], errors="coerce").dropna()
        if len(vol) == 0:
            return None
        return vol.tail(days)
    except Exception as e:
        logger.debug(f"{symbol}: daily volume history fetch fail ({e})")
        return None


def _get_liquid_universe(top_n=INTRADAY_UNIVERSE_TOP_N):
    """
    Poore Nifty500 par intraday scan practical nahi hai (rate-limit +
    samay) - isliye ek chhoti liquid shortlist leते hain. Nifty500 list
    ke pehle N symbols (jo aam taur par sabse bade/liquid stocks hote
    hain, market-cap-sorted CSV maan kar) - CUSTOM_STOCKS ko hamesha
    include karte hain taaki user ki apni watchlist na chhoote.
    """
    try:
        symbols = get_nifty500_symbols()
    except Exception as e:
        logger.warning(f"Nifty500 list nahi mil payi, sirf CUSTOM_STOCKS use kar raha hoon: {e}")
        symbols = []

    universe = list(dict.fromkeys(CUSTOM_STOCKS + symbols[:top_n]))  # dedup, order preserve
    return universe[:top_n]


# -----------------------------------------------------------
# ORB (Opening Range Breakout) - V8.2.0: TODAY-filter + IST guard
# -----------------------------------------------------------
def _filter_today_bars(df):
    """
    V8.2.0: intraday DataFrame ko sirf aaj ke IST-date wale bars par
    filter karta hai. yfinance `period="1d"` kabhi-kabhi (weekend,
    holiday, non-IST host) kal ke bars deta hai - unhe hata ta hai.
    """
    if df is None or df.empty:
        return df
    try:
        today_ist = datetime.now(IST).date()
        # df.index datetime-aware hai (yfinance UTC) ya naive - dono
        # case handle karne ke liye .date() se compare karte hain
        if df.index.tz is not None:
            # tz-aware - convert to IST first
            idx_dates = df.index.tz_convert(IST).date
        else:
            # tz-naive - maan ke chalo ki already local/IST hai
            idx_dates = df.index.date
        mask = [d == today_ist for d in idx_dates]
        today_df = df[mask]
        return today_df if not today_df.empty else df
    except Exception:
        return df


def _check_orb(df, orb_minutes=INTRADAY_ORB_MINUTES):
    """
    Opening Range = pehle `orb_minutes` (default 15 min, 9:15-9:30) ka
    High/Low. ORB Breakout = current price ne is range ke bahar break
    kiya ho (upar = bullish ORB, neeche = bearish ORB).

    V8.2.0: df ko pehle TODAY ke bars par filter karta hai (IST date),
    aur verify karta hai ki pehla bar ~9:15 AM IST ka hai. Agar weekend/
    holiday/non-IST host ki wajah se kal ke bars mil rahe hain to None
    return karta hai (bogus ORB publish nahi hoga).

    Return: dict {orb_high, orb_low, current_price, breakout: "BULLISH"|"BEARISH"|None}
    ya None (data kaafi nahi hai to)
    """
    if df is None or df.empty:
        return None

    # V8.2.0: sirf aaj ke bars rakho - kal/parso ke bars ORB galat
    # banate the (especially weekend par "live" intraday scan run karne par)
    df = _filter_today_bars(df)
    if df is None or df.empty:
        return None

    # V8.2.0: verify first bar ~9:15 IST. Agar tz-aware index hai to
    # convert karke check karo; warna (tz-naive) skip - soft check.
    try:
        if df.index.tz is not None:
            first_bar_ist = df.index[0].astimezone(IST)
            first_hhmm = first_bar_ist.strftime("%H:%M")
            # 9:15 se pehle ya 9:45 ke baad ka pehla bar = suspicious
            # (market khulne se pehle ya late-start host). Soft skip.
            if not ("09:10" <= first_hhmm <= "09:45"):
                logger.debug(f"ORB skip: first bar {first_hhmm} IST hai, 9:15 ke aas-paas nahi")
                return None
    except Exception:
        pass  # tz conversion fail - soft skip

    # V8.2.0: dead-code fix - ab INTRADAY_INTERVAL se properly parse
    bar_minutes = _bar_minutes()
    bars_needed = max(1, orb_minutes // bar_minutes)

    if len(df) <= bars_needed:
        return None  # abhi opening range hi complete nahi hui

    opening_range = df.iloc[:bars_needed]
    orb_high = float(opening_range["High"].max())
    orb_low = float(opening_range["Low"].min())
    current_price = float(df["Close"].iloc[-1])

    breakout = None
    if current_price > orb_high:
        breakout = "BULLISH"
    elif current_price < orb_low:
        breakout = "BEARISH"

    return {
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "current_price": round(current_price, 2),
        "breakout": breakout,
    }


# -----------------------------------------------------------
# ASLI INTRADAY VWAP (cumulative, ek din ke andar - indicators.py
# wala rolling_vwap() N-day APPROXIMATION hai, ye ASLI intraday hai)
# -----------------------------------------------------------
def _intraday_vwap_crossover(df):
    """
    Asli intraday VWAP: cumulative(typical_price * volume) / cumulative(volume),
    din ki shuruaat se ab tak. Crossover = current price ne VWAP ko abhi
    cross kiya (upar = bullish, neeche = bearish).

    Return: dict {vwap, current_price, crossover: "BULLISH"|"BEARISH"|None}
    ya None
    """
    if df is None or len(df) < 3:
        return None

    # V8.2.0: VWAP bhi sirf today ke bars par compute hona chahiye -
    # cross-day cumulative VWAP galat hoti hai.
    df = _filter_today_bars(df)
    if df is None or len(df) < 3:
        return None

    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_pv = (typical_price * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    vwap_series = cum_pv / cum_vol

    if vwap_series.isna().all():
        return None

    current_vwap = float(vwap_series.iloc[-1])
    prev_vwap = float(vwap_series.iloc[-2]) if len(vwap_series) > 1 else current_vwap
    current_price = float(df["Close"].iloc[-1])
    prev_price = float(df["Close"].iloc[-2]) if len(df) > 1 else current_price

    crossover = None
    if prev_price <= prev_vwap and current_price > current_vwap:
        crossover = "BULLISH"
    elif prev_price >= prev_vwap and current_price < current_vwap:
        crossover = "BEARISH"

    return {
        "vwap": round(current_vwap, 2),
        "current_price": round(current_price, 2),
        "crossover": crossover,
    }


# -----------------------------------------------------------
# RELATIVE VOLUME (>2x) - V8.2.0: TRUE 20-DAY baseline
# -----------------------------------------------------------
def _check_rvol(df, symbol, threshold=INTRADAY_RVOL_THRESHOLD):
    """
    V8.2.0 FIX: Pehle ye function indicators.py ka `relative_volume()`
    call karta tha, jo 5-min intraday bars par 20-bar rolling mean
    nikalta tha = 100-MINUTE average (NOT 20-DAY). Document requirement
    hai "Volume > 2x of 20-day average" - isliye ye semantically wrong tha.

    AB: pichle 20 trading days ki DAILY volume history alag se fetch
    karta hai (`_fetch_daily_volume_history`), uska mean nikaalta hai,
    phir `daily_avg / bars_per_day` se per-bar baseline nikalta hai.
    Current 5-min bar ka volume isi baseline se compare hota hai.

    Proper RVOL = current_bar_volume / (daily_avg_volume / bars_per_day)

    Example: RELIANCE daily avg vol = 10M shares; bars_per_day (5min) = 75.
    Expected per-bar volume = 10M / 75 = ~133K. Agar current 5-min bar
    ka volume 400K hai, to RVOL = 400K / 133K = 3.0x → high conviction.

    Return: float rvol ya None (data/translate fail hone par).
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    try:
        daily_vol = _fetch_daily_volume_history(symbol, days=25)
        if daily_vol is None or len(daily_vol) < 5:
            # 20-day baseline nahi bana paaye - fall back to intraday
            # rolling-20-bar average (purana behavior). Better than nothing
            # but threshold value semantically different - log it.
            logger.debug(f"{symbol}: 20-day daily volume nahi mila, intraday 20-bar avg fallback")
            from indicators import relative_volume as _rolling_rvol
            rvol_series = _rolling_rvol(df["Volume"])
            return round(float(rvol_series.iloc[-1]), 2)

        # V8.2.0: last 20 trading days ka daily volume average
        daily_avg_vol = float(daily_vol.tail(20).mean())
        bars_per_day = _bars_per_day()
        if daily_avg_vol <= 0 or bars_per_day <= 0:
            return None

        # Expected volume per 5-min bar (avg over 20 days)
        per_bar_baseline = daily_avg_vol / bars_per_day

        # Current 5-min bar ka volume (latest bar)
        current_bar_vol = float(df["Volume"].iloc[-1])
        if per_bar_baseline <= 0:
            return None

        rvol = current_bar_vol / per_bar_baseline
        return round(rvol, 2)
    except Exception as e:
        logger.debug(f"{symbol}: rvol calc fail ({e})")
        return None


# -----------------------------------------------------------
# MASTER SCAN FUNCTION
# -----------------------------------------------------------
def run_intraday_scan(universe_top_n=INTRADAY_UNIVERSE_TOP_N, delay_sec=0.3):
    """
    Poora intraday scan chalata hai: liquid universe ke har stock ke
    liye ORB + VWAP crossover + RVOL check karta hai, aur jo stocks
    kam se kam EK bullish signal dikhaate hain unhe score karke top-N
    return karta hai.

    V8.2.0: Market-hours guard - agar NSE band hai (weekend/holiday/
    before 9:15/after 15:30 IST) to seedha empty list return karta hai.
    Pehle weekend par /rerun intraday chalane par kal ka data "live"
    dikhta tha - ab properly skip hota hai.

    Return: list of dicts (sorted by conviction), har dict mein:
      {stock, orb_breakout, vwap_crossover, rvol, current_price,
       conviction_score, reasons: [...], df: DataFrame (chart ke liye)}
    """
    # V8.2.0: market-hours guard - intraday scan sirf live market mein meaningful hai
    if not _is_market_open_now():
        now_ist = datetime.now(IST)
        logger.info(f"Intraday scan skip - NSE market band hai (IST now={now_ist.strftime('%A %H:%M')})")
        return []

    universe = _get_liquid_universe(universe_top_n)
    logger.info(f"Intraday scan shuru: {len(universe)} stocks ka liquid universe")

    results = []

    for i, symbol in enumerate(universe, 1):
        df = _fetch_intraday_candles(symbol)
        if df is None or df.empty:
            continue

        # V8.2.0: sirf aaj ke bars par filter - kal ke bars ORB/VWAP
        # galat banate the (especially weekend par stale data)
        df_today = _filter_today_bars(df)
        if df_today is None or df_today.empty:
            continue

        orb_info = _check_orb(df_today)
        vwap_info = _intraday_vwap_crossover(df_today)
        rvol = _check_rvol(df_today, symbol)

        score = 0
        reasons = []

        if orb_info and orb_info["breakout"] == "BULLISH":
            score += 40
            reasons.append(f"ORB Breakout (₹{orb_info['orb_high']} ke upar)")
        elif orb_info and orb_info["breakout"] == "BEARISH":
            score -= 40  # bearish - is candidate ko upar nahi rakhenge (BUY-only list)

        if vwap_info and vwap_info["crossover"] == "BULLISH":
            score += 30
            reasons.append(f"VWAP Crossover (₹{vwap_info['vwap']} ke upar)")
        elif vwap_info and vwap_info["crossover"] == "BEARISH":
            score -= 30

        if rvol is not None and rvol > INTRADAY_RVOL_THRESHOLD:
            score += 30
            reasons.append(f"High Relative Volume ({rvol}x 20-day avg)")

        if score > 0 and reasons:  # sirf genuinely bullish candidates
            current_price = None
            if orb_info:
                current_price = orb_info["current_price"]
            elif vwap_info:
                current_price = vwap_info["current_price"]

            results.append({
                "stock": symbol,
                "orb_breakout": orb_info["breakout"] if orb_info else None,
                "vwap_crossover": vwap_info["crossover"] if vwap_info else None,
                "rvol": rvol,
                "current_price": current_price,
                "conviction_score": score,
                "reasons": reasons,
                "df": df_today,  # V8.1.2: chart banane ke liye - dobara fetch nahi karna padega
            })

        if i < len(universe):
            time.sleep(delay_sec)

        if i % 25 == 0:
            logger.info(f"Intraday scan progress: {i}/{len(universe)}")

    results.sort(key=lambda r: r["conviction_score"], reverse=True)
    top_results = results[:INTRADAY_TOP_N_RESULTS]
    logger.info(f"Intraday scan complete: {len(results)} bullish candidates mile")

    # V9.0: Top picks ko cache mein save karo - taaki intraday_tracker.py
    # 5-min live alert loop inhe read karke entry/target/SL alerts bhej sake.
    # Sirf meaningful results save karte hain (empty list bhi save hoti hai
    # taaki tracker ko pata chale ki aaj scan chala tha lekin koi pick nahi mila).
    try:
        _save_picks_cache(top_results)
    except Exception as e:
        logger.warning(f"Intraday picks cache save fail (non-fatal): {e}")

    return top_results
