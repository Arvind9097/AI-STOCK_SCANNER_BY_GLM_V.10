"""
===========================================================
 UNIVERSE FETCHER — NSE + BSE full stock list (V8.3.0)
===========================================================
Pehle sirf NIFTY 500 use hota tha. Ab ye module:

  1. NSE — sabhi listed equities (~2000+ symbols) laata hai
     NSE ki "equity" securities list se (archives CSV).
  2. BSE — sabhi listed equities (~5000+ symbols) laata hai
     BSE ke public "Scrip Code" CSV / Equity bhavcopy se.
  3. Dono ko merge karke ek unified symbol list banata hai:
       NSE symbols → "SYMBOL.NS"
       BSE symbols → "SYMBOL.BO"
     (yfinance dono suffixes samajhta hai)

FAST MODE (default): NIFTY 500 + NSE top-liquid (~700) + BSE
top-liquid (~300) = ~1300 symbols. Poore universe mein
7000+ symbols hote hain, lekin zyaada illiquid/penny stocks
scan karna time waste hai + signal noise badhta hai. Isliye
default mein liquidity-filtered subset use hota hai.

FULL MODE (config: UNIVERSE_MODE="FULL"): poore NSE + BSE
list (7000+ symbols). Slow (30+ min) lekin comprehensive.

Performance: yfinance batch download (CHUNK_SIZE=10) +
2-stage scan (scanner.py mein) — stage 1 quick filter,
stage 2 full indicator scan sirf top candidates par.

Sources (sab FREE, koi auth nahi):
  - NSE archives:  https://archives.nseindia.com/content/indices/
  - NSE equity:    https://archives.nseindia.com/content/equities/EQUITY_L.csv
  - BSE scrip:     https://api.bseindia.com/Msource/1D/getQoute.aspx
                   (ya bhavcopy se)
  - Fallback:      data/nifty500.csv (cached) + CUSTOM_STOCKS
===========================================================
"""

import os
import io
import csv
import time
import threading

from config import (
    NIFTY500_LOCAL_CSV, CUSTOM_STOCKS,
    UNIVERSE_MODE, UNIVERSE_MAX_SYMBOLS,
    UNIVERSE_MIN_PRICE, UNIVERSE_MIN_AVG_VOLUME_LAKH,
)
from nse_session import get_nse_session, invalidate_nse_session
from logger import logger

# NSE archive endpoints (free, no auth, sirf cookies chahiye homepage se)
NSE_EQUITY_CSV_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_NIFTY500_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_NIFTY_TOTAL_CSV_URL = "https://archives.nseindia.com/content/indices/ind_niftytotalmarketlist.csv"

# BSE — BSE apna public "Group A/B/T" scrip list CSV deti hai. Agar ye
# endpoint block ho, to BSE bhavcopy se symbols laa sakte hain (slower).
# Yahan sirf ek endpoint try karte hain, fail hone par NSE-only chalta hai.
BSE_SCRIP_CSV_URL = "https://www.bseindia.com/corporates/List_Scrips.csv"

# Cache (process-lifetime) — ek hi din mein dobara download na karna pade
_universe_cache = None
_universe_cache_lock = threading.Lock()


def _fetch_nse_equity_list():
    """
    NSE ki EQUITY_L.csv se sabhi listed equity symbols laata hai.
    Return: list of {"symbol": "RELIANCE", "name": "Reliance Industries", "series": "EQ"}
    Fail hone par empty list.
    """
    session = get_nse_session()
    try:
        resp = session.get(NSE_EQUITY_CSV_URL, timeout=20)
        if resp.status_code in (401, 403):
            invalidate_nse_session()
            session = get_nse_session(force_new=True)
            resp = session.get(NSE_EQUITY_CSV_URL, timeout=20)
        resp.raise_for_status()
        df_lines = resp.text.strip().splitlines()
        if len(df_lines) < 2:
            return []
        reader = csv.DictReader(df_lines)
        out = []
        for row in reader:
            sym = (row.get("SYMBOL") or row.get("Symbol") or "").strip()
            name = (row.get("NAME OF COMPANY") or row.get("Company") or "").strip()
            series = (row.get("SERIES") or row.get("Series") or "").strip()
            if sym:
                out.append({"symbol": sym, "name": name, "series": series, "exchange": "NSE"})
        logger.info(f"NSE equity list: {len(out)} symbols download hue")
        return out
    except Exception as e:
        logger.warning(f"NSE equity list download fail: {e}")
        return []


def _fetch_nse_index_list(url, label):
    """NSE index CSV (nifty500 / niftytotalmarket) se symbols laata hai."""
    session = get_nse_session()
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code in (401, 403):
            invalidate_nse_session()
            session = get_nse_session(force_new=True)
            resp = session.get(url, timeout=20)
        resp.raise_for_status()
        df_lines = resp.text.strip().splitlines()
        if len(df_lines) < 2:
            return []
        reader = csv.DictReader(df_lines)
        out = []
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            name = (row.get("Company Name") or row.get("Company") or "").strip()
            if sym:
                out.append({"symbol": sym, "name": name, "series": "EQ", "exchange": "NSE"})
        logger.info(f"NSE {label}: {len(out)} symbols")
        return out
    except Exception as e:
        logger.warning(f"NSE {label} download fail: {e}")
        return []


def _fetch_bse_scrip_list():
    """
    BSE ki List_Scrips.csv se equity symbols laata hai.
    Return: list of {"symbol": "RELIANCE", "name": "...", "series": "A"}
    Fail hone par empty list (NSE-only mode chalta hai).
    """
    import requests
    try:
        # BSE direct CSV download — no auth, but needs browser-like headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/csv,application/csv,text/plain,*/*",
        }
        resp = requests.get(BSE_SCRIP_CSV_URL, headers=headers, timeout=25)
        resp.raise_for_status()
        df_lines = resp.text.strip().splitlines()
        if len(df_lines) < 2:
            return []
        reader = csv.DictReader(df_lines)
        out = []
        for row in reader:
            # BSE CSV columns: "Security Code", "Security Name", "Status", "Group"
            sym = (row.get("Security Id") or row.get("Security Code") or "").strip()
            name = (row.get("Security Name") or row.get("Issuer Name") or "").strip()
            group = (row.get("Group") or row.get("Scrip Group") or "").strip()
            # Sirf equity groups (A, B, T, etc.) — debt/preference skip
            status = (row.get("Status") or "").strip().upper()
            if sym and status in ("ACTIVE", "A", "") and len(sym) <= 30:
                # BSE symbol mein spaces/special chars hote hain — clean
                clean_sym = sym.replace(" ", "").replace("&", "AND").upper()
                if clean_sym and clean_sym.isalnum():
                    out.append({"symbol": clean_sym, "name": name, "series": group, "exchange": "BSE"})
        logger.info(f"BSE scrip list: {len(out)} symbols download hue")
        return out
    except Exception as e:
        logger.warning(f"BSE scrip list download fail: {e} (NSE-only mode chalega)")
        return []


def _load_local_nifty500_csv():
    """Local cached CSV se NIFTY 500 list (offline fallback)."""
    if not os.path.exists(NIFTY500_LOCAL_CSV):
        return []
    try:
        import pandas as pd
        df = pd.read_csv(NIFTY500_LOCAL_CSV)
        if "Symbol" not in df.columns:
            return []
        out = []
        for _, row in df.iterrows():
            sym = str(row.get("Symbol", "")).strip()
            name = str(row.get("Company Name", "")).strip()
            if sym:
                out.append({"symbol": sym, "name": name, "series": "EQ", "exchange": "NSE"})
        logger.info(f"Local NIFTY500 CSV: {len(out)} symbols")
        return out
    except Exception as e:
        logger.warning(f"Local NIFTY500 CSV load fail: {e}")
        return []


def _apply_liquidity_filter(symbols_meta):
    """
    FAST mode: sirf liquid symbols rakho. Iss point par humein
    price/volume data nahi hai (woh scan time pe aata hai), isliye
    heuristic use karte hain:
      - Series EQ / BE / BM (NSE mein tradable equities)
      - Symbol naam mein obvious penny-stock patterns nahi (e.g. "PENNY", "BONUS")
      - NIFTY 500 + NIFTY Total Market = top-liquid ~750

    FULL mode mein ye filter skip hota hai.
    """
    if UNIVERSE_MODE == "FULL":
        return symbols_meta

    # FAST mode: NIFTY 500 + NIFTY Total Market + BSE Group A/B
    filtered = []
    seen = set()
    for m in symbols_meta:
        sym = m["symbol"]
        exch = m["exchange"]
        series = (m.get("series") or "").upper()
        key = f"{sym}.{exch}"
        if key in seen:
            continue
        # NSE: EQ/BE/BM tradable; BSE: A/B group tradable
        if exch == "NSE" and series and series not in ("EQ", "BE", "BM", "BZ", "SM", "ST"):
            continue
        if exch == "BSE" and series and series not in ("A", "B", "T", "S", "M"):
            continue
        seen.add(key)
        filtered.append(m)
        if len(filtered) >= UNIVERSE_MAX_SYMBOLS:
            break
    return filtered


def get_stock_universe():
    """
    MAIN ENTRY: unified NSE + BSE symbol list return karta hai.

    Return: list of dicts:
        [{"symbol": "RELIANCE.NS", "name": "Reliance Industries", "exchange": "NSE"}, ...]

    Caching: process-lifetime cache — ek hi run mein dobara call
    hone par cache se milta hai (network call nahi).

    Fallback chain:
      1. NSE archives (equity + nifty500 + niftytotalmarket)
      2. BSE scrip list
      3. Local CSV cache (data/nifty500.csv)
      4. CUSTOM_STOCKS (config.py — last resort, scan kam se kam chale)
    """
    global _universe_cache
    with _universe_cache_lock:
        if _universe_cache is not None:
            return _universe_cache

        all_meta = []

        # 1. NSE sources (3 CSVs — equity list is most comprehensive)
        nse_equity = _fetch_nse_equity_list()
        if not nse_equity:
            # Fallback: nifty500 + niftytotalmarket (agar EQUITY_L block ho)
            nse_equity = _fetch_nse_index_list(NSE_NIFTY500_CSV_URL, "nifty500")
            nse_equity += _fetch_nse_index_list(NSE_NIFTY_TOTAL_CSV_URL, "niftytotalmarket")
        all_meta.extend(nse_equity)

        # 2. BSE source
        bse = _fetch_bse_scrip_list()
        all_meta.extend(bse)

        # 3. Fallback: local CSV
        if not all_meta:
            local = _load_local_nifty500_csv()
            all_meta.extend(local)

        # 4. Last resort: static embedded list (V8.3.3)
        # Cloud par NSE archives BLOCK ho jaata hai (403). Local CSV bhi
        # nahi hoti. CUSTOM_STOCKS sirf 10 stocks hain (bahut kam). Isliye
        # static_universe.py mein NIFTY 500 + BSE top ~600 stocks embedded
        # hain — ye hamesha available, no network needed.
        if not all_meta:
            logger.warning("Sab live sources fail — STATIC universe use ho raha hai (NIFTY500+BSE embedded)")
            try:
                from static_universe import get_static_universe
                all_meta = get_static_universe()
            except ImportError:
                # static_universe.py nahi mila — last resort CUSTOM_STOCKS
                logger.warning("static_universe import fail — CUSTOM_STOCKS use ho raha hai")
                all_meta = [{"symbol": s.replace(".NS", "").replace(".BO", ""),
                             "name": "", "series": "EQ", "exchange": "NSE"}
                            for s in CUSTOM_STOCKS]

        # Liquidity filter (FAST mode) + dedup
        all_meta = _apply_liquidity_filter(all_meta)

        # Convert to yfinance-compatible symbols (.NS / .BO suffix)
        unified = []
        seen_final = set()
        for m in all_meta:
            sym = m["symbol"]
            exch = m["exchange"]
            suffix = ".NS" if exch == "NSE" else ".BO"
            yf_sym = sym + suffix
            if yf_sym in seen_final:
                continue
            seen_final.add(yf_sym)
            unified.append({
                "symbol": yf_sym,
                "name": m.get("name", ""),
                "exchange": exch,
            })

        # Final cap
        unified = unified[:UNIVERSE_MAX_SYMBOLS]

        n_nse = sum(1 for u in unified if u["exchange"] == "NSE")
        n_bse = sum(1 for u in unified if u["exchange"] == "BSE")
        logger.info(f"Universe ready: {len(unified)} symbols (NSE: {n_nse}, BSE: {n_bse}, mode: {UNIVERSE_MODE})")

        _universe_cache = unified
        return unified


def get_stock_symbols():
    """Convenience: sirf symbol list chahiye ho to."""
    return [u["symbol"] for u in get_stock_universe()]


def get_symbol_name_map():
    """symbol → company name map (dashboard display ke liye)."""
    return {u["symbol"]: u["name"] for u in get_stock_universe() if u.get("name")}
