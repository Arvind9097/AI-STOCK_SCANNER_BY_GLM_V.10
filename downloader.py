"""
===========================================================
 DATA DOWNLOADER (Yahoo Finance)
===========================================================
Yahoo Finance bahut zyada / bahut fast requests par
"YFRateLimitError: Too Many Requests" de deta hai. Isse bachne
ke liye:

1. Stocks ko chhote CHUNKS mein download karte hain (ek hi call
   mein kai symbols - isse total HTTP requests bahut kam ho jaate
   hain, parallel threads ke bajaye).
2. Batches SEQUENTIALLY (ek ke baad ek) chalte hain, beech mein
   RANDOM gap ke saath (predictable pattern na bane).
3. Agar rate-limit error mile, to exponential backoff ke saath
   retry hota hai (20s, 40s, 80s... MAX_BACKOFF tak).
4. LOCAL CACHE - jo stock already aaj download ho chuka hai, use
   dubara download nahi karta.
5. RESUME - agar beech mein ruk jaaye, agli baar wahin se aage
   badhta hai.
===========================================================
"""

import time
import random
import yfinance as yf

from config import (
    SYMBOL_SOURCE, CUSTOM_STOCKS, PERIOD, INTERVAL,
    CHUNK_SIZE, RANDOM_DELAY_MIN_SEC, RANDOM_DELAY_MAX_SEC,
    DOWNLOAD_RETRIES, RETRY_SLEEP_SEC,
    RATE_LIMIT_BACKOFF_BASE_SEC, RATE_LIMIT_MAX_BACKOFF_SEC,
    CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_COOLDOWN_SEC,
    BENCHMARK_SYMBOL,
    UNIVERSE_MODE, UNIVERSE_MAX_SYMBOLS,
)
# V8.3.0: universe_fetcher poore NSE + BSE universe laata hai (FAST/FULL
# mode ke saath). NSE block hone par nifty_symbols fallback chalta hai,
# aur sab kuch fail ho jaaye to CUSTOM_STOCKS (config.py).
from universe_fetcher import get_stock_symbols as _universe_get_symbols
from nifty_symbols import get_nifty500_symbols
from cache import load_from_cache, save_to_cache
from resume_state import load_resume_state, save_resume_state, clear_resume_state
from market_data_fetcher import fetch_daily_ohlcv
from logger import logger


def _get_symbol_list():
    """
    V8.3.0: Primary source = universe_fetcher (NSE + BSE full list).
    Fallback chain (preserve V8.2.0 behavior on failure):
      1. universe_fetcher.get_stock_symbols() — NSE + BSE unified
      2. nifty_symbols.get_nifty500_symbols() — NIFTY 500 only
      3. CUSTOM_STOCKS (config.py) — last resort

    SYMBOL_SOURCE="CUSTOM" use karne par direct CUSTOM_STOCKS use hota
    hai (backward-compat, debug/manual mode ke liye useful).
    """
    # Manual override: user explicitly wants CUSTOM_STOCKS only
    if SYMBOL_SOURCE == "CUSTOM":
        return list(CUSTOM_STOCKS)

    # Primary: universe_fetcher (V8.3.0)
    try:
        symbols = _universe_get_symbols()
        if symbols:
            logger.info(
                f"Universe fetcher se {len(symbols)} symbols mile "
                f"(mode={UNIVERSE_MODE}, cap={UNIVERSE_MAX_SYMBOLS})"
            )
            return symbols
        logger.warning("Universe fetcher se empty list aayi — fallback NIFTY 500 try karo")
    except Exception as e:
        logger.warning(f"Universe fetcher fail: {e} — fallback NIFTY 500 try karo")

    # Fallback 1: NIFTY 500 (V8.2.0 behavior)
    try:
        symbols = get_nifty500_symbols()
        if symbols:
            logger.info(f"Fallback: NIFTY 500 se {len(symbols)} symbols use ho rahe hain")
            return symbols
    except Exception as e:
        logger.warning(f"NIFTY 500 fetch bhi fail: {e}")

    # Fallback 2: CUSTOM_STOCKS (last resort — scan kam se kam chale)
    logger.warning(
        f"Sab symbol sources fail — config.CUSTOM_STOCKS use ho raha hai "
        f"({len(CUSTOM_STOCKS)} stocks)"
    )
    return list(CUSTOM_STOCKS)


def get_symbol_name_map():
    """
    V8.3.0 passthrough: universe_fetcher.get_symbol_name_map() return
    karta hai symbol → company name dict. Downloader ke saath share karne
    ke liye expose kiya gaya hai (charts/dashboard display ke liye).

    Agar universe_fetcher fail ho, to nifty_symbols.get_symbol_name_map()
    se fallback deta hai. Empty dict bhi return ho sakti hai (caller
    graceful handle karega).
    """
    try:
        from universe_fetcher import get_symbol_name_map as _univ_name_map
        name_map = _univ_name_map()
        if name_map:
            return name_map
    except Exception as e:
        logger.warning(f"universe_fetcher name map fail: {e}")
    # Fallback: nifty_symbols local CSV cache
    try:
        return get_nifty500_symbols_name_map_fallback()
    except Exception:
        return {}


def get_nifty500_symbols_name_map_fallback():
    """nifty_symbols.get_symbol_name_map() ko passthrough (downloader
    ke through expose karne ke liye helper)."""
    from nifty_symbols import get_symbol_name_map as _nifty_name_map
    return _nifty_name_map()


def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def _is_rate_limit_error(e):
    msg = str(e).lower()
    return (
        "rate limit" in msg
        or "too many requests" in msg
        or "429" in msg
        or type(e).__name__ == "YFRateLimitError"
    )


def _clean_single_ticker_df(df):
    """Ek ticker ka raw yfinance dataframe clean karke standard OHLCV format deta hai."""
    if df is None or df.empty:
        return None

    # ✅ FIX: Dynamic Multi-index handling taaki single-ticker (Benchmark) fail na ho
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        if "Close" in df.columns.get_level_values(1):
            df.columns = df.columns.get_level_values(1)
        else:
            df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    
    first_col = df.columns[0]
    if first_col != "Date":
        df = df.rename(columns={first_col: "Date"})

    required_cols = {"Close", "Open", "High", "Low", "Volume"}
    if not required_cols.issubset(set(df.columns)):
        return None

    if len(df) < 60:
        return None

    return df


def _download_chunk_primary_first(symbols):
    """
    V8.1.2: Batch mein yfinance download karne se PEHLE, har symbol ko
    individually PRIMARY sources (NSE Chart API -> Stooq.com, free/no-auth)
    se try karta hai. Jo symbols primary se mil jaayein unhe seedhe
    nikaal deta hai; baaki (jo primary se nahi mile) aage yfinance batch
    download mein jaate hain (SECONDARY/fallback, jaisa V8.1 mein tha).

    Return: (resolved_dict, still_pending_list)
    """
    resolved = {}
    pending = []

    for sym in symbols:
        df = fetch_daily_ohlcv(sym, period=PERIOD)
        if df is not None and len(df) >= 60:
            resolved[sym] = df
        else:
            pending.append(sym)

    if resolved:
        logger.info(f"Primary sources (NSE/Stooq) se {len(resolved)}/{len(symbols)} stocks mil gaye is batch mein")

    return resolved, pending


def _download_chunk(symbols):
    """
    Ek batch (list) of symbols ek hi yf.download() call mein download
    karta hai. Return: dict {symbol: df or None}

    V8.1.2 UPDATE: yfinance call se PEHLE, har symbol pehle PRIMARY (NSE
    Chart API -> Stooq.com) se try hota hai. Jo mil jaayein unke liye
    yfinance call hi nahi hota (rate-limit risk kam). Sirf jo primary se
    NAHI mile, unke liye hi neeche wala SECONDARY (yfinance) batch-download
    chalta hai - original retry/backoff/circuit-breaker logic waisा hi
    rehta hai, bas kam symbols par chalta hai.

    IMPORTANT FIX (V8.1 se unchanged): Naye yfinance versions rate-limit
    hone par exception RAISE nahi karte - internally hi catch karke sirf
    EMPTY dataframe return kar dete hain (asli YFRateLimitError sirf
    console par print hota hai, humare code tak exception ki tarah nahi
    pahunchta). Isliye "empty response" ko bhi HAMESHA rate-limit maan
    kar exponential backoff karte hain - warna sirf 5 sec wait karke
    turant dobara try karta, jo rate-limit window ko aur worse kar deta.
    """
    # ---- V8.1.2 STEP 1: PRIMARY sources se try karo (NSE/Stooq) ----
    resolved, symbols = _download_chunk_primary_first(symbols)

    if not symbols:
        return resolved  # poora batch primary se hi mil gaya

    # ---- STEP 2: SECONDARY (yfinance) - sirf pending symbols ke liye ----
    tickers_str = " ".join(symbols)

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            raw = yf.download(
                tickers_str,
                period=PERIOD,
                interval=INTERVAL,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=False,
            )

            if raw is None or raw.empty:
                raise ValueError("batch empty response (yfinance ne kuch nahi diya - likely rate-limit)")

            out = dict(resolved)  # V8.1.2: primary se mile symbols hamesha result mein rahein

            if len(symbols) == 1:
                out[symbols[0]] = _clean_single_ticker_df(raw)
            else:
                for sym in symbols:
                    try:
                        sub = raw[sym].copy()
                    except Exception:
                        # V8.2.0: `except (KeyError, Exception)` redundant tha
                        # (Exception already KeyError ko cover karta hai).
                        # Symbol raw multi-index DataFrame mein nahi mila -
                        # yfinance shayad us symbol ke liye data nahi laaya.
                        out[sym] = None
                        continue
                    out[sym] = _clean_single_ticker_df(sub)

            return out

        except Exception as e:
            # "empty response" ko bhi rate-limit jaisा treat karo - safest default
            is_probable_rate_limit = _is_rate_limit_error(e) or "empty response" in str(e)

            if is_probable_rate_limit and attempt < DOWNLOAD_RETRIES:
                wait = min(RATE_LIMIT_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), RATE_LIMIT_MAX_BACKOFF_SEC)
                wait += random.uniform(0, 3)  # thoda jitter
                logger.warning(
                    f"Rate limit lag gaya (attempt {attempt}/{DOWNLOAD_RETRIES}): {e}. "
                    f"{wait:.0f}s ruk raha hoon..."
                )
                time.sleep(wait)
            elif attempt < DOWNLOAD_RETRIES:
                logger.warning(f"Batch download error (attempt {attempt}/{DOWNLOAD_RETRIES}): {e}. Retry kar raha hoon...")
                time.sleep(RETRY_SLEEP_SEC)
            else:
                logger.warning(f"Batch download final fail: {e}. Is batch ke stocks skip ho rahe hain (primary se mile {len(resolved)} phir bhi safe hain).")
                return {**resolved, **{sym: None for sym in symbols}}

    return {**resolved, **{sym: None for sym in symbols}}


def download_all():
    """
    Return: dict { "RELIANCE.NS": DataFrame, ... }
    """
    all_symbols = _get_symbol_list()
    total = len(all_symbols)

    all_data = {}
    failed = []

    # ---- STEP 1: local cache se load karo ----
    still_needed = []
    for sym in all_symbols:
        cached_df = load_from_cache(sym)
        if cached_df is not None:
            all_data[sym] = cached_df
        else:
            still_needed.append(sym)

    if len(all_data) > 0:
        logger.info(f"Cache se {len(all_data)}/{total} stocks mil gaye (dubara download nahi honge)")

    # ---- STEP 2: resume state check karo ----
    completed_from_resume, pending = load_resume_state(still_needed)

    if not pending:
        logger.info("Sab stocks cache/resume se mil gaye, naya download zaroori nahi")
        clear_resume_state()
        return all_data

    chunks = list(_chunk(pending, CHUNK_SIZE))
    logger.info(
        f"{len(pending)} stocks download honge ({len(chunks)} batches of ~{CHUNK_SIZE}, "
        f"period={PERIOD}, interval={INTERVAL})"
    )

    completed_symbols = set(all_data.keys())
    consecutive_full_failures = 0

    for i, batch in enumerate(chunks, start=1):
        result = _download_chunk(batch)

        batch_success_count = sum(1 for df in result.values() if df is not None)

        for sym, df in result.items():
            if df is not None:
                all_data[sym] = df
                completed_symbols.add(sym)
                save_to_cache(sym, df)
            else:
                failed.append(sym)

        save_resume_state(completed_symbols)

        logger.info(f"Batch {i}/{len(chunks)} done | Progress: {len(all_data) + len(failed)}/{total}")

        # ---- CIRCUIT BREAKER: agar poora batch fail ho gaya (0 success),
        # to Yahoo shayad globally rate-limit kar raha hai - chhoti retry
        # se kaam nahi chalega, isliye LAMBI cooldown lagate hain taaki
        # rate-limit window reset ho sake ----
        if batch_success_count == 0 and len(batch) > 0:
            consecutive_full_failures += 1
            if consecutive_full_failures >= CIRCUIT_BREAKER_THRESHOLD:
                cooldown = CIRCUIT_BREAKER_COOLDOWN_SEC
                logger.warning(
                    f"{consecutive_full_failures} batches consecutively pura fail ho gaye - "
                    f"Yahoo shayad global rate-limit kar raha hai. {cooldown}s (long cooldown) "
                    f"ruk raha hoon taaki rate-limit window reset ho sake..."
                )
                time.sleep(cooldown)
                consecutive_full_failures = 0  # cooldown ke baad fresh start
        else:
            consecutive_full_failures = 0

        if i < len(chunks):
            delay = random.uniform(RANDOM_DELAY_MIN_SEC, RANDOM_DELAY_MAX_SEC)
            time.sleep(delay)

    logger.info(f"Download finished: {len(all_data)} success, {len(failed)} failed")
    if failed:
        logger.info(f"Failed symbols (sample): {failed[:10]}{'...' if len(failed) > 10 else ''}")

    clear_resume_state()
    return all_data


def download_benchmark():
    """
    Benchmark index (NIFTY 50, ^NSEI) ka data download karta hai.
    """
    cached = load_from_cache(BENCHMARK_SYMBOL)
    if cached is not None:
        return cached

    result = _download_chunk([BENCHMARK_SYMBOL])
    df = result.get(BENCHMARK_SYMBOL)
    if df is not None:
        save_to_cache(BENCHMARK_SYMBOL, df)
    else:
        logger.warning(f"Benchmark ({BENCHMARK_SYMBOL}) download nahi ho paya, Relative Strength skip hoga")
    return df