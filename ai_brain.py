"""
===========================================================
 AI BRAIN — GLM Conversational Engine (V9.0)
===========================================================
Bot ko AI jaisa natural Hinglish jawab dene ke liye. Pehle
rule-based NLU (nlu.py) intent detect karta hai. Agar UNKNOWN
aaye ya conversational question ho, to ye module GLM API ko
context ke saath call karta hai.

Approach (3-tier):
  1. Rule-based NLU (fast, free) — known intents jaise
     "top picks", "active trades", "nifty trend" etc.
  2. Stock-specific — agar user ne stock naam likha, to data
     fetch + indicators + GLM se analysis likhwata hai.
  3. GLM conversational — baki sab questions (market kaisa,
     kal kya expect, best stock batao) GLM ko context de kar.

Context jo GLM ko diya jaata hai:
  - Aaj ke top picks (scanner output)
  - Market breadth (advances/declines/sentiment)
  - Active trades (open positions)
  - Latest news headlines
  - User ka sawal

GLM ko Hinglish mein reply karne ka instruction:
  - Stock names English (RELIANCE, TCS) — translate nahi
  - Common words Hinglish (achha, kamzor, market, breakout)
  - 2-4 lines, actionable, no financial advice disclaimer

Fallback: agar ZAI_API_KEY nahi hai ya GLM fail ho, to
rule-based reply (generic helpful message).
===========================================================
"""

import json
import math
from config import (
    ZAI_API_KEY, ZAI_API_BASE, ZAI_MODEL,
    AI_BRAIN_ENABLED, AI_BRAIN_MAX_TOKENS,
)
from logger import logger

# System prompt — GLM ko role + rules define karta hai
_SYSTEM_PROMPT = (
    "Tu ek experienced Indian stock market trader aur technical analyst hai. "
    "Telegram bot ke through users ko Hinglish mein jawab de. "
    "STRICT RULES:\n"
    "1. Stock names / company names / tickers ENGLISH mein likho (RELIANCE, TCS, M&M) — "
    "translate mat karo.\n"
    "2. Common words HINGLISH mein (Roman script Hindi): achha, kamzor, market, "
    "breakout, target, stoploss, momentum, volume, support, resistance.\n"
    "3. 2-4 lines mein concise, actionable jawab de. Lamba essay mat likh.\n"
    "4. Sirf technical analysis — koi financial advice nahi. Numbers/data de.\n"
    "5. Agar context mein data hai to use kar, agar nahi to honestly bolo.\n"
    "6. Emoji use kar (🟢🔴🚀🎯🛑) but moderate — har line par nahi.\n"
    "7. Agar user ne kisi stock ke baare mein pucha aur context mein uska "
    "data hai, to entry/SL/target levels de."
)


def _call_glm(user_question, context_text):
    """GLM/Gemini API call karta hai with retry. Return: reply text ya None.

    V9.9 FIX: Pehle 'if not ZAI_API_KEY: return None' guard tha jo V9.9 mein
    Gemini ko reachable hone se rok deta tha (ZAI balance khatam, GLM hata diya).
    glm_retry.call_glm_with_retry() ab internally Gemini route karta hai —
    agar GEMINI_API_KEY set hai to AI Brain kaam karega bina ZAI key ke bhi.
    """
    from gemini_fetcher import is_gemini_available
    if not ZAI_API_KEY and not is_gemini_available():
        return None  # Dono keys missing — AI disabled
    try:
        # V9.1.1: glm_retry.call_glm_with_retry() use karta hai — exponential
        # backoff on 429 (5s->10s->20s->40s->80s, max 5 attempts) + 2s rate
        # limiter (prevents burst 429 jab user turant-turat stocks query kare).
        # Pehle raw requests.post tha jo 429 par seedha fail ho jaata tha.
        from glm_retry import call_glm_with_retry

        url = f"{ZAI_API_BASE.rstrip('/')}/chat/completions"
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"CONTEXT:\n{context_text}\n\nUSER KA SAWAL: {user_question}"},
        ]
        headers = {
            "Authorization": f"Bearer {ZAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": ZAI_MODEL,
            "messages": messages,
            "max_tokens": AI_BRAIN_MAX_TOKENS,
            "temperature": 0.6,
        }

        text = call_glm_with_retry(url, headers, payload, timeout=30)
        return text  # already stripped, or None on failure
    except Exception as e:
        logger.warning(f"AI Brain GLM call fail: {e}")
        return None


def safe_float(val, default=0.0):
    """V10.1 FIX: Handles NaN values safely for new IPOs without enough history."""
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (ValueError, TypeError):
        return default


def _build_context(intent, symbol=None):
    """
    Intent ke hisaab se context data gather karta hai.
    GLM ko ye context diya jaata hai taaki accurate jawab de.
    """
    import datetime
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now_str = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        now_str = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")

    parts = [f"Current time: {now_str}"]

    try:
        # Market breadth
        from market_breadth import compute_market_breadth, format_breadth_text
        from scanner import scan
        # Note: scan() needs all_data — too heavy for chat. Skip live breadth,
        # use cached/last scan if available from DB.
    except Exception:
        pass

    try:
        # Active trades from DB
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stock, signal, score, entry_price, sl_price, target_1, status "
            "FROM recommendations WHERE status = 'OPEN' ORDER BY date_added DESC LIMIT 10"
        )
        rows = cursor.fetchall()
        conn.close()
        if rows:
            parts.append("\n=== ACTIVE OPEN TRADES (latest 10) ===")
            for r in rows:
                stk = r["stock"].replace(".NS", "")
                parts.append(
                    f"• {stk}: Signal={r['signal']}, Score={r['score']}/100, "
                    f"Entry=₹{r['entry_price']}, SL=₹{r['sl_price']}, "
                    f"Target1=₹{r['target_1']}, Status={r['status']}"
                )
        else:
            parts.append("\nActive trades: Abhi koi open position nahi hai.")
    except Exception as e:
        logger.debug(f"AI Brain context (trades) fail: {e}")

    # Stock-specific data
    if symbol:
        try:
            from stock_lookup import _fetch_with_cache
            from indicators import add_indicators
            from utils import clean_symbol
            df = _fetch_with_cache(symbol)
            if df is not None and len(df) >= 30:
                df_ind = add_indicators(df.copy())
                last = df_ind.dropna(subset=["Close"]).iloc[-1]
                
                # V10.1 FIX: Using safe_float to prevent math logic crash due to NaN values
                close = safe_float(last.get("Close", 0))
                rsi = safe_float(last.get("RSI", 0))
                adx = safe_float(last.get("ADX", 0))
                ema20 = safe_float(last.get("EMA20", 0))
                ema50 = safe_float(last.get("EMA50", 0))
                ema200 = safe_float(last.get("EMA200", 0))
                
                display = clean_symbol(symbol)
                parts.append(
                    f"\n=== {display} LIVE DATA ===\n"
                    f"Close: ₹{close:.2f} | RSI: {rsi:.1f} | ADX: {adx:.1f}\n"
                    f"EMA20: ₹{ema20:.2f} | EMA50: ₹{ema50:.2f} | EMA200: ₹{ema200:.2f}"
                )
                # Trend verdict
                if close > ema20 > ema50 > ema200:
                    parts.append("Trend: Strong UPTREND (bullish alignment)")
                elif close < ema200:
                    parts.append("Trend: DOWNTREND (below 200 EMA)")
                else:
                    parts.append("Trend: MIXED/Sideways")
        except Exception as e:
            logger.debug(f"AI Brain context (stock {symbol}) fail: {e}")

    # Latest news
    try:
        from news import fetch_stock_news
        news = fetch_stock_news(symbol or "^NSEI", limit=2)
        if news:
            parts.append("\n=== LATEST NEWS ===")
            for n in news:
                parts.append(f"• {n.get('title', '')[:80]} ({n.get('publisher', '')})")
    except Exception:
        pass

    return "\n".join(parts)


def _fallback_reply(user_question):
    """GLM unavailable hone par rule-based fallback reply."""
    q = user_question.lower()
    if any(w in q for w in ["market", "bazaar", "kaisa", "trend"]):
        return (
            "📊 Market data abhi fetch nahi ho paya. Try:\n"
            "• \"nifty trend\" — NIFTY status\n"
            "• \"top picks\" — aaj ke best stocks\n"
            "• \"active trades\" — open positions\n"
            "Ya koi stock naam likho jaise \"RELIANCE\""
        )
    if any(w in q for w in ["help", "madad", "kya kar"]):
        return (
            "🤖 Main ye sab kar sakta hoon:\n"
            "• Stock analysis: \"RELIANCE analysis bhejo\"\n"
            "• Top picks: \"aaj ke top stocks\"\n"
            "• Active trades: \"mere active trades\"\n"
            "• Market trend: \"nifty ka trend\"\n"
            "• Target hit: \"target hit stocks\"\n"
            "• Help: \"help\""
        )
    return (
        "Main samajh nahi paaya. Koi stock naam likho (jaise \"RELIANCE\") "
        "ya \"help\" bhejo. GLM AI abhi unavailable hai — rule-based mode chal raha hai."
    )


def ask_ai(user_question, intent="UNKNOWN", symbol=None):
    """
    MAIN ENTRY: User ke sawal ka AI jawab return karta hai.

    Args:
        user_question: raw user text
        intent: NLU se detect hua intent (default UNKNOWN)
        symbol: agar STOCK_ANALYSIS intent hai to symbol

    Return: Hinglish reply text (HTML-safe for Telegram).
    """
    if not AI_BRAIN_ENABLED:
        return _fallback_reply(user_question)

    # Context gather karo
    context = _build_context(intent, symbol=symbol)

    # GLM call
    reply = _call_glm(user_question, context)
    if reply:
        return reply

    # Fallback
    return _fallback_reply(user_question)


def generate_evening_summary(day_stats):
    """
    8 PM evening report ke liye GLM se poore din ka Hinglish summary.

    Args:
        day_stats: dict with keys like:
            - intraday_picks: list of {stock, entry, target, sl, result}
            - swing_picks: list of {stock, entry, target, sl, result}
            - btst_picks: list of {stock, entry, target, sl, result}
            - win_rate: float
            - total_trades: int
            - target_hits: int
            - sl_hits: int

    Return: Hinglish summary text (2-4 paragraphs).
    """
    if not ZAI_API_KEY:
        return _fallback_evening_summary(day_stats)

    try:
        context = "=== AAJ KA DIN KA SUMMARY ===\n"
        context += f"Total trades: {day_stats.get('total_trades', 0)}\n"
        context += f"Win rate: {day_stats.get('win_rate', 0):.1f}%\n"
        context += f"Target hits: {day_stats.get('target_hits', 0)}\n"
        context += f"SL hits: {day_stats.get('sl_hits', 0)}\n"

        if day_stats.get("intraday_picks"):
            context += "\nIntraday picks result:\n"
            for p in day_stats["intraday_picks"][:5]:
                context += f"• {p.get('stock','')}: {p.get('result','?')}\n"

        if day_stats.get("swing_picks"):
            context += "\nSwing picks result:\n"
            for p in day_stats["swing_picks"][:5]:
                context += f"• {p.get('stock','')}: {p.get('result','?')}\n"

        if day_stats.get("btst_picks"):
            context += "\nBTST picks result:\n"
            for p in day_stats["btst_picks"][:3]:
                context += f"• {p.get('stock','')}: {p.get('result','?')}\n"

        prompt = (
            "Aaj ke trading din ka summary dekh kar ek brief Hinglish analysis de. "
            "3-4 lines mein: kya achha hua, kya bura, aur kal kya expect kar sakte hain. "
            "Stock names English mein. Koi financial advice nahi."
        )

        reply = _call_glm(prompt, context)
        return reply or _fallback_evening_summary(day_stats)
    except Exception as e:
        logger.warning(f"AI Brain evening summary fail: {e}")
        return _fallback_evening_summary(day_stats)


def _fallback_evening_summary(day_stats):
    """GLM unavailable hone par rule-based evening summary."""
    wr = day_stats.get("win_rate", 0)
    tt = day_stats.get("total_trades", 0)
    th = day_stats.get("target_hits", 0)
    sh = day_stats.get("sl_hits", 0)
    return (
        f"📊 AAJ KA DIN:\n"
        f"Total trades: {tt} | Win rate: {wr:.1f}%\n"
        f"🎯 Target hits: {th} | 🛑 SL hits: {sh}\n"
        f"Kal ke liye: market opening par GIFT Nifty dekho, "
        f"aur 9:30 AM intraday picks ka wait karo."
    )
