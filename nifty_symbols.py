"""
===========================================================
 NIFTY 500 SYMBOL LIST FETCHER
===========================================================
NSE apni website se scripted requests ko block karta hai agar
sahi headers/cookies na ho. Isliye yahan pehle NSE homepage hit
karke cookies leta hai, phir CSV list download karta hai.

Agar internet block ho / NSE fail ho jaaye, to local CSV file
(data/nifty500.csv) se list load hoti hai. Agar wo bhi na mile,
to safety ke liye CUSTOM_STOCKS (config.py) use hota hai taaki
scan कम से कम chal to sake.

V8.2.0 FIX: Pehle apni khud ki `requests.Session()` banaata tha -
ab nse_session.py ka shared `get_nse_session()` use karta hai.
Faayde: shared cookie cache (ek hi NSE homepage round-trip poore
process mein), consistent headers, aur 401/403 cookie-expiry
recovery automatically mil jaata hai.
===========================================================
"""

import os
import io
import csv
import pandas as pd

from config import NIFTY500_LOCAL_CSV, CUSTOM_STOCKS
from nse_session import get_nse_session, invalidate_nse_session
from logger import logger

NSE_HOME_URL = "https://www.nseindia.com"
NSE_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"


def _fetch_from_nse():
    """
    NSE se live NIFTY 500 list download karta hai. Return: (symbols, name_map)

    V8.2.0: nse_session.py ka shared session use karta hai (apna
    requests.Session() nahi banata) - shared cookies + 401/403
    cookie-expiry retry bhi automatic mil jaata hai.
    """
    session = get_nse_session()

    # V8.2.0: 401/403 cookie-expiry handler - agar archives endpoint
    # auth error de, to session invalidate karke ek baar retry karo.
    resp = session.get(NSE_CSV_URL, timeout=15)
    if resp.status_code in (401, 403):
        logger.warning(f"NSE CSV list {resp.status_code} mili - session invalidate karke retry")
        invalidate_nse_session()
        session = get_nse_session(force_new=True)
        resp = session.get(NSE_CSV_URL, timeout=15)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))

    if "Symbol" not in df.columns:
        raise ValueError("NSE CSV format unexpected, 'Symbol' column nahi mila")

    symbols = [f"{s.strip()}.NS" for s in df["Symbol"].dropna().tolist()]

    name_map = {}
    if "Company Name" in df.columns:
        for _, row in df.dropna(subset=["Symbol"]).iterrows():
            sym = f"{str(row['Symbol']).strip()}.NS"
            name_map[sym] = str(row.get("Company Name", "")).strip()

    return symbols, name_map


def _fetch_from_local_csv(path):
    """Local backup CSV se list load karta hai (agar user ne manually rakhi ho). Return: (symbols, name_map)"""
    if not os.path.exists(path):
        return None, {}

    df = pd.read_csv(path)
    if "Symbol" not in df.columns:
        return None, {}

    symbols = [f"{s.strip()}.NS" for s in df["Symbol"].dropna().tolist()]

    name_map = {}
    if "Company Name" in df.columns:
        for _, row in df.dropna(subset=["Symbol"]).iterrows():
            sym = f"{str(row['Symbol']).strip()}.NS"
            name_map[sym] = str(row.get("Company Name", "")).strip()

    return (symbols if symbols else None), name_map


def get_nifty500_symbols():
    """
    Return: list of NSE ticker symbols (e.g. "RELIANCE.NS")
    Order of preference: NSE live download -> local CSV -> CUSTOM_STOCKS fallback
    Company names bhi cache ho jaate hain (data/nifty500.csv mein
    "Company Name" column) - chatbot ki natural-language stock lookup
    (company_lookup.py) inhi ko use karti hai.
    """
    try:
        symbols, name_map = _fetch_from_nse()
        logger.info(f"NIFTY 500 list NSE se successfully fetch hui ({len(symbols)} stocks)")
        # cache karo taaki agli baar NSE down ho to bhi kaam chale
        os.makedirs(os.path.dirname(NIFTY500_LOCAL_CSV), exist_ok=True)
        with open(NIFTY500_LOCAL_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Symbol", "Company Name"])
            for s in symbols:
                writer.writerow([s.replace(".NS", ""), name_map.get(s, "")])
        return symbols

    except Exception as e:
        logger.warning(f"NSE se NIFTY 500 list download nahi ho payi: {e}")

    symbols, _ = _fetch_from_local_csv(NIFTY500_LOCAL_CSV)
    if symbols:
        logger.info(f"Local cached CSV se NIFTY 500 list li gayi ({len(symbols)} stocks)")
        return symbols

    logger.warning(
        "NIFTY 500 list kahin se nahi mili (NSE block + koi local CSV nahi). "
        "CUSTOM_STOCKS list use kar raha hoon. Behtar hoga ki "
        "https://www.nseindia.com/products-services/indices-nifty500-index "
        "se CSV manually download karke data/nifty500.csv mein daal do."
    )
    return CUSTOM_STOCKS


def get_symbol_name_map():
    """
    Return: dict {"RELIANCE.NS": "Reliance Industries Ltd.", ...}
    company_lookup.py (chatbot NLU) ke liye - agar cache nahi hai to
    khaali dict deta hai (chatbot fallback se direct-symbol lookup try karega).
    """
    _, name_map = _fetch_from_local_csv(NIFTY500_LOCAL_CSV)
    return name_map
