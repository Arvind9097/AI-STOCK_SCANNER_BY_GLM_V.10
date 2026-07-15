"""
===========================================================
 MARKET BREADTH + SENTIMENT INDICATOR (V8.3.0 — my addition)
===========================================================
Professional trading desks market breadth dekh kar overall
sentiment judge karte hain. Ye module scan results se breadth
metrics calculate karta hai:

  - Advances vs Declines (kitne stocks upar, kitne neeche)
  - Market Breadth % (advances / total * 100)
  - 52-week high/low count
  - Bullish vs Bearish signal distribution
  - Overall sentiment label (BULLISH / BEARISH / NEUTRAL)

Ye Telegram dashboard aur PDF report ke top par dikhaya jaata
hai — taaki user ko context mile ki "market overall kaaisa hai"
before looking at individual stock picks.

Use: scan results pass karo, breadth dict return karta hai.
No network calls — pure computation on scan results.
===========================================================
"""

from logger import logger


def compute_market_breadth(scan_results):
    """
    Scan results se market breadth metrics calculate karta hai.

    Input: list of scan result dicts (scanner.py output)
    Output: dict with breadth metrics, ya None agar results empty.
    """
    if not scan_results:
        return None

    try:
        total = len(scan_results)
        advances = 0
        declines = 0
        unchanged = 0
        new_highs = 0
        bullish_signals = 0
        bearish_signals = 0
        strong_buy = 0
        buy = 0
        watch = 0

        for r in scan_results:
            # Advance/decline: Close vs EMA20 (short-term direction)
            close = r.get("Close")
            ema20 = r.get("EMA20")
            if close is not None and ema20 is not None:
                if close > ema20:
                    advances += 1
                elif close < ema20:
                    declines += 1
                else:
                    unchanged += 1

            # Breakout = new 20-day high
            if r.get("Breakout"):
                new_highs += 1

            # Signal distribution
            signal = (r.get("Signal") or "").upper()
            if signal == "STRONG BUY":
                strong_buy += 1
                bullish_signals += 1
            elif signal == "BUY":
                buy += 1
                bullish_signals += 1
            elif signal == "WATCH":
                watch += 1
            elif signal == "SELL / AVOID":
                bearish_signals += 1

        # Breadth %: advances / (advances + declines) * 100
        ad_total = advances + declines
        breadth_pct = (advances / ad_total * 100) if ad_total > 0 else 50.0

        # Overall sentiment
        if breadth_pct >= 65 and bullish_signals >= (total * 0.15):
            sentiment = "BULLISH"
            sentiment_emoji = "🟢"
        elif breadth_pct <= 35 and bearish_signals >= (total * 0.10):
            sentiment = "BEARISH"
            sentiment_emoji = "🔴"
        else:
            sentiment = "NEUTRAL"
            sentiment_emoji = "🟡"

        return {
            "total_scanned": total,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "new_highs": new_highs,
            "breadth_pct": round(breadth_pct, 1),
            "strong_buy": strong_buy,
            "buy": buy,
            "watch": watch,
            "sell_avoid": bearish_signals,
            "bullish_signals": bullish_signals,
            "bearish_signals": bearish_signals,
            "sentiment": sentiment,
            "sentiment_emoji": sentiment_emoji,
        }
    except Exception as e:
        logger.warning(f"Market breadth compute fail: {e}")
        return None


def format_breadth_text(breadth):
    """
    Breadth dict ko Telegram/PDF ke liye Hinglish text mein convert.
    """
    if not breadth:
        return ""

    return (
        f"\n{breadth['sentiment_emoji']} <b>MARKET SENTIMENT: {breadth['sentiment']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total Scanned: <b>{breadth['total_scanned']}</b> stocks (NSE + BSE)\n"
        f"📈 Advances: <b>{breadth['advances']}</b> | 📉 Declines: <b>{breadth['declines']}</b>\n"
        f"🎯 Breadth: <b>{breadth['breadth_pct']}%</b> "
        f"(kitne stocks upar chale rahe hain)\n"
        f"🚀 New 20-day Highs: <b>{breadth['new_highs']}</b>\n"
        f"🔥 Strong Buy: <b>{breadth['strong_buy']}</b> | "
        f"⚡ Buy: <b>{breadth['buy']}</b> | "
        f"👀 Watch: <b>{breadth['watch']}</b> | "
        f"⚠️ Sell: <b>{breadth['sell_avoid']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
