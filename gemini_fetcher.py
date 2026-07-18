"""
===========================================================
 GEMINI AI FALLBACK MODULE (V9.3)
===========================================================
Jab GLM API fail ho (429 Too Many Requests / 5xx / network error),
to automatically Gemini AI pe switch ho jaata hai.

FLOW:
  1. GLM API call → success? → use GLM response
  2. GLM fail (429/5xx/network)? → try Gemini API
  3. Gemini success? → use Gemini response
  4. Gemini bhi fail? → rule-based fallback (existing behavior)

GEMINI API:
  - Google's Gemini 1.5 Flash (fast + cheap)
  - Endpoint: https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent
  - Auth: API key in URL query param
  - Free tier: 15 requests/min, 1500/day (generous)

SECURITY:
  - API key from environment variable (GEMINI_API_KEY)
  - Never hardcoded in code
  - Same retry logic as GLM (exponential backoff)
===========================================================
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Gemini API config
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def is_gemini_available() -> bool:
    """Check if Gemini API key is configured."""
    return bool(GEMINI_API_KEY)


def call_gemini(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 600,
    temperature: float = 0.6,
    timeout: int = 30,
) -> Optional[str]:
    """
    Call Gemini API with the given prompt.
    Returns response text or None on failure.

    Args:
        prompt: User prompt / question
        system_prompt: System instruction (role/behavior)
        max_tokens: Max response tokens
        temperature: Creativity (0=focused, 1=creative)
        timeout: Request timeout seconds

    Returns:
        Response text (stripped) or None on failure.
    """
    if not GEMINI_API_KEY:
        return None

    import requests

    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    # Build Gemini request format
    contents = []
    if system_prompt:
        # Gemini uses systemInstruction for system prompts
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
    else:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }

    try:
        resp = requests.post(url, json=payload, timeout=timeout)

        if resp.status_code == 429:
            logger.warning("Gemini API 429 (rate limit) — backing off")
            return None
        if resp.status_code != 200:
            logger.warning(f"Gemini API {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None

        text = parts[0].get("text", "")
        return (text or "").strip() or None

    except Exception as e:
        logger.warning(f"Gemini API call fail: {e}")
        return None


def call_gemini_with_retry(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 600,
    temperature: float = 0.6,
    timeout: int = 30,
    max_retries: int = 3,
) -> Optional[str]:
    """
    Call Gemini with exponential backoff retry.
    Retries on 429/5xx/network errors (max 3 attempts).
    """
    import time

    for attempt in range(1, max_retries + 1):
        result = call_gemini(prompt, system_prompt, max_tokens, temperature, timeout)
        if result:
            return result

        if attempt < max_retries:
            wait = min(5 * (2 ** (attempt - 1)), 30)
            logger.warning(f"Gemini retry {attempt}/{max_retries} in {wait}s")
            time.sleep(wait)

    logger.error(f"Gemini: all {max_retries} retries failed")
    return None
