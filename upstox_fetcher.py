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
UPSTOX_HISTORICAL_URL = f"{UPSTOX_BASE}/historical/candle"
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
    # Priority 1: Analytics Token (no expiry check needed — 1 year valid)
    if UPSTOX_ANALYTICS_TOKEN:
        return UPSTOX_ANALYTICS_TOKEN

    # Priority 2: OAuth token (old method — 24h, needs daily login)
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

    # Check OAuth token (old method)
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
                "message": "OAuth token expired. Set UPSTOX_ANALYTICS_TOKEN for 1-year access (no daily login).",
            }
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {
            "status": "no_token",
            "type": "none",
            "message": "No token. Set UPSTOX_ANALYTICS_TOKEN env var (1 year valid, no login needed).",
        }


# ───────────────────────────────────────────────────────────────────
# SYMBOL MASTER (all NSE/BSE instruments)
# ───────────────────────────────────────────────────────────────────
def _load_symbol_master() -> Optional[List[dict]]:
    """
    Download + cache Upstox symbol master file.
    Contains ALL NSE/BSE listed instruments with their instrument_keys.

    Returns list of instrument dicts, or None on failure.
    """
    global _symbol_master_cache

    if _symbol_master_cache is not None:
        return _symbol_master_cache

    with _symbol_master_lock:
        if _symbol_master_cache is not None:
            return _symbol_master_cache

        try:
            import requests
            logger.info("Downloading Upstox symbol master file...")
            resp = requests.get(UPSTOX_SYMBOL_MASTER_URL, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"Symbol master download failed: {resp.status_code}")
                return None

            # Decompress gzip
            import gzip
            data = gzip.decompress(resp.content)
            instruments = json.loads(data)

            # Filter: only NSE_EQ (equities, not F&O/debt)
            equities = [i for i in instruments if i.get("exchange") == "NSE_EQ"
                        and i.get("segment") == "EQ"]
            logger.info(f"Symbol master loaded: {len(equities)} NSE equities")
            _symbol_master_cache = equities
            return equities

        except Exception as e:
            logger.warning(f"Symbol master load fail: {e}")
            return None


def _get_instrument_key(symbol: str) -> Optional[str]:
    """
    Get Upstox instrument_key for a symbol.
    Example: "RELIANCE" → "NSE_EQ|RELIANCE"

    Returns instrument_key or None if not found.
    """
    master = _load_symbol_master()
    if not master:
        # Fallback: construct key directly (works for most NSE equities)
        clean = symbol.replace(".NS", "").replace(".BO", "").upper()
        return f"NSE_EQ|{clean}"

    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    for inst in master:
        if inst.get("tradingsymbol", "").upper() == clean:
            return inst.get("instrument_key")

    # Fallback
    return f"NSE_EQ|{clean}"


# ───────────────────────────────────────────────────────────────────
# DATA FETCHING
# ───────────────────────────────────────────────────────────────────
def _make_upstox_request(url: str, params: dict = None) -> Optional[dict]:
    """Make authenticated request to Upstox API using Analytics Token."""
    import requests

    token = get_access_token()
    if not token:
        logger.debug("Upstox: no valid token — skipping API call")
        return None

    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=15,
        )

        if resp.status_code == 401:
            logger.warning("Upstox: token unauthorized (expired?) — check UPSTOX_ANALYTICS_TOKEN")
            return None
        if resp.status_code == 429:
            logger.debug("Upstox: rate limited (429) — backing off")
            time.sleep(1)
            return None
        if resp.status_code != 200:
            logger.debug(f"Upstox API {resp.status_code}: {resp.text[:200]}")
            return None

        return resp.json()
    except Exception as e:
        logger.debug(f"Upstox API error: {e}")
        return None


def fetch_historical_data(symbol: str, days: int = 365) -> Optional[Any]:
    """
    Fetch historical OHLCV daily candles for a stock from Upstox.

    Args:
        symbol: NSE symbol (e.g. "RELIANCE" or "RELIANCE.NS")
        days: Number of days of history (default 365)

    Returns:
        pandas DataFrame [Date, Open, High, Low, Close, Volume] or None.
    """
    import pandas as pd

    instrument_key = _get_instrument_key(symbol)
    if not instrument_key:
        return None

    # Upstox historical candle API:
    # GET /v2/historical/candle/{instrument_key}/day/{to_date}/{from_date}
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    url = f"{UPSTOX_HISTORICAL_URL}/{instrument_key}/day/{to_date}/{from_date}"

    data = _make_upstox_request(url)
    if not data or "data" not in data:
        return None

    candles = data.get("data", {}).get("candles", [])
    if not candles:
        return None

    # Parse candles: [[timestamp, open, high, low, close, volume, ...], ...]
    rows = []
    for candle in candles:
        if len(candle) >= 6:
            rows.append({
                "Date": pd.to_datetime(candle[0]),
                "Open": float(candle[1]),
                "High": float(candle[2]),
                "Low": float(candle[3]),
                "Close": float(candle[4]),
                "Volume": int(candle[5]) if candle[5] else 0,
            })

    if not rows:
        return None

    df = pd.DataFrame(rows)
    return df


def fetch_batch_historical(symbols: List[str], days: int = 365) -> Dict[str, Any]:
    """
    Fetch historical data for multiple stocks (sequential, 25 req/sec limit).

    Args:
        symbols: List of NSE symbols (with or without .NS suffix)
        days: History days

    Returns:
        Dict {symbol: DataFrame} for successful fetches.
    """
    import time

    results = {}
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        try:
            df = fetch_historical_data(symbol, days)
            if df is not None and not df.empty:
                # Normalize symbol (add .NS if not present)
                clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
                results[f"{clean_sym}.NS"] = df
        except Exception as e:
            logger.debug(f"Upstox fetch {symbol}: {e}")

        # Progress log
        if (i + 1) % 100 == 0 or (i + 1) == total:
            logger.info(f"Upstox batch download: {i+1}/{total} ({len(results)} success)")

        # Rate limit: 25 req/sec → 0.04s delay (safe margin: 0.05s)
        time.sleep(0.05)

    return results


def fetch_all_stocks_from_csv(csv_path: str = "data/nse_all_stocks.csv") -> Dict[str, Any]:
    """
    Fetch ALL stocks listed in the user's CSV file from Upstox.

    This is the MAIN entry point for bulk data download.
    1. Reads symbols from CSV
    2. Fetches historical data for each via Upstox API
    3. Returns dict {symbol: DataFrame}

    Expected speed: 7000 stocks × 0.05s = ~6 minutes (1-year token, no login!)
    """
    import pandas as pd

    if not is_token_valid():
        logger.warning("Upstox: token invalid — cannot fetch batch data.")
        return {}

    # Load symbols from CSV
    try:
        df = pd.read_csv(csv_path)

        # Find Symbol column
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

        logger.info(f"Upstox: fetching data for {len(symbols)} stocks from CSV...")
        return fetch_batch_historical(symbols)

    except Exception as e:
        logger.error(f"Upstox CSV batch fetch error: {e}")
        return {}


def get_live_price(symbol: str) -> Optional[float]:
    """
    Get current market price (LTP) for a stock.

    Args:
        symbol: NSE symbol (e.g. "RELIANCE")

    Returns:
        Last traded price (float) or None.
    """
    instrument_key = _get_instrument_key(symbol)
    if not instrument_key:
        return None

    url = f"{UPSTOX_QUOTE_URL}"
    params = {"instrument_key": instrument_key}

    data = _make_upstox_request(url, params)
    if not data:
        return None

    # Response: {"data": {"NSE_EQ|RELIANCE": {"last_price": 2950.5, ...}}}
    quote_data = data.get("data", {})
    for key, val in quote_data.items():
        return val.get("last_price")

    return None


# ───────────────────────────────────────────────────────────────────
# LEGACY OAuth SUPPORT (backward compat — analytics token takes priority)
# ───────────────────────────────────────────────────────────────────
def get_login_url() -> str:
    """OAuth login URL (only used if analytics token not set)."""
    params = {
        "response_type": "code",
        "client_id": UPSTOX_API_KEY,
        "redirect_uri": os.environ.get("UPSTOX_REDIRECT_URI", ""),
        "state": "ai_scanner_login",
    }
    from urllib.parse import urlencode
    return f"{UPSTOX_BASE}/login/authorization/dialog?{urlencode(params)}"


def exchange_code_for_token(auth_code: str) -> bool:
    """OAuth code exchange (only used if analytics token not set)."""
    import requests

    try:
        resp = requests.post(
            f"{UPSTOX_BASE}/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": auth_code,
                "client_id": UPSTOX_API_KEY,
                "client_secret": UPSTOX_API_SECRET,
                "redirect_uri": os.environ.get("UPSTOX_REDIRECT_URI", ""),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )

        if resp.status_code != 200:
            return False

        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return False

        expires_in = token_data.get("expires_in", 86400)
        token_info = {
            "access_token": access_token,
            "obtained_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(seconds=expires_in)).isoformat(),
        }

        from utils import atomic_write_json
        os.makedirs("data", exist_ok=True)
        atomic_write_json(TOKEN_FILE, token_info)
        logger.info("✅ Upstox OAuth token saved (24h valid)")
        return True

    except Exception as e:
        logger.error(f"Upstox OAuth token exchange error: {e}")
        return False


def check_token_and_alert() -> None:
    """
    Check if token is valid. If not, send Telegram login alert.
    V9.6: Analytics token = no alert needed (1 year valid).
    Only alerts if using old OAuth method.
    """
    # Analytics token — no alert needed (1 year valid)
    if UPSTOX_ANALYTICS_TOKEN:
        return

    # OAuth token — check + alert if expired
    if not UPSTOX_API_KEY:
        return

    if is_token_valid():
        return

    try:
        from telegram_alerts import send_telegram_text
        login_url = get_login_url()
        message = (
            "🔔 <b>UPSTOX LOGIN REQUIRED</b> 🔔\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "OAuth token expire ho gaya hai.\n"
            f'<a href="{login_url}">👉 CLICK HERE TO LOGIN</a>\n\n'
            "<i>Tip: UPSTOX_ANALYTICS_TOKEN set karo to 1 saal tak login nahi karna padega!</i>"
        )
        send_telegram_text(message)
        logger.info("Upstox login alert sent (OAuth token expired)")
    except Exception as e:
        logger.warning(f"Failed to send Upstox login alert: {e}")
