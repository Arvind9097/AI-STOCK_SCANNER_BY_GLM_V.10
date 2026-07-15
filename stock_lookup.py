"""
===========================================================
 ON-DEMAND STOCK LOOKUP (for Telegram bot free-text queries)
===========================================================
Jab user Telegram par koi stock symbol type karta hai (e.g. "TCS"
ya "RELIANCE"), ye module us stock ka turant snapshot deta hai -
pehle aaj ka cached data try karta hai (fast), warna V8.1.2 ke
primary/secondary chain (NSE Chart API -> Stooq -> yfinance) se
fresh single-stock download karta hai.

V8.3.1 FIX (Render hosting): Render ke server IP par NSE sources
(NSE Chart / nse-package / jugaad-data) frequently block ho jaate
hain, aur yfinance bhi shared-IP rate-limit lagta deta hai. Pehle
bot-lookup bhi poora multi-source chain try karta tha — isse 5-10s
lag jaate the aur sab sources fail ho jaate the. Ab:
  1. In-memory cache (5 min TTL) — repeated queries instant.
  2. Direct yfinance Ticker fast-path (single call, retry with
     backoff) pehle try hota hai — bot reply ke liye yeh kaafi hai
     (sirf last 6 month OHLCV chahiye, full 1y nahi).
  3. Agar yfinance bhi rate-limit de, tab full multi-source chain
     fallback (NSE Chart -> Stooq -> yfinance) try hota hai.
===========================================================
"""

import re
import time
import threading

from cache import load_from_cache
from indicators import add_indicators
from news import format_news_text
from utils import escape_html
from market_data_fetcher import fetch_daily_ohlcv
from logger import logger

# V8.1.2 BUG FIX: NSE cash-equity symbols sirf LETTERS, NUMBERS, aur
# "&" (jaise M&M, L&T) use karte hain - koi space, hyphen, ya doosre
# special-characters nahi (hyphen sirf futures-contract naming mein
# hota hai, jaise NIFTY-I - jo is bot ka scope nahi hai). Ye pattern
# "Start", "--scan" jaise Telegram-command-jaisा text ko GALTI SE
# NSE symbol samajh kar poori data-fetch chain (NSE Chart/Stooq/
# jugaad-data/yfinance) trigger karne se rokta hai.
_VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9&]{1,20}$")

# V8.3.1: bot-lookup in-memory cache (symbol -> (timestamp, df)).
# 5-min TTL — taaki ek hi stock dobara query hone par instant reply
# ho, aur yfinance rate-limit na lage. Thread-safe (bot_listener +
# breaking-news dono thread se call ho sakta hai).
_LOOKUP_CACHE = {}
_LOOKUP_CACHE_TTL = 300  # 5 minutes
_LOOKUP_CACHE_LOCK = threading.Lock()


def _yfinance_fast_path(symbol, period="6mo"):
    """
    V8.3.2: Bot quick-lookup ke liye Yahoo Finance DIRECT API use
    karta hai (yfinance library ki jagah). Browser User-Agent header
    ke saath cloud IPs par reliably kaam karta hai — NSE-block aur
    yfinance-rate-limit dono se bachata hai. query2.finance.yahoo.com
    fallback host ke saath.

    Return: DataFrame ya None.
    """
    try:
        from market_data_fetcher import _fetch_yahoo_direct
        df = _fetch_yahoo_direct(symbol, period=period)
        if df is not None and not df.empty:
            return df
        # Fallback: yfinance library (slower, but sometimes works)
        import yfinance as yf
        for attempt in range(2):
            try:
                raw = yf.download(symbol, period=period, interval="1d",
                                  auto_adjust=True, progress=False)
                if raw is not None and not raw.empty:
                    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
                        raw.columns = raw.columns.get_level_values(0)
                    df = raw.reset_index()
                    first_col = df.columns[0]
                    if first_col != "Date":
                        df = df.rename(columns={first_col: "Date"})
                    required = {"Close", "Open", "High", "Low", "Volume"}
                    if required.issubset(set(df.columns)):
                        return df
                logger.debug(f"{symbol}: yfinance lib empty (attempt {attempt+1})")
                time.sleep(3)
            except Exception as e:
                logger.debug(f"{symbol}: yfinance lib attempt {attempt+1} fail ({e})")
                time.sleep(3)
        return None
    except Exception as e:
        logger.debug(f"{symbol}: fast-path setup fail ({e})")
        return None


def _fetch_with_cache(symbol):
    """
    V8.3.1: Cache-aware fetch. Pehle in-memory cache check (5 min TTL),
    phir yfinance fast-path, phir full multi-source chain fallback.
    """
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        cached = _LOOKUP_CACHE.get(symbol)
        if cached and (now - cached[0]) < _LOOKUP_CACHE_TTL:
            logger.debug(f"{symbol}: bot-lookup cache HIT")
            return cached[1]

    # 1. Aaj ka downloaded-data cache (morning scan ne save kiya hoga)
    df = load_from_cache(symbol)

    # 2. yfinance fast-path (Render par most reliable for single-stock)
    if df is None or df.empty:
        df = _yfinance_fast_path(symbol, period="6mo")

    # 3. Full multi-source chain fallback (NSE Chart -> Stooq -> yfinance)
    if df is None or df.empty:
        try:
            df = fetch_daily_ohlcv(symbol, period="6mo")
        except Exception as e:
            logger.warning(f"{symbol}: full chain fetch fail ({e})")

    # Cache mein save (chahe None ho — 5 min tak dobara try na karna pade)
    with _LOOKUP_CACHE_LOCK:
        _LOOKUP_CACHE[symbol] = (now, df)
        # Cap cache size (100 entries — LRU-style)
        if len(_LOOKUP_CACHE) > 100:
            oldest = min(_LOOKUP_CACHE, key=lambda k: _LOOKUP_CACHE[k][0])
            _LOOKUP_CACHE.pop(oldest, None)

    return df


def _normalize_symbol(text):
    """
    User ke free-text input ko valid NSE symbol mein convert karta hai.
    Return: valid symbol (e.g. "TCS.NS", "^NSEI") ya None (invalid input).

    V8.2.0 FIX: Pehle `if core.startswith("^")` wale branch mein ek
    NO-OP ternary tha (`return text if ... else f"{text}"` - dono
    branches same `text` return karte the). Ab sahi logic hai: `^`
    prefix wale index symbols ke liye `.NS` suffix strip kar dete hain
    (yfinance `^NSEI` chahata hai, `^NSEI.NS` nahi).
    """
    if not isinstance(text, str):
        return None
    text = text.strip().upper()
    if not text:
        return None

    # `.NS` suffix strip karke asli symbol-part validate karo
    if text.endswith(".NS"):
        core = text[:-3]
    else:
        core = text

    # Index symbols (^NSEI, ^NSEBANK) - .NS strip karke return karo
    if core.startswith("^"):
        return core

    if not _VALID_SYMBOL_PATTERN.match(core):
        return None  # "Start", "--scan" jaise galat-format text yahan reject ho jaate hain

    return f"{core}.NS"


def get_stock_snapshot(user_text):
    """
    user_text: jo bhi user ne Telegram par likha (e.g. "tcs", "TCS.NS")
    Return: formatted plain-text snapshot (Telegram HTML safe - no < > & in content expected from numbers)
    """
    symbol = _normalize_symbol(user_text)
    if not symbol:
        return "Stock symbol samajh nahi aaya. Example: TCS ya RELIANCE likh kar bhejo."

    # V8.3.1: cache-aware fetch (5-min TTL + yfinance fast-path + chain fallback)
    df = _fetch_with_cache(symbol)

    if df is None or df.empty:
        return (
            f"{symbol.replace('.NS', '')} ka data abhi fetch nahi ho paya "
            f"(server IP rate-limit ya network issue). Thodi der baad try karo."
        )

    if df is None or len(df) < 30:
        return f"{symbol.replace('.NS', '')} ke liye kaafi data available nahi hai."

    try:
        df_ind = add_indicators(df.copy())
        # V8.2.0: dropna Close pe - agar add_indicators kisi row mein NaN
        # dhar diya (jaise EMA warm-up period ke liye initial rows) to
        # last row NaN ho sakti hai. Safe .iloc[-1] ke liye dropna zaroori.
        if df_ind is None or df_ind.empty:
            return f"{symbol.replace('.NS', '')} ka analysis calculate nahi ho paya."
        last = df_ind.dropna(subset=["Close"]).iloc[-1]
    except Exception as e:
        logger.warning(f"{symbol}: indicator calc fail ({e})")
        return f"{symbol.replace('.NS', '')} ka analysis calculate nahi ho paya."

    display_name = escape_html(symbol.replace(".NS", ""))
    close = float(last["Close"])
    rsi = float(last["RSI"]) if "RSI" in last and last["RSI"] == last["RSI"] else None
    ema20 = float(last["EMA20"]) if "EMA20" in last and last["EMA20"] == last["EMA20"] else None
    ema50 = float(last["EMA50"]) if "EMA50" in last and last["EMA50"] == last["EMA50"] else None

    trend = "pata nahi"
    if ema20 and ema50:
        trend = "Uptrend (Close EMA20/50 se upar)" if close > ema20 > ema50 else \
                "Downtrend (Close EMA20/50 se neeche)" if close < ema20 < ema50 else "Mixed/Sideways"

    lines = [
        f"{display_name} - Quick Snapshot",
        f"CMP: Rs.{close:.2f}",
        f"Trend: {trend}",
    ]
    if rsi is not None:
        lines.append(f"RSI: {rsi:.1f}")
    if ema20 is not None:
        lines.append(f"EMA20: Rs.{ema20:.2f}")

    lines.append("")
    lines.append("Latest News:")
    lines.append(format_news_text(symbol, limit=2))

    return "\n".join(lines)
