"""Retry helper for the two HTTP clients.

Long solves poll Timefold every few seconds. A connection left idle between
polls can be closed by the server, and the next request on that pooled
connection fails with `RemoteProtocolError: Server disconnected without sending
a response`. The same happens against Dataverse over the open internet.

Neither is a real failure. Both should be retried.

Retry policy:

  * Connection-level errors where the request certainly never arrived
    (connect errors, pool timeouts) are retried for any method.
  * A dropped or timed-out response is retried only for methods that are safe
    to repeat: GET, HEAD, DELETE. A POST or PATCH may already have been
    applied, so it is surfaced rather than silently duplicated.
  * 429 and the 502/503/504 family are retried, honouring Retry-After.
"""
from __future__ import annotations

import logging
import random
import time

import httpx

log = logging.getLogger("tfd365.http")

# The request never left, so repeating it is always safe.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The request may have been received; only repeat for idempotent methods.
MAYBE_SENT = (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout,
              httpx.WriteError, httpx.NetworkError)

IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "DELETE"}
RETRY_STATUS = {429, 502, 503, 504}

MAX_ATTEMPTS = 4
BASE_DELAY = 0.5
MAX_DELAY = 8.0


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so parallel clients do not sync up."""
    return min(MAX_DELAY, BASE_DELAY * (2 ** attempt)) * (0.5 + random.random() / 2)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), MAX_DELAY * 2)
    except ValueError:
        return None                     # HTTP-date form; fall back to backoff


def request(client: httpx.Client, method: str, url: str, *,
            context: str = "", max_attempts: int = MAX_ATTEMPTS,
            **kwargs) -> httpx.Response:
    """Send a request, retrying transient failures."""
    safe_to_repeat = method.upper() in IDEMPOTENT
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, **kwargs)
        except NEVER_SENT as e:
            last_error = e
            if attempt == max_attempts - 1:
                break
            delay = _backoff(attempt)
            log.warning("%s %s did not connect (%s); retrying in %.1fs [%d/%d]",
                        method, context or url, type(e).__name__, delay,
                        attempt + 1, max_attempts - 1)
            time.sleep(delay)
            continue
        except MAYBE_SENT as e:
            last_error = e
            if not safe_to_repeat or attempt == max_attempts - 1:
                break
            delay = _backoff(attempt)
            log.warning("%s %s dropped (%s); retrying in %.1fs [%d/%d]",
                        method, context or url, type(e).__name__, delay,
                        attempt + 1, max_attempts - 1)
            time.sleep(delay)
            continue

        if response.status_code in RETRY_STATUS and attempt < max_attempts - 1:
            delay = _retry_after(response) or _backoff(attempt)
            log.warning("%s %s returned %d; retrying in %.1fs [%d/%d]",
                        method, context or url, response.status_code, delay,
                        attempt + 1, max_attempts - 1)
            time.sleep(delay)
            continue

        return response

    raise last_error if last_error else RuntimeError("retry loop ended with no result")
