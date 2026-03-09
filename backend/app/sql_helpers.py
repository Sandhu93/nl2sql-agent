"""
SQL parsing and execution utilities.

Pure functions — no state, no LangChain chains, no LLM calls.
Imported by agent.py for SQL cleaning, error detection, and execution.
"""

import re

from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool


def _strip_prefix_and_prose(text: str) -> str:
    """
    Strip a 'SQLQuery:' / 'SQL:' prefix and any leading non-SQL prose
    from a fragment that is already expected to be mostly SQL.
    Applied both to the full raw output (no-code-block path) and to
    the content extracted from each individual code block.
    """
    text = text.strip()

    # Remove known LangChain / model label prefixes
    for prefix in ("SQLQuery:", "SQL Query:", "SQL:"):
        if text.upper().startswith(prefix.upper()):
            text = text[len(prefix):].strip()
            break

    # If prose still precedes the SQL, jump to the first SQL keyword
    sql_start = re.search(
        r"\b(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|DROP|ALTER)\b",
        text,
        re.IGNORECASE,
    )
    if sql_start:
        text = text[sql_start.start():]

    return text.strip()


def _clean_sql(raw: str) -> str:
    """
    Extract pure SQL from LLM output that may contain:
      - Markdown code fences:  ```sql ... ```  or  ``` ... ```
      - Prefixes:              'SQLQuery:'  'SQL Query:'  'SQL:'
      - Explanatory prose before / after the actual query
      - MULTIPLE code blocks (e.g. one per sub-query)

    Strategy:
      1. Find ALL ```sql/``` blocks with findall (not just the first one).
         Each block is individually cleaned of any prefix inside it.
         All blocks are joined so _run_sql can execute them sequentially.
      2. No code blocks → fall back to prefix stripping + keyword search
         on the full raw text.
    """
    text = raw.strip()

    # 1. Collect every markdown code block (```sql ... ``` or ``` ... ```)
    #    Bug fix: re.search only returned the FIRST block; re.findall gets ALL.
    blocks = re.findall(r"```(?:sql)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if blocks:
        cleaned = [_strip_prefix_and_prose(b) for b in blocks]
        # Drop empty fragments; join with a blank line so _run_sql can split them
        return "\n\n".join(b for b in cleaned if b)

    # 2. No code fences — strip prefix / prose from the full text
    return _strip_prefix_and_prose(text)


def _is_sql_error(result: str) -> bool:
    """
    Return True if QuerySQLDataBaseTool returned a database error string.

    QuerySQLDataBaseTool does NOT raise on SQL failure — it returns the
    exception message as a plain string starting with 'Error:'.  We must
    detect this pattern to decide whether to retry via _fix_sql().
    """
    return result.strip().startswith("Error:")


# ---------------------------------------------------------------------------
# SQL output validation — called after _clean_sql(), before execution.
# Ensures the LLM can never produce a destructive or system-level statement
# regardless of what was injected into the prompt.
# ---------------------------------------------------------------------------

_ALLOWED_SQL_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_SQL_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|UPDATE|INSERT|ALTER|CREATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)
_SYSTEM_TABLE_ACCESS = re.compile(
    r"\b(pg_[a-z_]+|information_schema)\b",
    re.IGNORECASE,
)

# Semantic guardrails for cricket stats queries.
# `batsman_runs` is per-ball runs (0..6), so filters like batsman_runs = 119 are
# almost always a grain mismatch where the model intended innings total runs.
_BATSMAN_RUNS_EQ_LITERAL = re.compile(
    r"\bbatsman_runs\b\s*=\s*(\d+)\b",
    re.IGNORECASE,
)
_BATSMAN_RUNS_GT_LITERAL = re.compile(
    r"\bbatsman_runs\b\s*(>=|>|<=|<)\s*(\d+)\b",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> None:
    """
    Reject any SQL that is not a read-only SELECT/WITH query.

    This is a defence-in-depth check: even if a prompt-injection or
    model hallucination causes the LLM to emit a destructive statement,
    it will be blocked here before it ever reaches the database.

    Args:
        sql: Cleaned SQL string produced by _clean_sql().

    Raises:
        ValueError: With a user-safe message if the SQL is disallowed.
                    The blocked SQL is logged server-side for audit.
    """
    import logging
    _logger = logging.getLogger(__name__)

    if not _ALLOWED_SQL_START.match(sql):
        _logger.warning("SQL blocked: does not start with SELECT/WITH | sql=%r", sql[:120])
        raise ValueError("Only read-only SELECT queries are supported.")

    if _FORBIDDEN_SQL_KEYWORDS.search(sql):
        match = _FORBIDDEN_SQL_KEYWORDS.search(sql)
        _logger.warning(
            "SQL blocked: forbidden keyword %r | sql=%r",
            match.group(0) if match else "?", sql[:120],
        )
        raise ValueError("Only read-only SELECT queries are supported.")

    if _SYSTEM_TABLE_ACCESS.search(sql):
        _logger.warning("SQL blocked: system table access | sql=%r", sql[:120])
        raise ValueError("Only read-only SELECT queries are supported.")


def detect_semantic_sql_issue(sql: str) -> str | None:
    """
    Detect high-confidence logical SQL issues that still execute syntactically.

    Current checks:
      - Impossible per-ball batsman_runs comparisons with values > 6

    Returns:
      Human-readable issue string, or None if no issue detected.
    """
    # batsman_runs = N where N > 6 is always impossible in IPL ball-level data.
    for match in _BATSMAN_RUNS_EQ_LITERAL.finditer(sql):
        if int(match.group(1)) > 6:
            return (
                "batsman_runs is per-ball (0-6). Do not filter innings totals "
                "with batsman_runs = N where N > 6; aggregate by innings and use "
                "HAVING SUM(batsman_runs) = N."
            )

    # Range comparisons above 6 on ball-level runs are also invalid patterns.
    for match in _BATSMAN_RUNS_GT_LITERAL.finditer(sql):
        op = match.group(1)
        literal = int(match.group(2))
        if (op in (">", ">=") and literal > 6) or (op in ("<", "<=") and literal > 6):
            return (
                "batsman_runs comparisons use per-ball runs (0-6). For innings "
                "milestones, aggregate at (match_id, inning, batsman) first."
            )

    return None


async def _run_sql(execute_query: QuerySQLDataBaseTool, sql: str) -> str:
    """
    Execute one or more SQL statements separated by semicolons.
    psycopg2 does not support multiple statements in a single execute() call,
    so we split on ';', run each non-empty statement, and join the results.

    SQL-line comments (-- ...) are stripped before splitting so they don't
    accidentally appear as empty statements.
    """
    # Remove single-line comments
    cleaned = re.sub(r"--[^\n]*", "", sql)

    # Split on semicolons; keep only non-empty statements
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]

    if len(statements) == 1:
        return await execute_query.ainvoke(statements[0])

    results = []
    for stmt in statements:
        res = await execute_query.ainvoke(stmt)
        results.append(res)
    return "\n".join(results)
