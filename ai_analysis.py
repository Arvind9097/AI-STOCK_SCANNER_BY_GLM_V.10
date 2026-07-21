"""
===========================================================
 AI ANALYSIS MODULE  (V8.2.0 — Z.AI GLM API)
===========================================================
Har stock ke liye ek chhota sa human-readable analysis text
banata hai.

AI_MODE = "RULE_BASED" (default):
    Koi API key nahi chahiye. Indicators ke basis par
    templated Hindi/English analysis generate hota hai.
    500 stocks ke liye bhi instant + free.

AI_MODE = "GLM_API" (V8.2.0):
    Z.AI ke GLM API (OpenAI-compatible) ko call karke real
    natural-language analysis banwata hai. config.py / .env
    mein ZAI_API_KEY daalna zaroori hai.
    Dhyaan rahe - 500 stocks ke liye 500 API calls lagenge,
    isliye ye mode sirf STRONG BUY / BUY signal wale top
    stocks ke liye hi use hota hai (neeche GLM_API_ONLY_FOR_SIGNALS).

    API key: https://z.ai par account banao, API key lo,
             .env mein ZAI_API_KEY=<key> daalo.
    Model:   ZAI_MODEL (default "glm-4.5"). Agar "glm-5.2"
             available ho to .env mein ZAI_MODEL=glm-5.2 set karo.

V8.2.0 CHANGE: Pehle ye Anthropic Claude API use karta tha
(model "claude-sonnet-4-6" — jo ek INVALID model name tha,
isliye CLAUDE_API mode kabhi kaam hi nahi karta tha). Ab
Z.AI GLM API use hota hai jo OpenAI-compatible, sasta, aur
Hinglish analysis ke liye behtar hai.
===========================================================
"""

from config import AI_MODE, ZAI_API_KEY, ZAI_API_BASE, ZAI_MODEL
from logger import logger

# GLM_API mode mein sirf in signals ke liye real API call karo
# (baaki sab rule-based - taaki cost/time control mein rahe)
GLM_API_ONLY_FOR_SIGNALS = {"STRONG BUY", "BUY"}

# Backward-compat: purana "CLAUDE_API" mode naam bhi accept karo
# (warn karke GLM_API jaisa hi behave karega)
_CLAUDE_MODE_ALIAS = "CLAUDE_API"


def _rule_based_analysis(row):
    parts = []

    trend_ok = row["Close"] > row["EMA20"] > row["EMA50"] > row["EMA200"]
    if trend_ok:
        parts.append(f"{row['Stock']} strong uptrend mein hai (Close 20/50/200 EMA se upar).")
    elif row["Close"] < row["EMA200"]:
        parts.append(f"{row['Stock']} long-term downtrend mein hai (Close 200 EMA se neeche).")
    else:
        parts.append(f"{row['Stock']} mixed trend dikha raha hai.")

    if row["RSI"] > 70:
        parts.append(f"RSI {row['RSI']} - overbought zone, short-term pullback possible.")
    elif row["RSI"] > 55:
        parts.append(f"RSI {row['RSI']} - momentum bullish side par hai.")
    elif row["RSI"] < 30:
        parts.append(f"RSI {row['RSI']} - oversold zone, bounce ho sakta hai.")
    else:
        parts.append(f"RSI {row['RSI']} - neutral zone mein hai.")

    if row["ADX"] > 25:
        parts.append(f"ADX {row['ADX']} batata hai trend strong hai.")
    elif row["ADX"] < 15:
        parts.append(f"ADX {row['ADX']} kam hai, stock range-bound/sideways ho sakta hai.")

    if row["Breakout"]:
        parts.append("Aaj 20-day high breakout hua hai.")

    if row["Volume_Spike"]:
        parts.append("Volume mein spike hai - institutional interest ka sign ho sakta hai.")
    elif row.get("Volume_Dryup"):
        parts.append("Volume dry-up hai (average se kaafi kam) - bade move se pehle ki khaamoshi ho sakti hai.")

    if row.get("Patterns"):
        parts.append(f"Chart pattern dikha: {', '.join(row['Patterns'])}.")

    if row.get("Relative_Strength") is not None:
        if row["Relative_Strength"] > 0:
            parts.append(f"NIFTY se {row['Relative_Strength']:+.1f} points behtar perform kar raha hai.")
        else:
            parts.append(f"NIFTY se {row['Relative_Strength']:+.1f} points kamzor perform kar raha hai.")

    if row["Consolidating"]:
        parts.append("Stock consolidation phase mein hai, breakout ka wait chal raha hai.")

    if row["Risk_Reward"] is not None:
        parts.append(
            f"Entry ~{row['Entry']}, Stoploss ~{row['Stoploss']}, Target ~{row['Target']} "
            f"(Risk:Reward = 1:{row['Risk_Reward']})."
        )

    parts.append(f"Overall Score: {row['Score']}/100 -> Signal: {row['Signal']}.")

    return " ".join(parts)


def _glm_api_analysis(row):
    """
    Z.AI GLM API (OpenAI-compatible) se natural-language analysis.
    - Endpoint: {ZAI_API_BASE}/chat/completions
    - Auth: Bearer {ZAI_API_KEY}
    - Body: OpenAI format (model, messages, max_tokens, temperature)
    - Response: data["choices"][0]["message"]["content"]

    Kisi bhi failure par gracefully rule-based analysis return karta
    hai (crash nahi hota), taaki ek stock ka API error poore scan ko
    rok na de.
    """
    try:
        import requests

        prompt = (
            f"Neeche ek stock ke technical indicators diye hain. Isko 2-3 lines mein "
            f"Hinglish mein analyze karo, ek trader ke perspective se, bina financial "
            f"advice diye (sirf technical observation):\n\n"
            f"Stock: {row['Stock']}\n"
            f"Close: {row['Close']}, EMA20: {row['EMA20']}, EMA50: {row['EMA50']}, EMA200: {row['EMA200']}\n"
            f"RSI: {row['RSI']}, MACD: {row['MACD']} vs Signal: {row['MACD_SIGNAL']}\n"
            f"ADX: {row['ADX']}, Supertrend: {row['Supertrend']}\n"
            f"Volume Spike: {row['Volume_Spike']}, Breakout: {row['Breakout']}, "
            f"Consolidating: {row['Consolidating']}\n"
            f"Support: {row['Support']}, Resistance: {row['Resistance']}\n"
            f"Score: {row['Score']}/100, Signal: {row['Signal']}"
        )

        # V9.1.1: glm_retry.call_glm_with_retry() — exponential backoff on
        # 429 + 2s rate limiter. Pehle raw requests.post tha jo 429 par
        # directly fail ho jaata tha (500-stock scan mein rate-limit common).
        from glm_retry import call_glm_with_retry

        url = f"{ZAI_API_BASE.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {ZAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": ZAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
            "temperature": 0.7,
        }

        text = call_glm_with_retry(url, headers, payload, timeout=30)
        if not text:
            logger.warning(f"{row['Stock']}: GLM API fail (429/5xx after retries ya client error), rule-based use kar raha hoon")
            return _rule_based_analysis(row)

        return text

    except Exception as e:
        logger.warning(
            f"{row['Stock']}: GLM API analysis fail hui ({e}), rule-based analysis use kar raha hoon"
        )
        return _rule_based_analysis(row)


def generate_analysis(row):
    """
    Row ke basis par analysis text generate karta hai.
    - AI_MODE="RULE_BASED" -> free templated analysis
    - AI_MODE="GLM_API"    -> top-signal stocks ke liye Z.AI GLM API,
                              baaki rule-based
    - AI_MODE="CLAUDE_API" -> (deprecated alias) GLM_API jaisa behave karega
    """
    # Backward-compat: purana CLAUDE_API mode naam bhi accept karo
    effective_mode = AI_MODE
    if effective_mode == _CLAUDE_MODE_ALIAS:
        logger.warning(
            "AI_MODE='CLAUDE_API' deprecated hai (V8.2.0 mein Claude hata diya gaya). "
            "Ab Z.AI GLM API use hota hai. config.py mein AI_MODE='GLM_API' set karo."
        )
        effective_mode = "GLM_API"

    if effective_mode == "GLM_API":
        if not ZAI_API_KEY:
            logger.warning(
                "AI_MODE=GLM_API hai lekin ZAI_API_KEY set nahi - rule-based mode use ho raha hai. "
                "Z.AI se API key lo (https://z.ai) aur .env mein ZAI_API_KEY=<key> daalo."
            )
            return _rule_based_analysis(row)

        if row["Signal"] in GLM_API_ONLY_FOR_SIGNALS:
            return _glm_api_analysis(row)

        return _rule_based_analysis(row)

    return _rule_based_analysis(row)
