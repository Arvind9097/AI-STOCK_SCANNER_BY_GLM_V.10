"""
===========================================================
 UPSTOX ANALYTICS API (V9.6 — Long-lived Token, No Daily Login)
===========================================================
Upstox Analytics Token: 1 साल valid, read-only, कोई OAuth/login नहीं!

यह OAuth flow को पूरी तरह हटाता है। बस token environment variable
में डालो, 1 साल तक बिना re-login के data मिलेगा।

API Features:
  - Historical daily candles (1 year+)
  - Historical intraday candles (1min, 5min, 15min, 1hour)
  - Live quotes (LTP)
  - Symbol master (all NSE/BSE stocks)
  - Read-only (safe — कोई accidental trades)

Rate Limit: 25 requests/second (very generous)

Setup:
  Render Environment → UPSTOX_ANALYTICS_TOKEN = <your-token>
  बस! कोई redirect URI, OAuth, daily login नहीं।
===========================================================
"""

import os
import json
import time
import logging
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────
# CONFIG (from environment variables)
# ───────────────────────────────────────────────────────────────────
# V9.6: Analytics Token (1 year valid, no OAuth needed)
UPSTOX_ANALYTICS_TOKEN = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "")

# Keep old OAuth vars for backward compat (but analytics token takes priority)
UPSTOX_API_KEY = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.environ.get("UPSTOX_API_SECRET", "")

# Upstox API v2 endpoints
UPSTOX_BASE = "https://api.upstox.com/v2"

# 🔥 FIX: URL MUST be historical-candle (with dash, no slash)
UPSTOX_HISTORICAL_URL = f"{UPSTOX_BASE}/historical-candle"
UPSTOX_QUOTE_URL = f"{UPSTOX_BASE}/market-quote/ltp"
UPSTOX_PROFILE_URL = f"{UPSTOX_BASE}/user/profile"

# Symbol master file URL (all NSE/BSE instruments)
UPSTOX_SYMBOL_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

# Token storage (for OAuth fallback — analytics token doesn't need file)
TOKEN_FILE = os.path.join("data", "upstox_token.json")
_token_lock = threading.Lock()

# Symbol master cache (loaded once per process)
_symbol_master_cache = None
_symbol_master_lock = threading.Lock()


# ───────────────────────────────────────────────────────────────────
# TOKEN MANAGEMENT (Analytics Token — no OAuth needed!)
# ───────────────────────────────────────────────────────────────────
def is_analytics_token_available() -> bool:
    """Check if Analytics Token is configured (1-year valid, no login needed)."""
    return bool(UPSTOX_ANALYTICS_TOKEN)


def get_access_token() -> Optional[str]:
    """
    Get valid access token for API calls.

    V9.6 PRIORITY:
      1. Analytics Token (1 year valid, no login) — BEST
      2. OAuth access token (24h, from file) — FALLBACK (old method)

    Returns token string or None.
    """
    if UPSTOX_ANALYTICS_TOKEN:
        return UPSTOX_ANALYTICS_TOKEN

    with _token_lock:
        try:
            with open(TOKEN_FILE, "r") as f:
                token_info = json.load(f)
            expires_at = datetime.fromisoformat(token_info.get("expires_at", ""))
            if datetime.now() < expires_at:
                return token_info.get("access_token")
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            pass

    return None


def is_token_valid() -> bool:
    """Check if we have a valid token (analytics or OAuth)."""
    return get_access_token() is not None


def get_token_status() -> dict:
    """Get token status for diagnostics."""
    if UPSTOX_ANALYTICS_TOKEN:
        return {
            "status": "valid",
            "type": "analytics",
            "message": "Analytics Token active (1 year valid, no daily login needed)",
        }

    try:
        with open(TOKEN_FILE, "r") as f:
            token_info = json.load(f)
        expires_at = datetime.fromisoformat(token_info.get("expires_at", ""))
        if datetime.now() < expires_at:
            remaining = expires_at - datetime.now()
            hours_left = remaining.total_seconds() / 3600
            return {
                "status": "valid",
                "type": "oauth",
                "message": f"OAuth token valid for {hours_left:.1f} more hours",
                "expires_at": token_info.get("expires_at"),
            }
        else:
            return {
                "status": "expired",
                "type": "oauth",
                "message": "OAuth token expired. Set UPSTOX_ANALYTICS_TOKEN for 1-year access.",
            }
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {
            "status": "no_token",
            "type": "none",
            "message": "No token. Set UPSTOX_ANALYTICS_TOKEN env var.",
        }


# ───────────────────────────────────────────────────────────────────
# SYMBOL MASTER (all NSE/BSE instruments)
# ───────────────────────────────────────────────────────────────────
def _load_symbol_master() -> Optional[List[dict]]:
    """Download + cache Upstox symbol master file."""
    global _symbol_master_cache

    if _symbol_master_cache is not None:
        return _symbol_master_cache

    with _symbol_master_lock:
        if _symbol_master_cache is not None:
            return _symbol_master_cache

        try:
            import requests
            resp = requests.get(UPSTOX_SYMBOL_MASTER_URL, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"Symbol master download failed: {resp.status_code}")
                return None

            import gzip
            data = gzip.decompress(resp.content)
            instruments = json.loads(data)

            equities = [i for i in instruments if i.get("exchange") == "NSE_EQ" and i.get("segment") == "EQ"]
            _symbol_master_cache = equities
            return equities

        except Exception as e:
            logger.warning(f"Symbol master load fail: {e}")
            return None


def _get_instrument_key(symbol: str) -> Optional[str]:
    """Get Upstox instrument_key for a symbol."""
    master = _load_symbol_master()
    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    
    if not master:
        return f"NSE_EQ|{clean}"

    for inst in master:
        if inst.get("tradingsymbol", "").upper() == clean:
            return inst.get("instrument_key")

    return f"NSE_EQ|{clean}"


# ───────────────────────────────────────────────────────────────────
# DATA FETCHING
# ───────────────────────────────────────────────────────────────────
def _make_upstox_request(url: str, params: dict = None) -> Optional[dict]:
    """Make authenticated request to Upstox API using Analytics Token."""
    import requests

    token = get_access_token()
    if not token:
        return None

    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Api-Version": "2.0" # 🔥 MUST INCLUDE API VERSION
            },
            params=params,
            timeout=15,
        )

        if resp.status_code == 401:
            logger.warning("Upstox: token unauthorized (expired?).")
            return None
        if resp.status_code == 429:
            time.sleep(1)
            return None
        if resp.status_code != 200:
            # 🔥 CHANGED TO WARNING TO SEE EXACT UPSTOX ERROR IN LOGS
            logger.warning(f"Upstox API Error [{resp.status_code}]: {resp.text[:300]}")
            return None

        return resp.json()
    except Exception as e:
        logger.warning(f"Upstox API Request Exception: {e}")
        return None


def fetch_historical_data(symbol: str, days: int = 365) -> Optional[Any]:
    import pandas as pd

    instrument_key = _get_instrument_key(symbol)
    if not instrument_key:
        return None

    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # 🔥 SAFE ENCODING FOR URL
    encoded_key = urllib.parse.quote(instrument_key)
    
    url = f"{UPSTOX_HISTORICAL_URL}/{encoded_key}/day/{to_date}/{from_date}"

    data = _make_upstox_request(url)
    if not data or "data" not in data or not data.get("data"):
        return None

    candles = data.get("data", {}).get("candles", [])
    if not candles:
        return None

    rows = []
    for candle in candles:
        if len(candle) >= 6:
            rows.append({
                "Date": pd.to_datetime(candle[0]).tz_localize(None),
                "Open": float(candle[1]),
                "High": float(candle[2]),
                "Low": float(candle[3]),
                "Close": float(candle[4]),
                "Volume": int(candle[5]) if candle[5] else 0,
            })

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.sort_values("Date").reset_index(drop=True)
    return df


def fetch_batch_historical(symbols: List[str], days: int = 365) -> Dict[str, Any]:
    import time
    results = {}
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        try:
            df = fetch_historical_data(symbol, days)
            if df is not None and not df.empty:
                clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
                results[f"{clean_sym}.NS"] = df
        except Exception as e:
            pass

        time.sleep(0.05)

    return results

def fetch_all_stocks_from_csv(csv_path: str = "data/nse_all_stocks.csv") -> Dict[str, Any]:
    import pandas as pd
    if not is_token_valid():
        return {}

    try:
        df = pd.read_csv(csv_path)
        sym_col = None
        for col in df.columns:
            if col.strip().lower() in ("symbol", "symbols", "ticker"):
                sym_col = col
                break
        if sym_col is None:
            sym_col = df.columns[0]

        symbols = df[sym_col].dropna().astype(str).str.strip().str.upper()
        symbols = symbols.str.replace(".NS", "").str.replace(".BO", "")
        symbols = symbols[symbols.str[0].str.isalpha()].tolist()

        return fetch_batch_historical(symbols)
    except Exception as e:
        return {}

def get_live_price(symbol: str) -> Optional[float]:
    instrument_key = _get_instrument_key(symbol)
    if not instrument_key:
        return None

    params = {"instrument_key": instrument_key}
    data = _make_upstox_request(UPSTOX_QUOTE_URL, params)
    
    if not data:
        return None

    quote_data = data.get("data", {})
    for key, val in quote_data.items():
        return val.get("last_price")

    return None

# Legacy functions logic skipped here for brevity but keep them as-is if you use OAuth.
```eof

**Action Plan:**
1. `config.py` mein jakar `AI_MODE = "RULE_BASED"` kar dein (Taaki "Insufficient Balance" ka error solve ho jaye).
2. Upar di gayi `upstox_fetcher.py` ko update karein.
3. Deploy karein! Agar iske baad Upstox fail hota hai toh Logs mein likha aayega ki "Upstox API Error [400]..." jisse humein exact problem pata chal jayegi!
