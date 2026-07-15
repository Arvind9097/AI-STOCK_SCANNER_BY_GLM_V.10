"""
===========================================================
 STOCK NEWS FETCHER (shared, cached, schema-safe, HINGLISH)
===========================================================
yfinance ke Ticker.news ka schema samay ke saath badla hai -
purane versions mein top-level 'title'/'publisher' keys hoti thi,
naye versions mein 'content' ke andar nested hoti hain. Ye function
DONO formats handle karta hai.

Caching: same din mein ek stock ki news dubara download nahi hoti
(rate-limit se bachne ke liye - Telegram report + PDF dono isi
function ko call karte hain, cache na ho to double download hoga).

V8.3.0 (Task G5) - HINGLISH TRANSLATION:
  User requirement: "News always Hinglish me hona chahiye... stocks
  name English me rakhna hain."
  Ab `format_news_text()` news titles ko `to_hinglish()` se translate
  karta hai (Roman Hinglish agar ZAI_API_KEY set hai, warna
  Devanagari + English stock names fallback). Stock names/tickers
  (RELIANCE, TCS, M&M, NIFTY) English mein hi preserve hote hain.
  `to_hindi()` import backward-compat ke liye rakha gaya hai (kabhi
  fallback chain mein use ho sakta hai agar to_hinglish unavailable
  ho).
===========================================================
"""

import time
import threading
import yfinance as yf

from logger import logger

# V8.3.0 (Task G5): Hinglish translator import with graceful fallback.
# Priority: to_hinglish (best - preserves English stock names) >
# to_hindi (legacy - full Devanagari) > identity (no translation).
try:
    from translator import to_hinglish as _to_hinglish
except ImportError:
    _to_hinglish = None
    logger.warning(
        "translator.to_hinglish import fail - news titles English "
        "mein hi rahengi (Hinglish translation disabled)."
    )

# Backward-compat: to_hindi fallback agar to_hinglish na mile.
try:
    from translator import to_hindi as _to_hindi
except ImportError:
    _to_hindi = None

_news_cache = {}  # {symbol: (timestamp, news_list)}
_CACHE_TTL_SEC = 3 * 3600  # 3 ghante ke andar dubara na maango
# V8.2.0 FIX (bug #25): thread-safety - agar bot_listener + scanner +
# breaking_news poller simultaneously fetch_stock_news call karein to
# dict read-modify-write race ho sakti thi. Lock se safe.
_cache_lock = threading.Lock()
# V8.2.0 FIX (bug #24): unbounded cache growth preventer - agar
# NIFTY500 universe dynamically change ho to cache memory leak ho
# sakta tha. Max-size eviction (LRU-style).
_CACHE_MAX_ENTRIES = 1000


def _parse_news_item(item):
    """Purane aur naye dono yfinance news schema handle karta hai."""
    # Naya schema (2024+): {'id':..., 'content': {'title':..., 'provider': {'displayName':...}, 'canonicalUrl': {'url':...}}}
    if "content" in item and isinstance(item["content"], dict):
        content = item["content"]
        title = content.get("title", "No Title")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "Unknown") if isinstance(provider, dict) else "Unknown"
        link = ""
        canonical = content.get("canonicalUrl", {})
        if isinstance(canonical, dict):
            link = canonical.get("url", "")
        return {"title": title, "publisher": publisher, "link": link}

    # Purana schema: top-level 'title'/'publisher'/'link'
    return {
        "title": item.get("title", "No Title"),
        "publisher": item.get("publisher", "Unknown"),
        "link": item.get("link", ""),
    }


def fetch_stock_news(symbol, limit=2):
    """
    Return: list of {"title":..., "publisher":..., "link":...} (max `limit` items)
    Kabhi exception nahi uthata - fail hone par khaali list deta hai.

    V8.2.0 FIXES:
    - bug #13: Cache ab FULL parsed list store karta hai (slice nahi)
      taaki alag callers (alag `limit`) ko sahi count mil sake. Pehle
      `fetch_stock_news('X', limit=1)` call ne cache mein 1 item store
      kiya tha, phir `format_news_text('X', limit=2)` ko sirf 1 milta tha.
    - bug #24: Max-size eviction (1000 entries) - LRU-style.
    - bug #25: thread-safety via _cache_lock.
    """
    now = time.time()
    # V8.2.0 FIX (bug #25): thread-safe cache read.
    with _cache_lock:
        cached = _news_cache.get(symbol)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        # V8.2.0 FIX (bug #13): slice at RETURN time, not cache time.
        return cached[1][:limit]

    try:
        ticker = yf.Ticker(symbol)
        raw_news = ticker.news or []
        # V8.2.0 FIX (bug #13): cache FULL parsed list (no slice), so
        # different callers (limit=1, limit=2, ...) all get correct count.
        parsed = [_parse_news_item(item) for item in raw_news]
        with _cache_lock:
            _news_cache[symbol] = (now, parsed)
            # V8.2.0 FIX (bug #24): max-size eviction (LRU-style).
            # Dict insertion order = LRU (Python 3.7+). Oldest entry
            # evict karte hain agar cache over limit ho gaya.
            if len(_news_cache) > _CACHE_MAX_ENTRIES:
                oldest_key = next(iter(_news_cache))
                _news_cache.pop(oldest_key, None)
        return parsed[:limit]
    except Exception as e:
        logger.warning(f"{symbol}: news fetch fail ({e})")
        return []


def _translate_title_to_hinglish(title):
    """
    V8.3.0 (Task G5): News title ko Hinglish mein translate karta hai.
    Stock names/tickers English mein preserve hote hain (RELIANCE, TCS,
    M&M, NIFTY, etc).

    Priority chain (kabhi crash nahi karta, worst case original
    English title return):
      1. to_hinglish() - GLM API (Roman Hinglish) ya placeholder
         technique (Devanagari + English stock names).
      2. to_hindi() - legacy Devanagari fallback (agar to_hinglish
         import fail ho gaya ho).
      3. Original English title (sab fail).
    """
    if not title:
        return title
    # 1. Best: to_hinglish
    if _to_hinglish is not None:
        try:
            return _to_hinglish(title)
        except Exception as e:
            logger.debug(f"to_hinglish fail on title ({e}), fallback chain")
    # 2. Legacy: to_hindi (full Devanagari - stock names bhi translate)
    if _to_hindi is not None:
        try:
            return _to_hindi(title)
        except Exception as e:
            logger.debug(f"to_hindi fail on title ({e}), original return")
    # 3. Original English
    return title


def format_news_text(symbol, limit=2):
    """
    Telegram/PDF ke liye ready-made plain text banata hai.

    V8.3.0 (Task G5): News titles ab Hinglish mein translate hote
    hain (Roman Hinglish agar ZAI_API_KEY set, warna Devanagari +
    English stock names). Stock names ENGLISH mein hi preserve hote
    hain (RELIANCE, TCS, M&M, NIFTY) - traders ke liye padhne mein
    easy. Symbol argument caller ko display context deta hai (function
    internal use ke liye; output mein symbol nahi aata - caller apne
    context mein add karta hai jaise "📰 <b>{stk}</b>: {news}").

    Output format (plain text, caller escape_html apply karta hai):
      "• <Hinglish title 1> (publisher 1)
       • <Hinglish title 2> (publisher 2)"

    News na milne par:
      "Abhi koi recent news nahi mili."
    """
    news = fetch_stock_news(symbol, limit=limit)
    if not news:
        return "Abhi koi recent news nahi mili."
    # V8.2.0 FIX (bug #16): news list empty hone par bhi safe (already
    # handled by if-not-news check above). Also handles entries with
    # None title/publisher gracefully.
    lines = []
    for n in news:
        title = n.get('title') or 'No Title'
        publisher = n.get('publisher') or 'Unknown'
        # V8.3.0 (Task G5): Hinglish translation with English stock
        # name preservation. Kabhi crash nahi karta.
        hinglish_title = _translate_title_to_hinglish(title)
        lines.append(f"\u2022 {hinglish_title} ({publisher})")
    return "\n".join(lines)


def clear_cache():
    """V8.2.0 NAYA (Improvement #8): Cache clear karne ke liye function.
    Testing/debugging ke liye useful - manually force re-fetch kar sakte
    hain bina process restart kiye."""
    with _cache_lock:
        _news_cache.clear()
