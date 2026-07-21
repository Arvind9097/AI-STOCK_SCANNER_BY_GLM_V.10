"""
===========================================================
 AI ANALYSIS MODULE  (V10.2 — Google Gemini API)
===========================================================
Har stock ke liye ek chhota sa human-readable analysis text
banata hai.

AI_MODE = "RULE_BASED" (Secondary/Fallback):
    Koi API key nahi chahiye. Indicators ke basis par
    templated Hindi/English analysis generate hota hai.
    Instant + 100% Free.

AI_MODE = "GEMINI_API" (Primary):
    Google ke Gemini API (gemini-1.5-flash) ko call karke real
    natural-language analysis banwata hai.
    Gemini ka free tier kaafi bada hai (15 req/min). Agar limit 
    hit hoti hai, toh ye automatically RULE_BASED par shift ho jayega.

    API key: https://aistudio.google.com/ par jakar free API key lo,
             .env mein GEMINI_API_KEY=<key> daalo.
===========================================================
"""

import os
import config
from logger import logger

# Safe imports taaki agar config mein variables na ho toh crash na kare
AI_MODE = getattr(config, "AI_MODE", "RULE_BASED")
GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

# Sirf STRONG BUY / BUY signal wale stocks ke liye Gemini API call karo
# (Taaki free API limit cross na ho)
GEMINI_API_ONLY_FOR_SIGNALS = {"STRONG BUY", "BUY"}

try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("google-generativeai package missing. Please run: pip install google-generativeai")


def _rule_based_analysis(row):
    """Secondary Fallback AI: 100% Free, Offline, and Instant."""
    parts = []

    trend_ok = row.get("Close", 0) > row.get("EMA20", 0) > row.get("EMA50", 0) > row.get("EMA200", 0)
    if trend_ok:
        parts.append(f"{row['Stock']} strong uptrend mein hai (Close 20/50/200 EMA se upar).")
    elif row.get("Close", 0) < row.get("EMA200", 0):
        parts.append(f"{row['Stock']} long-term downtrend mein hai (Close 200 EMA se neeche).")
    else:
        parts.append(f"{row['Stock']} mixed trend dikha raha hai.")

    rsi = row.get("RSI", 50)
    if rsi > 70:
        parts.append(f"RSI {rsi} - overbought zone, short-term pullback possible.")
    elif rsi > 55:
        parts.append(f"RSI {rsi} - momentum bullish side par hai.")
    elif rsi < 30:
        parts.append(f"RSI {rsi} - oversold zone, bounce ho sakta hai.")
    else:
        parts.append(f"RSI {rsi} - neutral zone mein hai.")

    adx = row.get("ADX", 20)
    if adx > 25:
        parts.append(f"ADX {adx} batata hai trend strong hai.")
    elif adx < 15:
        parts.append(f"ADX {adx} kam hai, stock range-bound/sideways ho sakta hai.")

    if row.get("Breakout"):
        parts.append("Aaj 20-din ka high breakout hua hai.")

    if row.get("Volume_Spike"):
        parts.append("Volume mein spike hai - institutional interest ka sign ho sakta hai.")
    elif row.get("Volume_Dryup"):
        parts.append("Volume dry-up hai - bade move se pehle ki khaamoshi ho sakti hai.")

    if row.get("Patterns"):
        parts.append(f"Chart pattern dikha: {', '.join(row['Patterns'])}.")

    if row.get("Consolidating"):
        parts.append("Stock consolidation phase mein hai, breakout ka wait chal raha hai.")

    if row.get("Risk_Reward") is not None:
        parts.append(
            f"Entry ~{row.get('Entry')}, Stoploss ~{row.get('Stoploss')}, Target ~{row.get('Target')} "
            f"(Risk:Reward = 1:{row['Risk_Reward']})."
        )

    parts.append(f"Overall Score: {row.get('Score')}/100 -> Signal: {row.get('Signal')}.")

    return " ".join(parts)


def _gemini_api_analysis(row):
    """
    Primary AI: Google Gemini API.
    Agar rate limit ya koi error aata hai toh gracefully rule-based par shift hota hai.
    """
    try:
        prompt = (
            f"Neeche ek Indian stock ke technical indicators diye hain. Isko 2-3 lines mein "
            f"Hinglish (Hindi written in English alphabets) mein analyze karo. "
            f"Ek trader ke perspective se batao bina financial advice diye:\n\n"
            f"Stock: {row.get('Stock')}\n"
            f"Close: {row.get('Close')}, EMA20: {row.get('EMA20')}, EMA50: {row.get('EMA50')}, EMA200: {row.get('EMA200')}\n"
            f"RSI: {row.get('RSI')}, MACD: {row.get('MACD')} vs Signal: {row.get('MACD_SIGNAL')}\n"
            f"ADX: {row.get('ADX')}, Supertrend: {row.get('Supertrend')}\n"
            f"Volume Spike: {row.get('Volume_Spike')}, Breakout: {row.get('Breakout')}\n"
            f"Support: {row.get('Support')}, Resistance: {row.get('Resistance')}\n"
            f"Score: {row.get('Score')}/100, Signal: {row.get('Signal')}"
        )

        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        
        if response and response.text:
            return response.text.strip()
        else:
            raise ValueError("Empty response from Gemini")

    except Exception as e:
        logger.warning(
            f"{row.get('Stock')}: Gemini API analysis fail hui ({e}), Rule-Based analysis use kar raha hoon"
        )
        return _rule_based_analysis(row)


def generate_analysis(row):
    """
    Row ke basis par analysis text generate karta hai.
    """
    if AI_MODE in ["GEMINI_API", "GLM_API"]:
        if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
            logger.warning(
                "GEMINI_API_KEY missing ya package install nahi. Rule-Based mode use ho raha hai."
            )
            return _rule_based_analysis(row)

        if row.get("Signal") in GEMINI_API_ONLY_FOR_SIGNALS:
            return _gemini_api_analysis(row)

    return _rule_based_analysis(row)
