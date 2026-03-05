"""
Input validation and prompt-injection defence for user questions.

validate_question(question) is the single public function.
It raises ValueError with a safe, user-visible message on any violation.
All security decisions are logged at WARNING level for audit purposes.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Maximum characters allowed in a single question.
# Long inputs can flood the context window and are a common token-stuffing vector.
_MAX_QUESTION_LENGTH = 500

# Regex patterns that indicate prompt-injection attempts.
# These phrases have no place in a natural-language cricket question — they are
# instructions directed at the LLM, not queries about IPL data.
_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore previous instructions",
     re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE)),
    ("role override",
     re.compile(r"you\s+are\s+now\s+(a\s+)?(?!cricket|ipl|expert)", re.IGNORECASE)),
    ("forget instructions",
     re.compile(r"forget\s+(your\s+)?(role|instructions?|context|rules?)", re.IGNORECASE)),
    ("disregard directive",
     re.compile(r"disregard\s+(all\s+)?(previous|prior|above|your)", re.IGNORECASE)),
    ("new instructions injection",
     re.compile(r"new\s+instructions?\s*:", re.IGNORECASE)),
    ("fake system message",
     re.compile(r"\bsystem\s*:\s*you\s+are\b", re.IGNORECASE)),
    ("jailbreak DAN",
     re.compile(r"\bdo\s+anything\s+now\b|\bdan\s+mode\b", re.IGNORECASE)),
]

# SQL DDL/DML keywords that have no reason to appear in a natural-language
# question. A user asking about cricket will never need to say "DROP" or
# "DELETE" — these signal either injection or an attempt to pass raw SQL.
_DANGEROUS_SQL_IN_INPUT = re.compile(
    r"\b(drop|delete|truncate|update|insert|alter|create|grant|revoke|execute|copy)\b",
    re.IGNORECASE,
)


def validate_question(question: str) -> str:
    """
    Validate and sanitize a natural-language question before it reaches the LLM.

    Checks (in order):
      1. Non-empty after stripping whitespace.
      2. Length within _MAX_QUESTION_LENGTH.
      3. No prompt-injection phrases.
      4. No SQL DDL/DML keywords (they belong in queries, not questions).

    Args:
        question: Raw string from the user.

    Returns:
        The stripped question string if all checks pass.

    Raises:
        ValueError: With a user-safe message if any check fails.
                    The detailed reason is logged server-side only.
    """
    question = question.strip()

    if not question:
        raise ValueError("Question cannot be empty.")

    if len(question) > _MAX_QUESTION_LENGTH:
        logger.warning(
            "Input rejected: question too long | length=%d | max=%d",
            len(question), _MAX_QUESTION_LENGTH,
        )
        raise ValueError(
            f"Question is too long (max {_MAX_QUESTION_LENGTH} characters). "
            "Please shorten your question."
        )

    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(question):
            logger.warning(
                "Input rejected: prompt-injection pattern detected | pattern=%r | question=%r",
                label, question,
            )
            raise ValueError("I can only answer questions about IPL cricket data.")

    if _DANGEROUS_SQL_IN_INPUT.search(question):
        logger.warning(
            "Input rejected: SQL DDL/DML keyword in question | question=%r", question,
        )
        raise ValueError("I can only answer questions about IPL cricket data.")

    return question
