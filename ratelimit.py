"""Client-side rate limiting + exponential backoff for outbound API calls."""

import logging
import random
import threading
import time

import requests

log = logging.getLogger("sitehunter.ratelimit")


class RateLimiter:
    """Simple thread-safe token bucket: at most `rate` calls per `per` seconds."""

    def __init__(self, name: str, rate: int, per: float = 60.0):
        self.name = name
        self.rate = max(1, rate)
        self.per = per
        self._lock = threading.Lock()
        self._tokens = float(self.rate)
        self._updated = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.rate, self._tokens + elapsed * (self.rate / self.per))
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) * (self.per / self.rate)
            log.debug("%s rate limit reached, sleeping %.2fs", self.name, wait)
            time.sleep(min(wait, self.per))


class RateLimitError(Exception):
    """Raised when a call keeps failing after all retries."""


RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def _retry_after(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def request_with_backoff(
    limiter: RateLimiter,
    method: str,
    url: str,
    *,
    max_retries: int = 5,
    base_delay: float = 1.5,
    max_delay: float = 60.0,
    **kwargs,
) -> requests.Response:
    """Perform an HTTP request under a rate limit, retrying with exponential backoff."""
    attempt = 0
    while True:
        limiter.acquire()
        response = None
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in RETRY_STATUS:
                raise requests.HTTPError(
                    f"{response.status_code} from {url}", response=response
                )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status in RETRY_STATUS
            attempt += 1
            if not retryable or attempt > max_retries:
                raise RateLimitError(
                    f"{limiter.name} request failed after {attempt} attempt(s): {exc}"
                ) from exc
            delay = _retry_after(response) or min(
                max_delay, base_delay * (2 ** (attempt - 1))
            )
            delay += random.uniform(0, delay * 0.25)
            log.warning(
                "%s call failed (%s) — retry %s/%s in %.1fs",
                limiter.name, exc, attempt, max_retries, delay,
            )
            time.sleep(min(delay, max_delay))
