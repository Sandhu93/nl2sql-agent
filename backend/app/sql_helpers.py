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
