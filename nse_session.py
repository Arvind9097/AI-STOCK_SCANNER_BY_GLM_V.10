"""
===========================================================
 SHARED NSE SESSION HELPER
===========================================================
NSE ke API calls (marketStatus, fiidiiTradeReact, largedeal, etc.)
sabko same cookie-handshake pattern chahiye - pehle homepage visit
karo, phir actual API call karo. Ye function har jagah reuse hota hai
(nifty_symbols.py, nse_market_data.py, market_data_fetcher.py) taaki
code duplicate na ho.

V8.1.2 PERFORMANCE FIX: Pehle har call par NAYA session banta tha (poora
NSE-homepage round-trip) - 500-stock bulk scan mein ye matlab 500 baar
homepage fetch hoti thi, jabki NSE cookies typically kai minute tak valid
rehte hain. Ab session module-level CACHE hota hai (SESSION_TTL_SECONDS
tak reuse hota hai) - bulk scan mein ab poore scan ke liye sirf ek baar
(ya TTL expire hone par dobara) homepage-handshake hota hai, har symbol
ke liye nahi. Isse per-symbol overhead kaafi kam ho jaata hai.

V8.2.0 FIXES:
- THREAD-SAFETY: `_cached_session` / `_session_created_at` ko lock ke
  saath guard kiya gaya hai - bot_listener, breaking_news aur main.py
  scheduler sabhi threads mein se concurrent call hone par race condition
  na bane (otherwise dono threads naya session banate aur NSE par double
  homepage-request maarte).
- raise_for_status: agar homepage 403 de (cloud-block), tab bhi pehle
  chup-chaap bad session cache ho jaata tha. Ab raise_for_status()
  exception raise karega, cache poison nahi hoga.
===========================================================
"""

import time
import threading
import requests

NSE_HOME_URL = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Session itni der tak cache/reuse hoti hai (seconds) - NSE cookies
# generally isse zyada der tak valid rehte hain, lekin conservative
# rakha hai taaki bahut purani cookies par bhi na atke.
SESSION_TTL_SECONDS = 240  # 4 minute

_cached_session = None
_session_created_at = 0.0
_session_lock = threading.Lock()


def get_nse_session(timeout=10, force_new=False):
    """
    Ek requests.Session() deta hai jisme NSE ke liye valid cookies
    already set hain (homepage visit karke). Isse har API call
    successful hone ke chances badh jaate hain.

    V8.1.2: Ab session CACHE hoti hai (module-level) aur SESSION_TTL_SECONDS
    tak REUSE hoti hai - naya session (naya homepage round-trip) sirf
    tab banta hai jab:
      (a) pehli baar call ho raha ho, YA
      (b) cached session TTL se purani ho chuki ho, YA
      (c) force_new=True explicitly diya gaya ho (jaise koi request
          cookie-related reason se fail ho jaaye, caller naya session
          force kar sakta hai)

    V8.2.0: Lock ke andar cache check + assignment, taaki concurrent
    threads race condition mein do-bara homepage round-trip na karein.
    """
    global _cached_session, _session_created_at

    # Fast-path: bina lock ke purani session check karo (common case)
    age = time.time() - _session_created_at
    if not force_new and _cached_session is not None and age < SESSION_TTL_SECONDS:
        return _cached_session

    with _session_lock:
        # Lock milne ke baad dobara check karo - kisi aur thread ne
        # shayad beech mein naya session bana diya ho (double-checked lock)
        age = time.time() - _session_created_at
        if not force_new and _cached_session is not None and age < SESSION_TTL_SECONDS:
            return _cached_session

        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(NSE_HOME_URL, timeout=timeout)
        # V8.2.0: agar NSE homepage 403/5xx de raha hai (cloud block /
        # outage), to raise_for_status exception raise karega - hum
        # kabhi bhi beemar/invalid-cookies wale session ko cache nahi
        # karenge (warna agle 4 minute saari API calls silently fail
        # karti aur cache galat data serve karta).
        resp.raise_for_status()

        _cached_session = session
        _session_created_at = time.time()
        return session


def invalidate_nse_session():
    """
    Cached session ko turant invalidate karta hai - agla get_nse_session()
    call naya session banayega. Callers ise use kar sakte hain agar koi
    request cookie-expiry jaisi reason se fail ho jaaye (401/403 waghera),
    taaki agli call fresh cookies ke saath ho, TTL expire hone ka wait na
    karna pade.

    V8.2.0: Ab ACTUALLY called hai - market_data_fetcher.py aur
    nse_market_data.py dono mein 401/403 handler se call hota hai
    (pehle ye dead code tha, ab live hai).
    """
    global _cached_session, _session_created_at
    with _session_lock:
        _cached_session = None
        _session_created_at = 0.0
