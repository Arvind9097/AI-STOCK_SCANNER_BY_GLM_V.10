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
    """
    if not USE_CACHE:
        return None

    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None

    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        # NOTE: 20h TTL design choice - bulk-scan cache ke liye theek hai.
        # stock_lookup.py ka on-demand snapshot (market close ke baad) isse
        # yesterday's close serve karega - documented, acceptable for now.
        return None

    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except (_pickle.UnpicklingError, EOFError, _pickle.PickleError) as e:
        # V8.2.0: Corrupt cache file (half-written by old non-atomic save,
        # ya disk error) - silently skip, fresh download trigger karo.
        logger.warning(f"{symbol}: cache file corrupt thi ({e}), fresh download hoga")
        # Corrupt file ko delete kar do taaki disk par junk na rahe.
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    except Exception as e:
        logger.warning(f"{symbol}: cache read fail ({e}), fresh download hoga")
        return None


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
