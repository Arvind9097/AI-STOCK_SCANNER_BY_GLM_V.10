"""
===========================================================
 GEMINI FETCHER (V9.9 — Primary + Only AI, Rate-Limit Safe)
===========================================================
Gemini 3 Pro with proper rate limiting:
  - Free tier: 15 req/min → 4s gap between calls
  - Batch AI calls (sirf top stocks ke liye, har stock ke liye nahi)
  - Exponential backoff on 429

Model: gemini-2.5-flash (stable, no 429 like gemini-3-pro-preview)
  - gemini-3-pro-preview = experimental, 429 frequently
  - gemini-2.5-flash = stable, free tier friendly, fast
===========================================================
"""

import os
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# V9.9: Rate limiter — 4s gap between Gemini calls (free tier = 15 req/min)
_gemini_last_call = 0.0
_gemini_lock = threading.Lock()
GEMINI_MIN_GAP = 4.0  # 4 seconds = 15 req/min max


def is_gemini_available() -> bool:
    return bool(GEMINI_API_KEY)


def _rate_limit_wait():
    """Ensure 4s gap between Gemini API calls."""
    global _gemini_last_call
    with _gemini_lock:
        now = time.time()
        elapsed = now - _gemini_last_call
        if elapsed < GEMINI_MIN_GAP:
            wait = GEMINI_MIN_GAP - elapsed
            logger.debug(f"Gemini rate limit: waiting {wait:.1f}s")
            time.sleep(wait)
        _gemini_last_call = time.time()


def call_gemini(prompt, system_prompt="", max_tokens=600, temperature=0.6, timeout=30):
    """Single Gemini API call with rate limiting."""
    if not GEMINI_API_KEY:
        return None

    import requests
    _rate_limit_wait()

    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 429:
            logger.warning("Gemini 429 — rate limited, backing off 10s")
            time.sleep(10)
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
        logger.warning(f"Gemini API fail: {e}")
        return None


def call_gemini_with_retry(prompt, system_prompt="", max_tokens=600, temperature=0.6, timeout=30, max_retries=3):
    """Gemini with retry on 429 (10s, 20s, 40s backoff)."""
    for attempt in range(1, max_retries + 1):
        result = call_gemini(prompt, system_prompt, max_tokens, temperature, timeout)
        if result:
            return result
        if attempt < max_retries:
            wait = min(10 * (2 ** (attempt - 1)), 60)
            logger.warning(f"Gemini retry {attempt}/{max_retries} in {wait}s")
            time.sleep(wait)
    logger.error(f"Gemini: all {max_retries} retries failed")
    return None
