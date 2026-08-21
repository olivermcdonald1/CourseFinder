"""
Per-IP request throttle.

No dependency and no Redis: this runs as a single machine, so an in-process
counter is the whole truth. That is also its limit -- the window resets on
deploy and two machines would each allow the full rate. Scaling out means
moving this to a shared store, not raising the number.

Why it exists at all: every /courses request is three Postgres queries against
a free-tier database, and there is no auth in front of it. One loop in someone's
terminal is enough to make the site unusable for everyone else.
"""

import os
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

WINDOW = 60.0
LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))

# A student clicking filters quickly is maybe 20 requests a minute, so 120
# leaves generous headroom for real use while stopping a scripted sweep.
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request) -> str:
    """
    The real client address, which behind a proxy is not request.client.

    Fly (and any sane edge) sets X-Forwarded-For as a chain, appending each hop,
    so the LEFTMOST entry is the originating client. Trusting it requires that
    only the proxy can reach this process -- true here, since Fly terminates TLS
    and nothing else routes to the machine. Exposed directly to the internet
    this header is client-controlled and must not be trusted.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimit(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Static assets and the liveness probe are cheap and must never be
        # throttled -- a 429 on /health would make the platform kill a machine
        # that is actually fine.
        if request.method != "GET" or request.url.path in ("/health", "/"):
            return await call_next(request)

        now = time.monotonic()
        bucket = _hits[_client_ip(request)]
        while bucket and now - bucket[0] > WINDOW:
            bucket.popleft()

        if len(bucket) >= LIMIT:
            retry = int(WINDOW - (now - bucket[0])) + 1
            return JSONResponse(
                {"detail": "Too many requests. Slow down and try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        bucket.append(now)

        # Unbounded dicts are how a long-running process leaks. Sweeping only
        # when it grows large keeps the common path free of housekeeping.
        if len(_hits) > 4096:
            for ip in [k for k, v in _hits.items() if not v or now - v[-1] > WINDOW]:
                del _hits[ip]

        return await call_next(request)
