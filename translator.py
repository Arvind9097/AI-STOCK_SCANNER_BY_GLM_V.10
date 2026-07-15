"""
===========================================================
 HINDI / HINGLISH TRANSLATOR (graceful fallback + thread-safe + cache)
===========================================================
News headlines English mein aate hain (yfinance/NSE se) - user ne
Hindi/Hinglish mein chahiye hai. 'deep-translator' library (Google
Translate ka free tier wrap karti hai) use karte hain.

IMPORTANT: Agar library install nahi hai, ya translation fail ho
jaaye (internet issue, rate-limit), to ORIGINAL ENGLISH TEXT return
hota hai - kabhi crash nahi karta. Isliye pipeline hamesha chalta
rahega, worst case sirf kuch lines English mein reh jaayengi.

V8.2.0 FIX (Task F5):
  1. Thread-safe: `threading.Lock` se translator calls guarded.
     Pehle ek shared `_translator` instance concurrent calls mein
     race-kar sakta tha (deep-translator internal state corruption).
  2. `functools.lru_cache` (maxsize=200) - same headline dobara
     translate nahi hota (Google API rate-limit save).
  3. `max_len` truncation ab last-whitespace-boundary par hota hai -
     pehle mid-word cut ho jaata tha (broken Hindi/URLs).

V8.3.0 (Task G5) - HINGLISH SUPPORT:
  User requirement: "News always Hinglish me hona chahiye, jo common
  word hain jaise Hinglish me likhte hain, or stocks name English me
  rakhna hain."
  Problem: to_hindi() POORA text ko Devanagari mein translate karta
  tha - stock names (RELIANCE, TATA STEEL, M&M) bhi Devanagari mein
  ho jaate the (रिलायंस, टाटा स्टील) - traders ke liye padhne mein
  mushkil.
  Solution: Naya `to_hinglish()` function (to_hindi() as-is preserved
  for backward-compat). Do strategies:
    1. Agar ZAI_API_KEY set hai: GLM (Z.AI) API se Roman Hinglish
       translate karta hai - GLM Hinglish mein excellent hai. Prompt
       mein explicit instruction: "STOCK NAMES ENGLISH mein hi rakhna".
    2. Fallback (no API key ya GLM fail): Placeholder technique -
       stock names/tickers extract karke Unicode private-use-area
       chars (U+E000+) se replace, baaki text to_hindi() se Devanagari
       translate, phir placeholders restore. Output: Devanagari Hindi
       + English stock names (Option C - still better than full
       Devanagari jisme stock names bhi translate ho jaate the).
    3. Worst case (translator bhi unavailable): original English text.
  Thread-safe + LRU cached (maxsize=200).
===========================================================
"""

import re
import threading
from functools import lru_cache

from logger import logger

_translator = None
_translator_unavailable = False

# V8.2.0: thread-safety - shared GoogleTranslator instance ke concurrent
# calls race-karte the (internal requests.Session state corruption).
_translator_lock = threading.Lock()
# V8.2.0: cache-compute lock - functools.lru_cache reads are thread-safe
# but compute (miss + populate) is NOT. Multiple threads calling same
# uncached key simultaneously all execute the function, wasting API
# calls. This lock ensures compute-once semantics for cache misses.
_cache_lock = threading.Lock()


def _get_translator():
    global _translator, _translator_unavailable
    if _translator_unavailable:
        return None
    if _translator is None:
        try:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="en", target="hi")
        except ImportError:
            logger.warning(
                "'deep-translator' package installed nahi hai - Hindi translation "
                "skip hogi (English text use hoga). Install: pip install deep-translator"
            )
            _translator_unavailable = True
            return None
    return _translator


def _truncate_at_word_boundary(text, max_len):
    """
    V8.2.0: text ko max_len par cut karta hai, lekin mid-word nahi -
    last whitespace boundary par todta hai. Pehle direct text[:max_len]
    use hota tha, jo Hindi words aur URLs ko beech mein tod deta tha.
    """
    if not text or len(text) <= max_len:
        return text
    # last whitespace within max_len
    cut = text.rfind(" ", 0, max_len)
    if cut <= 0:
        # no whitespace found - hard cut (no choice)
        return text[:max_len]
    return text[:cut]


@lru_cache(maxsize=200)
def _translate_cached(snippet):
    """
    V8.2.0: LRU cache (maxsize=200) - same headline dobara translate
    nahi hota. Google free-tier rate-limit save.
    Thread-safe: translator call ke around lock liya jaata hai.
    """
    translator = _get_translator()
    if translator is None:
        return None  # signal caller to return original
    with _translator_lock:
        return translator.translate(snippet)


def _translate_thread_safe(snippet):
    """
    V8.2.0: Compute-once semantics for cache misses. functools.lru_cache
    is thread-safe for reads but NOT for misses - multiple threads
    calling same uncached key all execute the underlying function.
    This wrapper holds a lock during cache-miss compute so only one
    thread hits Google API; others wait + read from cache.
    """
    # Fast path: already cached? lru_cache read is thread-safe.
    cached = _translate_cached.cache_info()
    # Check if snippet is in cache by trying to peek (compute if missing).
    # We use _cache_lock to serialize miss-compute.
    with _cache_lock:
        # Inside lock - re-check cache (another thread may have populated)
        # by calling _translate_cached (will return cached value if present).
        return _translate_cached(snippet)


def to_hindi(text, max_len=450):
    """
    text: English string
    Return: Hindi translation, ya original text agar translation fail ho jaaye
    (kabhi exception nahi uthata - hamesha kuch na kuch return karta hai)

    V8.2.0: thread-safe + cached + word-boundary truncation.

    V8.3.0 NOTE: For news headlines jismein stock names hain, `to_hinglish()`
    use karna better hai - wo stock names English mein preserve karta hai.
    Ye function POORA text Devanagari mein translate karta hai (including
    stock names -> रिलायंस, टाटा स्टील). Backward-compat ke liye as-is
    rakha gaya hai (main.py / tracker.py isey directly call karte hain).
    """
    if not text or not text.strip():
        return text

    translator = _get_translator()
    if translator is None:
        return text

    try:
        # V8.2.0: truncate at word boundary (mid-word cut prevent)
        snippet = _truncate_at_word_boundary(text, max_len)
        result = _translate_thread_safe(snippet)
        return result if result else text
    except Exception as e:
        logger.warning(f"Hindi translation fail ({e}), original English text use kar raha hoon")
        return text


# ===========================================================
# V8.3.0 (Task G5): HINGLISH TRANSLATION
# ===========================================================
# User requirement: "News always Hinglish me hona chahiye... stocks
# name English me rakhna hain."
#
# to_hinglish() do strategies use karta hai:
#   1. GLM (Z.AI) API se Roman Hinglish - BEST quality, stock names
#      English mein preserve hote hain (prompt mein instruction).
#      Requires ZAI_API_KEY in env / config.
#   2. Fallback (no API key / GLM fail): Placeholder technique -
#      stock names extract -> Unicode PUA chars se replace ->
#      baaki text to_hindi() se Devanagari translate -> placeholders
#      restore. Output: Devanagari + English stock names.
#   3. Worst case: original English text (kabhi crash nahi).
#
# Placeholder technique ke liye Unicode PRIVATE USE AREA chars
# (U+E000+) use karte hain - Google Translate in chars ko untouched
# chhod deta hai (no Unicode name to translate), isliye restore
# reliable hai.
# ===========================================================

# Known stock symbols / indices jo HAMESHA English mein rakhne hain
# (inhe placeholder technique mein explicitly detect karke preserve
# karte hain). Disclaimer: ye exhaustive nahi hai - regex bhi all-caps
# tickers ko catch karta hai, isliye missing symbols automatically
# handle ho jaate hain. Ye set mainly documentation / clarity ke liye.
STOCK_SYMBOLS_PRESERVE = {
    # Indices
    "NIFTY", "SENSEX", "BANKNIFTY", "NIFTYBANK", "INDIAVIX", "VIX",
    "NIFTYIT", "NIFTYPHARMA", "NIFTYAUTO", "NIFTYMETAL", "NIFTYFMCG",
    "NIFTYPSE", "NIFTYNEXT50", "NIFTY50", "NIFTY100", "NIFTY200",
    "NIFTY500", "MIDCAP", "SMALLCAP", "LARGECAP",
    # Major NSE tickers (top by market cap - frequent in news)
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "TITAN", "TATASTEEL", "TATAMOTORS",
    "TATAPOWER", "TATACONSUM", "SUNPHARMA", "WIPRO", "HCLTECH",
    "ULTRACEMCO", "NESTLEIND", "BAJFINANCE", "BAJAJFINSV", "BAJAJ-AUTO",
    "ONGC", "NTPC", "POWERGRID", "COALINDIA", "ADANIENT", "ADANIPORTS",
    "JSWSTEEL", "HDFCLIFE", "SBILIFE", "TECHM", "DIVISLAB", "CIPLA",
    "DRREDDY", "GRASIM", "BRITANNIA", "EICHERMOT", "HEROMOTOCO",
    "BAJAJHLDNG", "INDUSINDBK", "SHRIRAMFIN", "BPCL", "IOC", "GAIL",
    "VEDL", "HINDALCO", "JSPL", "DLF", "PIDILITIND", "DABUR",
    "GODREJCP", "MARICO", "HAVELLS", "AMBUJACEM", "ACC", "SHREECEM",
    "UPL", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB",
    "BANKBARODA", "CANBK", "LICHSGFIN", "MMTC", "IRCTC", "IEX",
    "ZOMATO", "NYKAA", "PAYTM", "POLICYBZR", "CARTRADE",
    # Special-char tickers
    "M&M", "L&T", "J&K", "ABB",
}

# Common all-caps English words jo ticker-LIKE lagte hain (2+ caps)
# lekin actual mein common English words hain - inhe placeholder se
# EXCLUDE karte hain (translate hone dein). Glitches avoid karne ke
# liye - warna "THE STOCK fell" mein "THE" placeholder ban jaata.
_TICKER_LOOKALIKE_STOPWORDS = {
    "AN", "THE", "OF", "IN", "ON", "AT", "TO", "IS", "IT", "OR", "BY",
    "AS", "BE", "WE", "HE", "SO", "IF", "NO", "DO", "GO", "UP", "US",
    "UK", "EU", "UN", "VS", "AM", "PM", "ET", "PT", "IST", "GMT",
    "USA", "UAE", "GDP", "CFO", "CEO", "CTO", "CIO", "COO", "AGM",
    "EGM", "FY", "YTD", "QTD", "MTD", "PROMO", "TBA", "TBD", "FAQ",
    "API", "URL", "RSS", "HTML", "XML", "JSON", "PDF", "SMS",
}

# Regex: all-caps tickers (2+ chars, allows &/./- in middle).
# Matches: RELIANCE, TCS, M&M, L&T, BAJAJ-AUTO, Q1, Q2, NSE, BSE,
# FII, DII, IPO, RBI, SEBI, NIFTY, SENSEX, HDFCBANK, etc.
# Word boundary `\b` ensures we don't match inside other words.
_STOCK_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:[&.\-][A-Z0-9]+)*\b")

# Regex: Multi-word proper company names ending in common suffix.
# Matches: "Reliance Industries", "Tata Steel", "HDFC Bank",
# "Infosys Technologies", "Bajaj Finance", "State Bank of India" (no -
# that pattern is more complex, skip for now).
_STOCK_COMPANY_RE = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+"
    r"(?:Industries|Steel|Bank|Motors|Tech|Technologies|Pharma|"
    r"Power|Oil|Gas|Financial|Capital|Holdings|Enterprises|"
    r"Limited|Ltd|Corp|Corporation|Group|Securities|Finance|"
    r"Automobiles|Energy|Petroleum|Chemicals|Cement)\b"
)

# Regex: Known standalone brand/index names (title-case, no suffix).
# "Sensex", "Nifty", "Reliance", "Tata" etc - jab inke aage suffix
# na ho (jaise "Tata Steel" already _STOCK_COMPANY_RE se capture ho
# jaata) lekin news headline mein akele "Reliance" likha ho, tab ye
# regex inhe English mein preserve karta hai.
# Note: case-sensitive (title-case only - "Sensex" match, "SENSEX"
# already _STOCK_TICKER_RE se match).
_STOCK_BRAND_RE = re.compile(
    r"\b(?:Sensex|Nifty|Nifty\s*\d+|Nifty\s+Bank|Bank\s+Nifty|"
    r"Reliance|Tata|Infosys|Wipro|Maruti|Hindustan|Mahindra|"
    r"Adani|Bajaj|Bharti|Vedanta|Larsen|Toubro|Pidilite|Dabur|"
    r"Godrej|Britannia|Nestle|Cipla|Dr\.?\s*Reddy|Sun\s+Pharma|"
    r"Divi'?s|Asian\s+Paints|Akzo|Nobel|UltraTech|Shree\s+Cement)\b"
)

# GLM prompt for Hinglish translation - explicit instructions about
# stock name preservation and Roman script.
_GLM_HINGLISH_PROMPT = (
    "Neeche ek English news headline/summary hai. Ise HINGLISH "
    "(Roman script Hindi) mein translate karo.\n\n"
    "RULES:\n"
    "1. Common Hindi words ko Roman script mein likho (jaise: kamzor, "
    "mazboot, badha, gira, gir gaya, utha, store, lekin, kyunki, jaise).\n"
    "2. Financial/trading terms ENGLISH mein rakho: stock, share, "
    "market, breakout, target, SL, buy, sell, rally, surge, dip, "
    "results, Q1, Q2, Q3, Q4, IPO, FII, DII, RBI, SEBI, NSE, BSE, "
    "NIFTY, SENSEX, profit, loss, percent, point.\n"
    "3. STOCK NAMES / COMPANY NAMES / TICKERS ENGLISH mein hi rakho "
    "(translate mat karo). Jaise: Reliance, TATA STEEL, HDFC Bank, "
    "Infosys, M&M, TCS, SBIN, BAJAJ-AUTO.\n"
    "4. Numbers, percentages, dates, currency (\u20b9, Rs.), symbols "
    "unchanged rakho.\n"
    "5. Sirf translated Hinglish text do - koi explanation, "
    "quotation marks, ya markdown formatting nahi.\n\n"
    "English: {text}\n"
    "Hinglish:"
)

# Separate lock for Hinglish cache-compute - taaki to_hinglish()
# calls to_hindi() calls ko block na karein (different locks).
_hinglish_lock = threading.Lock()


def _placeholder_for_index(i):
    """
    Unique placeholder char for stock index i. Unicode PRIVATE USE
    AREA (U+E000 to U+E0FF) use karte hain - Google Translate in
    chars ko untouched chhod deta hai (no Unicode name to translate,
    no dictionary entry). Isliye restore reliable hai.

    Index 0..255 tak single char. 256+ (extremely unlikely - ek
    headline mein 256+ stock names) ke liye 2-char sequence.
    """
    if i < 0:
        raise ValueError("index cannot be negative")
    if i < 256:
        return chr(0xE000 + i)
    # 2-char sequence for indices 256+ (very rare)
    high = (i >> 8) & 0xFF
    low = i & 0xFF
    return chr(0xE000 + high) + chr(0xF000 + low)


def _extract_stock_names(text):
    """
    Text se stock names/tickers extract karke ordered list return
    karta hai (placeholder technique ke liye). Order:
      1. Multi-word company names FIRST (so "Reliance Industries"
         ek hi token ban ke preserve ho - alag-alag na ho).
      2. Known standalone brand/index names (Sensex, Nifty, Reliance).
      3. All-caps tickers (RELIANCE, TCS, M&M, Q1, NSE).

    Return: list of unique strings (insertion-order preserved).
    """
    found = []
    seen = set()

    # 1. Multi-word company names pehle capture karo
    for m in _STOCK_COMPANY_RE.finditer(text):
        name = m.group(0).strip()
        if name and name not in seen:
            found.append(name)
            seen.add(name)

    # 2. Known standalone brand/index names (Sensex, Nifty, Reliance, etc)
    for m in _STOCK_BRAND_RE.finditer(text):
        name = m.group(0).strip()
        # Normalize internal whitespace (regex allows \s+)
        name = re.sub(r"\s+", " ", name)
        if name and name not in seen:
            found.append(name)
            seen.add(name)

    # 3. All-caps tickers
    for m in _STOCK_TICKER_RE.finditer(text):
        name = m.group(0).strip()
        if not name or len(name) < 2:
            continue
        # Skip common English words (AN, THE, etc.)
        if name in _TICKER_LOOKALIKE_STOPWORDS:
            continue
        # Skip pure numbers (no letters - shouldn't match but defensive)
        if not re.search(r"[A-Z]", name):
            continue
        if name not in seen:
            found.append(name)
            seen.add(name)

    return found


def _replace_stocks_with_placeholders(text, names):
    """
    text mein har stock name ko unique Unicode PUA placeholder se
    replace karta hai. Order same as `names` list.

    Important: Longest names pehle replace karte hain taaki substring
    conflicts na ho (e.g., "TATA STEEL" pehle, "TATA" baad mein -
    warna "TATA" match "TATA STEEL" ke andar ho jaata aur placeholder
    galat jagah lag jaata).
    """
    # Sort by length DESCENDING (longest first) for safe replacement
    sorted_names = sorted(enumerate(names), key=lambda x: len(x[1]), reverse=True)
    result = text
    for original_idx, name in sorted_names:
        placeholder = _placeholder_for_index(original_idx)
        result = result.replace(name, placeholder)
    return result


def _restore_placeholders(text, names):
    """
    Unicode PUA placeholders wapas original English stock names se
    replace karta hai. Simple str.replace() - PUA chars unique hain
    isliye koi ambiguity nahi.
    """
    result = text
    for i, name in enumerate(names):
        placeholder = _placeholder_for_index(i)
        result = result.replace(placeholder, name)
    return result


def _translate_hinglish_glm(text):
    """
    GLM (Z.AI) API se Roman Hinglish translate karta hai.

    Return: Hinglish string ya None (fail / unavailable / no API key).
    KABHI exception raise nahi karta - always None on failure.
    """
    try:
        from config import ZAI_API_KEY, ZAI_API_BASE, ZAI_MODEL
    except Exception as e:
        logger.debug(f"GLM Hinglish: config import fail ({e})")
        return None

    if not ZAI_API_KEY:
        return None  # No API key - silently fallback to placeholder technique

    try:
        import requests
        url = f"{ZAI_API_BASE.rstrip('/')}/chat/completions"
        prompt = _GLM_HINGLISH_PROMPT.format(text=text)
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {ZAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": ZAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.3,  # low temp = consistent Hinglish
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content", "")
        if not content:
            return None
        # Clean up: strip markdown fences agar GLM ne wrap kiya
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        # Strip leading "Hinglish:" agar GLM ne prompt label repeat kiya
        content = re.sub(r"^Hinglish:\s*", "", content, flags=re.IGNORECASE)
        # Strip surrounding quotes agar hain
        if (content.startswith('"') and content.endswith('"')) or \
           (content.startswith("'") and content.endswith("'")):
            content = content[1:-1]
        return content.strip() or None
    except Exception as e:
        logger.debug(f"GLM Hinglish translation fail ({e})")
        return None


def _translate_hinglish_placeholder(text):
    """
    Placeholder technique: stock names extract -> PUA chars se replace
    -> baaki text to_hindi() se Devanagari translate -> placeholders
    restore. Output: Devanagari Hindi + English stock names embedded.

    Return: translated string ya None (fail).
    """
    names = _extract_stock_names(text)
    if names:
        placeholdered = _replace_stocks_with_placeholders(text, names)
        snippet = _truncate_at_word_boundary(placeholdered, 450)
        translated = _translate_thread_safe(snippet)
        if not translated:
            return None
        restored = _restore_placeholders(translated, names)
        return restored
    # No stock names found - direct translation
    snippet = _truncate_at_word_boundary(text, 450)
    return _translate_thread_safe(snippet)  # returns None if translator unavailable


@lru_cache(maxsize=200)
def _hinglish_cached(snippet):
    """
    LRU cache for Hinglish translations. Same headline dobara
    translate nahi hota (GLM API cost / Google rate-limit save).

    Returns: (source, result) tuple - source is "glm" / "placeholder" /
    None, result is the translated string or None.
    """
    # 1. GLM path first (Roman Hinglish - BEST quality)
    glm_result = _translate_hinglish_glm(snippet)
    if glm_result:
        return ("glm", glm_result)

    # 2. Fallback: placeholder + Devanagari
    placeholder_result = _translate_hinglish_placeholder(snippet)
    if placeholder_result:
        return ("placeholder", placeholder_result)

    # 3. Both failed
    return (None, None)


def to_hinglish(text, max_len=450):
    """
    V8.3.0 (Task G5): text ko Hinglish mein translate karta hai.
    Stock names/tickers ENGLISH mein preserve hote hain.

    Strategy (best-effort, kabhi crash nahi karta):
      1. ZAI_API_KEY set hai -> GLM API se Roman Hinglish (BEST).
         Prompt mein explicit: "STOCK NAMES ENGLISH mein rakho".
      2. No API key / GLM fail -> Placeholder technique:
         stock names -> Unicode PUA chars -> to_hindi() Devanagari
         translate -> restore English stock names. Output: Devanagari
         Hindi + English stock names.
      3. Sab fail -> original English text.

    Thread-safe (separate `_hinglish_lock` - to_hindi calls ke saath
    interference nahi) + LRU cached (maxsize=200).

    Args:
        text: English (ya mixed) string
        max_len: max chars to send to translator (default 450, word
                 boundary par truncate hota hai - mid-word cut nahi)

    Return: Hinglish/Roman-ya-Devanagari-with-English-stocks string.
            KABHI None nahi return karta - worst case original text.
    """
    if not text or not text.strip():
        return text

    # Truncate input at word boundary (avoid mid-word cuts)
    snippet = _truncate_at_word_boundary(text, max_len)

    try:
        # _hinglish_lock ensures compute-once for cache misses
        # (lru_cache reads are thread-safe, misses are not)
        with _hinglish_lock:
            source, result = _hinglish_cached(snippet)
        if result:
            return result
    except Exception as e:
        logger.debug(f"to_hinglish fail ({e}), original text use kar raha hoon")

    # Worst case - return original (kabhi crash nahi)
    return text
