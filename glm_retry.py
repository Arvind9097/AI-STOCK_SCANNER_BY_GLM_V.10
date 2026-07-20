"""
===========================================================
 GLM API RETRY + RATE LIMITER MODULE (V9.1.1 — 429 Fix)
===========================================================
Z.ai GLM API ke 429 Too Many Requests errors ko handle karne
ke liye exponential backoff + retry + rate limiting.

PROBLEM (production logs):
  Bot par user turant-turat stock queries bhejta hai:
    "TCS" → GLM call
    "LT" → GLM call (1 sec baad)
    "BHEL" → GLM call (1 sec baad)
  GLM API rate-limit lagta hai (per-minute/per-second limit):
    429 Too Many Requests
  Pehle ye error seedha fail ho jaata tha — bot "data nahi mila"
  reply deta tha. Ab retry + rate limiter se handle hoga.

SOLUTION (2-layer defense):

  Layer 1 — RATE LIMITER (prevent bursts):
    Minimum 2 second gap between GLM calls (thread-safe).
    Agar user 3 stocks 1 sec mein query kare, to:
      Call 1 → immediate
      Call 2 → wait 2s, then call
      Call 3 → wait 2s more, then call
    Isse GLM ko "breathing room" milta hai, 429 kam aate hain.

  Layer 2 — EXPONENTIAL BACKOFF RETRY (recover from 429):
    Agar 429 aa bhi gaya, to retry with increasing delay:
      Attempt 1 → fail (429) → wait 5s
      Attempt 2 → fail (429) → wait 10s
      Attempt 3 → fail (429) → wait 20s
      Attempt 4 → fail (429) → wait 40s
      Attempt 5 → fail (429) → give up, return None
    Max 5 attempts. Caller handles None gracefully (rule-based fallback).

  Exception classification (critical):
    429 Too Many Requests -> RETRY (rate limit, will clear)
    5xx server errors      -> RETRY (transient)
    Network/timeout        -> RETRY (transient)
    400/401/403 client     -> NO RETRY (bad request/auth, retry won't fix)

  Thread-safety:
    Rate limiter uses a lock (multiple bot threads can call safely).
    Retry is per-call (stateless), safe for concurrent use.

FALLBACK:
    Agar 'tenacity' package installed nahi hai, to pure-Python fallback
    use hota hai (basic backoff, no jitter). Module NEVER fails to import.
===========================================================
"""

import time
import threading
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Try importing tenacity (preferred). Fallback to pure-Python if missing.
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
    logger.warning(
        "'tenacity' package not installed — GLM retry will use pure-Python fallback. "
        "Install: pip install tenacity"
    )


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION CONSTANTS
# ═══════════════════════════════════════════════════════════════════
MAX_RETRY_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 5          # first retry after 5s
BACKOFF_MAX_SECONDS = 90          # cap at 90s
BACKOFF_JITTER_SECONDS = 2        # random 0-2s jitter
DEFAULT_REQUEST_TIMEOUT = 30      # seconds

# Rate limiter: minimum gap between GLM calls (prevents burst 429)
MIN_GAP_BETWEEN_CALLS = 2.0       # 2 seconds between calls


# ═══════════════════════════════════════════════════════════════════
# CUSTOM EXCEPTIONS (for granular retry control)
# ═══════════════════════════════════════════════════════════════════
class GLMRateLimitError(Exception):
    """429 Too Many Requests — retryable."""
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"GLM API {status_code} Too Many Requests"
                         f"{' for ' + url if url else ''}. Response: {response_text[:200]}")


class GLMServerError(Exception):
    """5xx server error — retryable."""
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"GLM API {status_code} server error"
                         f"{' for ' + url if url else ''}. Response: {response_text[:200]}")


class GLMClientError(Exception):
    """4xx client error (400/401/403) — NOT retryable."""
    def __init__(self, status_code, response_text="", url=""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"GLM API {status_code} client error"
                         f"{' for ' + url if url else ''}. Response: {response_text[:200]}")


class GLMNetworkError(Exception):
    """Network/timeout error — retryable."""
    pass


class GLMEmptyResponseError(Exception):
    """200 OK but empty/malformed JSON — retryable."""
    pass


# ═══════════════════════════════════════════════════════════════════
# RATE LIMITER (Layer 1 — prevent burst 429)
# ═══════════════════════════════════════════════════════════════════
class GLMRateLimiter:
    """
    Thread-safe rate limiter for GLM API calls.

    Ensures minimum MIN_GAP_BETWEEN_CALLS seconds between calls.
    Prevents burst requests that trigger 429.

    USAGE (internal — call_glm_with_retry uses this automatically):
        limiter = GLMRateLimiter(min_gap=2.0)
        limiter.wait()  # blocks if last call was < 2s ago
        # ... make GLM call ...
        limiter.mark()  # record call time
    """

    def __init__(self, min_gap: float = MIN_GAP_BETWEEN_CALLS):
        self.min_gap = min_gap
        self._last_call_time = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until min_gap has passed since last call. Thread-safe."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self.min_gap:
                wait_time = self.min_gap - elapsed
                logger.debug(f"GLM rate limiter: waiting {wait_time:.1f}s (prevent burst)")
                time.sleep(wait_time)

    def mark(self) -> None:
        """Record that a call was made (call this AFTER the request)."""
        with self._lock:
            self._last_call_time = time.time()


# Module-level singleton rate limiter (shared across all GLM callers)
_rate_limiter = GLMRateLimiter()


# ═══════════════════════════════════════════════════════════════════
# HTTP STATUS CLASSIFIER
# ═══════════════════════════════════════════════════════════════════
def _classify_http_status(status_code: int, response_text: str = "", url: str = "") -> None:
    """
    Classify HTTP status + raise appropriate exception.
    429/5xx -> retryable, 4xx -> non-retryable, 2xx -> no exception.
    """
    if status_code == 429:
        raise GLMRateLimitError(status_code, response_text, url)
    elif 500 <= status_code < 600:
        raise GLMServerError(status_code, response_text, url)
    elif 400 <= status_code < 500:
        raise GLMClientError(status_code, response_text, url)
    # 2xx — success, no exception


# ═══════════════════════════════════════════════════════════════════
# TENACITY-BASED RETRY DECORATOR (Layer 2 — recover from 429)
# ═══════════════════════════════════════════════════════════════════
if _TENACITY_AVAILABLE:
    def _glm_retry_decorator(func):
        """Tenacity-based exponential backoff: 5s -> 10s -> 20s -> 40s -> 80s."""
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
    # Pure-Python fallback (no tenacity)
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
                        logger.error(f"{func.__name__}: all {MAX_RETRY_ATTEMPTS} attempts failed: {exc}")
                        raise
                    wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
                    logger.warning(f"{func.__name__}: attempt {attempt} failed ({exc}), retrying in {wait}s...")
                    time.sleep(wait)
            raise last_exc
        return wrapper


# ═══════════════════════════════════════════════════════════════════
# SINGLE GLM CALL (with classification)
# ═══════════════════════════════════════════════════════════════════
def _single_glm_call(url, headers, payload, timeout):
    """
    Single GLM API attempt. Raises classified exceptions on failure.
    Used internally by call_glm_with_retry (retry decorator handles retries).
    """
    import requests
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as e:
        raise GLMNetworkError(f"GLM API timeout after {timeout}s: {e}")
    except requests.exceptions.ConnectionError as e:
        raise GLMNetworkError(f"GLM API connection error: {e}")
    except requests.exceptions.RequestException as e:
        raise GLMNetworkError(f"GLM API request error: {e}")

    # Classify HTTP status (raises appropriate exception on error)
    _classify_http_status(resp.status_code, resp.text, url)

    # Success — parse JSON
    try:
        data = resp.json()
    except ValueError as e:
        raise GLMEmptyResponseError(f"GLM API invalid JSON from {url}: {e}. Body: {resp.text[:200]}")

    choices = data.get("choices") or []
    if not choices:
        return None  # not an error, just empty
    content = choices[0].get("message", {}).get("content", "")
    return (content or "").strip() or None


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API: call_glm_with_retry()
# ═══════════════════════════════════════════════════════════════════
def call_glm_with_retry(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> Optional[str]:
    """
    Execute GLM API call with rate limiting + exponential backoff retry.

    This is the SINGLE ENTRY POINT for all GLM API calls.
    Handles:
      - Rate limiting (2s gap between calls — prevents burst 429)
      - Retry on 429/5xx/network errors (5 attempts: 5s->10s->20s->40s->80s)
      - No retry on 4xx client errors (bad request/auth — fail fast)
      - Graceful failure (returns None — caller handles fallback)

    Args:
        url: Full API endpoint URL
        headers: HTTP headers (must include Authorization: Bearer <key>)
        payload: JSON body (model, messages, max_tokens, etc.)
        timeout: Request timeout in seconds

    Returns:
        GLM response text on success.
        None on failure (after all retries, or non-retryable error).

    Thread-safety: stateless + uses shared rate limiter. Safe for
    concurrent calls from bot_listener + scanner + breaking_news threads.

    Example:
        text = call_glm_with_retry(
            url="https://api.z.ai/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload={"model": "glm-4.5", "messages": [...], "max_tokens": 300},
        )
        if text is None:
            # fall back to rule-based
        else:
            print(text)
    """
    # Layer 1: Rate limit (prevent burst)
    _rate_limiter.wait()

    # V9.6: GEMINI PRIMARY (user request — GLM ko secondary kiya gaya)
    # Pehle Gemini try karo, agar fail ho to GLM try karo
    try:
        from gemini_fetcher import call_gemini_with_retry, is_gemini_available
        if is_gemini_available():
            # Extract prompt from GLM payload (OpenAI format)
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

            if user_prompt:
                max_tokens = payload.get("max_tokens", 600)
                temperature = payload.get("temperature", 0.6)
                logger.info("🤖 Gemini AI (primary) call...")
                gemini_result = call_gemini_with_retry(
                    prompt=user_prompt, system_prompt=system_prompt,
                    max_tokens=max_tokens, temperature=temperature,
                )
                if gemini_result:
                    _rate_limiter.mark()
                    logger.info("✅ Gemini AI response received")
                    return gemini_result
                logger.warning("Gemini failed — trying GLM fallback...")
    except ImportError:
        pass  # gemini_fetcher not available
    except Exception as e:
        logger.warning(f"Gemini primary error: {e} — trying GLM fallback...")

    # Layer 2: GLM fallback (with retry)
    @_glm_retry_decorator
    def _attempt():
        result = _single_glm_call(url, headers, payload, timeout)
        _rate_limiter.mark()
        return result

    try:
        result = _attempt()
        if result:
            return result
        # GLM returned None — try GLM-4-Flash (Turbo, faster/cheaper model)
        logger.warning("GLM primary failed — trying GLM-4-Flash (Turbo)...")
        turbo_payload = dict(payload)
        turbo_payload["model"] = "glm-4-flash"
        turbo_result = _single_glm_call(url, headers, turbo_payload, timeout)
        if turbo_result:
            logger.info("✅ GLM-4-Flash (Turbo) response received")
            return turbo_result
        return None
    except (GLMRateLimitError, GLMServerError, GLMNetworkError, GLMEmptyResponseError) as e:
        logger.warning(f"GLM failed: {type(e).__name__}. Trying GLM-4-Flash (Turbo)...")
        try:
            turbo_payload = dict(payload)
            turbo_payload["model"] = "glm-4-flash"
            turbo_result = _single_glm_call(url, headers, turbo_payload, timeout)
            if turbo_result:
                logger.info("✅ GLM-4-Flash (Turbo) response received")
                return turbo_result
        except Exception:
            pass
        return None
    except GLMClientError as e:
        logger.error(f"GLM client error (non-retryable): {e}")
        return None
    except RetryError as e:
        logger.error(f"GLM retry exhausted: {e}")
        return None
    except Exception as e:
        logger.error(f"GLM call to {url} unexpected error: {type(e).__name__}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# V9.3: GEMINI FALLBACK — jab GLM fail ho jaaye
# ═══════════════════════════════════════════════════════════════════
def _try_gemini_fallback(glm_payload: dict) -> Optional[str]:
    """
    V9.3: GLM fail hone par Gemini AI try karta hai.

    GLM payload (OpenAI format) se prompt extract karke Gemini API
    ko bhejta hai. Agar Gemini bhi fail ho, None return (rule-based fallback).

    Args:
        glm_payload: Original GLM payload dict (messages, max_tokens, etc.)

    Returns:
        Gemini response text, ya None (agar Gemini bhi fail).
    """
    try:
        from gemini_fetcher import call_gemini_with_retry, is_gemini_available

        if not is_gemini_available():
            logger.debug("Gemini fallback skipped — no GEMINI_API_KEY set")
            return None

        # Extract prompt from GLM payload (OpenAI format)
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
            # Agar user prompt nahi mila, system prompt use karo
            user_prompt = system_prompt
            system_prompt = ""

        if not user_prompt:
            return None

        # GLM max_tokens → Gemini maxOutputTokens
        max_tokens = glm_payload.get("max_tokens", 600)
        temperature = glm_payload.get("temperature", 0.6)

        logger.info(f"🔄 GLM→Gemini fallback: calling Gemini (prompt {len(user_prompt)} chars)")
        result = call_gemini_with_retry(
            prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if result:
            logger.info("✅ Gemini fallback successful — response received")
        else:
            logger.warning("❌ Gemini fallback also failed — rule-based will be used")

        return result

    except ImportError:
        logger.debug("gemini_fetcher module not found — fallback skipped")
        return None
    except Exception as e:
        logger.warning(f"Gemini fallback error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════
def get_retry_config() -> dict:
    """Returns current retry configuration (for logging/diagnostics)."""
    return {
        "library": "tenacity" if _TENACITY_AVAILABLE else "pure-python-fallback",
        "max_attempts": MAX_RETRY_ATTEMPTS,
        "backoff_base_seconds": BACKOFF_BASE_SECONDS,
        "backoff_max_seconds": BACKOFF_MAX_SECONDS,
        "jitter_seconds": BACKOFF_JITTER_SECONDS,
        "rate_limit_gap_seconds": MIN_GAP_BETWEEN_CALLS,
        "retryable": ["GLMRateLimitError (429)", "GLMServerError (5xx)",
                      "GLMNetworkError (timeout)", "GLMEmptyResponseError (bad JSON)"],
        "non_retryable": ["GLMClientError (400/401/403 — fail immediately)"],
    }


def is_retry_enabled() -> bool:
    """Returns True if tenacity-based retry is active."""
    return _TENACITY_AVAILABLE
