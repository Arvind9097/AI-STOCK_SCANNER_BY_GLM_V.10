"""
===========================================================
 API RETRY MODULE — Exponential Backoff for AI APIs (V9.2)
===========================================================
Deep reasoning for 429 Too Many Requests handling:

ROOT CAUSE ANALYSIS (Z.ai/GLM 429):
  The trading system makes GLM API calls in BURSTS:
    - Per-stock AI analysis (could be 500 stocks in a scan)
    - GLM screener ranking (1 call for top 30 candidates)
    - News Hinglish translation (per headline, 20+ per RSS poll)
    - Evening summary (1 call at 8 PM)
    - Conversational bot replies (user-triggered, unpredictable)
  429 triggers when:
    - Burst of concurrent calls exceeds per-second limit
    - Daily/hourly quota exhausted
    - Multiple scanner threads calling simultaneously

SOLUTION ARCHITECTURE:
  1. tenacity-based decorator with EXPONENTIAL BACKOFF:
       5s -> 10s -> 20s -> 40s -> 80s (max 5 attempts)
  2. JITTER (random 0-2s added) — avoids thundering herd when
     multiple threads retry simultaneously (if all wait exactly
     5s, they all hit the API at the same moment again).
  3. Exception CLASSIFICATION (critical — not all errors retryable):
     - 429 Too Many Requests -> RETRY (rate limit, will clear)
     - 5xx server errors      -> RETRY (transient)
     - Network/timeout        -> RETRY (transient)
     - 400/401/403 client     -> NO RETRY (bad request/auth, retry
                                won't fix — fail immediately)
  4. Logging: each retry attempt logged at WARNING level (visible
     in production logs for debugging rate-limit patterns).
  5. Graceful final failure: returns None (callers must handle,
     e.g. fall back to rule-based analysis). NEVER raises an
     unhandled exception that crashes the evening summary thread.

EDGE CASES HANDLED:
  - Connection timeout (requests.exceptions.Timeout) -> retryable
  - DNS/connection error (requests.exceptions.ConnectionError) -> retryable
  - Empty JSON response (ValueError on .json()) -> treated as failure
  - 3xx redirects -> followed automatically by requests
  - Response with choices=[] -> returns None (not an error, just empty)
  - Thread-safety: tenacity decorators are stateless per-call, safe
    for concurrent use across scanner threads.
===========================================================
"""

import logging
import random
import time
from typing import Optional, Callable, Any, Tuple
from functools import wraps

logger = logging.getLogger(__name__)

# Try importing tenacity (production-grade retry library). If unavailable,
# fall back to a pure-Python implementation so the module NEVER fails to
# import — degraded mode is better than a crash.
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
        "'tenacity' package not installed — API retry logic will use "
        "pure-Python fallback (less sophisticated). Install: pip install tenacity"
    )


# ───────────────────────────────────────────────────────────────────
# Custom exception hierarchy — enables granular retry control.
# Callers can catch specific types if they want custom handling.
# ───────────────────────────────────────────────────────────────────
class APIRateLimitError(Exception):
    """
    429 Too Many Requests — the API server is rate-limiting us.
    RETRYABLE: the limit will clear after the backoff window.
    """
    def __init__(self, status_code: int, response_text: str = "", url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"API {status_code} Too Many Requests"
            f"{' for ' + url if url else ''}. Response: {response_text[:200]}"
        )


class APIServerError(Exception):
    """
    5xx server error — the API server had an internal error.
    RETRYABLE: transient, usually clears on retry.
    """
    def __init__(self, status_code: int, response_text: str = "", url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"API {status_code} server error"
            f"{' for ' + url if url else ''}. Response: {response_text[:200]}"
        )


class APIClientError(Exception):
    """
    4xx client error (except 429) — bad request, unauthorized, forbidden.
    NOT RETRYABLE: retrying won't fix a malformed request or bad API key.
    Failing immediately saves retry budget and avoids log spam.
    """
    def __init__(self, status_code: int, response_text: str = "", url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"API {status_code} client error"
            f"{' for ' + url if url else ''}. Response: {response_text[:200]}"
        )


class APINetworkError(Exception):
    """
    Network-level error — timeout, connection refused, DNS failure.
    RETRYABLE: often transient (ISP hiccup, server restart).
    """
    pass


class APIEmptyResponseError(Exception):
    """
    Server returned 200 but body was empty or unparseable JSON.
    RETRYABLE: sometimes a transient server glitch.
    """
    pass


# ───────────────────────────────────────────────────────────────────
# Configuration constants — tuned for Z.ai/GLM API rate limits.
# ───────────────────────────────────────────────────────────────────
MAX_RETRY_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 5          # first retry after 5s
BACKOFF_MAX_SECONDS = 90          # cap at 90s (don't wait forever)
BACKOFF_JITTER_SECONDS = 2        # random 0-2s jitter added
DEFAULT_REQUEST_TIMEOUT = 30      # seconds


def _is_retryable_exception(exc: Exception) -> bool:
    """
    Determine if an exception is worth retrying.
    Used by both tenacity and pure-Python fallback.

    Retryable: rate limit, server error, network glitch, empty response.
    Not retryable: client error (400/401/403) — retrying wastes time.
    """
    return isinstance(exc, (
        APIRateLimitError,
        APIServerError,
        APINetworkError,
        APIEmptyResponseError,
    ))


# ───────────────────────────────────────────────────────────────────
# TENACITY-BASED retry decorator (production-grade, preferred).
# ───────────────────────────────────────────────────────────────────
if _TENACITY_AVAILABLE:
    def api_retry(func: Callable) -> Callable:
        """
        Decorator: wraps a function with exponential backoff retry logic.

        Behavior:
          - Retries on APIRateLimitError / APIServerError / APINetworkError / APIEmptyResponseError
          - Max 5 attempts (4 retries after the initial call)
          - Wait: exponential 5s base, 90s cap, +2s random jitter
          - Logs each retry at WARNING level (for production debugging)
          - Reraises the final exception if all retries exhausted

        The wrapped function should raise one of the API* exceptions
        on retryable failures, or return a result on success.
        """
        return _tenacity_retry(
            stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(
                initial=BACKOFF_BASE_SECONDS,
                max=BACKOFF_MAX_SECONDS,
                jitter=BACKOFF_JITTER_SECONDS,
            ),
            retry=retry_if_exception_type(
                (APIRateLimitError, APIServerError, APINetworkError, APIEmptyResponseError)
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,  # raise the last exception if all retries fail
        )(func)
else:
    # ─────────────────────────────────────────────────────────────────
    # PURE-PYTHON FALLBACK — used when tenacity isn't installed.
    # Less sophisticated (no jitter, simpler logging) but functional.
    # ─────────────────────────────────────────────────────────────────
    def api_retry(func: Callable) -> Callable:
        """
        Pure-Python fallback decorator (tenacity unavailable).
        Implements basic exponential backoff: 5s, 10s, 20s, 40s, 80s.
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if not _is_retryable_exception(exc):
                        raise  # non-retryable, fail immediately
                    if attempt >= MAX_RETRY_ATTEMPTS:
                        logger.error(
                            f"{func.__name__}: all {MAX_RETRY_ATTEMPTS} attempts failed: {exc}"
                        )
                        raise
                    # Exponential backoff: 5, 10, 20, 40, 80...
                    wait = min(
                        BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                        BACKOFF_MAX_SECONDS,
                    )
                    logger.warning(
                        f"{func.__name__}: attempt {attempt} failed ({exc}), "
                        f"retrying in {wait}s..."
                    )
                    time.sleep(wait)
            raise last_exc  # type: ignore
        return wrapper


# ───────────────────────────────────────────────────────────────────
# HTTP status classifier — used by callers to classify raw responses.
# ───────────────────────────────────────────────────────────────────
def classify_http_status(status_code: int, response_text: str = "", url: str = "") -> None:
    """
    Classify an HTTP status code and raise the appropriate exception.

    This is the CORE decision point for retry logic. Callers should
    call this after getting a response, BEFORE parsing the body.

    Args:
        status_code: HTTP status from response.status_code
        response_text: response body (truncated in error message)
        url: request URL (for debugging)

    Raises:
        APIRateLimitError: 429
        APIServerError: 5xx (500, 502, 503, 504, etc.)
        APIClientError: 4xx except 429 (400, 401, 403, 404, etc.)
        (no exception for 2xx success)
    """
    if status_code == 429:
        raise APIRateLimitError(status_code, response_text, url)
    elif 500 <= status_code < 600:
        raise APIServerError(status_code, response_text, url)
    elif 400 <= status_code < 500:
        raise APIClientError(status_code, response_text, url)
    # 2xx and 3xx — no exception (success / redirect handled by requests)


# ───────────────────────────────────────────────────────────────────
# PUBLIC API: High-level call_with_retry() — single entry point.
# Callers should use THIS rather than the decorator directly, because
# it handles the full lifecycle: call -> classify -> retry -> parse.
# ───────────────────────────────────────────────────────────────────
def call_with_retry(
    url: str,
    method: str = "POST",
    headers: Optional[dict] = None,
    json_payload: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    expect_json: bool = True,
    session: Any = None,
) -> Optional[Any]:
    """
    Execute an HTTP API call with full retry + error handling.

    This is the SINGLE ENTRY POINT for all AI API calls (GLM, etc.).
    It handles:
      - Making the request (via provided session or new requests call)
      - Classifying HTTP status (429/5xx retryable, 4xx fail-fast)
      - Retrying with exponential backoff on retryable errors
      - Parsing JSON response
      - Returning None on any failure (callers handle gracefully)

    Args:
        url: Full API endpoint URL
        method: HTTP method ("POST" or "GET")
        headers: Request headers dict (must include Authorization for GLM)
        json_payload: JSON body for POST requests
        params: Query params for GET requests
        timeout: Request timeout in seconds
        expect_json: If True, parse response as JSON and return dict.
                     If False, return raw response text.
        session: Optional requests.Session to reuse (for cookie persistence).
                 If None, a fresh request is made (no session reuse).

    Returns:
        - On success: parsed JSON dict (if expect_json=True) or response text
        - On failure (after all retries, or non-retryable error): None

    Thread-safety: stateless — safe for concurrent calls from multiple
    scanner threads. The retry state is local to each call.

    Example (GLM API call):
        result = call_with_retry(
            url="https://api.z.ai/api/paas/v4/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_payload={"model": "glm-4.5", "messages": [...], "max_tokens": 300},
        )
        if result is None:
            # fall back to rule-based analysis
        else:
            text = result["choices"][0]["message"]["content"]
    """
    import requests

    @api_retry
    def _attempt() -> Any:
        """Single API attempt — raises classified exceptions on failure."""
        try:
            if session is not None:
                resp = session.request(
                    method, url,
                    headers=headers, json=json_payload, params=params,
                    timeout=timeout,
                )
            else:
                resp = requests.request(
                    method, url,
                    headers=headers, json=json_payload, params=params,
                    timeout=timeout,
                )
        except requests.exceptions.Timeout as e:
            raise APINetworkError(f"Request timeout after {timeout}s: {e}")
        except requests.exceptions.ConnectionError as e:
            raise APINetworkError(f"Connection error: {e}")
        except requests.exceptions.RequestException as e:
            raise APINetworkError(f"Request error: {e}")

        # Classify HTTP status (raises APIRateLimitError/APIServerError/APIClientError)
        classify_http_status(resp.status_code, resp.text, url)

        # Success (2xx) — parse response
        if expect_json:
            try:
                return resp.json()
            except ValueError as e:
                # Empty or malformed JSON — treat as transient (retry)
                raise APIEmptyResponseError(
                    f"Invalid/empty JSON response from {url}: {e}. Body: {resp.text[:200]}"
                )
        return resp.text

    # Execute with retry — catch ALL exceptions and return None on failure.
    try:
        return _attempt()
    except (APIRateLimitError, APIServerError, APINetworkError, APIEmptyResponseError) as e:
        logger.error(
            f"API call to {url} failed after {MAX_RETRY_ATTEMPTS} retries: "
            f"{type(e).__name__}: {e}"
        )
        return None
    except APIClientError as e:
        logger.error(f"API call to {url} failed (non-retryable client error): {e}")
        return None
    except RetryError as e:
        logger.error(f"API call to {url} retry exhausted: {e}")
        return None
    except Exception as e:
        # Catch-all for any unexpected error — NEVER crash the caller.
        logger.error(f"API call to {url} unexpected error: {type(e).__name__}: {e}")
        return None


# ───────────────────────────────────────────────────────────────────
# Convenience: diagnostic function for checking retry config.
# ───────────────────────────────────────────────────────────────────
def get_retry_config() -> dict:
    """Returns the current retry configuration (for logging/diagnostics)."""
    return {
        "library": "tenacity" if _TENACITY_AVAILABLE else "pure-python-fallback",
        "max_attempts": MAX_RETRY_ATTEMPTS,
        "backoff_base_seconds": BACKOFF_BASE_SECONDS,
        "backoff_max_seconds": BACKOFF_MAX_SECONDS,
        "jitter_seconds": BACKOFF_JITTER_SECONDS,
        "retryable_exceptions": [
            "APIRateLimitError (429)",
            "APIServerError (5xx)",
            "APINetworkError (timeout/connection)",
            "APIEmptyResponseError (malformed JSON)",
        ],
        "non_retryable_exceptions": [
            "APIClientError (400/401/403 — fail immediately)",
        ],
    }


def is_retry_library_available() -> bool:
    """Returns True if tenacity is active (full retry capability)."""
    return _TENACITY_AVAILABLE
