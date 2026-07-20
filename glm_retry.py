"""
===========================================================
 AI CALL DISPATCHER (V9.9 — Gemini Only, No GLM)
===========================================================
V9.9: GLM पूरी तरह से हटा दिया गया है (बैलेंस खत्म, हर बार रिट्राय = समय बर्बाद)।
अब सिर्फ Gemini AI का उपयोग होता है (gemini-2.5-flash, rate-limit safe)।

FLOW:
  1. Gemini AI call (rate-limited, 4s gap)
  2. 429 होने पर backoff (10s, 20s, 40s)
  3. पूरी तरह फेल होने पर None return → caller rule-based fallback का उपयोग करता है
===========================================================
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def call_glm_with_retry(url, headers, payload, timeout=30):
    """
    V9.9: अब सिर्फ Gemini AI का उपयोग होता है (GLM हटा दिया गया)।
    यह फंक्शन backward-compat के लिए नाम बरकरार रखता है।

    GLM payload से prompt निकालता है → Gemini को भेजता है।
    """
    try:
        from gemini_fetcher import call_gemini_with_retry, is_gemini_available

        if not is_gemini_available():
            logger.warning("Gemini API key not set — AI analysis disabled")
            return None

        # GLM payload (OpenAI format) से prompt निकालें
        messages = payload.get("messages", [])
        system_prompt = ""
        user_prompt = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                system_prompt = content
            elif role == "user":
                user_prompt = content

        if not user_prompt:
            user_prompt = system_prompt
            system_prompt = ""

        if not user_prompt:
            return None

        max_tokens = payload.get("max_tokens", 600)
        temperature = payload.get("temperature", 0.6)

        logger.info("🤖 Gemini AI call...")
        result = call_gemini_with_retry(
            prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

        if result:
            logger.info("✅ Gemini response received")
        else:
            logger.warning("❌ Gemini failed — rule-based fallback will be used")

        return result

    except ImportError:
        logger.warning("gemini_fetcher module not found")
        return None
    except Exception as e:
        logger.warning(f"AI call error: {e}")
        return None


# Backward-compat: exception classes (अब केवल stubs हैं, कोई GLM नहीं)
class GLMRateLimitError(Exception):
    pass

class GLMServerError(Exception):
    pass

class GLMClientError(Exception):
    pass

class GLMNetworkError(Exception):
    pass

class GLMEmptyResponseError(Exception):
    pass

class GLMRateLimiter:
    """Stub — rate limiting अब gemini_fetcher.py में है।"""
    def wait(self): pass
    def mark(self): pass

_rate_limiter = GLMRateLimiter()
MAX_RETRY_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 60


def get_retry_config():
    return {
        "library": "gemini-only (V9.9)",
        "model": "gemini-2.5-flash",
        "rate_limit_gap": "4s (15 req/min free tier)",
        "max_attempts": 3,
        "backoff": "10s -> 20s -> 40s",
    }

def is_retry_enabled():
    return True
