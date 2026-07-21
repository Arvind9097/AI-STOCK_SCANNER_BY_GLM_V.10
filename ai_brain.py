### 2. `ai_brain.py` (Updated for Gemini AI Brain)
Is file mein ab Gemini API Telegram par natural sawalon ka Hinglish jawab dega. Purana code delete karke ye paste karein:

```python:ai_brain.py
"""
===========================================================
 AI BRAIN — Gemini Conversational Engine (V10.2)
===========================================================
Bot ko AI jaisa natural Hinglish jawab dene ke liye. Pehle
rule-based NLU (nlu.py) intent detect karta hai. Agar UNKNOWN
aaye ya conversational question ho, to ye module Gemini API ko
context ke saath call karta hai.

Approach (3-tier):
  1. Rule-based NLU (fast, free)
  2. Stock-specific — data fetch + indicators + Gemini analysis.
  3. Gemini conversational — baki sab questions ke liye.

Fallback: agar GEMINI_API_KEY nahi hai ya Gemini fail ho, to
rule-based reply (generic helpful message) return hota hai.
===========================================================
"""

import json
import math
import os
import config
from logger import logger

# Safe config variables fetching
GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
AI_BRAIN_ENABLED = getattr(config, "AI_BRAIN_ENABLED", True)

try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


# System prompt — Gemini ko role + rules define karta hai
_SYSTEM_PROMPT = (
    "Tu ek experienced Indian stock market trader aur technical analyst hai. "
    "Telegram bot ke through users ko Hinglish mein jawab de. "
    "STRICT RULES:\n"
    "1. Stock names / company names ENGLISH mein likho (RELIANCE, TCS) — translate mat karo.\n"
    "2. Common words HINGLISH mein (Roman script Hindi): achha, kamzor, market, breakout.\n"
    "3. 2-4 lines mein concise, actionable jawab de. Lamba essay mat likh.\n"
    "4. Sirf technical analysis — koi financial advice nahi. Numbers/data de.\n"
    "5. Agar context mein data hai to use kar, agar nahi to honestly bolo.\n"
    "6. Emoji use kar (🟢🔴🚀🎯) but moderate — har line par nahi."
)

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (ValueError, TypeError):
        return default

def _call_gemini(user_question, context_text):
    """Gemini API call karta hai."""
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return None
        
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        full_prompt = f"SYSTEM INSTRUCTIONS:\n{_SYSTEM_PROMPT}\n\nCONTEXT DATA:\n{context_text}\n\nUSER KA SAWAL:\n{user_question}"
        
        response = model.generate_content(full_prompt)
        if response and response.text:
            return response.text.strip()
        return None
    except Exception as e:
        logger.warning(f"AI Brain Gemini call fail: {e}")
        return None

def _build_context(intent, symbol=None):
    import datetime
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now_str = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        now_str = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")

    parts = [f"Current time: {now_str}"]

    try:
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
                    f"• {stk}: Signal={r['signal']}, Score={r['score']}/100, Entry=₹{r['entry_price']}, Target1=₹{r['target_1']}"
                )
    except Exception:
        pass

    if symbol:
        try:
            from stock_lookup import _fetch_with_cache
            from indicators import add_indicators
            from utils import clean_symbol
            df = _fetch_with_cache(symbol)
            if df is not None and not df.empty and len(df) >= 30:
                df_ind = add_indicators(df.copy())
                last = df_ind.dropna(subset=["Close"]).iloc[-1]
                
                close = safe_float(last.get("Close", 0))
                rsi = safe_float(last.get("RSI", 0))
                ema20 = safe_float(last.get("EMA20", 0))
                ema200 = safe_float(last.get("EMA200", 0))
                
                display = clean_symbol(symbol)
                parts.append(
                    f"\n=== {display} LIVE DATA ===\n"
                    f"Close: ₹{close:.2f} | RSI: {rsi:.1f}\n"
                    f"EMA20: ₹{ema20:.2f} | EMA200: ₹{ema200:.2f}"
                )
        except Exception as e:
            logger.debug(f"AI Brain context (stock {symbol}) fail: {e}")

    try:
        from news import fetch_stock_news
        news = fetch_stock_news(symbol or "^NSEI", limit=2)
        if news:
            parts.append("\n=== LATEST NEWS ===")
            for n in news:
                parts.append(f"• {n.get('title', '')[:80]}")
    except Exception:
        pass

    return "\n".join(parts)


def _fallback_reply(user_question):
    q = user_question.lower()
    if any(w in q for w in ["market", "bazaar", "kaisa", "trend"]):
        return (
            "📊 Market data abhi fetch nahi ho paya. Try:\n"
            "• \"nifty trend\" — NIFTY status\n"
            "• \"top picks\" — aaj ke best stocks\n"
            "Ya koi stock naam likho jaise \"RELIANCE\""
        )
    return (
        "🤖 Main ye sab kar sakta hoon:\n"
        "• Stock analysis: \"RELIANCE analysis bhejo\"\n"
        "• Top picks: \"aaj ke top stocks\"\n"
        "• Active trades: \"mere active trades\""
    )


def ask_ai(user_question, intent="UNKNOWN", symbol=None):
    if not AI_BRAIN_ENABLED:
        return _fallback_reply(user_question)

    context = _build_context(intent, symbol=symbol)
    reply = _call_gemini(user_question, context)
    
    if reply:
        return reply

    return _fallback_reply(user_question)


def generate_evening_summary(day_stats):
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return _fallback_evening_summary(day_stats)

    try:
        context = "=== AAJ KA DIN KA SUMMARY ===\n"
        context += f"Total trades: {day_stats.get('total_trades', 0)}\n"
        context += f"Win rate: {day_stats.get('win_rate', 0):.1f}%\n"

        prompt = (
            "Aaj ke trading din ka summary dekh kar ek brief Hinglish analysis de. "
            "3-4 lines mein: kya achha hua, kya bura, aur kal kya expect kar sakte hain."
        )

        reply = _call_gemini(prompt, context)
        return reply or _fallback_evening_summary(day_stats)
    except Exception:
        return _fallback_evening_summary(day_stats)


def _fallback_evening_summary(day_stats):
    wr = day_stats.get("win_rate", 0)
    tt = day_stats.get("total_trades", 0)
    return (
        f"📊 AAJ KA DIN:\n"
        f"Total trades: {tt} | Win rate: {wr:.1f}%\n"
        f"Kal ke liye: 9:30 AM intraday picks ka wait karo."
    )
