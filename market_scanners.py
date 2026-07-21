"""
===========================================================
 MARKET SCANNERS (V9.7 — Top Gainers, 52w High, Volume Surge)
===========================================================
Upstox/Exchange pre-built lists (trending/gainers/most bought) नहीं
देता, इसलिए हम खुद compute करते हैं:

  1. Top Gainers — आज सबसे ज्यादा बढ़ने वाले stocks
  2. Near 52-Week High — 52-week high के पास वाले stocks
  3. Volume Surge / Most Active — आज volume सबसे ज्यादा वाले
  4. Trending Stocks — high volume + breakout + price action

DATA SOURCE:
  - Upstox API (अगर token available) → fast, 25 req/sec
  - Yahoo Direct API (fallback) → reliable, 2000 req/hr
  - Static universe CSV (symbol list)

USAGE:
    from market_scanners import scan_top_gainers, scan_52w_high
    gainers = scan_top_gainers(top_n=10)
    highs = scan_52w_high(top_n=10)
    # Returns list of dicts, ready for Telegram display
===========================================================
"""

import os
import logging
import time
from typing import List, Dict, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _get_symbols() -> List[str]:
    """Get stock universe from CSV या static fallback."""
    try:
        from universe_fetcher import get_stock_symbols
        symbols = get_stock_symbols()
        if symbols:
            return symbols[:500]  # cap at 500 for speed
    except Exception:
        pass

    # Static fallback (top 50 liquid)
    return [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "SBIN.NS", "ITC.NS", "BHARTIARTL.NS", "LT.NS", "HINDUNILVR.NS",
        "KOTAKBANK.NS", "AXISBANK.NS", "MARUTI.NS", "ASIANPAINT.NS", "WIPRO.NS",
        "HCLTECH.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS", "TATAMOTORS.NS",
        "TATASTEEL.NS", "SUNPHARMA.NS", "ULTRACEMCO.NS", "TITAN.NS", "NESTLEIND.NS",
        "BAJFINANCE.NS", "BAJAJFINSV.NS", "ADANIPORTS.NS", "ADANIENT.NS", "JSWSTEEL.NS",
        "GRASIM.NS", "CIPLA.NS", "COALINDIA.NS", "BPCL.NS", "HEROMOTOCO.NS",
        "DRREDDY.NS", "DIVISLAB.NS", "BRITANNIA.NS", "EICHERMOT.NS", "SHRIRAMFIN.NS",
        "BAJAJ-AUTO.NS", "M&M.NS", "TATACONSUM.NS", "ADANIGREEN.NS", "HDFCLIFE.NS",
        "SBILIFE.NS", "TECHM.NS", "INDUSINDBK.NS", "LICI.NS", "DMART.NS",
    ]


def _fetch_stock_data(symbol: str) -> Optional[Dict]:
    """
    Single stock का data fetch करो (5-day + today's change + volume).
    Returns dict with: symbol, close, prev_close, change_pct, volume, 52w_high
    """
    try:
        from market_data_fetcher import fetch_daily_ohlcv
        df = fetch_daily_ohlcv(symbol, period="1y")
        if df is None or df.empty or len(df) < 2:
            return None

        today = df.iloc[-1]
        prev = df.iloc[-2]

        close = float(today["Close"])
        prev_close = float(prev["Close"])
        change_pct = ((close - prev_close) / prev_close) * 100 if prev_close > 0 else 0
        volume = float(today["Volume"]) if "Volume" in today and today["Volume"] == today["Volume"] else 0
        avg_volume = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
        high_52w = float(df["High"].max())
        low_52w = float(df["Low"].min())

        # Near 52w high check (within 5% of 52w high)
        near_52w_high = (close >= high_52w * 0.95)

        # Volume surge (today's volume > 2x avg)
        vol_surge = (avg_volume > 0 and volume > avg_volume * 2.0)
        vol_ratio = (volume / avg_volume) if avg_volume > 0 else 0

        return {
            "symbol": symbol,
            "close": round(close, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(volume),
            "avg_volume": int(avg_volume),
            "vol_ratio": round(vol_ratio, 2),
            "vol_surge": vol_surge,
            "52w_high": round(high_52w, 2),
            "52w_low": round(low_52w, 2),
            "near_52w_high": near_52w_high,
            "dist_from_52w_high_pct": round(((high_52w - close) / high_52w) * 100, 2) if high_52w > 0 else 0,
        }
    except Exception as e:
        logger.debug(f"Market scanner {symbol}: {e}")
        return None


def _batch_fetch(symbols: List[str], max_workers: int = 8) -> List[Dict]:
    """Parallel fetch multiple stocks."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_stock_data, sym): sym for sym in symbols}
        for future in as_completed(futures):
            try:
                data = future.result()
                if data:
                    results.append(data)
            except Exception:
                pass
    return results


# ═══════════════════════════════════════════════════════════════════
# SCANNER 1: TOP GAINERS
# ═══════════════════════════════════════════════════════════════════
def scan_top_gainers(top_n: int = 10) -> List[Dict]:
    """
    आज के top gainers (सबसे ज्यादा बढ़ने वाले stocks).
    Returns top N stocks sorted by % change (descending).
    """
    logger.info(f"Market Scanner: Top Gainers (top {top_n})")
    symbols = _get_symbols()
    all_data = _batch_fetch(symbols)

    # Filter: only positive change, sort by change_pct DESC
    gainers = [d for d in all_data if d["change_pct"] > 0]
    gainers.sort(key=lambda x: x["change_pct"], reverse=True)
    return gainers[:top_n]


# ═══════════════════════════════════════════════════════════════════
# SCANNER 2: NEAR 52-WEEK HIGH
# ═══════════════════════════════════════════════════════════════════
def scan_52w_high(top_n: int = 10, max_distance_pct: float = 5.0) -> List[Dict]:
    """
    52-week high के पास वाले stocks (within 5% of 52w high).
    """
    logger.info(f"Market Scanner: Near 52-Week High (top {top_n})")
    symbols = _get_symbols()
    all_data = _batch_fetch(symbols)

    # Filter: near 52w high, sort by distance (ascending = closest first)
    near_high = [d for d in all_data if d["near_52w_high"] and d["dist_from_52w_high_pct"] <= max_distance_pct]
    near_high.sort(key=lambda x: x["dist_from_52w_high_pct"])
    return near_high[:top_n]


# ═══════════════════════════════════════════════════════════════════
# SCANNER 3: VOLUME SURGE / MOST ACTIVE
# ═══════════════════════════════════════════════════════════════════
def scan_volume_surge(top_n: int = 10) -> List[Dict]:
    """
    आज volume सबसे ज्यादा वाले stocks (vol > 2x average).
    Most active stocks by volume.
    """
    logger.info(f"Market Scanner: Volume Surge (top {top_n})")
    symbols = _get_symbols()
    all_data = _batch_fetch(symbols)

    # Filter: volume surge, sort by vol_ratio DESC
    vol_surge = [d for d in all_data if d["vol_surge"]]
    vol_surge.sort(key=lambda x: x["vol_ratio"], reverse=True)
    return vol_surge[:top_n]


# ═══════════════════════════════════════════════════════════════════
# SCANNER 4: TRENDING STOCKS (volume + breakout + price action)
# ═══════════════════════════════════════════════════════════════════
def scan_trending(top_n: int = 10) -> List[Dict]:
    """
    Trending stocks = high volume + positive momentum + near 52w high.
    Combined score for "trending" determination.
    """
    logger.info(f"Market Scanner: Trending Stocks (top {top_n})")
    symbols = _get_symbols()
    all_data = _batch_fetch(symbols)

    # Score each stock: volume surge + gain + near 52w high
    for d in all_data:
        score = 0
        if d["vol_surge"]:
            score += 30
        if d["change_pct"] > 2:
            score += 25
        elif d["change_pct"] > 0:
            score += 10
        if d["near_52w_high"]:
            score += 25
        if d["vol_ratio"] > 3:
            score += 20
        d["trending_score"] = score

    # Filter: score > 30, sort by trending_score DESC
    trending = [d for d in all_data if d["trending_score"] >= 30]
    trending.sort(key=lambda x: x["trending_score"], reverse=True)
    return trending[:top_n]


# ═══════════════════════════════════════════════════════════════════
# FORMAT FOR TELEGRAM
# ═══════════════════════════════════════════════════════════════════
def format_scanner_telegram(scanner_type: str, results: List[Dict]) -> str:
    """
    Format scanner results for Telegram (HTML).
    """
    from utils import escape_html, clean_symbol

    titles = {
        "gainers": "🔥 TOP GAINERS",
        "losers": "🔻 TOP LOSERS",
        "52w_high": "🎯 NEAR 52-WEEK HIGH",
        "volume": "📊 VOLUME SURGE / MOST ACTIVE",
        "trending": "🚀 TRENDING STOCKS",
    }

    title = titles.get(scanner_type, "MARKET SCAN")
    lines = [f"{title} (Live)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    if not results:
        lines.append("Abhi koi qualifying stock nahi mila.")
        return "\n".join(lines)

    for i, d in enumerate(results, 1):
        sym = escape_html(clean_symbol(d["symbol"]))
        close = d["close"]
        change = d["change_pct"]

        # Color emoji
        if change > 0:
            emoji = "🟢"
            change_str = f"+{change}%"
        elif change < 0:
            emoji = "🔴"
            change_str = f"{change}%"
        else:
            emoji = "⚪"
            change_str = "0%"

        line = f"{i}. {emoji} <b>{sym}</b> — ₹{close} ({change_str})"

        if scanner_type == "volume":
            vol_ratio = d["vol_ratio"]
            line += f" | Vol: {vol_ratio}x avg"

        if scanner_type == "52w_high":
            dist = d["dist_from_52w_high_pct"]
            high = d["52w_high"]
            line += f" | 52w High: ₹{high} ({dist}% below)"

        if scanner_type == "trending":
            score = d["trending_score"]
            vol_r = d["vol_ratio"]
            line += f" | Score: {score} | Vol: {vol_r}x"

        lines.append(line)

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ Sirf technical data — apna research karo.")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("Market Scanners — Self Test")
    print("=" * 60)

    # Test formatting (mock data)
    mock_gainers = [
        {"symbol": "RELIANCE.NS", "close": 2950, "change_pct": 5.2, "vol_ratio": 1.5,
         "52w_high": 3100, "dist_from_52w_high_pct": 4.8, "near_52w_high": True,
         "vol_surge": False, "trending_score": 50},
        {"symbol": "TCS.NS", "close": 3850, "change_pct": 3.1, "vol_ratio": 1.2,
         "52w_high": 4000, "dist_from_52w_high_pct": 3.7, "near_52w_high": True,
         "vol_surge": False, "trending_score": 35},
    ]
    output = format_scanner_telegram("gainers", mock_gainers)
    print("\n--- Top Gainers Format ---")
    print(output[:200])
    assert "TOP GAINERS" in output
    assert "RELIANCE" in output
    print("  ✅ Format works")

    # Test 52w high format
    mock_high = [
        {"symbol": "INFY.NS", "close": 1500, "change_pct": 1.5,
         "52w_high": 1520, "dist_from_52w_high_pct": 1.3, "near_52w_high": True,
         "vol_surge": True, "vol_ratio": 2.5, "trending_score": 60},
    ]
    output2 = format_scanner_telegram("52w_high", mock_high)
    print("\n--- 52-Week High Format ---")
    print(output2[:200])
    assert "52-WEEK HIGH" in output2
    print("  ✅ 52w format works")

    print("\n" + "=" * 60)
    print("✅ Market Scanners module ready")
    print("=" * 60)
