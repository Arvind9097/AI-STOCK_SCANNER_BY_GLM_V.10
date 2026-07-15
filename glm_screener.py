"""
===========================================================
 GLM AI SCREENER — AI-powered stock ranking (V8.3.0)
===========================================================
Pehle GLM sirf individual stock ka analysis text likhta tha.
Ab ye module TOP N technical candidates ko ek saath GLM ko
deta hai aur puchta hai:

  "In candidates mein se sabse best 5-10 stocks kaunse hain?
   Har stock ke liye 2-3 line ka Hinglish rationale likho,
   confidence score (1-10) do, aur entry timing note do
   (abhi enter karo / wait karo / avoid)."

Use case:
  - scanner.py poore universe (NSE+BSE ~1300) ko technical
    indicators se score karta hai (fast, free, rule-based).
  - Top ~30 candidates (Score >= BUY threshold) yahan aate hain.
  - glm_screener.rank_with_glm() in 30 mein se best 5-10
    select karta hai GLM ke through, detailed rationale ke
    saath.

Output format (har stock ke liye):
  {
    "stock": "RELIANCE.NS",
    "name": "Reliance Industries",
    "rank": 1,
    "glm_confidence": 8.5,        # 1-10 scale
    "glm_action": "ENTER_NOW",   # ENTER_NOW / WAIT / AVOID
    "glm_rationale": "Hinglish 2-3 lines...",
    "glm_risk_note": "Hinglish 1 line risk warning",
  }

Fallback: agar ZAI_API_KEY set nahi hai, ya API fail ho,
to technical score se hi top-N return karta hai (GLM
rationale empty rehta hai). Pipeline kabhi crash nahi hota.
===========================================================
"""

import json
import re

from config import ZAI_API_KEY, ZAI_API_BASE, ZAI_MODEL
from logger import logger

# Kitne top candidates GLM ko bhejne hain (zyada = zyada API cost/token)
GLM_CANDIDATE_POOL = 30
# GLM se kitne final picks chahiye
GLM_FINAL_PICKS = 8


def _build_prompt(candidates):
    """
    Top technical candidates ka data GLM ke liye structured prompt
    mein convert karta hai. Hinglish instruction deta hai.
    """
    stocks_block = []
    for i, c in enumerate(candidates, 1):
        stocks_block.append(
            f"{i}. {c['symbol']} ({c.get('name','')})\n"
            f"   Signal: {c.get('signal','?')} | Technical Score: {c.get('score',0)}/100\n"
            f"   Close: {c.get('close','?')} | RSI: {c.get('rsi','?')} | ADX: {c.get('adx','?')}\n"
            f"   EMA20/50/200: {c.get('ema20','?')}/{c.get('ema50','?')}/{c.get('ema200','?')}\n"
            f"   MACD: {c.get('macd','?')} vs Signal: {c.get('macd_signal','?')}\n"
            f"   Supertrend: {c.get('supertrend','?')} | Volume Spike: {c.get('volume_spike','?')}\n"
            f"   Breakout: {c.get('breakout','?')} | Patterns: {c.get('patterns','?')}\n"
            f"   Weekly Trend: {c.get('weekly_trend','?')} | 1H Confirm: {c.get('mtf_status','?')}\n"
            f"   Entry Zone: {c.get('entry_low','?')}-{c.get('entry_high','?')} | "
            f"SL: {c.get('sl','?')} | Targets: {c.get('t1','?')}/{c.get('t2','?')}/{c.get('t3','?')}\n"
            f"   Risk:Reward: 1:{c.get('rr','?')} | Rel Strength vs NIFTY: {c.get('rel_strength','?')}"
        )
    stocks_text = "\n\n".join(stocks_block)

    prompt = (
        f"Tu ek experienced Indian stock market trader aur technical analyst hai.\n"
        f"Niche {len(candidates)} stocks ke technical indicators diye gaye hain "
        f"(NSE + BSE universe se scan kiye gaye). Inme se sabse best "
        f"{GLM_FINAL_PICKS} stocks select karo jo abhi entry ke liye best hain.\n\n"
        f"Selection criteria (priority order):\n"
        f"1. Trend strength (EMA alignment + ADX > 20)\n"
        f"2. Momentum (RSI 55-70 ideal, >70 overbought caution)\n"
        f"3. Volume confirmation (volume spike + breakout)\n"
        f"4. Multi-timeframe confluence (weekly bullish + 1H confirmed)\n"
        f"5. Risk:Reward (kam se kam 1:1.5, ideal 1:2+)\n"
        f"6. Chart pattern (bullish patterns bonus)\n"
        f"7. Relative strength vs NIFTY (positive = outperformer)\n\n"
        f"STRICT RULES:\n"
        f"- Sirf technical analysis, koi financial advice nahi\n"
        f"- Stock names ENGLISH mein likho (translate mat karo)\n"
        f"- Rationale aur risk notes HINGLISH mein likho "
        f"(common words Hindi jaise 'achha', 'kamzor', 'tyaar', 'momentum', 'breakout')\n"
        f"- Overbought stocks (RSI>75) ke liye 'WAIT' bolo\n"
        f"- Weak trend (ADX<15) ya poor R:R ke liye 'AVOID' bolo\n\n"
        f"RESPONSE FORMAT (sirf valid JSON, koi markdown/code fence nahi):\n"
        f'{{\n'
        f'  "picks": [\n'
        f'    {{\n'
        f'      "rank": 1,\n'
        f'      "symbol": "RELIANCE.NS",\n'
        f'      "confidence": 8.5,\n'
        f'      "action": "ENTER_NOW",\n'
        f'      "rationale": "Strong uptrend, EMA aligned, volume spike confirmed...",\n'
        f'      "risk_note": "Global market volatility se dhyaan rakho"\n'
        f'    }}\n'
        f'  ]\n'
        f'}}\n\n'
        f"CANDIDATES:\n\n{stocks_text}\n\n"
        f"Ab apna JSON response do (sirf JSON, koi aur text nahi):"
    )
    return prompt


def _call_glm(prompt):
    """Z.AI GLM API ko call karta hai. Returns raw text response or None."""
    try:
        import requests
        url = f"{ZAI_API_BASE.rstrip('/')}/chat/completions"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {ZAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": ZAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2500,
                "temperature": 0.4,  # kam temperature = consistent JSON
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        return choices[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"GLM screener API call fail: {e}")
        return None


def _extract_json(text):
    """GLM response se JSON extract karta hai (markdown fences handle)."""
    if not text:
        return None
    # Strip markdown code fences agar hain
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove ```json ... ``` or ``` ... ```
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Find first { ... } block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning(f"GLM JSON parse fail: {e}")
        return None


def _fallback_ranking(candidates, top_n):
    """
    GLM unavailable hone par technical score se hi top-N return karta hai.
    GLM fields (rationale, confidence, action) empty rehte hain.
    """
    sorted_cands = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)
    out = []
    for i, c in enumerate(sorted_cands[:top_n], 1):
        out.append({
            "stock": c["symbol"],
            "name": c.get("name", ""),
            "rank": i,
            "glm_confidence": None,
            "glm_action": None,
            "glm_rationale": "",
            "glm_risk_note": "",
            "technical_score": c.get("score", 0),
            "technical_signal": c.get("signal", ""),
        })
    return out


def rank_with_glm(candidates):
    """
    MAIN ENTRY: top technical candidates ko GLM se rank karwata hai.

    Input: list of candidate dicts (scanner.py output, top Score wale)
           Each must have: symbol, name, score, signal, close, rsi, adx,
           ema20/50/200, macd, macd_signal, supertrend, volume_spike,
           breakout, patterns, weekly_trend, mtf_status, entry_low,
           entry_high, sl, t1, t2, t3, rr, rel_strength

    Output: list of dicts (GLM_FINAL_PICKS length), har entry mein:
            stock, name, rank, glm_confidence, glm_action,
            glm_rationale, glm_risk_note, technical_score, technical_signal

    Fallback: GLM fail hone par technical score se ranking.
    """
    if not candidates:
        return []

    # Pool limit — zyada candidates bhejne se token cost badhta hai
    pool = candidates[:GLM_CANDIDATE_POOL]

    if not ZAI_API_KEY:
        logger.info("GLM screener: ZAI_API_KEY set nahi — technical-score ranking use ho raha hai")
        return _fallback_ranking(pool, GLM_FINAL_PICKS)

    prompt = _build_prompt(pool)
    raw = _call_glm(prompt)
    if not raw:
        logger.warning("GLM screener: empty response — fallback technical ranking")
        return _fallback_ranking(pool, GLM_FINAL_PICKS)

    parsed = _extract_json(raw)
    if not parsed or "picks" not in parsed:
        logger.warning("GLM screener: JSON parse fail — fallback technical ranking")
        return _fallback_ranking(pool, GLM_FINAL_PICKS)

    # Build lookup: symbol → candidate (for technical_score/signal)
    cand_lookup = {c["symbol"]: c for c in pool}

    out = []
    for pick in parsed["picks"][:GLM_FINAL_PICKS]:
        sym = pick.get("symbol", "")
        cand = cand_lookup.get(sym, {})
        out.append({
            "stock": sym,
            "name": cand.get("name", ""),
            "rank": pick.get("rank", len(out) + 1),
            "glm_confidence": pick.get("confidence"),
            "glm_action": pick.get("action", ""),
            "glm_rationale": pick.get("rationale", ""),
            "glm_risk_note": pick.get("risk_note", ""),
            "technical_score": cand.get("score", 0),
            "technical_signal": cand.get("signal", ""),
        })

    if not out:
        logger.warning("GLM screener: no valid picks parsed — fallback technical ranking")
        return _fallback_ranking(pool, GLM_FINAL_PICKS)

    logger.info(f"GLM screener: {len(out)} picks ranked (model: {ZAI_MODEL})")
    return out


def format_glm_picks_text(glm_picks):
    """
    GLM picks ko Telegram/PDF ke liye Hinglish text mein convert karta hai.
    Stock names bold + English, rationale Hinglish.
    """
    if not glm_picks:
        return ""

    lines = ["🤖 <b>GLM AI TOP PICKS</b> 🤖\n━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    for p in glm_picks:
        sym = p["stock"]
        # Display: remove .NS/.BO for readability
        display = sym.replace(".NS", "").replace(".BO", "")
        rank = p.get("rank", "?")
        conf = p.get("glm_confidence")
        action = p.get("glm_action", "")
        rationale = p.get("glm_rationale", "")
        risk = p.get("glm_risk_note", "")
        tech_score = p.get("technical_score", 0)

        # Action emoji
        if action == "ENTER_NOW":
            action_emoji = "🟢"
        elif action == "WAIT":
            action_emoji = "🟡"
        elif action == "AVOID":
            action_emoji = "🔴"
        else:
            action_emoji = "⚪"

        conf_str = f"{conf}/10" if conf is not None else "N/A"
        lines.append(
            f"\n{action_emoji} <b>#{rank} {display}</b> "
            f"| 🎯 Confidence: <b>{conf_str}</b> "
            f"| 📊 Tech: {tech_score}/100"
        )
        if action:
            lines.append(f"📌 Action: <b>{action}</b>")
        if rationale:
            lines.append(f"💡 {rationale}")
        if risk:
            lines.append(f"⚠️ Risk: {risk}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    lines.append("\n🧠 <i>GLM AI ne technical indicators dekh kar ye picks chune hain. "
                 "Apna research zaroor karo.</i>")
    return "\n".join(lines)
