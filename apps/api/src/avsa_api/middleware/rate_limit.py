"""Sliding-window rate limiter - FastAPI dependency, scoped to individual routes."""

from collections import OrderedDict, deque
from time import monotonic
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from prometheus_client import Counter

_RATE_LIMIT_TOTAL = Counter(
    "avsa_api_rate_limit_total",
    "Requests rejected by the per-IP sliding-window rate limiter",
)

_MAX_TRACKED_IPS = 100_000


class SlidingWindowLimiter:
    """Per-IP sliding-window rate limiter using a deque of monotonic timestamps.

    An OrderedDict provides O(1) LRU eviction: keys are moved to the end on
    each access, and the oldest key is popped when the dict exceeds
    _MAX_TRACKED_IPS. This bounds memory under slow-drip multi-IP attacks.
    """

    def __init__(self, rpm: int) -> None:
        self._rpm = rpm
        self._window: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> None:
        """Record a request for key, raising 429 if the window is exhausted."""
        now = monotonic()
        if key in self._window:
            self._window.move_to_end(key)
        q = self._window.setdefault(key, deque())
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._rpm:
            _RATE_LIMIT_TOTAL.inc()
            raise HTTPException(
                status_code=429,
                headers={"Retry-After": "60"},
                detail="Rate limit exceeded",
            )
        q.append(now)
        while len(self._window) > _MAX_TRACKED_IPS:
            self._window.popitem(last=False)


def _client_ip(request: Request) -> str:
    """Extract client IP from the rightmost X-Forwarded-For entry.

    In a single-reverse-proxy deployment the trusted proxy appends the real
    client IP at the right of the header, making the rightmost value the
    one the proxy observed - not a client-supplied field. Falls back to the
    TCP peer address when the header is absent.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    if request.client:
        return request.client.host
    return "unknown"  # all requests without an identifiable IP count against the same allowance


def get_limiter(request: Request) -> SlidingWindowLimiter:
    """FastAPI dependency - returns the app-scoped limiter stored in app.state."""
    try:
        return request.app.state.limiter  # type: ignore[no-any-return]
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="Service not ready") from exc


RateLimiter = Annotated[SlidingWindowLimiter, Depends(get_limiter)]
