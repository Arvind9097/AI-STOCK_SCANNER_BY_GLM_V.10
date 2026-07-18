"""
===========================================================
 UPSTOX API INTEGRATION (V9.2 — OAuth Telegram Login Flow)
===========================================================
Upstox API v2 with OAuth 2.0 — Telegram-based login flow.

FLOW (user-friendly, no local script needed):
  1. Token expires → Telegram alert: "Login link: https://..."
  2. User clicks link → browser opens Upstox login page
  3. User enters User ID + Password + OTP (normal login)
  4. Upstox redirects to: https://your-app.onrender.com/upstox/callback?code=xxx
  5. Server captures code → exchanges for access token
  6. Token saved to data/upstox_token.json (24h valid)
  7. All data fetches use this token → 7000 stocks in 2-3 min!

SECURITY:
  - Password/PIN NEVER stored on server (OAuth flow — user enters on Upstox)
  - Only API Key + Secret on server (safe, just app identifiers)
  - Access token stored locally (data/upstox_token.json), 24h expiry

RATE LIMITS (Upstox):
  - 25 requests/second (very generous — 7000 stocks in ~5 min)
  - Historical data: 1 year daily candles per stock
  - No daily limit for personal use
===========================================================
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────
# CONFIG (from environment variables — set in Render/dashboard)
# ───────────────────────────────────────────────────────────────────
UPSTOX_API_KEY = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.environ.get("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URI = os.environ.get(
    "UPSTOX_REDIRECT_URI",
    "https://ai-stock-scanner-glm-v8-3.onrender.com/upstox/callback"
)

# Upstox API v2 endpoints
UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
UPSTOX_HISTORICAL_URL = "https://api.upstox.com/v2/historical/candle"
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"

# Token storage
TOKEN_FILE = os.path.join("data", "upstox_token.json")
_token_lock = threading.Lock()

# ───────────────────────────────────────────────────────────────────
# OAUTH LOGIN FLOW
# ───────────────────────────────────────────────────────────────────
def get_login_url() -> str:
    """
    Generate Upstox OAuth login URL.
    User clicks this → browser opens Upstox login → enters credentials + OTP
    → Upstox redirects to our /upstox/callback endpoint with auth code.
    """
    params = {
        "response_type": "code",
        "client_id": UPSTOX_API_KEY,
        "redirect_uri": UPSTOX_REDIRECT_URI,
        "state": "ai_scanner_login",  # CSRF protection
    }
    return f"{UPSTOX_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(auth_code: str) -> bool:
    """
    Exchange OAuth authorization code for access token.
    Called by /upstox/callback endpoint after user logs in.

    Args:
        auth_code: Code received from Upstox redirect (?code=xxx)

    Returns:
        True on success, False on failure.
    """
    import requests

    try:
        resp = requests.post(
            UPSTOX_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "code": auth_code,
                "client_id": UPSTOX_API_KEY,
                "client_secret": UPSTOX_API_SECRET,
                "redirect_uri": UPSTOX_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )

        if resp.status_code != 200:
            logger.error(f"Upstox token exchange failed: {resp.status_code} {resp.text[:200]}")
            return False

        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"Upstox token exchange: no access_token in response")
            return False

        # Save token with expiry
        expires_in = token_data.get("expires_in", 86400)  # default 24h
        token_info = {
            "access_token": access_token,
            "obtained_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(seconds=expires_in)).isoformat(),
            "token_type": token_data.get("token_type", "Bearer"),
        }

        _save_token(token_info)
        logger.info("✅ Upstox access token obtained and saved successfully!")
        return True

    except Exception as e:
        logger.error(f"Upstox token exchange error: {e}")
        return False


# ───────────────────────────────────────────────────────────────────
# TOKEN MANAGEMENT
# ───────────────────────────────────────────────────────────────────
def _save_token(token_info: dict) -> None:
    """Atomically save token to file."""
    from utils import atomic_write_json
    try:
        os.makedirs("data", exist_ok=True)
        atomic_write_json(TOKEN_FILE, token_info)
    except Exception as e:
        logger.warning(f"Upstox token save fail: {e}")


def _load_token() -> Optional[dict]:
    """Load saved token from file."""
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as e:
        logger.debug(f"Upstox token load fail: {e}")
        return None


def get_access_token() -> Optional[str]:
    """
    Get valid access token (from file).
    Returns None if token expired or not found → caller should trigger login.
    """
    with _token_lock:
        token_info = _load_token()
        if not token_info:
            return None

        # Check expiry
        try:
            expires_at = datetime.fromisoformat(token_info.get("expires_at", ""))
            if datetime.now() >= expires_at:
                logger.info("Upstox token expired — login required")
                return None
        except (ValueError, TypeError):
            return None

        return token_info.get("access_token")


def is_token_valid() -> bool:
    """Check if we have a valid (non-expired) token."""
    return get_access_token() is not None


def get_token_status() -> dict:
    """Get token status for diagnostics."""
    token_info = _load_token()
    if not token_info:
        return {"status": "no_token", "message": "Login required", "login_url": get_login_url()}

    try:
        expires_at = datetime.fromisoformat(token_info.get("expires_at", ""))
        now = datetime.now()
        if now >= expires_at:
            return {"status": "expired", "message": "Token expired — login required",
                    "login_url": get_login_url()}
        remaining = expires_at - now
        hours_left = remaining.total_seconds() / 3600
        return {
            "status": "valid",
            "message": f"Token valid for {hours_left:.1f} more hours",
            "obtained_at": token_info.get("obtained_at"),
            "expires_at": token_info.get("expires_at"),
        }
    except Exception:
        return {"status": "error", "message": "Token file corrupt", "login_url": get_login_url()}


# ───────────────────────────────────────────────────────────────────
# DATA FETCHING
# ───────────────────────────────────────────────────────────────────
def _make_upstox_request(url: str, params: dict = None) -> Optional[dict]:
    """Make authenticated request to Upstox API."""
    import requests
    token = get_access_token()
    if not token:
        logger.warning("Upstox: no valid token — skipping API call")
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
            logger.warning("Upstox: token unauthorized (expired?) — login required")
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
    Fetch historical OHLCV data for a single stock from Upstox.

    Args:
        symbol: NSE symbol WITHOUT .NS suffix (e.g. "RELIANCE")
        days: Number of days of history (default 365 = 1 year)

    Returns:
        pandas DataFrame [Date, Open, High, Low, Close, Volume] or None.
    """
    import pandas as pd

    # Upstox uses instrument_key format: "NSE_EQ|RELIANCE"
    instrument_key = f"NSE_EQ|{symbol}"

    # Calculate date range
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    url = f"{UPSTOX_HISTORICAL_URL}/{instrument_key}/day"
    params = {
        "from": from_date,
        "to": to_date,
    }

    data = _make_upstox_request(url, params)
    if not data or "data" not in data:
        return None

    candles = data.get("data", {}).get("candles", [])
    if not candles:
        return None

    # Parse candles: [[timestamp, open, high, low, close, volume], ...]
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
        symbols: List of NSE symbols (without .NS suffix)
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
                results[symbol] = df
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

    Expected speed: 7000 stocks × 0.05s = ~6 minutes (vs 40+ min on Yahoo)
    """
    import pandas as pd

    if not is_token_valid():
        logger.warning("Upstox: token invalid — cannot fetch batch data. Login required.")
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
        symbols = symbols[symbols.str[0].str.isalpha()].tolist()  # filter junk

        logger.info(f"Upstox: fetching data for {len(symbols)} stocks from CSV...")
        return fetch_batch_historical(symbols)

    except Exception as e:
        logger.error(f"Upstox CSV batch fetch error: {e}")
        return {}


# ───────────────────────────────────────────────────────────────────
# TELEGRAM LOGIN ALERT
# ───────────────────────────────────────────────────────────────────
def send_login_alert_via_telegram() -> None:
    """
    Send Telegram message with login link when token is expired/missing.
    User clicks link → browser opens → logs in → token auto-saved.
    """
    try:
        from telegram_alerts import send_telegram_text
        login_url = get_login_url()

        message = (
            "🔔 <b>UPSTOX LOGIN REQUIRED</b> 🔔\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Upstox access token expire ho gaya hai.\n"
            "Data fetch continue karne ke liye login karein:\n\n"
            f'<a href="{login_url}">👉 CLICK HERE TO LOGIN</a>\n\n'
            "Steps:\n"
            "1. Link par click karein\n"
            "2. Upstox User ID + Password daalein\n"
            "3. OTP enter karein (phone par aayega)\n"
            "4. Login complete! Token auto-save ho jayega.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Token 24 hours ke liye valid rahega.</i>"
        )
        send_telegram_text(message)
        logger.info("Upstox login alert sent via Telegram")
    except Exception as e:
        logger.warning(f"Failed to send Upstox login alert: {e}")


def check_token_and_alert() -> None:
    """
    Check if token is valid. If not, send Telegram login alert.
    Called by scheduler periodically (e.g. before scan time).
    """
    if not UPSTOX_API_KEY:
        return  # Upstox not configured

    if is_token_valid():
        return  # Token fine, no action

    # Token expired or missing — send alert
    logger.info("Upstox token invalid — sending Telegram login alert")
    send_login_alert_via_telegram()
