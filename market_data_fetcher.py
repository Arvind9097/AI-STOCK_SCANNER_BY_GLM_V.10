"""
===========================================================
 MARKET DATA FETCHER (V8.1.2) - Unified Primary/Secondary Chain
===========================================================
Ye poore codebase ke saare yfinance calls (downloader.py, stock_lookup.py,
mtf.py, tracker.py, master_dashboard.py) ke liye EK common entry point hai.

PRIORITY CHAIN:
  PRIMARY   (Free, No Auth, NSE/BSE direct):
    1. nse_session.py wala shared session + charting.nseindia.com
       (existing cookie-handshake reuse karta hai - koi naya session
       logic duplicate nahi kiya)
    2. 'nse' package (naya, V8.1.2 update) - BennyThadikaran/NseIndiaApi,
       ALAG NSE endpoint (www.nseindia.com/api/NextApi) - explicitly
       server/cloud-deployment ke liye design kiya gaya hai (docs mein
       "works in AWS" confirm), 100-din chunking (efficient - 1 saal
       sirf 4 requests mein). Agar charting.nseindia.com block ho par
       www.nseindia.com/api accessible ho, ye extra-resilience deta hai.
    3. Stooq.com free CSV (global mirror, NSE India symbols support karta hai)
    4. jugaad-data library - NSE ka hi historical data, alag maintained
       library ke through access (period-capped, dekho JUGAAD_DATA_MAX_
       PERIOD_DAYS - iska internal chunking calendar-month-based hai,
       kam efficient)
  SECONDARY (Fallback, tabhi try hota hai jab saare PRIMARY fail ho
             jaayein - rate-limit, network issue, ya symbol na mile):
    5. yfinance (jaisa V8.1 mein pehle se tha)

IMPORTANT HONEST NOTE (V8.1.2): In sabhi primary sources mein se jitne
bhi NSE ke servers ko seedha hit karte hain (1, 2, 3, 4 - Stooq NSE ka
nahi hai lekin NSE-hi-data mirror karta hai), agar hosting provider
(Render) ka IP kabhi NSE se rate-limited/blocked ho jaaye, to inmein
se kai EK SAATH fail ho sakte hain (alag library/endpoint hone se
HAMESHA madad nahi milti agar underlying network-block hai). Isliye
yfinance (jo Yahoo Finance - poori tarah alag infrastructure - use
karta hai) hamesha secondary ke roop mein maujood rehta hai as final
fallback. Agar sabhi primary + yfinance fail ho jaayein (jaisa bulk
500-stock scan mein ek saath sabka rate-limit lag sakta hai), to
system None return karta hai us symbol ke liye us cycle mein - agla
scheduled scan cycle phir try karega (kabhi crash nahi hota).

IMPORTANT DESIGN NOTE: 1H/intraday interval (mtf.py 1H confirmation)
sirf yfinance se milta hai - koi free NSE source live intraday history
nahi deta (sirf EOD daily). Isliye intraday calls seedha yfinance par
jaate hain (fallback chain sirf DAILY data ke liye hai).

FAILOVER: Har source track hota hai - agar CONSECUTIVE_FAIL_LIMIT baar
laगातार fail ho, to us source ko RECOVER_MINUTES ke liye disable kar
diya jaata hai (baar-baar dead source try karke time waste na ho),
phir apne aap wapas try hone lagta hai.
===========================================================
"""

import io
import time
import random
import threading
import requests
import pandas as pd
from datetime import date, timedelta, datetime

from nse_session import get_nse_session, invalidate_nse_session
from logger import logger

CONSECUTIVE_FAIL_LIMIT = 3
RECOVER_MINUTES = 30

# V8.1.2 PERFORMANCE FIX: jugaad-data ka internal break_dates() logic
# date-range ko CALENDAR-MONTH boundaries par todता hai - matlab agar
# hum ek pura saal (370 din) maangte hain, to ye EK symbol ke liye
# 13 ALAG NSE-API-calls karta hai (month-by-month), jo bahut dheema hai
# (isi wajah se logs mein ~35 second/10-stock-batch dikh raha tha - NSE
# Chart/Stooq single-request mein poora saal de dete hain, jugaad-data
# nahi). Isliye jab bhi jugaad_data ko lamba period maanga jaaye, hum
# use itne din tak hi CAP kar dete hain ki chunk-count kam rahe -
# jugaad_data yahan sirf FALLBACK hai (NSE Chart/Stooq dono fail hone
# ke baad hi trigger hota hai), isliye "jaldi kuch data mil jaana"
# "13-chunk-wait karke poora-saal ka data milna" se behtar hai.
JUGAAD_DATA_MAX_PERIOD_DAYS = 90  # ~4 month-chunks max, still reasonably fast

_source_health = {
    "nse_chart":    {"ok": True, "fails": 0, "last_fail": None},
    "nse_package":  {"ok": True, "fails": 0, "last_fail": None},
    "stooq":        {"ok": True, "fails": 0, "last_fail": None},
    "jugaad_data":  {"ok": True, "fails": 0, "last_fail": None},
    "yfinance":     {"ok": True, "fails": 0, "last_fail": None},
}

_last_nse_call = 0.0
NSE_MIN_INTERVAL_SEC = 0.4  # NSE ko 3 req/sec se zyada mat maaro

# V8.2.0 THREAD-SAFETY: bot_listener, breaking_news aur main.py scheduler
# alag threads mein chalte hain - saare _source_health / _last_nse_call
# mutations ke liye lock use karte hain (warna `+=` race condition se
# count galat ho jaata tha).
_health_lock = threading.Lock()
_nse_call_lock = threading.Lock()


def _nse_rate_limit():
    """Ensure at least NSE_MIN_INTERVAL_SEC gap between consecutive NSE calls."""
    global _last_nse_call
    with _nse_call_lock:
        elapsed = time.time() - _last_nse_call
        if elapsed < NSE_MIN_INTERVAL_SEC:
            time.sleep(NSE_MIN_INTERVAL_SEC - elapsed)
        _last_nse_call = time.time()


def _is_healthy(source):
    with _health_lock:
        h = _source_health[source]
        if h["ok"]:
            return True
        if h["last_fail"] and (datetime.now() - h["last_fail"]).total_seconds() > RECOVER_MINUTES * 60:
            logger.info(f"Source '{source}' auto-recover kar raha hoon ({RECOVER_MINUTES} min ho gaye)")
            h["ok"] = True
            h["fails"] = 0
        return h["ok"]


def _mark_fail(source):
    with _health_lock:
        h = _source_health[source]
        h["fails"] += 1
        h["last_fail"] = datetime.now()
        if h["fails"] >= CONSECUTIVE_FAIL_LIMIT:
            logger.warning(f"Source '{source}' {h['fails']} baar consecutively fail hua - {RECOVER_MINUTES} min ke liye disable kar raha hoon")
            h["ok"] = False


def _mark_success(source):
    with _health_lock:
        _source_health[source]["fails"] = 0
        _source_health[source]["ok"] = True


def get_source_health():
    """Debug/status ke liye - kaunsa source abhi healthy hai."""
    return {
        src: {
            "healthy": info["ok"],
            "consecutive_fails": info["fails"],
            "last_fail": info["last_fail"].strftime("%H:%M:%S") if info["last_fail"] else "Never",
        }
        for src, info in _source_health.items()
    }


def _clean_nse_symbol(symbol):
    """'.NS'/'.BO' hata kar pure NSE symbol deta hai (M&M.NS -> M&M, RELIANCE.NS -> RELIANCE)."""
    return symbol.upper().strip().replace(".NS", "").replace(".BO", "")


# ───────────────────────────────────────────────────────────────────
# PRIMARY SOURCE 1: NSE Direct Chart API (existing nse_session.py reuse)
# ───────────────────────────────────────────────────────────────────
def _fetch_nse_chart(symbol, period_days):
    """
    charting.nseindia.com ka historical endpoint - NSE ka apna official
    charting-data API (unofficial/undocumented, lekin free aur reliable).
    Return: DataFrame [Date, Open, High, Low, Close, Volume] ya None

    NOTE: Ye endpoint sirf CLOSE price ki timeseries deta hai - Open/
    High/Low ko humne Close se hi fill kiya hai (shape consistency ke
    liye). Isliye SL/Target hit-detection jaisi jagah is source ko
    SKIP kiya jaata hai (dekho fetch_latest_ohlc_batch) - sirf bulk
    scanner/indicator calc (jo Close-based hai) ke liye safe hai.
    """
    try:
        clean_sym = _clean_nse_symbol(symbol)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=period_days)

        _nse_rate_limit()
        session = get_nse_session()

        url = (
            "https://charting.nseindia.com/Charts/GetHistoricalData"
            f"?Identifier={clean_sym}&Type=EQ"
            f"&StartDate={start_dt.strftime('%d-%m-%Y')}"
            f"&EndDate={end_dt.strftime('%d-%m-%Y')}&interval=1"
        )
        session.headers.update({"X-Requested-With": "XMLHttpRequest"})
        resp = session.get(url, timeout=15)
        # V8.2.0: 401/403 cookie-expiry handler - ek baar invalidate karke
        # retry karte hain (pehle ye dead code tha, ab live hai).
        if resp.status_code in (401, 403):
            logger.warning(f"NSE chart API {resp.status_code} - session invalidate karke retry")
            invalidate_nse_session()
            session = get_nse_session(force_new=True)
            session.headers.update({"X-Requested-With": "XMLHttpRequest"})
            resp = session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rows = data.get("grapthData") if isinstance(data, dict) else None
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=["timestamp", "Close"])
        df["Date"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.drop(columns=["timestamp"]).sort_values("Date")
        df["Open"] = df["High"] = df["Low"] = df["Close"]
        df["Volume"] = 0
        df = df.reset_index(drop=True)

        if len(df) < 5:
            return None
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]]

    except Exception as e:
        logger.debug(f"{symbol}: NSE chart API fail ({e})")
        return None


# ───────────────────────────────────────────────────────────────────
# PRIMARY SOURCE 2: 'nse' package (naya, V8.1.2 - BennyThadikaran/
# NseIndiaApi) - ALAG endpoint (www.nseindia.com/api/NextApi), server-
# deployment ke liye explicitly designed
# ───────────────────────────────────────────────────────────────────
# Library docs khud confirm karte hain: "NSE package now works in
# server environments like AWS" (v1.2.0+) - jabki nsepython (jo humara
# ek doosra option tha) apni documentation mein khud kehta hai "local
# version does NOT work with AWS, Google Cloud and web servers" (NSE
# robots.txt webservers ko block karta hai) - is farak ki wajah se
# 'nse' package ko naya primary source banaya gaya hai.
#
# Module-level SINGLETON instance use karte hain (jaisa session-caching
# fix mein seekha - nse_session.py) taaki har call par naya NSE()
# object na banana pade (jo apna khud ka cookie-handshake karta hai).
# V8.2.0: _nse_pkg_lock singleton creation / invalidation ke liye.
_nse_pkg_instance = None
_nse_pkg_lock = threading.Lock()


def _get_nse_package_instance():
    """
    'nse' package ka NSE() instance ek baar banake reuse karta hai
    (module-level singleton). download_folder ek local temp-folder
    hai (library ko cookies/downloaded-files store karne ke liye
    chahiye - hamare use-case mein zyada disk-usage nahi hoga).

    V8.2.0: Lock ke saath double-checked pattern - concurrent threads
    (bot_listener + scheduler) ek saath naya NSE() instance na bana lein.
    """
    global _nse_pkg_instance
    if _nse_pkg_instance is not None:
        return _nse_pkg_instance

    with _nse_pkg_lock:
        if _nse_pkg_instance is not None:
            return _nse_pkg_instance
        try:
            from nse import NSE
            import os
            folder = os.path.join(os.getcwd(), "nse_cache")
            os.makedirs(folder, exist_ok=True)
            _nse_pkg_instance = NSE(download_folder=folder, server=True, timeout=15)
            return _nse_pkg_instance
        except Exception as e:
            logger.debug(f"'nse' package instance banane mein fail ({e})")
            return None


def invalidate_nse_pkg():
    """
    V8.2.0: 'nse' package ka singleton instance invalidate karta hai -
    agla _fetch_nse_package call naya NSE() object banayega (apne fresh
    cookies ke saath). 401/403 handler se call hota hai jab NSE chart API
    ya nse-package dono mein se koi cookie-expiry indicate kare.

    Pehle ye singleton NEVER invalidate hota tha - agar long-running
    Render process mein NSE cookies expire ho jaate to ye source 30-min
    circuit-break ke baad bhi wahi dead singleton recover ho jaata tha.
    """
    global _nse_pkg_instance
    with _nse_pkg_lock:
        _nse_pkg_instance = None


def _fetch_nse_package(symbol, period_days):
    """
    'nse' package se historical data - www.nseindia.com/api/NextApi
    endpoint (charting.nseindia.com se ALAG). Genuine OHLC deta hai
    (SL/Target check ke liye bhi safe).

    DEFENSIVE PARSING NOTE: Library apni response ko rename/normalize
    nahi karti (raw NSE JSON return karti hai), aur documentation mein
    exact column-names (jaise mOPEN/mHIGH/mCLOSE) confirm nahi ho paye
    training-data se - isliye multiple POSSIBLE key-names try karte
    hain (defensive). Agar koi bhi expected key na mile, gracefully
    None return hota hai (crash nahi) - baaki chain (Stooq/jugaad-data/
    yfinance) still kaam karegi. Deploy ke baad logs mein agar ye
    source consistently kaam na kare, exact schema production-logs se
    confirm karke yahan update kiya ja sakta hai.
    """
    try:
        nse = _get_nse_package_instance()
        if nse is None:
            return None

        clean_sym = _clean_nse_symbol(symbol)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=period_days)

        _nse_rate_limit()
        raw_rows = nse.fetch_equity_historical_data(
            symbol=clean_sym, from_date=start_dt, to_date=end_dt, series="EQ"
        )

        if not raw_rows:
            return None

        def _first_present(row, keys):
            for k in keys:
                if k in row and row[k] not in (None, "", "-"):
                    return row[k]
            return None

        parsed_rows = []
        for row in raw_rows:
            date_val = _first_present(row, ["mTIMESTAMP", "CH_TIMESTAMP", "TIMESTAMP", "date"])
            open_val = _first_present(row, ["mOPEN", "CH_OPENING_PRICE", "OPEN", "open"])
            high_val = _first_present(row, ["mHIGH", "CH_TRADE_HIGH_PRICE", "HIGH", "high"])
            low_val = _first_present(row, ["mLOW", "CH_TRADE_LOW_PRICE", "LOW", "low"])
            close_val = _first_present(row, ["mCLOSE", "CH_CLOSING_PRICE", "CLOSE", "close"])
            vol_val = _first_present(row, ["mTOTTRDQTY", "CH_TOT_TRADED_QTY", "VOLUME", "volume"])

            if date_val is None or close_val is None:
                continue  # is row ko skip karo, poori chain ko fail mat karo

            parsed_rows.append({
                "Date": date_val,
                "Open": open_val if open_val is not None else close_val,
                "High": high_val if high_val is not None else close_val,
                "Low": low_val if low_val is not None else close_val,
                "Close": close_val,
                "Volume": vol_val if vol_val is not None else 0,
            })

        if not parsed_rows:
            return None

        df = pd.DataFrame(parsed_rows)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)

        if len(df) < 5:
            return None
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]]

    except ImportError:
        logger.debug("'nse' package install nahi hai - is source ko skip kar raha hoon")
        return None
    except Exception as e:
        # V8.2.0: Agar error auth-related (HTTPError 401/403 ya
        # ConnectionError) lagta hai, to nse-package ka singleton bhi
        # invalidate karo taaki agla call fresh NSE() object banaye
        # (warna dead singleton 30 min tak fail karta raheta).
        err_msg = str(e).lower()
        if "401" in err_msg or "403" in err_msg or "unauthorized" in err_msg or "forbidden" in err_msg:
            logger.warning(f"{symbol}: nse-package ko auth error mila ({e}) - singleton invalidate kar raha hoon")
            invalidate_nse_pkg()
            invalidate_nse_session()
        logger.debug(f"{symbol}: 'nse' package fail ({e})")
        return None


# ───────────────────────────────────────────────────────────────────
# PRIMARY SOURCE 2: Stooq.com (free CSV, no auth, global mirror)
# ───────────────────────────────────────────────────────────────────
def _fetch_stooq(symbol, period_days):
    """
    Stooq NSE India stocks ko '.in' suffix ke saath serve karta hai
    (e.g. reliance.in). Free, no-auth, GENUINE daily OHLCV CSV (asli
    High/Low/Close - synthetic nahi, isliye SL/Target check ke liye
    bhi safe hai).

    V8.2.0 FIX: Pehle symbol ko f-string se URL mein interpolate karte
    the - M&M aur L&T jaise symbols mein `&` URL ko tod deta tha
    (`?s=m&m.in&d1=...` parses as s=m + empty m.in param + d1=...).
    Ab requests.get(url, params={...}) use karte hain jo automatically
    URL-encode kar deta hai (M&M -> M%26M).
    """
    try:
        clean_sym = _clean_nse_symbol(symbol).lower()
        stooq_sym = f"{clean_sym}.in"

        end_dt = date.today()
        start_dt = end_dt - timedelta(days=period_days)

        # V8.2.0: params dict use karte hain taaki requests automatically
        # URL-encode kar de - `&` aur special characters safe rahein.
        url = "https://stooq.com/q/d/l/"
        params = {
            "s": stooq_sym,
            "d1": start_dt.strftime("%Y%m%d"),
            "d2": end_dt.strftime("%Y%m%d"),
            "i": "d",
        }
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()

        if "No data" in resp.text or len(resp.text) < 50:
            return None

        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty or "Date" not in df.columns or "Close" not in df.columns:
            return None

        df["Date"] = pd.to_datetime(df["Date"])
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col not in df.columns:
                df[col] = df["Close"] if col != "Volume" else 0
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].apply(
            lambda c: pd.to_numeric(c, errors="coerce") if c.name != "Date" else c
        ).dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)

        if len(df) < 5:
            return None
        return df

    except Exception as e:
        logger.debug(f"{symbol}: Stooq fail ({e})")
        return None


# ───────────────────────────────────────────────────────────────────
# PRIMARY SOURCE 3: jugaad-data library (naya, V8.1.2 - NSE ka hi data,
# alag maintained library ke through, alag headers/retry-implementation)
# ───────────────────────────────────────────────────────────────────
def _fetch_jugaad_data(symbol, period_days):
    """
    jugaad-data (PyPI package) - NSE ke hi historical bhavcopy-based
    data ko access karta hai, koi authentication nahi chahiye. Genuine
    OHLC data deta hai (synthetic nahi) - isliye SL/Target check ke
    liye bhi safe hai.

    NOTE: Ye bhi NSE ke servers ko hi hit karta hai (jaisे NSE Chart
    API aur alag tareeke se) - agar hosting-provider ka IP NSE se
    genuinely block ho, to ye bhi fail ho sakta hai (network-level
    issue hai, library-choice se independent). Isliye phir bhi
    yfinance secondary hamesha maujood rehta hai.

    PERFORMANCE NOTE: period_days ko JUGAAD_DATA_MAX_PERIOD_DAYS tak
    CAP kiya jaata hai - library ka internal date-chunking calendar-
    month-boundaries par hota hai, isliye bahut lamba period (jaise
    1 saal) maangne se 13+ alag NSE-calls ho jaate hain per-symbol
    (dekho module-level comment). Ye sirf fallback-source hai, isliye
    "kam data jaldi" "poora data bahut der mein" se behtar hai.
    """
    try:
        from jugaad_data.nse import stock_df

        clean_sym = _clean_nse_symbol(symbol)
        capped_days = min(period_days, JUGAAD_DATA_MAX_PERIOD_DAYS)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=capped_days)

        _nse_rate_limit()  # yahi shared NSE rate-limiter use karte hain
        raw = stock_df(symbol=clean_sym, from_date=start_dt, to_date=end_dt, series="EQ")

        if raw is None or raw.empty:
            return None

        df = pd.DataFrame({
            "Date": pd.to_datetime(raw["DATE"]),
            "Open": pd.to_numeric(raw["OPEN"], errors="coerce"),
            "High": pd.to_numeric(raw["HIGH"], errors="coerce"),
            "Low": pd.to_numeric(raw["LOW"], errors="coerce"),
            "Close": pd.to_numeric(raw["CLOSE"], errors="coerce"),
            "Volume": pd.to_numeric(raw["VOLUME"], errors="coerce"),
        }).dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)

        if len(df) < 5:
            return None
        return df

    except ImportError:
        logger.debug("jugaad-data package install nahi hai - is source ko skip kar raha hoon")
        return None
    except Exception as e:
        logger.debug(f"{symbol}: jugaad-data fail ({e})")
        return None


# ───────────────────────────────────────────────────────────────────
# SECONDARY (Fallback): yfinance - jaisa V8.1 mein pehle se tha
# ───────────────────────────────────────────────────────────────────
def _fetch_yfinance_daily(symbol, period="1y"):
    """V8.1 ka original single-symbol yfinance fallback (unchanged behavior)."""
    try:
        import yfinance as yf
        raw = yf.download(symbol, period=period, interval="1d", auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None
        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        df = raw.reset_index()
        first_col = df.columns[0]
        if first_col != "Date":
            df = df.rename(columns={first_col: "Date"})
        required = {"Close", "Open", "High", "Low", "Volume"}
        if not required.issubset(set(df.columns)):
            return None
        return df
    except Exception as e:
        logger.debug(f"{symbol}: yfinance fail ({e})")
        return None


# ───────────────────────────────────────────────────────────────────
# V8.3.2: YAHOO FINANCE DIRECT API (cloud-reliable, no rate-limit)
# ───────────────────────────────────────────────────────────────────
# Yfinance LIBRARY Render/cloud IPs par rate-limit lagta deta hai
# (shared IP, internal session handling). Lekin Yahoo ka DIRECT API
# endpoint (query1.finance.yahoo.com) browser User-Agent header ke
# saath reliably kaam karta hai — 2000 req/hour per IP.
#
# Ye source CLOUD PAR SABSE RELIABLE hai (NSE blocks cloud IPs,
# Stooq limited coverage, yfinance-lib rate-limits). Isliye ise
# PRIORITY #1 banaya gaya hai.
_YAHOO_DIRECT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}
_YAHOO_DIRECT_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",  # fallback host
]


def _fetch_yahoo_direct(symbol, period="1y"):
    """
    V8.3.2: Yahoo Finance ka DIRECT chart API use karta hai (yfinance
    library ki jagah). Browser User-Agent header ke saath cloud IPs
    par reliably kaam karta hai — NSE-block aur yfinance-rate-limit
    dono se bachata hai.

    Endpoint: /v8/finance/chart/{symbol}?range={period}&interval=1d
    Returns: DataFrame [Date, Open, High, Low, Close, Volume] ya None.
    """
    import pandas as pd
    # Yahoo uses "1y", "6mo", "3mo", "1mo", "5d", "1d" directly
    # (same format as yfinance). Map our period to Yahoo range.
    yahoo_range = period if period in ("1d","5d","1mo","3mo","6mo","1y","2y","5y","10y","ytd","max") else "1y"

    for host in _YAHOO_DIRECT_HOSTS:
        url = f"{host}/v8/finance/chart/{symbol}?range={yahoo_range}&interval=1d"
        try:
            resp = requests.get(url, headers=_YAHOO_DIRECT_HEADERS, timeout=15)
            if resp.status_code == 404:
                # Symbol Yahoo par exist nahi karta — dusre host try karne
                # ka matlab nahi. Seedha return None.
                logger.debug(f"{symbol}: Yahoo direct 404 (symbol not found)")
                return None
            if resp.status_code == 429:
                # Rate-limit — dusra host try karo (backoff caller handle karega)
                logger.debug(f"{symbol}: Yahoo direct 429 (rate-limit), next host try")
                continue
            if resp.status_code != 200:
                logger.debug(f"{symbol}: Yahoo direct status {resp.status_code}")
                continue

            data = resp.json()
            result = data.get("chart", {}).get("result")
            if not result:
                return None
            result = result[0]

            timestamps = result.get("timestamp", [])
            quote = result.get("indicators", {}).get("quote", [{}])[0]

            opens = quote.get("open", [])
            highs = quote.get("high", [])
            lows = quote.get("low", [])
            closes = quote.get("close", [])
            volumes = quote.get("volume", [])

            if not timestamps or not closes:
                return None

            dates = pd.to_datetime(timestamps, unit="s").tz_localize(None)
            df = pd.DataFrame({
                "Date": dates,
                "Open": opens,
                "High": highs,
                "Low": lows,
                "Close": closes,
                "Volume": volumes,
            })
            # Drop rows where Close is NaN (Yahoo sometimes has null candles)
            df = df.dropna(subset=["Close"]).reset_index(drop=True)
            if df.empty:
                return None
            return df

        except requests.exceptions.RequestException as e:
            logger.debug(f"{symbol}: Yahoo direct {host} fail ({e})")
            continue
        except Exception as e:
            logger.debug(f"{symbol}: Yahoo direct parse fail ({e})")
            continue

    return None


def _period_to_days(period):
    return {
        "1d": 3, "5d": 8, "1mo": 35, "3mo": 95,
        "6mo": 185, "1y": 370, "2y": 740, "5y": 1830,
    }.get(period, 370)


# ───────────────────────────────────────────────────────────────────
# PUBLIC API - drop-in replacement for single-symbol yfinance calls
# ───────────────────────────────────────────────────────────────────
def fetch_daily_ohlcv(symbol, period="1y"):
    """
    V9.9: SIRF Upstox Analytics API (user request — Yahoo/NSE/yfinance hata do).
    Agar Upstox token missing hai, to Yahoo Direct fallback (emergency only).

    Priority: Upstox Analytics API -> Yahoo Direct (emergency fallback)
    """
    # V9.9: UPSTOX ANALYTICS API — PRIMARY + ONLY (1 year token, 25 req/sec)
    try:
        from upstox_fetcher import fetch_historical_data, is_token_valid, UPSTOX_ANALYTICS_TOKEN
        if UPSTOX_ANALYTICS_TOKEN and is_token_valid():
            clean_sym = symbol.replace(".NS", "").replace(".BO", "")
            if not clean_sym.startswith("^"):
                days_map = {"1d": 3, "5d": 8, "1mo": 35, "3mo": 95,
                            "6mo": 185, "1y": 370, "2y": 740, "5y": 1830}
                days = days_map.get(period, 370)
                df = fetch_historical_data(clean_sym, days=days)
                if df is not None and not df.empty:
                    return df
    except Exception as e:
        logger.debug(f"Upstox fetch fail for {symbol}: {e}")

    # V9.9: Emergency fallback — Yahoo Direct (sirf if Upstox token not set)
    # Ye sirf tab chalega jab UPSTOX_ANALYTICS_TOKEN set nahi hai
    df = _fetch_yahoo_direct(symbol, period)
    if df is not None and not df.empty:
        return df

    # Index symbols ke liye yfinance-lib (last resort for ^NSEI etc)
    if symbol.strip().startswith("^"):
        return _fetch_yfinance_daily(symbol, period)

    logger.warning(f"{symbol}: Upstox + Yahoo dono fail. UPSTOX_ANALYTICS_TOKEN check karo.")
    return None


def fetch_daily_batch(symbols, period="1y", delay_range=(0.3, 0.8)):
    """
    Multiple symbols ke liye batch fetch. Har symbol individually try
    hota hai (NSE/Stooq per-symbol hi kaam karte hain, koi bulk-multi-
    ticker endpoint nahi hai in free sources mein).

    Return: dict {symbol: df or None}
    """
    results = {}
    for i, sym in enumerate(symbols):
        results[sym] = fetch_daily_ohlcv(sym, period)
        if i < len(symbols) - 1:
            time.sleep(random.uniform(*delay_range))
    return results


def fetch_latest_close_batch(symbols, yfinance_batch_fallback=None):
    """
    Watchlist/live-tracking ke liye - har symbol ka SIRF AAJ ka last
    close price chahiye (poora OHLCV history nahi). Pehle har symbol
    ko individually PRIMARY (NSE Chart -> Stooq) se try karta hai;
    jo na milein, unke liye caller ka diya SECONDARY multi-ticker
    yfinance batch function call hota hai (agar diya gaya ho).

    Args:
        symbols: list of NSE symbols (e.g. ["RELIANCE.NS", "TCS.NS"])
        yfinance_batch_fallback: callable(list_of_symbols) -> dict{symbol: price}

    Return: dict {symbol: last_close_price}
    """
    prices = {}
    pending = []

    for sym in symbols:
        df = fetch_daily_ohlcv(sym, period="5d")
        if df is not None and not df.empty:
            try:
                prices[sym] = round(float(df["Close"].iloc[-1]), 2)
            except Exception:
                pending.append(sym)
        else:
            pending.append(sym)

    if pending and yfinance_batch_fallback:
        try:
            fallback_prices = yfinance_batch_fallback(pending) or {}
            prices.update(fallback_prices)
        except Exception as e:
            logger.warning(f"yfinance batch fallback fail ({len(pending)} symbols): {e}")

    return prices


def fetch_latest_ohlc_batch(symbols, yfinance_batch_fallback=None):
    """
    check_live_market_hits() jaise SL/Target-hit detection ke liye -
    sirf Close nahi, AAJ ka GENUINE High/Low/Close teenon chahiye
    (SL/Target "aaj ke High/Low ne touch kiya ki nahi" check karne
    ke liye).

    IMPORTANT CORRECTNESS NOTE: NSE Chart API (_fetch_nse_chart) sirf
    Close price ki timeseries deta hai - Open/High/Low ko humne Close
    se hi fill kiya hai (fallback OHLCV shape maintain karne ke liye,
    downloader.py ke bulk-scan jaisा use-case jahan sirf Close-based
    indicators chalte hain). LEKIN SL/Target-hit detection ke liye
    ye GALAT hoga (High=Low=Close hone se "aaj high ne target touch
    kiya" jaisa check hamesha false ya galat trigger karega). Isliye
    ye function NSE Chart API ko SKIP karta hai aur sirf GENUINE-OHLC
    sources (nse-package -> Stooq -> jugaad-data -> yfinance) hi try
    karta hai.

    Args:
        symbols: list of NSE symbols
        yfinance_batch_fallback: callable(list_of_symbols) ->
            dict{symbol: {"High":.., "Low":.., "Close":..}}

    Return: dict {symbol: {"High":.., "Low":.., "Close":..}}
    """
    result = {}
    pending = []

    for sym in symbols:
        # Sirf GENUINE-OHLC sources try karo (nse-package, Stooq, phir
        # jugaad-data) - NSE Chart API skip (uska High/Low synthetic
        # hai, SL/Target check ke liye unsafe)
        df = None
        if _is_healthy("nse_package"):
            df = _fetch_nse_package(sym, period_days=5)
            if df is not None and not df.empty:
                _mark_success("nse_package")
            else:
                _mark_fail("nse_package")
                df = None

        if df is None and _is_healthy("stooq"):
            df = _fetch_stooq(sym, period_days=5)
            if df is not None and not df.empty:
                _mark_success("stooq")
            else:
                _mark_fail("stooq")
                df = None

        if df is None and _is_healthy("jugaad_data"):
            df = _fetch_jugaad_data(sym, period_days=5)
            if df is not None and not df.empty:
                _mark_success("jugaad_data")
            else:
                _mark_fail("jugaad_data")
                df = None

        if df is not None and not df.empty:
            try:
                last = df.iloc[-1]
                result[sym] = {
                    "High": round(float(last["High"]), 2),
                    "Low": round(float(last["Low"]), 2),
                    "Close": round(float(last["Close"]), 2),
                }
            except Exception:
                pending.append(sym)
        else:
            pending.append(sym)

    if pending and yfinance_batch_fallback:
        try:
            fallback = yfinance_batch_fallback(pending) or {}
            result.update(fallback)
        except Exception as e:
            logger.warning(f"yfinance OHLC batch fallback fail ({len(pending)} symbols): {e}")

    return result


def fetch_intraday(symbol, period="3mo", interval="60m"):
    """
    Intraday/1H data - SIRF yfinance se milta hai (koi free NSE/Stooq
    source live intraday history nahi deta, sirf EOD). Isliye ye function
    seedha secondary (yfinance) call karta hai, primary chain skip karta
    hai (mtf.py 1H confirmation ke liye).
    """
    try:
        import yfinance as yf
        raw = yf.download(symbol, period=period, interval=interval, auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None
        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        return raw
    except Exception as e:
        logger.debug(f"{symbol}: intraday (yfinance) fail ({e})")
        return None
