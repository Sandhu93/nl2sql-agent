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
from collections import defaultdict

import psycopg2

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_FULL_TO_SHORT: dict[str, str] | None = None
_SHORT_NAMES: set[str] | None = None
_SURNAME_INITIAL_TO_SHORT: dict[tuple[str, str], list[str]] | None = None


def _norm(text: str) -> str:
    """Lowercase + collapse non-alnum separators for robust name matching."""
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def _load_player_index() -> None:
    """Load players name index from DB once and cache in module globals."""
    global _FULL_TO_SHORT, _SHORT_NAMES, _SURNAME_INITIAL_TO_SHORT
    if _FULL_TO_SHORT is not None:
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
        logger.info(
            "Player resolver index loaded | players=%d | full_names=%d",
            len(rows),
            len(_FULL_TO_SHORT),
        )
    except Exception as exc:
        logger.warning("Player resolver index load failed (non-blocking): %s", exc)
        _FULL_TO_SHORT = {}
        _SHORT_NAMES = set()
        _SURNAME_INITIAL_TO_SHORT = {}


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
