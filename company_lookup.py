"""
===========================================================
 COMPANY NAME LOOKUP (for natural-language chatbot)
===========================================================
User "Reliance ka analysis bhejo" jaisा likhta hai - "Reliance" ek
company NAAM hai, symbol "RELIANCE.NS" nahi. Ye module NIFTY 500 ki
cached company-name list (nifty_symbols.py se) ke against fuzzy
match karta hai.

Kuch bahut common short-forms (TCS, SBI, L&T, M&M) company ke asli
naam mein literally nahi aate ("Tata Consultancy Services", "State
Bank of India", "Larsen & Toubro", "Mahindra & Mahindra") - unke
liye ek chhoti ALIASES dict bhi hai.
===========================================================
"""

import re
import difflib

from nifty_symbols import get_symbol_name_map
from logger import logger

# V10.0: Expanded aliases — BHEL, BAJAJ FINANCE, etc add kiye gaye
ALIASES = {
    # IT
    "tcs": "TCS.NS",
    "infosys": "INFY.NS",
    "wipro": "WIPRO.NS",
    "hcl": "HCLTECH.NS",
    "hcl tech": "HCLTECH.NS",
    "tech mahindra": "TECHM.NS",
    # Banks
    "sbi": "SBIN.NS",
    "hdfc bank": "HDFCBANK.NS",
    "icici bank": "ICICIBANK.NS",
    "kotak": "KOTAKBANK.NS",
    "kotak bank": "KOTAKBANK.NS",
    "axis bank": "AXISBANK.NS",
    "pnb": "PNB.NS",
    "bank of baroda": "BANKBARODA.NS",
    "canara bank": "CANBK.NS",
    "yes bank": "YESBANK.NS",
    "idfc": "IDFCFIRSTB.NS",
    "bandhan": "BANDHANBNK.NS",
    "rbl bank": "RBLBANK.NS",
    "federal bank": "FEDERALBNK.NS",
    # Conglomerates
    "reliance": "RELIANCE.NS",
    "tata": "TATAMOTORS.NS",  # default tata = motors
    "tata motors": "TATAMOTORS.NS",
    "tata steel": "TATASTEEL.NS",
    "tata power": "TATAPOWER.NS",
    "tata chemicals": "TATACHEM.NS",
    "tata consumer": "TATACONSUM.NS",
    "tata invest": "TATAINVEST.NS",
    "tata elxsi": "TATAELXSI.NS",
    # Engineering/Construction
    "l&t": "LT.NS",
    "lt": "LT.NS",
    "larsen": "LT.NS",
    "larsen & toubro": "LT.NS",
    "bhel": "BHEL.NS",
    "bharat heavy": "BHEL.NS",
    "siemens": "SIEMENS.NS",
    "abb": "ABB.NS",
    "hal": "HAL.NS",
    "hindustan aeronautics": "HAL.NS",
    "bel": "BEL.NS",
    "bharat electronics": "BEL.NS",
    # Auto
    "m&m": "M&M.NS",
    "mahindra": "M&M.NS",
    "maruti": "MARUTI.NS",
    "maruti suzuki": "MARUTI.NS",
    "hero": "HEROMOTOCO.NS",
    "hero motocorp": "HEROMOTOCO.NS",
    "eicher": "EICHERMOT.NS",
    "bajaj auto": "BAJAJ-AUTO.NS",
    "tvs": "TVSMOTOR.NS",
    "tvs motor": "TVSMOTOR.NS",
    "ashok leyland": "ASHOKLEY.NS",
    "escort": "ESCORTS.NS",
    # Pharma
    "sun pharma": "SUNPHARMA.NS",
    "sunpharma": "SUNPHARMA.NS",
    "dr reddy": "DRREDDY.NS",
    "drreddy": "DRREDDY.NS",
    "cipla": "CIPLA.NS",
    "divis": "DIVISLAB.NS",
    "divi's": "DIVISLAB.NS",
    "lupin": "LUPIN.NS",
    "aurobindo": "AUROPHARMA.NS",
    "biocon": "BIOCON.NS",
    "apollo": "APOLLOHOSP.NS",
    "apollo hospital": "APOLLOHOSP.NS",
    # FMCG
    "itc": "ITC.NS",
    "hul": "HINDUNILVR.NS",
    "hindustan unilever": "HINDUNILVR.NS",
    "nestle": "NESTLEIND.NS",
    "britannia": "BRITANNIA.NS",
    "dabur": "DABUR.NS",
    "godrej": "GODREJCP.NS",
    "godrej consumer": "GODREJCP.NS",
    "marico": "MARICO.NS",
    "colgate": "COLPAL.NS",
    "asian paints": "ASIANPAINT.NS",
    "berger": "BERGEPAINT.NS",
    "titan": "TITAN.NS",
    # Bajaj group
    "bajaj finance": "BAJFINANCE.NS",
    "bajaj finserv": "BAJAJFINSV.NS",
    "bajfinance": "BAJFINANCE.NS",
    "bajaj": "BAJFINANCE.NS",  # default bajaj = finance
    # Adani group
    "adani": "ADANIENT.NS",
    "adani enterprises": "ADANIENT.NS",
    "adani ports": "ADANIPORTS.NS",
    "adani green": "ADANIGREEN.NS",
    "adani power": "ADANIPOWER.NS",
    "adani total": "ADANITOTAL.NS",
    # Energy/Oil
    "ongc": "ONGC.NS",
    "ntpc": "NTPC.NS",
    "power grid": "POWERGRID.NS",
    "powergrid": "POWERGRID.NS",
    "bpcl": "BPCL.NS",
    "ioc": "IOC.NS",
    "gail": "GAIL.NS",
    "coal india": "COALINDIA.NS",
    "coalindia": "COALINDIA.NS",
    "petronet": "PETRONET.NS",
    # Metals
    "tata steel": "TATASTEEL.NS",
    "jsw steel": "JSWSTEEL.NS",
    "hindalco": "HINDALCO.NS",
    "vedanta": "VEDL.NS",
    "vedl": "VEDL.NS",
    "sail": "SAIL.NS",
    "jindal steel": "JINDALSTEL.NS",
    "jindalstel": "JINDALSTEL.NS",
    "nmdc": "NMDC.NS",
    "hindzinc": "HINDZINC.NS",
    "hindustan zinc": "HINDZINC.NS",
    # Telecom
    "airtel": "BHARTIARTL.NS",
    "bharti airtel": "BHARTIARTL.NS",
    "idea": "IDEA.NS",
    "indus tower": "INDUSTOWER.NS",
    # Cement
    "ultratech": "ULTRACEMCO.NS",
    "ultratech cement": "ULTRACEMCO.NS",
    "grasim": "GRASIM.NS",
    "ambuja": "AMBUJACEM.NS",
    "acc cement": "ACC.NS",
    "shree cement": "SHREECEM.NS",
    # Insurance
    "hdfc life": "HDFCLIFE.NS",
    "sbi life": "SBILIFE.NS",
    "icici prudential": "ICICIPRULI.NS",
    "icici lombard": "ICICIGI.NS",
    # Retail/Other
    "dmart": "DMART.NS",
    "avenue supermarts": "DMART.NS",
    "zomato": "ZOMATO.NS",
    "paytm": "PAYTM.NS",
    "one97": "PAYTM.NS",
    "nykaa": "NYKAA.NS",
    "fsn": "NYKAA.NS",
    "irctc": "IRCTC.NS",
    "naukri": "NAUKRI.NS",
    "info edge": "NAUKRI.NS",
    # Indices
    "nifty": "^NSEI",
    "nifty50": "^NSEI",
    "nifty 50": "^NSEI",
    "bank nifty": "^NSEBANK",
    "sensex": "^BSESN",
}

_name_map_cache = None


def _get_name_map():
    global _name_map_cache
    if _name_map_cache is None:
        _name_map_cache = get_symbol_name_map() or {}
    return _name_map_cache


def find_symbol_by_name(text, min_score=0.55):
    """
    text: koi bhi phrase (jisme company ka naam ho sakta hai)
    Return: matching symbol (jaise "RELIANCE.NS") ya None

    Approach: text ke andar se 1/2/3-word candidate phrases nikaal kar
    unhe (a) ALIASES dict, (b) official company names ke against
    match karta hai - substring match (high confidence) pehle try
    karta hai, phir fuzzy ratio match (typos/short-forms ke liye).

    V8.2.0 FIX: Empty / None / non-string input safely handle karta
    hai (pehle .lower() crash ho jaata tha None pe). Alias matching
    ke liye word-boundary regex use karte hain taaki "nifty 100" jaisa
    text alias "nifty" se galat match na ho jaaye ("nifty" word
    boundary ke saath match ho, lekin uske baad aur prefix nahi).
    """
    # V8.2.0: None / non-string input safely reject karo
    if not isinstance(text, str):
        return None

    text_lower = text.lower().strip()
    if not text_lower:
        return None

    # 1) Direct alias match - WORD-BOUNDARY regex use karte hain
    # (V8.2.0: pehle plain `alias in text_lower` use hota tha, jo
    # "nifty" alias ko "nifty 100" jais text mein bhi match karta
    # tha aur galat symbol ^NSEI return karta tha. Ab `\b` use karke
    # exact word match karte hain.)
    for alias, symbol in ALIASES.items():
        try:
            if re.search(rf"\b{re.escape(alias)}\b", text_lower):
                return symbol
        except re.error:
            # alias mein regex-special chars hain (already escaped),
            # fallback to plain substring
            if alias in text_lower:
                return symbol

    name_map = _get_name_map()
    if not name_map:
        return None

    words = text_lower.split()
    if not words:
        return None

    candidates = []
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            candidates.append(" ".join(words[i:i + n]))

    # 2) Substring match (high confidence) - candidate company name ke
    # andar kahin bhi aata ho
    for cand in candidates:
        if len(cand) < 3:
            continue
        for symbol, name in name_map.items():
            if not name:
                continue
            if cand in name.lower():
                return symbol

    # 3) Fuzzy ratio match (typos / partial names ke liye)
    best_symbol, best_score = None, 0.0
    for cand in candidates:
        if len(cand) < 3:
            continue
        for symbol, name in name_map.items():
            if not name:
                continue
            score = difflib.SequenceMatcher(None, cand, name.lower()).ratio()
            if score > best_score:
                best_score, best_symbol = score, symbol

    if best_score >= min_score:
        return best_symbol

    return None
