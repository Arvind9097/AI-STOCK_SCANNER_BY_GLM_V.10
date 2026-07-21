"""
===========================================================
 DOWNLOADER MODULE (V10.2 - Auto-Fix Edition)
===========================================================
Ye module main.py ke liye `download_all` aur `download_benchmark`
functions provide karta hai taaki koi ImportError na aaye.
===========================================================
"""
import os
import time
import random
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

# Config se saare variables import kiye gaye hain
from config import (
    CSV_PATH, PERIOD, INTERVAL, MAX_WORKERS,
    DOWNLOAD_RETRIES, RETRY_SLEEP_SEC, RANDOM_DELAY_MIN_SEC, RANDOM_DELAY_MAX_SEC,
    RATE_LIMIT_BACKOFF_BASE_SEC, RATE_LIMIT_BACKOFF_MAX_SEC, RATE_LIMIT_MAX_RETRIES,
    SYMBOL_SOURCE
)

# Safe imports for Cache and Logger
try:
    from cache import load_from_cache, save_to_cache
except ImportError:
    def load_from_cache(symbol): return None
    def save_to_cache(symbol, df): pass

try:
    from logger import logger
except ImportError:
    import logging
    logger = logging.getLogger("Downloader")
    logger.setLevel(logging.INFO)


def download_benchmark():
    """
    main.py ko start hone ke liye NIFTY 50 ka data chahiye.
    Ye function wahi provide karta hai.
    """
    logger.info("NIFTY 50 benchmark fetch kar raha hoon...")
    ticker = "^NSEI"
    try:
        df = yf.download(ticker, period=PERIOD, interval=INTERVAL, progress=False)
        if df is not None and not df.empty:
            # Fix multi-index DataFrame behavior for newer yfinance versions
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            return df
    except Exception as e:
        logger.error(f"Benchmark download fail: {e}")
    return None


def _fetch_single_stock(symbol, force_fresh=False):
    """Ek single stock ko download karne ka logic with Retries & Rate Limiting."""
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    
    # 1. Cache Check (Save API Calls)
    if not force_fresh:
        cached_df = load_from_cache(ticker)
        if cached_df is not None and not cached_df.empty:
            return ticker, cached_df

    # 2. Random Delay to prevent IP Block
    time.sleep(random.uniform(RANDOM_DELAY_MIN_SEC, RANDOM_DELAY_MAX_SEC))

    # 3. Fetch Data with Retries
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            df = yf.download(ticker, period=PERIOD, interval=INTERVAL, progress=False)
            
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])
                
                if len(df) > 0:
                    save_to_cache(ticker, df)
                    return ticker, df
                    
        except Exception as e:
            logger.debug(f"{ticker} fetch attempt {attempt+1} fail: {e}")
            
        # Backoff sleep before retry
        time.sleep(RETRY_SLEEP_SEC)

    return ticker, None


def download_all(force_fresh=False):
    """
    Bulk downloader for all stocks (Required by main.py line 22).
    Returns a dictionary mapping {symbol: DataFrame}.
    """
    logger.info(f"Bulk download shuru ho raha hai... (Workers: {MAX_WORKERS})")
    
    if not os.path.exists(CSV_PATH):
        logger.error(f"CSV file nahi mili: {CSV_PATH}. Fallback to NIFTY50 stocks.")
        symbols = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"] # Fallback list
    else:
        try:
            df_csv = pd.read_csv(CSV_PATH)
            # Find the symbol column dynamically
            sym_col = next((c for c in df_csv.columns if c.strip().lower() in ['symbol', 'symbols', 'ticker']), df_csv.columns[0])
            symbols = df_csv[sym_col].dropna().astype(str).str.strip().tolist()
        except Exception as e:
            logger.error(f"CSV read error: {e}")
            return {}

    results = {}
    total = len(symbols)
    logger.info(f"Total {total} stocks fetch karne hain...")

    # Multi-threading for fast downloads
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_single_stock, sym, force_fresh): sym for sym in symbols}
        
        completed = 0
        for future in as_completed(futures):
            completed += 1
            ticker, df = future.result()
            if df is not None:
                results[ticker] = df
                
            if completed % 50 == 0 or completed == total:
                logger.info(f"Download Progress: {completed}/{total} stocks processed.")

    logger.info(f"Bulk download complete. {len(results)}/{total} stocks ka data mila.")
    return results
