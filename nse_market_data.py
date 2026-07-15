"""
===========================================================
 NSE MARKET DATA (GIFT Nifty, FII/DII, Bulk/Block Deals, Pre-Open)
===========================================================
Ye sab NSE ki apni official website se FREE mil jaata hai (koi paid
subscription nahi chahiye) - lekin ye "unofficial" hai (NSE ka koi
published public API contract nahi hai), isliye:
- Endpoints kabhi bhi NSE ki taraf se badal sakte hain
- Response schema kabhi bhi change ho sakta hai
- NSE kabhi kabhi rate-limit/block kar deta hai

Isliye HAR function try/except mein hai aur fail hone par None/empty
return karta hai (crash nahi karta) - calling code (main.py) ko
gracefully handle karna chahiye ("data available nahi hai" jaisa).

V8.2.0 FIXES:
- 401/403 retry: agar NSE cookie-expiry (401/403) jaisa response aaye,
  to invalidate_nse_session() call karke ek baar retry karte hain -
  pehle ye dead code tha (invalidate function kabhi call nahi hota tha).
- `data.get("data", {})` pattern ko `(data.get("data") or {})` se
  replace kiya - agar NSE {"data": null} return kare to AttributeError
  nahi aata (None.get(...) crash).
- `float(gn.get("LASTPRICE"))` par None guard lagaya - schema change
  hone par misleading TypeError log nahi hoga.
===========================================================
"""

import requests

from nse_session import get_nse_session, invalidate_nse_session
from logger import logger

MARKET_STATUS_URL = "https://www.nseindia.com/api/marketStatus"
FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
LARGE_DEAL_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
PRE_OPEN_URL = "https://www.nseindia.com/api/market-data-pre-open"


def _nse_get_with_retry(url, params=None, timeout=10):
    """
    NSE API GET with one-shot 401/403 cookie-retry.
    Agar pehli request 401/403 (cookie expiry / cloud-block) de, to
    invalidate_nse_session() call karke fresh cookies le kar ek baar
    retry karta hai. Do baar fail ho to exception raise kar deta hai
    (caller ka apna try/except handle karega).
    """
    session = get_nse_session()
    resp = session.get(url, params=params, timeout=timeout)
    if resp.status_code in (401, 403):
        logger.warning(f"NSE {url} ne {resp.status_code} diya - session invalidate karke retry kar raha hoon")
        invalidate_nse_session()
        session = get_nse_session(force_new=True)
        resp = session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def get_gift_nifty():
    """
    Return: dict {last, change, pct_change, expiry} ya None (fail hone par)
    Source: NSE ka apna marketStatus API - isme 'giftnifty' key hoti hai.
    """
    try:
        resp = _nse_get_with_retry(MARKET_STATUS_URL, timeout=10)
        data = resp.json()

        # V8.2.0: `data.get("data", {})` agar NSE {"data": null} return
        # kare to None return karta hai (default {} use nahi hota),
        # phir None.get("giftnifty") AttributeError raise karta tha.
        # `or {}` pattern None ko empty dict mein convert karta hai.
        gn = data.get("giftnifty") or (data.get("data") or {}).get("giftnifty")
        if not gn:
            return None

        last_val = gn.get("LASTPRICE")
        change_val = gn.get("DAYCHANGE")
        pct_val = gn.get("PERCHANGE")

        # V8.2.0: None guard - agar key missing hai to float(None) TypeError
        # deta tha (misleading log). Ab 0.0 default use karte hain.
        return {
            "last": float(last_val) if last_val is not None else 0.0,
            "change": float(change_val) if change_val is not None else 0.0,
            "pct_change": float(pct_val) if pct_val is not None else 0.0,
            "expiry": gn.get("EXPIRYDATE", ""),
        }
    except Exception as e:
        logger.warning(f"GIFT Nifty fetch fail: {e}")
        return None


def get_fii_dii_report():
    """
    Return: dict {"FII": {buy, sell, net, date}, "DII": {...}} ya None
    Source: NSE ka fiidiiTradeReact API (previous trading din ka
    provisional cash-market data).
    """
    try:
        resp = _nse_get_with_retry(FII_DII_URL, timeout=10)
        rows = resp.json()

        result = {}
        for row in rows or []:
            category = row.get("category", "")
            key = "FII" if "FII" in category.upper() or "FPI" in category.upper() else "DII"
            result[key] = {
                "date": row.get("date", ""),
                "buy": float(row.get("buyValue", 0) or 0),
                "sell": float(row.get("sellValue", 0) or 0),
                "net": float(row.get("netValue", 0) or 0),
            }

        return result if result else None
    except Exception as e:
        logger.warning(f"FII/DII fetch fail: {e}")
        return None


def get_bulk_block_deals(top_n=10):
    """
    Return: dict {"bulk": [...], "block": [...]} (har item mein symbol,
    client_name, deal_type, quantity, price hota hai) ya None
    """
    try:
        resp = _nse_get_with_retry(
            LARGE_DEAL_URL,
            params={"index": "equities", "bandtype": "bulk_deals"},
            timeout=10,
        )
        data = resp.json()

        def _parse(rows):
            out = []
            for r in (rows or [])[:top_n]:
                out.append({
                    "symbol": r.get("symbol", r.get("BD_SYMBOL", "")),
                    "client": r.get("clientName", r.get("BD_CLIENT_NAME", "")),
                    "deal_type": r.get("buySell", r.get("BD_BUY_SELL", "")),
                    "quantity": r.get("qty", r.get("BD_QTY_TRD", "")),
                    "price": r.get("price", r.get("BD_TP_WATP", "")),
                })
            return out

        # V8.2.0: `data.get("data", {})` -> `(data.get("data") or {})` -
        # agar NSE {"data": null} return kare to None.get(...) crash.
        d_obj = data.get("data") or {}
        bulk = _parse(data.get("BULK_DEALS_DATA") or d_obj.get("BULK_DEALS_DATA"))
        block = _parse(data.get("BLOCK_DEALS_DATA") or d_obj.get("BLOCK_DEALS_DATA"))

        if not bulk and not block:
            return None

        return {"bulk": bulk, "block": block}
    except Exception as e:
        logger.warning(f"Bulk/Block deals fetch fail: {e}")
        return None


def get_pre_open_movers(top_n=8):
    """
    Return: dict {"gainers": [...], "losers": [...]} - pre-open session
    ke top gap-up/gap-down stocks ya None (fail hone par).

    NOTE: Ye endpoint ka exact schema NSE kabhi badal sakta hai - isliye
    defensive parsing hai (multiple possible key names try karte hain).
    Fail hone par pura pipeline nahi रुकता, sirf ye section skip hota hai.
    """
    try:
        resp = _nse_get_with_retry(PRE_OPEN_URL, params={"key": "ALL"}, timeout=10)
        data = resp.json()

        rows = data.get("data") or []
        if not rows:
            return None

        parsed = []
        for r in rows:
            meta = r.get("metadata", r)
            try:
                pct = float(meta.get("pChange", meta.get("perChange", 0)) or 0)
            except (TypeError, ValueError):
                continue
            parsed.append({
                "symbol": meta.get("symbol", ""),
                "last_price": meta.get("lastPrice", meta.get("iep", "")),
                "pct_change": pct,
            })

        if not parsed:
            return None

        parsed.sort(key=lambda x: x["pct_change"], reverse=True)
        gainers = [p for p in parsed if p["pct_change"] > 0][:top_n]
        losers = sorted([p for p in parsed if p["pct_change"] < 0], key=lambda x: x["pct_change"])[:top_n]

        return {"gainers": gainers, "losers": losers}
    except Exception as e:
        logger.warning(f"Pre-open movers fetch fail: {e}")
        return None


def get_sectoral_indices_performance():
    """
    NAYA (document requirement, 4 PM report): Sectoral indices ka
    aaj ka % change - Nifty Bank, Nifty IT, Nifty Auto, waghera
    (config.py ke SECTORAL_INDICES dict se list aati hai).

    Data source: market_data_fetcher.py ka fetch_daily_ohlcv() (jo
    already index-symbols ke liye seedha yfinance secondary use
    karta hai - dekho market_data_fetcher.py ka "^" prefix check).
    Isliye ye function directly primary/secondary chain reuse karta
    hai, koi naya NSE endpoint nahi banaya.

    Return: list of dicts [{"name": "NIFTY BANK", "pct_change": 1.23,
    "last_close": 51234.56}, ...] (sorted by pct_change descending)
    ya [] agar sab fail ho jaayein (kabhi crash nahi karta)
    """
    from config import SECTORAL_INDICES
    from market_data_fetcher import fetch_daily_ohlcv

    results = []
    for name, symbol in SECTORAL_INDICES.items():
        try:
            df = fetch_daily_ohlcv(symbol, period="5d")
            if df is None or len(df) < 2:
                continue

            # V8.2.0: dropna - yfinance kabhi-kabhi market-open hone par
            # aaj ka Close NaN deta hai, jis-se NaN% dikhta tha report mein.
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                continue

            today_close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            if prev_close <= 0:
                continue

            pct_change = round((today_close - prev_close) / prev_close * 100, 2)
            results.append({
                "name": name,
                "pct_change": pct_change,
                "last_close": round(today_close, 2),
            })
        except Exception as e:
            logger.debug(f"{name} ({symbol}): sectoral index fetch fail ({e})")
            continue

    results.sort(key=lambda r: r["pct_change"], reverse=True)
    return results
