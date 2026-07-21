"""
===========================================================
 AI CALL DISPATCHER (V10.0 — GLM Primary, Gemini Secondary)
===========================================================
V10.0: GLM वापस primary किया गया (free version), Gemini secondary.
V9.9 में GLM हटा दिया था — अब वापस लाया गया (free tier काम करता है).

FLOW:
  1. GLM API call (free tier, ZAI_API_KEY से)
  2. GLM fail (429/error) → Gemini AI fallback
  3. Gemini fail → None return → caller rule-based fallback

RATE LIMITING:
  - GLM: 2s gap between calls
  - Gemini: 4s gap between calls (free tier = 15 req/min)
  - Exponential backoff on 429: 5s → 10s → 20s → 40s → 80s
===========================================================
"""

import time
import threading
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Try importing tenacity for retry logic
try:
    from tenacity import (
        retry as _tenacity_retry,
        stop_after_attempt,
        wait_exponential_jitter,
        retry_if_exception_type,
        before_sleep_log,
        RetryError,
    )
    _TENACITY_AVAILABLE = True
except ImportError:
    _TENACITY_AVAILABLE = False

# ─── Configuration ───
MAX_RETRY_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 90
BACKOFF_JITTER_SECONDS = 2
MIN_GAP_BETWEEN_GLM_CALLS = 2.0   # 2s between GLM calls
MIN_GAP_BETWEEN_GEMINI_CALLS = 4.0  # 4s between Gemini calls (15 req/min free tier)


# ─── Rate Limiters ───
class _RateLimiter:
    def __init__(self, min_gap):
        self.min_gap = min_gap
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.min_gap:
                time.sleep(self.min_gap - elapsed)

    def mark(self):
        with self._lock:
            self._last_call = time.time()


_glm_limiter = _RateLimiter(MIN_GAP_BETWEEN_GLM_CALLS)
_gemini_limiter = _RateLimiter(MIN_GAP_BETWEEN_GEMINI_CALLS)


# ─── Exception Classes ───
class GLMRateLimitError(Exception):
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        super().__init__(f"GLM API {status_code} Too Many Requests. Response: {response_text[:200]}")

class GLMServerError(Exception):
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        super().__init__(f"GLM API {status_code} server error. Response: {response_text[:200]}")

class GLMClientError(Exception):
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        super().__init__(f"GLM API {status_code} client error. Response: {response_text[:200]}")

class GLMNetworkError(Exception):
    pass

class GLMEmptyResponseError(Exception):
    pass


# ─── HTTP Status Classifier ───
def _classify_http_status(status_code, response_text="", url=""):
    if status_code == 429:
        raise GLMRateLimitError(status_code, response_text, url)
    elif 500 <= status_code < 600:
        raise GLMServerError(status_code, response_text, url)
    elif 400 <= status_code < 500:
        raise GLMClientError(status_code, response_text, url)


# ─── Single GLM Call ───
def _single_glm_call(url, headers, payload, timeout):
    import requests
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as e:
        raise GLMNetworkError(f"GLM timeout: {e}")
    except requests.exceptions.ConnectionError as e:
        raise GLMNetworkError(f"GLM connection error: {e}")
    except requests.exceptions.RequestException as e:
        raise GLMNetworkError(f"GLM request error: {e}")

    _classify_http_status(resp.status_code, resp.text, url)

    try:
        data = resp.json()
    except ValueError as e:
        raise GLMEmptyResponseError(f"GLM invalid JSON: {e}")

    choices = data.get("choices") or []
    if not choices:
        return None
    content = choices[0].get("message", {}).get("content", "")
    return (content or "").strip() or None


# ─── Gemini Fallback ───
def _try_gemini_fallback(glm_payload):
    """GLM fail hone par Gemini AI try karta hai."""
    try:
        from gemini_fetcher import call_gemini_with_retry, is_gemini_available
        if not is_gemini_available():
            return None

        messages = glm_payload.get("messages", [])
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

        max_tokens = glm_payload.get("max_tokens", 600)
        temperature = glm_payload.get("temperature", 0.6)

        logger.info("🔄 GLM→Gemini fallback: calling Gemini")
        result = call_gemini_with_retry(
            prompt=user_prompt, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature,
        )
        if result:
            logger.info("✅ Gemini fallback successful")
        return result
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"Gemini fallback error: {e}")
        return None


# ─── Retry Decorator ───
if _TENACITY_AVAILABLE:
    def _glm_retry_decorator(func):
        return _tenacity_retry(
            stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(
                initial=BACKOFF_BASE_SECONDS,
                max=BACKOFF_MAX_SECONDS,
                jitter=BACKOFF_JITTER_SECONDS,
            ),
            retry=retry_if_exception_type(
                (GLMRateLimitError, GLMServerError, GLMNetworkError, GLMEmptyResponseError)
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )(func)
else:
    def _glm_retry_decorator(func):
        from functools import wraps
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                try:
                    return func(*args, **kwargs)
                except (GLMRateLimitError, GLMServerError, GLMNetworkError, GLMEmptyResponseError) as exc:
                    last_exc = exc
                    if attempt >= MAX_RETRY_ATTEMPTS:
                        raise
                    wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
                    logger.warning(f"{func.__name__}: attempt {attempt} failed ({exc}), retrying in {wait}s")
                    time.sleep(wait)
            raise last_exc
        return wrapper


# ─── MAIN ENTRY POINT ───
def call_glm_with_retry(url, headers, payload, timeout=30):
    """
    V10.0: GLM primary → Gemini secondary → None (rule-based fallback).

    Args:
        url: GLM API endpoint URL
        headers: HTTP headers (Authorization: Bearer <key>)
        payload: JSON body (model, messages, max_tokens, etc.)
        timeout: Request timeout

    Returns: AI response text or None
    """
    from config import ZAI_API_KEY

    # ─── LAYER 1: GLM Primary (free tier) ───
    if ZAI_API_KEY:
        _glm_limiter.wait()

        @_glm_retry_decorator
        def _glm_attempt():
            result = _single_glm_call(url, headers, payload, timeout)
            _glm_limiter.mark()
            return result

        try:
            result = _glm_attempt()
            if result:
                logger.info("✅ GLM response received")
                return result
            # GLM returned None — try Gemini
            logger.warning("GLM returned None — trying Gemini fallback")
        except (GLMRateLimitError, GLMServerError, GLMNetworkError, GLMEmptyResponseError) as e:
            logger.warning(f"GLM failed: {type(e).__name__} — trying Gemini fallback")
        except GLMClientError as e:
            logger.warning(f"GLM client error: {e} — trying Gemini fallback")
        except RetryError as e:
            logger.warning(f"GLM retry exhausted: {e} — trying Gemini fallback")
        except Exception as e:
            logger.warning(f"GLM unexpected error: {e} — trying Gemini fallback")
    else:
        logger.debug("ZAI_API_KEY not set — skipping GLM, using Gemini directly")

    # ─── LAYER 2: Gemini Secondary ───
    return _try_gemini_fallback(payload)


# ─── Diagnostics ───
def get_retry_config():
    return {
        "library": "tenacity" if _TENACITY_AVAILABLE else "pure-python",
        "primary_ai": "GLM (Z.AI free tier)",
        "secondary_ai": "Gemini (gemini-2.5-flash)",
        "max_attempts": MAX_RETRY_ATTEMPTS,
        "glm_gap": f"{MIN_GAP_BETWEEN_GLM_CALLS}s",
        "gemini_gap": f"{MIN_GAP_BETWEEN_GEMINI_CALLS}s",
        "backoff": f"{BACKOFF_BASE_SECONDS}s -> {BACKOFF_MAX_SECONDS}s",
    }

def is_retry_enabled():
    return True

# Backward compat
_rate_limiter = _glm_limiter
