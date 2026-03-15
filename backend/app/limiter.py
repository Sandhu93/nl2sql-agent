"""
Rate limiter — Phase 10 (production hardening).

Singleton slowapi Limiter used by the /api/query endpoint.

Storage strategy
----------------
Redis backend (preferred):
  Limits are shared across all backend replicas and survive restarts.
  Uses the same Redis instance as conversation history so there is no
  extra infrastructure cost.

In-memory fallback:
  If Redis is unreachable at startup we fall back to in-process counters.
  Limits work correctly for a single replica but reset on restart and are
  NOT shared across replicas.  This keeps local dev without Redis working.

Usage
-----
In main.py (app registration):

    from app.limiter import limiter
    app.state.limiter = limiter

In routes (decorator):

    from app.limiter import limiter

    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def my_endpoint(request: Request, ...):
        ...

slowapi requires `request: Request` as an explicit parameter on any
decorated endpoint — FastAPI will not inject it automatically.
"""

import logging

import redis as redis_lib
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_limiter() -> Limiter:
    """
    Build the rate limiter, preferring Redis storage over in-memory.

    Redis storage is required for correctness when running multiple backend
    replicas — in-memory counters are per-process and reset on restart.

    Falls back gracefully so local dev without Redis still works.
    """
    try:
        client = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        client.ping()
        logger.info(
            "Rate limiter using Redis backend | url=%s | limit=%d/min",
            settings.redis_url,
            settings.rate_limit_per_minute,
        )
        return Limiter(
            key_func=get_remote_address,
            storage_uri=settings.redis_url,
        )
    except Exception as exc:
        logger.warning(
            "Redis unavailable — rate limiter falling back to in-memory storage "
            "(limits will not persist across restarts or replicas) | error=%s",
            exc,
        )
        return Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Module-level singleton
# Imported by:
#   main.py          — app.state.limiter + RateLimitExceeded handler
#   routes/query.py  — @limiter.limit() decorator
# ---------------------------------------------------------------------------
# TODO: If you need per-user limits (authenticated API), swap get_remote_address
#       for a key_func that extracts the user ID from the JWT token.
limiter = _build_limiter()
