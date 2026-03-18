"""
Entity resolution helpers for NL questions.

Current scope:
  - Player-name normalization (full name -> dataset short name)
    Example: "Rohit Sharma" -> "RG Sharma"

The resolver reads the players table once (lazy singleton) and augments the
question with an explicit mapping hint so SQL generation can reliably match
the dataset naming convention.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

import psycopg2

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_FULL_TO_SHORT: dict[str, str] | None = None
_SHORT_NAMES: set[str] | None = None
_SURNAME_INITIAL_TO_SHORT: dict[tuple[str, str], list[str]] | None = None
# Monotonic timestamp of the last successful index load.
# None means the index has never been loaded (or was explicitly reset).
_INDEX_LOADED_AT: float | None = None


def _is_index_stale() -> bool:
    """
    Return True when the index has never been loaded or the TTL has expired.

    Uses monotonic time so the comparison is unaffected by wall-clock changes
    (e.g. DST or NTP adjustments).  TTL is controlled by
    settings.player_index_ttl_seconds (default 3600 s / 1 hour).
    """
    if _INDEX_LOADED_AT is None:
        return True
    return (time.monotonic() - _INDEX_LOADED_AT) > settings.player_index_ttl_seconds


def refresh_player_index() -> None:
    """
    Force an immediate reload of the player name index from the database.

    Use this after inserting new players into the players table mid-season,
    or to recover from a failed initial load (e.g. DB unreachable at startup).

    The index globals are reset to None before the reload so that a concurrent
    request will wait for the new load rather than reading stale data.

    TODO: This is not concurrency-safe under multiple uvicorn workers sharing
          the same process space (threaded workers).  For multi-worker safety,
          add a threading.Lock around the reset+reload block.
    """
    global _FULL_TO_SHORT, _SHORT_NAMES, _SURNAME_INITIAL_TO_SHORT, _INDEX_LOADED_AT
    _FULL_TO_SHORT = None
    _SHORT_NAMES = None
    _SURNAME_INITIAL_TO_SHORT = None
    _INDEX_LOADED_AT = None
    logger.info("Player resolver index refresh triggered")
    _load_player_index()


def _norm(text: str) -> str:
    """Lowercase + collapse non-alnum separators for robust name matching."""
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def _load_player_index() -> None:
    """
    Load (or refresh) the player name index from the DB and cache in module globals.

    Skips the load when the index is already populated AND has not exceeded its
    TTL (settings.player_index_ttl_seconds).  This means:
      - First call: always loads.
      - Subsequent calls within TTL: returns immediately (free).
      - Subsequent calls after TTL: silently reloads from DB.
    """
    global _FULL_TO_SHORT, _SHORT_NAMES, _SURNAME_INITIAL_TO_SHORT, _INDEX_LOADED_AT
    if _FULL_TO_SHORT is not None and not _is_index_stale():
        return

    full_to_short: dict[str, str] = {}
    short_names: set[str] = set()
    surname_initial_to_short: dict[tuple[str, str], list[str]] = defaultdict(list)

    try:
        conn = psycopg2.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT player_name, COALESCE(player_full_name, '')
                    FROM players
                    WHERE player_name IS NOT NULL
                    """
                )
                rows = cur.fetchall()
        conn.close()

        for short_name, full_name in rows:
            short_name = str(short_name).strip()
            full_name = str(full_name).strip()
            if not short_name:
                continue

            short_names.add(short_name.lower())

            if full_name:
                full_to_short[_norm(full_name)] = short_name

            parts = short_name.split()
            if len(parts) >= 2:
                first_initial = parts[0][0].lower()
                surname = parts[-1].lower()
                surname_initial_to_short[(first_initial, surname)].append(short_name)

        _FULL_TO_SHORT = full_to_short
        _SHORT_NAMES = short_names
        _SURNAME_INITIAL_TO_SHORT = surname_initial_to_short
        _INDEX_LOADED_AT = time.monotonic()
        logger.info(
            "Player resolver index loaded | players=%d | full_names=%d | ttl=%ds",
            len(rows),
            len(_FULL_TO_SHORT),
            settings.player_index_ttl_seconds,
        )
    except Exception as exc:
        logger.warning("Player resolver index load failed (non-blocking): %s", exc)
        _FULL_TO_SHORT = {}
        _SHORT_NAMES = set()
        _SURNAME_INITIAL_TO_SHORT = {}
        # Do not update _INDEX_LOADED_AT on failure so the next call retries.


def resolve_player_mentions(question: str) -> tuple[str, dict[str, str]]:
    """
    Resolve full-name mentions to dataset short names.

    Returns:
        (possibly_augmented_question, mapping_dict)
    """
    _load_player_index()
    assert _FULL_TO_SHORT is not None
    assert _SHORT_NAMES is not None
    assert _SURNAME_INITIAL_TO_SHORT is not None

    mapping: dict[str, str] = {}
    question_lc = question.lower()

    # Heuristic: capitalized two-token names in user questions.
    # Example: "Rohit Sharma", "Virat Kohli"
    candidates = re.findall(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", question)
    for first, last in candidates:
        full_name = f"{first} {last}"
        normalized = _norm(full_name)

        short = _FULL_TO_SHORT.get(normalized)
        if short is None:
            key = (first[0].lower(), last.lower())
            surname_candidates = _SURNAME_INITIAL_TO_SHORT.get(key, [])
            if len(surname_candidates) == 1:
                short = surname_candidates[0]

        if not short:
            continue

        # If the short dataset name is already present, no hint needed.
        if short.lower() in question_lc:
            continue

        mapping[full_name] = short

    if not mapping:
        return question, {}

    mapping_hint = "; ".join(f"{full} -> {short}" for full, short in mapping.items())
    augmented = f"{question.rstrip()} (Dataset player name mapping: {mapping_hint})"
    return augmented, mapping
