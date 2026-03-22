"""
schema_watcher.py — Phase 14: Schema Drift Detection

Runs once at startup (via FastAPI lifespan) to detect if the PostgreSQL schema
for the 9 known IPL tables has changed since the last baseline was recorded.

What it checks
--------------
Queries information_schema.columns for the 9 known tables, builds a
deterministic SHA-256 fingerprint from sorted (table_name, column_name,
data_type, is_nullable) tuples, then compares it against the baseline stored
in Redis under "nl2sql:schema_hash" (no TTL — persists until Redis is flushed).

  On drift:         logs WARNING with stored vs. current hash prefix, then
                    updates the baseline so subsequent restarts don't re-warn
                    until another schema change occurs.
  On match:         logs INFO (schema clean, table + hash count).
  No hash (first):  writes the baseline and logs INFO.

It also logs data coverage stats (MAX(year), match count, delivery count)
so operators can verify data freshness after deployments or bulk imports.

Non-blocking guarantee
----------------------
The entire function is wrapped in try/except. A DB or Redis failure at
startup never prevents the app from serving requests. When Redis is
unavailable the watcher still logs coverage stats from the DB alone.

TODO: When circuit breaker state moves to Redis (multi-replica consistency),
      consider co-locating the schema hash in the same Redis instance.
"""

import asyncio
import hashlib
import logging
from typing import Optional

import psycopg2
import redis as redis_lib

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The 9 tables whose schema we track. Hardcoded so a missing table is flagged
# rather than silently ignored (as would happen with a dynamic catalog scan).
KNOWN_TABLES: list[str] = sorted([
    "deliveries",
    "drs_reviews",
    "matches",
    "players",
    "playing_xi",
    "replacements",
    "team_aliases",
    "teams",
    "wicket_fielders",
])

_SCHEMA_HASH_KEY = "nl2sql:schema_hash"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect_db() -> psycopg2.extensions.connection:
    """Open a short-lived psycopg2 connection for the watcher."""
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        dbname=settings.db_name,
        connect_timeout=3,
    )


def _build_schema_fingerprint(conn: psycopg2.extensions.connection) -> str:
    """
    Build a deterministic SHA-256 fingerprint of the known tables' columns.

    Covers table_name, column_name, data_type, is_nullable — the attributes
    that matter for SQL generation correctness. Deliberately excludes
    column_default and character_maximum_length to avoid false-positive alerts
    from sequence resets or length tweaks that don't affect query logic.

    Also logs a WARNING if any of the 9 expected tables are missing entirely
    from the public schema, which is a more severe form of drift.
    """
    placeholders = ",".join(["%s"] * len(KNOWN_TABLES))
    query = f"""
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ({placeholders})
        ORDER BY table_name, column_name
    """
    with conn.cursor() as cur:
        cur.execute(query, KNOWN_TABLES)
        rows = cur.fetchall()

    found_tables = {row[0] for row in rows}
    missing = set(KNOWN_TABLES) - found_tables
    if missing:
        logger.warning(
            "Schema watcher: expected tables missing from DB | missing=%s",
            sorted(missing),
        )

    fingerprint = "|".join(f"{t}:{c}:{dt}:{nn}" for t, c, dt, nn in rows)
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _log_data_coverage(conn: psycopg2.extensions.connection) -> None:
    """Log data freshness: MAX(year), total matches, total deliveries."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(year), COUNT(*) FROM matches")
            max_year, match_count = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM deliveries")
            delivery_count = cur.fetchone()[0]
        logger.info(
            "Data coverage | max_year=%s | matches=%d | deliveries=%d",
            max_year,
            match_count,
            delivery_count,
        )
    except Exception as exc:
        logger.warning("Schema watcher: data coverage query failed: %s", exc)


# ---------------------------------------------------------------------------
# Hash comparison
# ---------------------------------------------------------------------------

def _check_and_store_hash(
    conn: psycopg2.extensions.connection,
    redis_client: Optional[redis_lib.Redis],
) -> None:
    """Compare the current fingerprint against the Redis-stored baseline."""
    current_hash = _build_schema_fingerprint(conn)

    if redis_client is None:
        logger.warning(
            "Schema watcher: Redis unavailable — schema hash not persisted | current=%s",
            current_hash[:8],
        )
        return

    stored_raw = redis_client.get(_SCHEMA_HASH_KEY)

    if stored_raw is None:
        # First run or Redis was flushed — write the baseline.
        redis_client.set(_SCHEMA_HASH_KEY, current_hash)
        logger.info(
            "Schema watcher: no baseline found — baseline recorded | hash=%s",
            current_hash[:8],
        )
    elif stored_raw.decode("ascii", errors="replace") == current_hash:
        logger.info(
            "Schema drift check: no changes detected | tables=%d | hash=%s",
            len(KNOWN_TABLES),
            current_hash[:8],
        )
    else:
        logger.warning(
            "Schema drift detected — columns may have changed. "
            "Review prompts and few-shot examples before next release. "
            "| stored=%s | current=%s",
            stored_raw.decode("ascii", errors="replace")[:8],
            current_hash[:8],
        )
        # Update baseline so subsequent restarts don't re-warn until
        # another schema change occurs. The WARNING is the operator's signal.
        redis_client.set(_SCHEMA_HASH_KEY, current_hash)


# ---------------------------------------------------------------------------
# Core (synchronous) — called via asyncio.to_thread
# ---------------------------------------------------------------------------

def _run_watcher() -> None:
    """
    Synchronous core of the schema watcher.

    Opens its own short-lived DB and Redis connections (independent of the
    agent module singletons) so there are no circular imports or startup
    ordering issues. Both connections are closed in the finally block.
    """
    conn: Optional[psycopg2.extensions.connection] = None
    redis_client: Optional[redis_lib.Redis] = None

    # Attempt Redis — failure is non-fatal.
    try:
        redis_client = redis_lib.from_url(
            settings.redis_url, socket_connect_timeout=2
        )
        redis_client.ping()
    except Exception as exc:
        logger.warning(
            "Schema watcher: Redis unavailable (%s) — hash not persisted",
            type(exc).__name__,
        )
        redis_client = None

    # Attempt DB — failure aborts the check (nothing to hash without a DB).
    try:
        conn = _connect_db()
    except Exception as exc:
        logger.warning(
            "Schema watcher: DB unavailable — skipping drift check (%s)",
            type(exc).__name__,
        )
        return

    try:
        _log_data_coverage(conn)
        _check_and_store_hash(conn, redis_client)
    finally:
        conn.close()
        if redis_client:
            try:
                redis_client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Async entry point — called from main.py lifespan
# ---------------------------------------------------------------------------

async def run_schema_watcher() -> None:
    """
    Async entry point called once from the FastAPI lifespan at startup.

    Delegates all blocking I/O to a thread via asyncio.to_thread() so the
    event loop is never blocked during startup. Any unexpected exception is
    caught and logged — the app always proceeds to serve requests.

    TODO: Add a Prometheus counter here when observability is added (Phase 14b)
          to track schema drift events across deployments.
    """
    try:
        await asyncio.to_thread(_run_watcher)
    except Exception as exc:
        logger.warning(
            "Schema watcher: unexpected error during startup check: %s", exc
        )
