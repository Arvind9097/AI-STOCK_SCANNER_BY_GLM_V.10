"""
===========================================================
 LOCAL CACHE
===========================================================
Har stock ka downloaded data data/cache/<SYMBOL>.pkl mein save
hota hai. Agla run agar CACHE_MAX_AGE_HOURS ke andar ho, to
Yahoo se dubara download nahi hota - seedha cache se load hota hai.

Isse:
- Rate-limit risk kam hota hai (kam requests)
- Repeat runs bahut fast hote hain

V8.2.0 FIXES:
- ATOMIC WRITES: pickle file pehle temp file mein likh kar phir
  os.replace() se rename karte hain (atomic_write_bytes helper).
  Pehle direct `open(path, "wb")` use hota tha - agar process
  mid-write kill ho jaaye (Ctrl+C / OOM / Render restart) to
  file truncated/corrupt reh jaati thi aur agla load_from_cache
  UnpicklingError deta tha. Ab ye risk gone.
- CORRUPT PICKLE HANDLING: UnpicklingError / EOFError / pickle
  exceptions ko explicitly catch karke None return karte hain
  (corrupt file ko silently skip, fresh download trigger).
- PATH TRAVERSAL DEFENSE: _cache_path ab `/`, `\\`, `..` bhi
  strip karta hai - upstream validation (stock_lookup.py pattern)
  already rokta hai, lekin defense-in-depth.
- CACHE_MAX_AGE_HOURS = 20 ghante (design choice): bulk scan ke
  liye theek hai, lekin stock_lookup.py ka on-demand snapshot
  market-close ke baad aaj ka close serve karega (stale). Documented
  here - if needed, add a separate shorter TTL for snapshot use-case.
===========================================================
"""

import os
import time
import pickle
import pickle as _pickle  # explicit alias for clearer exception reference

from config import USE_CACHE, CACHE_DIR, CACHE_MAX_AGE_HOURS
from logger import logger
from utils import atomic_write_bytes


def _cache_path(symbol):
    """
    Symbol se safe cache file path banata hai. `.` aur `^` ke saath
    path-safe name banata hai.

    V8.2.0: `/`, `\\`, `..` bhi strip karte hain - defense-in-depth
    (upstream stock_lookup.py already regex validate karta hai, lekin
    agar koi doosra caller directly cache.py use kare to bhi safe rahe).
    """
    safe = (
        symbol
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
        .replace(".", "_")
        .replace("^", "IDX_")
    )
    return os.path.join(CACHE_DIR, f"{safe}.pkl")


def load_from_cache(symbol):
    """
    Return: df ya None (agar cache nahi hai, expire ho chuka hai, ya
    corrupt hai). Corrupt pickle (UnpicklingError / EOFError) silently
    None return karta hai - caller fresh download karega.

    V9.1.2 MARKET-AWARE CACHE EXPIRY (Claude AI review fix #5):
      Pehle sirf CACHE_MAX_AGE_HOURS (20h) check hota tha. Problem:
      agar bot 8 PM ko restart ho aur agle din 9 AM scan chale, cache
      still valid rahega (13h < 20h) — lekin data aaj ke market close
      ka nahi hoga, kal ka hoga. Stale data serve hota tha.

      Ab 2-layer expiry:
        1. Age check (20h TTL) — basic staleness
        2. Market-date check — agar cached data ka last date aaj ki
           market date se purana hai (and market already open today),
           to cache expire maano, fresh download trigger karo.

      Logic:
        - Cache mein DataFrame ka last Date column check karo
        - Aaj ka IST date nikalo
        - Agar market abhi open hai (9:15-15:30 IST) aur cache ka last
          date < aaj hai, to cache stale hai (force refresh)
        - Agar market band hai (after 15:30), to aaj ka close serve karo
          (cached data aaj ka hona chahiye, warna stale)
    """
    if not USE_CACHE:
        return None

    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None

    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        return None

    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
    except (_pickle.UnpicklingError, EOFError, _pickle.PickleError) as e:
        logger.warning(f"{symbol}: cache file corrupt thi ({e}), fresh download hoga")
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    except Exception as e:
        logger.warning(f"{symbol}: cache read fail ({e}), fresh download hoga")
        return None

    # V9.1.2: Market-aware cache expiry check
    if _is_cache_stale_for_market(df):
        logger.debug(f"{symbol}: cache stale (market date mismatch), fresh download hoga")
        return None

    return df


def _is_cache_stale_for_market(df):
    """
    V9.1.2: Check if cached DataFrame's last date is stale relative to
    today's market session.

    Logic:
      - Get last date from df (Date column or index)
      - Get today's IST date
      - If today is a weekday AND market is open (9:15-15:30 IST):
          Cache is stale if last_date < today (should have today's data)
      - If today is a weekend OR market closed:
          Cache is stale if last_date < last trading day
          (Friday if weekend, today if after market close but before midnight)

    Returns: True if cache is stale (should refresh), False if fresh enough.
    """
    try:
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        IST = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(IST)
        today_ist = now_ist.date()

        # Get last date from DataFrame
        if "Date" in df.columns:
            last_date = pd.to_datetime(df["Date"]).iloc[-1].date()
        else:
            last_date = pd.to_datetime(df.index).date  # DatetimeIndex

        # Weekend check (Saturday=5, Sunday=6)
        if now_ist.weekday() >= 5:
            # Weekend — last trading day was Friday
            from datetime import timedelta
            days_since_friday = (now_ist.weekday() - 4) % 7
            last_trading_day = today_ist - timedelta(days=days_since_friday)
            return last_date < last_trading_day
        else:
            # Weekday — check if market is open (9:15-15:30 IST)
            current_time = now_ist.time()
            from datetime import time as dt_time
            market_open = dt_time(9, 15)
            market_close = dt_time(15, 30)

            if market_open <= current_time <= market_close:
                # Market is open right now — cache should have at least
                # yesterday's data (today's data may not be complete yet)
                from datetime import timedelta
                yesterday = today_ist - timedelta(days=1)
                # If yesterday was weekend, use Friday
                while yesterday.weekday() >= 5:
                    yesterday = yesterday - timedelta(days=1)
                return last_date < yesterday
            else:
                # Before market open or after market close
                # Cache should have today's data (if after close) or
                # yesterday's data (if before open)
                if current_time > market_close:
                    # After close — should have today's data
                    return last_date < today_ist
                else:
                    # Before open — yesterday's data is fine
                    from datetime import timedelta
                    yesterday = today_ist - timedelta(days=1)
                    while yesterday.weekday() >= 5:
                        yesterday = yesterday - timedelta(days=1)
                    return last_date < yesterday
    except Exception as e:
        # If market-date check fails, fall back to age-based expiry only
        logger.debug(f"Market-aware cache check failed ({e}), using age-based only")
        return False


def save_to_cache(symbol, df):
    """
    DataFrame ko pickle karke atomically cache file mein likhta hai.

    V8.2.0: atomic_write_bytes() use karta hai - pehle temp file mein
    likho, phir os.replace() se rename (atomic on same filesystem).
    Crash hone par destination file KABHI half-written nahi hogi.
    """
    if not USE_CACHE or df is None:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        data = pickle.dumps(df)
        atomic_write_bytes(_cache_path(symbol), data)
    except Exception as e:
        logger.warning(f"{symbol}: cache save fail ({e})")


def cache_age_hours(symbol):
    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 3600
