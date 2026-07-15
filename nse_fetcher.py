"""
===========================================================
 NSE DATA FETCHER — Robust Anti-403 Session Management (V9.2)
===========================================================
Deep reasoning for NSE India 403 Forbidden:

ROOT CAUSE ANALYSIS (NSE 403):
  NSE India employs MULTIPLE anti-scraping mechanisms:
    1. COOKIE-BASED SESSION VALIDATION:
       NSE requires valid session cookies (nsit, bm_sv, ak_bmsc).
       These are set by the homepage response headers. Without them,
       API calls return 403. Standard requests.get() has no cookies.
    2. USER-AGENT CHECKING:
       Default `python-requests/2.x` UA is immediately flagged as bot.
       Must use realistic browser UA (Chrome/Firefox on real OS).
    3. REFERER CHECKING:
       API calls without Referer from nseindia.com are blocked.
       Direct API hits (no navigation context) look like bots.
    4. HEADER CONSISTENCY (modern bot detection):
       Real Chrome sends Sec-Ch-Ua, Sec-Fetch-Dest/Mode/Site/User.
       Missing/inconsistent these headers flag automated requests.
    5. IP-BASED RATE LIMITING:
       Cloud IPs (AWS/Render/GCP) sometimes blocked entirely.
       Persistent 403 from same IP = permanent block for that session.

SOLUTION ARCHITECTURE (NSEDataFetcher class):
  1. requests.Session() for COOKIE PERSISTENCE across all API calls.
     Session cookies set once (via homepage visit), reused for all
     subsequent API calls — exactly like a real browser tab.
  2. HOMEPAGE HANDSHAKE before first API call:
     GET https://www.nseindia.com/ -> captures cookies in session.
     Cookies persist for the session's lifetime.
  3. DYNAMIC BROWSER HEADERS:
     Full Chrome header set (UA, Accept, Accept-Language, Accept-Encoding,
     Sec-Ch-Ua, Sec-Fetch-*, Connection, Upgrade-Insecure-Requests).
     UA ROTATION across 4 real browser UAs (defeats fingerprinting —
     same UA every time = detectable pattern).
  4. REFERER INJECTION:
     All API calls include Referer: https://www.nseindia.com/ — looks
     like internal navigation, not direct bot hit.
  5. 403 RETRY with UA ROTATION:
     If homepage or API returns 403, retry up to 3 times with a
     DIFFERENT UA + random delay (2-5s). NSE sometimes transient-blocks.
  6. COOKIE EXPIRY DETECTION:
     If API returns 401/403 mid-session (cookies expired), invalidate
     session and refresh cookies on next call.
  7. PERSISTENT BLOCK DETECTION:
     If all retries fail, set `is_blocked=True` flag. Callers check
     this and use fallback (yfinance/jugaad-data) — no point retrying
     a permanently-blocked IP.
  8. FALLBACK CHAIN (fetch_with_fallback method):
     NSE direct -> nsepython library -> jugaad-data library -> yfinance.
     Each fallback tried only if previous fails. yfinance is the most
     reliable on cloud IPs (Yahoo doesn't block by IP like NSE does).

EDGE CASES HANDLED:
  - Connection timeout -> retry, then fallback
  - Homepage returns 5xx -> retry with different UA
  - API returns empty JSON ({}) -> treat as failure, fallback
  - Cookies set but expired mid-session -> auto-refresh on 401/403
  - Multiple threads calling simultaneously -> session lock prevents
    race condition (double homepage handshake)
  - nsepython/jugaad-data not installed -> skip gracefully to yfinance
  - yfinance rate-limit -> caller handles via separate retry logic
===========================================================
"""

import time
import random
import threading
import logging
from typing import Optional, Dict, Any, List, Union
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────
# CONSTANTS
# ───────────────────────────────────────────────────────────────────
NSE_HOME_URL = "https://www.nseindia.com"
NSE_API_BASE = "https://www.nseindia.com/api"

# Session cookie refresh interval. NSE cookies typically valid 5-10 min,
# but we refresh proactively at 4 min to avoid mid-request expiry.
SESSION_TTL_SECONDS = 240  # 4 minutes

# 403 retry config — NSE sometimes transient-blocks; retry with fresh UA.
MAX_403_RETRIES = 3
RETRY_DELAY_RANGE = (2.0, 5.0)  # random delay between 403 retries (sec)

# 4 real browser User-Agents for rotation. Using the SAME UA every time
# creates a fingerprint pattern NSE can detect. Rotation defeats this.
_USER_AGENTS = [
    # Chrome on Windows (most common browser config)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Chrome on Linux (Render runs Linux — this UA is most "native")
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Full Chrome header set for HOMEPAGE visit. Real Chrome sends ALL of these.
# NSE bot detection checks for presence + consistency of Sec-* headers.
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# API call headers — slightly different (JSON accept, X-Requested-With for AJAX).
_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class NSEDataFetcher:
    """
    Robust NSE India data fetcher with anti-403 session management.

    SINGLE responsibility: fetch JSON data from NSE India APIs without
    getting blocked. Handles cookie persistence, header rotation, 403
    retries, and fallback to alternative data sources.

    THREAD-SAFETY: All public methods are thread-safe. Internal session
    access is guarded by a lock to prevent race conditions when multiple
    scanner threads call simultaneously (e.g. during a 500-stock scan).

    USAGE:
        fetcher = NSEDataFetcher()
        # FII/DII activity
        fiidii = fetcher.fetch_json("/fiidiiTradeReact")
        # Market status (GIFT Nifty)
        status = fetcher.fetch_json("/marketStatus")
        # With fallback (stock data)
        df = fetcher.fetch_with_fallback("RELIANCE", period="1y")
    """

    def __init__(self, timeout: int = 15):
        """
        Initialize the NSE data fetcher.

        Args:
            timeout: Request timeout in seconds (default 15). NSE is usually
                     fast, but homepage can be slow under load.
        """
        self.timeout = timeout
        self._session: Optional[requests.Session] = None
        self._session_created_at: float = 0.0
        self._lock = threading.Lock()
        self._is_blocked: bool = False  # persistent block flag

        # Optional fallback libraries — lazily imported to avoid hard deps.
        self._nsepython = None  # cached nsepython module
        self._jugaad_data = None  # cached jugaad_data module
        self._yfinance = None  # cached yfinance module
        self._fallback_libs_checked = False

    # ───────────────────────────────────────────────────────────────
    # PUBLIC PROPERTIES
    # ───────────────────────────────────────────────────────────────
    @property
    def is_blocked(self) -> bool:
        """
        Returns True if NSE has persistently blocked us (all retries failed).
        Callers should check this and use fallback data sources instead of
        wasting time retrying a permanently-blocked IP.
        """
        return self._is_blocked

    @property
    def session_age(self) -> float:
        """Age of current session in seconds (for diagnostics)."""
        if self._session is None:
            return 0.0
        return time.time() - self._session_created_at

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Browser header construction (with UA rotation)
    # ───────────────────────────────────────────────────────────────
    def _build_browser_headers(self) -> Dict[str, str]:
        """
        Build dynamic browser headers for homepage visit.
        Includes a RANDOMLY-SELECTED User-Agent (rotation defeats fingerprinting).
        Sets Referer/Origin to NSE homepage.
        """
        headers = dict(_BROWSER_HEADERS)
        headers["User-Agent"] = random.choice(_USER_AGENTS)
        headers["Referer"] = NSE_HOME_URL + "/"
        headers["Origin"] = NSE_HOME_URL
        return headers

    def _build_api_headers(self) -> Dict[str, str]:
        """
        Build API call headers (JSON accept, AJAX indicator).
        Includes Referer = NSE homepage (so request looks like internal nav).
        """
        headers = dict(_API_HEADERS)
        headers["User-Agent"] = random.choice(_USER_AGENTS)
        headers["Referer"] = NSE_HOME_URL + "/"
        headers["Origin"] = NSE_HOME_URL
        return headers

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Homepage handshake (cookie acquisition)
    # ───────────────────────────────────────────────────────────────
    def _do_homepage_handshake(self) -> bool:
        """
        Visit NSE homepage to acquire session cookies.

        Retries up to MAX_403_RETRIES times on 403, each time with a
        FRESH User-Agent (rotation) + random delay. NSE sometimes
        transient-blocks; a different UA often gets through.

        Returns:
            True if homepage fetched successfully (cookies acquired).
            False if all retries failed (NSE blocking us — set _is_blocked).

        Thread-safety: caller must hold self._lock.
        """
        for attempt in range(1, MAX_403_RETRIES + 1):
            try:
                # Fresh headers each attempt (new random UA)
                self._session.headers.clear()
                self._session.headers.update(self._build_browser_headers())

                resp = self._session.get(
                    NSE_HOME_URL, timeout=self.timeout, allow_redirects=True
                )

                if resp.status_code == 200:
                    # Success — cookies now in session.cookies
                    self._is_blocked = False
                    logger.debug(
                        f"NSE homepage handshake OK (attempt {attempt}, "
                        f"{len(self._session.cookies)} cookies acquired)"
                    )
                    return True

                if resp.status_code == 403:
                    # NSE blocked this UA/headers — try different UA next
                    logger.warning(
                        f"NSE homepage 403 (attempt {attempt}/{MAX_403_RETRIES}) — "
                        f"will retry with different User-Agent"
                    )
                    delay = random.uniform(*RETRY_DELAY_RANGE)
                    time.sleep(delay)
                    continue

                # Other errors (5xx, etc.) — retry
                logger.warning(
                    f"NSE homepage status {resp.status_code} (attempt {attempt}) — retrying"
                )
                delay = random.uniform(*RETRY_DELAY_RANGE)
                time.sleep(delay)

            except requests.exceptions.RequestException as e:
                logger.warning(
                    f"NSE homepage network error (attempt {attempt}): {e}"
                )
                delay = random.uniform(*RETRY_DELAY_RANGE)
                time.sleep(delay)

        # All retries failed — NSE is blocking us (cloud IP or persistent block)
        self._is_blocked = True
        logger.error(
            f"NSE homepage blocked after {MAX_403_RETRIES} attempts — "
            f"persistent block detected, will use fallback data sources"
        )
        return False

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Session management (get or refresh)
    # ───────────────────────────────────────────────────────────────
    def _get_session(self, force_new: bool = False) -> requests.Session:
        """
        Get a valid session with NSE cookies. Refreshes if:
          - No session exists yet, OR
          - Session older than SESSION_TTL_SECONDS, OR
          - force_new=True (caller wants fresh cookies after 401/403)

        Thread-safety: double-checked locking pattern. Multiple threads
        calling simultaneously won't trigger multiple homepage handshakes.

        Args:
            force_new: If True, discard cached session and create new.

        Returns:
            requests.Session with NSE cookies (if handshake succeeded).
            If NSE blocked, returns a session with browser headers only
            (callers should check is_blocked and use fallback).
        """
        # Fast path (no lock) — common case for existing fresh session
        if (
            not force_new
            and self._session is not None
            and (time.time() - self._session_created_at) < SESSION_TTL_SECONDS
        ):
            return self._session

        with self._lock:
            # Double-checked lock — another thread may have refreshed while
            # we were waiting for the lock. Re-check before doing handshake.
            if (
                not force_new
                and self._session is not None
                and (time.time() - self._session_created_at) < SESSION_TTL_SECONDS
            ):
                return self._session

            # Create new session + do homepage handshake for cookies
            self._session = requests.Session()
            success = self._do_homepage_handshake()
            self._session_created_at = time.time()

            if not success:
                # NSE blocked — session has browser headers but no valid cookies.
                # Callers should check is_blocked and use fallback.
                logger.warning(
                    "NSE session created but homepage handshake failed — "
                    "is_blocked=True, callers should use fallback data sources"
                )

            return self._session

    def _invalidate_session(self) -> None:
        """
        Invalidate cached session (e.g. after 401/403 mid-session).
        Next _get_session() call will do a fresh homepage handshake.
        Thread-safe.
        """
        with self._lock:
            self._session = None
            self._session_created_at = 0.0
            # Don't set _is_blocked here — 401/403 mid-session might just be
            # cookie expiry, not a persistent block. Let next handshake decide.

    # ───────────────────────────────────────────────────────────────
    # PUBLIC: Main JSON fetch method
    # ───────────────────────────────────────────────────────────────
    def fetch_json(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch JSON data from an NSE API endpoint.

        Handles cookie management, 403 retries, and empty response detection.
        Does NOT do fallback (use fetch_with_fallback for stock data).

        Args:
            endpoint: API path (e.g. "/marketStatus", "/fiidiiTradeReact").
                      Can be relative (prepended with NSE_API_BASE) or full URL.
            params: Query parameters dict.
            timeout: Override default timeout (seconds).

        Returns:
            Parsed JSON dict on success.
            None on failure (NSE blocked, network error, empty response).

        Example:
            fetcher = NSEDataFetcher()
            # GIFT Nifty / market status
            status = fetcher.fetch_json("/marketStatus")
            # FII/DII activity
            fiidii = fetcher.fetch_json("/fiidiiTradeReact", params={"section":"equities"})
        """
        # Build full URL
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = NSE_API_BASE + endpoint

        # Quick check — if NSE is persistently blocked, don't waste time
        if self._is_blocked:
            logger.debug(f"NSE blocked — skipping fetch_json({endpoint})")
            return None

        # Get session (with cookies)
        session = self._get_session()
        headers = self._build_api_headers()
        req_timeout = timeout or self.timeout

        try:
            resp = session.get(
                url, params=params, headers=headers, timeout=req_timeout
            )
        except requests.exceptions.RequestException as e:
            logger.warning(f"NSE fetch_json({endpoint}) network error: {e}")
            return None

        # 401/403 mid-session = cookies expired. Refresh + retry ONCE.
        if resp.status_code in (401, 403):
            logger.warning(
                f"NSE fetch_json({endpoint}) got {resp.status_code} — "
                f"refreshing session cookies and retrying once"
            )
            self._invalidate_session()
            session = self._get_session(force_new=True)
            headers = self._build_api_headers()
            try:
                resp = session.get(
                    url, params=params, headers=headers, timeout=req_timeout
                )
            except requests.exceptions.RequestException as e:
                logger.warning(f"NSE fetch_json({endpoint}) retry failed: {e}")
                return None

        # Check final status
        if resp.status_code != 200:
            logger.warning(
                f"NSE fetch_json({endpoint}) failed with status {resp.status_code}"
            )
            if resp.status_code == 403:
                self._is_blocked = True  # persistent block
            return None

        # Parse JSON
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning(
                f"NSE fetch_json({endpoint}) returned invalid JSON: {e}. "
                f"Body: {resp.text[:200]}"
            )
            return None

        # NSE sometimes returns {"data": null} on error — treat as failure
        if data is None or (isinstance(data, dict) and data.get("data") is None and len(data) <= 1):
            logger.debug(f"NSE fetch_json({endpoint}) returned empty/null data")
            return None

        return data

    # ───────────────────────────────────────────────────────────────
    # PUBLIC: Stock data fetch with FULL fallback chain
    # ───────────────────────────────────────────────────────────────
    def fetch_with_fallback(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> Optional[Any]:
        """
        Fetch stock OHLCV data with automatic fallback chain.

        Tries each source in order until one succeeds:
          1. NSE direct API (best data, real-time, but often blocks cloud IPs)
          2. nsepython library (wraps NSE with its own session mgmt)
          3. jugaad-data library (NSE bhavcopy-based, good for daily history)
          4. yfinance (Yahoo Finance — MOST RELIABLE on cloud IPs, has
             NSE (.NS) and BSE (.BO) symbols, but prices are split/dividend
             adjusted which may differ slightly from NSE raw prices)

        Args:
            symbol: NSE symbol WITHOUT suffix (e.g. "RELIANCE", "TCS").
                    yfinance will append .NS automatically.
            period: Data period ("1d","5d","1mo","3mo","6mo","1y","2y","5y","max").
            interval: Candle interval ("1d","1wk","1mo" for daily/weekly/monthly).

        Returns:
            pandas.DataFrame with columns [Date, Open, High, Low, Close, Volume]
            on success. None if ALL sources fail.
        """
        # Try each fallback source in order
        for source_name, fetch_func in [
            ("nse-direct", lambda: self._fetch_nse_direct(symbol, period)),
            ("nsepython", lambda: self._fetch_via_nsepython(symbol, period)),
            ("jugaad-data", lambda: self._fetch_via_jugaad_data(symbol, period)),
            ("yfinance", lambda: self._fetch_via_yfinance(symbol, period, interval)),
        ]:
            try:
                df = fetch_func()
                if df is not None and not df.empty:
                    logger.debug(f"{symbol}: data fetched via {source_name}")
                    return df
            except Exception as e:
                logger.debug(f"{symbol}: {source_name} failed ({e})")
                continue

        logger.warning(f"{symbol}: ALL data sources failed (nse/nsepython/jugaad/yfinance)")
        return None

    # ───────────────────────────────────────────────────────────────
    # PRIVATE: Fallback source implementations
    # ───────────────────────────────────────────────────────────────
    def _fetch_nse_direct(self, symbol: str, period: str) -> Optional[Any]:
        """Fetch via NSE direct API (charting.nseindia.com)."""
        if self._is_blocked:
            return None  # don't waste time if NSE blocked

        # NSE chart API endpoint
        from datetime import datetime, timedelta
        days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
        days = days_map.get(period, 365)
        end = datetime.now()
        start = end - timedelta(days=days)

        url = "https://charting.nseindia.com/Charts/GetHistoricalData"
        params = {
            "symbol": symbol,
            "fromDate": start.strftime("%d-%m-%Y"),
            "toDate": end.strftime("%d-%m-%Y"),
            "series": "EQ",
        }

        session = self._get_session()
        headers = self._build_api_headers()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data or not isinstance(data, list):
                return None
            import pandas as pd
            df = pd.DataFrame(data)
            if "CH_TIMESTAMP" in df.columns:
                df = df.rename(columns={"CH_TIMESTAMP": "Date"})
            required = {"Open", "High", "Low", "Close", "Volume"}
            if not required.issubset(set(df.columns)):
                return None
            return df
        except Exception:
            return None

    def _fetch_via_nsepython(self, symbol: str, period: str) -> Optional[Any]:
        """Fetch via nsepython library (if installed)."""
        if self._nsepython is None and not self._fallback_libs_checked:
            try:
                import nsepython as nsepython
                self._nsepython = nsepython
            except ImportError:
                return None  # nsepython not installed
        if self._nsepython is None:
            return None

        try:
            # nsepython has equity_history function
            from datetime import datetime, timedelta
            days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
            days = days_map.get(period, 365)
            end = datetime.now()
            start = end - timedelta(days=days)
            df = self._nsepython.equity_history(
                symbol, start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")
            )
            if df is not None and not df.empty:
                import pandas as pd
                return df
            return None
        except Exception as e:
            logger.debug(f"nsepython fetch failed for {symbol}: {e}")
            return None

    def _fetch_via_jugaad_data(self, symbol: str, period: str) -> Optional[Any]:
        """Fetch via jugaad-data library (if installed)."""
        if self._jugaad_data is None and not self._fallback_libs_checked:
            try:
                import jugaad_data as jd
                self._jugaad_data = jd
            except ImportError:
                return None  # jugaad-data not installed
        if self._jugaad_data is None:
            return None

        try:
            from datetime import datetime, timedelta
            days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
            days = days_map.get(period, 365)
            end = datetime.now()
            start = end - timedelta(days=days)
            # jugaad_data.stock_data(symbol, start, end)
            df = self._jugaad_data.stock_data(symbol, start, end)
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            logger.debug(f"jugaad-data fetch failed for {symbol}: {e}")
            return None

    def _fetch_via_yfinance(self, symbol: str, period: str, interval: str) -> Optional[Any]:
        """
        Fetch via yfinance (most reliable on cloud IPs).
        Yahoo Finance doesn't block by IP like NSE does.
        """
        if self._yfinance is None:
            try:
                import yfinance as yf
                self._yfinance = yf
            except ImportError:
                logger.warning("yfinance not installed — last fallback unavailable")
                return None

        try:
            # Append .NS suffix for NSE stocks (yfinance convention)
            yf_symbol = symbol if symbol.startswith(("^", ".")) else f"{symbol}.NS"
            raw = self._yfinance.download(
                yf_symbol, period=period, interval=interval,
                auto_adjust=True, progress=False,
            )
            if raw is None or raw.empty:
                return None
            # Handle multi-index columns (yfinance 0.2.x)
            if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
                raw.columns = raw.columns.get_level_values(0)
            import pandas as pd
            df = raw.reset_index()
            first_col = df.columns[0]
            if first_col != "Date":
                df = df.rename(columns={first_col: "Date"})
            required = {"Open", "High", "Low", "Close", "Volume"}
            if not required.issubset(set(df.columns)):
                return None
            return df
        except Exception as e:
            logger.debug(f"yfinance fetch failed for {symbol}: {e}")
            return None

    # ───────────────────────────────────────────────────────────────
    # PUBLIC: Diagnostics
    # ───────────────────────────────────────────────────────────────
    def get_status(self) -> Dict[str, Any]:
        """
        Return fetcher status for diagnostics/logging.
        Useful for health checks and debugging.
        """
        return {
            "is_blocked": self._is_blocked,
            "session_active": self._session is not None,
            "session_age_seconds": round(self.session_age, 1),
            "session_ttl_seconds": SESSION_TTL_SECONDS,
            "cookies_count": len(self._session.cookies) if self._session else 0,
            "fallback_libs": {
                "nsepython": self._nsepython is not None,
                "jugaad_data": self._jugaad_data is not None,
                "yfinance": self._yfinance is not None,
            },
        }


# ───────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON — most callers should use this.
# Avoid creating multiple NSEDataFetcher instances (each does its own
# homepage handshake, wasting NSE bandwidth and risking IP block).
# ───────────────────────────────────────────────────────────────────
_default_fetcher: Optional[NSEDataFetcher] = None
_singleton_lock = threading.Lock()


def get_default_fetcher() -> NSEDataFetcher:
    """
    Get the module-level singleton NSEDataFetcher instance.
    All callers share the same session (efficient cookie reuse).

    Returns:
        NSEDataFetcher singleton.
    """
    global _default_fetcher
    if _default_fetcher is None:
        with _singleton_lock:
            if _default_fetcher is None:
                _default_fetcher = NSEDataFetcher()
                logger.info("NSEDataFetcher singleton initialized")
    return _default_fetcher


def reset_default_fetcher() -> None:
    """
    Reset the singleton (useful for testing or after persistent block).
    Next get_default_fetcher() call creates a fresh instance.
    """
    global _default_fetcher
    with _singleton_lock:
        _default_fetcher = None
