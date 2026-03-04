"""
NL2SQL Agent — orchestrator (Step 6: Conversation Memory).

This module is the single entry point called by routes/query.py.
It wires together the components defined in the helper modules:

  sql_helpers.py      — SQL parsing and execution utilities
  prompts.py          — IPL few-shot examples + prompt template
  table_selector.py   — CSV-backed plain-English table descriptions

Pipeline
--------
  User question
      │
      ▼
  _select_table chain      ← LLM picks which tables are relevant (table_selector)
      │
      ▼
  _generate_query chain    ← LLM turns NL into SQL with dynamic few-shot examples
                             and per-thread conversation history (prompts)
      │
      ▼
  _clean_sql()             ← strips markdown, prose, prefixes (sql_helpers)
      │
      ▼
  _run_sql()               ← executes SQL, handles multi-statement (sql_helpers)
      │
      ▼
  _rephrase_answer chain   ← (question + SQL + result) → readable sentence
      │
      ▼
  {"answer": <sentence>, "sql": <clean SQL>}
  + history updated        ← turn appended to per-thread ChatMessageHistory
"""

import logging
from typing import List

from langchain.chains import create_sql_query_chain
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.prompts import _build_few_shot_prompt
from app.sql_helpers import _clean_sql, _is_sql_error, _run_sql
from app.table_selector import get_table_details, get_table_names

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Lazy singletons — initialised on the first request so that a missing DB
# or bad API key surfaces as a clear runtime error, not a startup crash.
# TODO: Move to module-level once the environment is stable in production.
# ---------------------------------------------------------------------------
_db: SQLDatabase | None = None
_llm: ChatOpenAI | None = None
_generate_query = None
_execute_query: QuerySQLDataBaseTool | None = None
_rephrase_answer = None
_select_table = None  # chain: {"question": str} → List[str] of relevant table names

# Per-thread conversation history (in-memory, lost on restart).
# Keyed by thread_id so each browser session has its own message history.
_conversation_histories: dict[str, ChatMessageHistory] = {}

# Maximum number of LLM-driven correction attempts after a SQL execution error.
_MAX_SQL_RETRIES = 2


async def _fix_sql(
    bad_sql: str,
    question: str,
    error: str,
    table_names: List[str],
) -> str:
    """
    Ask the LLM to correct a SQL query that failed at execution time.

    Feeds the original question, the failing SQL, the database error, and the
    relevant schema back to the LLM so it can produce a corrected query.
    Called by run_agent() inside the retry loop when _run_sql() returns an error.

    Args:
        bad_sql:     The SQL string that caused the error.
        question:    The original natural-language question.
        error:       The error string returned by QuerySQLDataBaseTool.
        table_names: Tables selected for this query (used to filter schema).

    Returns:
        Cleaned SQL string ready to pass to _run_sql().
    """
    schema = (
        _db.get_table_info(table_names=table_names)
        if table_names
        else _db.get_table_info()
    )
    fix_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a PostgreSQL expert. A SQL query failed with an error. "
                    "Correct only the broken part and return valid PostgreSQL.\n\n"
                    "Common mistakes to watch for:\n"
                    "- CTE alias references: if a CTE is aliased as `b`, use `b.col` not `bp.col`\n"
                    "- Column names that don't exist in the schema\n"
                    "- Expressions in ORDER BY that reference SELECT-clause aliases\n\n"
                    f"Relevant table schema:\n{schema}"
                ),
            ),
            (
                "human",
                (
                    f"Original question: {question}\n\n"
                    f"Failing SQL:\n{bad_sql}\n\n"
                    f"Error:\n{error}\n\n"
                    "Return ONLY the corrected SQL. No explanation, no markdown fences."
                ),
            ),
        ]
    )
    raw = await (fix_prompt | _llm | StrOutputParser()).ainvoke({})
    return _clean_sql(raw)


def _get_chain():
    """Return (generate_query, execute_query, rephrase_answer, select_table), initialising them once."""
    global _db, _llm, _generate_query, _execute_query, _rephrase_answer, _select_table

    if _generate_query is not None:
        return _generate_query, _execute_query, _rephrase_answer, _select_table

    # --- Database connection ---
    # sample_rows_in_table_info: sends N real rows per table in the prompt so
    # the LLM can see actual column values and infer data types / naming.
    # TODO: Add include_tables=[...] to restrict which tables are visible,
    #       e.g. include_tables=["matches", "deliveries"] for the IPL dataset.
    _db = SQLDatabase.from_uri(
        settings.database_url,
        sample_rows_in_table_info=3,
    )
    logger.info("Connected to database | dialect=%s | tables=%s",
                _db.dialect, _db.get_usable_table_names())

    # --- LLM ---
    # gpt-4o has significantly better SQL reasoning than gpt-3.5-turbo:
    # correctly handles aliases in ORDER BY, subqueries, window functions, etc.
    _llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        api_key=settings.openai_api_key,
    )
    llm = _llm  # local alias for readability within this function

    # --- Chain: natural language → SQL string (with few-shot examples) ---
    _generate_query = create_sql_query_chain(llm, _db, prompt=_build_few_shot_prompt())

    # --- Tool: executes a SQL string and returns the raw DB result ---
    _execute_query = QuerySQLDataBaseTool(db=_db)

    # --- Chain: (question + SQL + raw result) → natural language answer ---
    answer_prompt = PromptTemplate.from_template(
        """Given the following user question, corresponding SQL query, and SQL result, answer the user question.

Question: {question}
SQL Query: {query}
SQL Result: {result}
Answer: """
    )
    _rephrase_answer = answer_prompt | llm | StrOutputParser()

    # --- Chain: question → List[str] of relevant table names ---
    # Reads plain-English table descriptions from the CSV and asks the LLM
    # which tables are needed to answer the question.  Only those tables'
    # schemas are then included in the SQL-generation prompt, keeping it
    # compact and focused regardless of how many tables the database has.
    table_details_prompt = (
        "Return the names of ALL the SQL tables that MIGHT be relevant to the user question. "
        f"The tables are:\n\n{get_table_details()}\n"
        "Remember to include ALL POTENTIALLY RELEVANT tables, even if you're not sure that they're needed."
    )
    _select_table = (
        ChatPromptTemplate.from_messages(
            [
                ("system", table_details_prompt),
                (
                    "human",
                    "Question: {question}\n\n"
                    "Reply with ONLY a comma-separated list of table names. "
                    "No explanation, no punctuation other than commas. "
                    "Example: deliveries,matches",
                ),
            ]
        )
        | llm
        | StrOutputParser()
        | (lambda raw: [t.strip() for t in raw.split(",") if t.strip()])
    )
    logger.info("Table selector chain built | available_tables=%s", get_table_names())

    return _generate_query, _execute_query, _rephrase_answer, _select_table


async def run_agent(question: str, thread_id: str) -> dict[str, str]:
    """
    Execute the NL2SQL pipeline with natural-language rephrasing.

    Pipeline:
        1. select_table    — LLM picks relevant tables from descriptions CSV
        2. generate_query  — NL → raw LLM output (only relevant schemas shown;
                             conversation history injected via MessagesPlaceholder)
        3. _clean_sql()    — extract pure SQL statements
        4. _run_sql()      — execute each statement, combine results
        5. rephrase_answer — (question + SQL + result) → readable sentence

    Args:
        question:  Natural-language question from the user.
        thread_id: Session identifier — used to look up / create the
                   per-thread ChatMessageHistory so follow-up questions
                   can reference earlier answers in the same session.

    Returns:
        {"answer": <natural language sentence>, "sql": <clean SQL>}
    """
    logger.info("run_agent | thread_id=%s | question=%r", thread_id, question)

    generate_query, execute_query, rephrase_answer, select_table = _get_chain()

    # Retrieve (or create) the conversation history for this session.
    # history.messages is [] on the first turn, which is fine — the
    # MessagesPlaceholder simply adds nothing to the prompt.
    if thread_id not in _conversation_histories:
        _conversation_histories[thread_id] = ChatMessageHistory()
        logger.info("New conversation history created | thread_id=%s", thread_id)
    history = _conversation_histories[thread_id]

    # Step 1 — Identify which tables are relevant to this question.
    # The selector reads plain-English table descriptions and asks the LLM to
    # pick only the tables needed, so the SQL-generation prompt is focused.
    # Fallback to all available tables if the selector returns nothing so that
    # SQL generation always has a schema to work with.
    available_tables = set(_db.get_usable_table_names())
    raw_selection: List[str] = await select_table.ainvoke({"question": question})
    # Discard hallucinated names; keep only tables that actually exist in the DB.
    table_names = [t for t in raw_selection if t in available_tables]
    if not table_names:
        table_names = list(available_tables)
        logger.warning("Table selector returned no valid tables; falling back to all: %s", table_names)
    logger.info("Tables selected: %s", table_names)

    # Step 2 — Generate SQL using only the selected tables' schemas.
    # history.messages injects prior (HumanMessage, AIMessage) turns into the
    # MessagesPlaceholder so the model can resolve follow-up references like
    # "and what about in 2017?" or "who was the runner-up?".
    raw: str = await generate_query.ainvoke({
        "question": question,
        "table_names_to_use": table_names,
        "messages": history.messages,
    })
    logger.info("Raw LLM output: %s", raw)

    # Step 3 — Extract clean SQL from whatever the LLM returned.
    sql = _clean_sql(raw)
    logger.info("Cleaned SQL: %s", sql)

    # Step 4 — Execute with automatic error correction on failure.
    # IMPORTANT: QuerySQLDataBaseTool never raises on SQL errors — it returns
    # the psycopg2 exception as a plain string starting with "Error:".
    # We detect that pattern with _is_sql_error() and drive the retry loop on
    # it, rather than relying on try/except which would never trigger.
    sql_to_run = sql
    result: str = ""
    for attempt in range(1 + _MAX_SQL_RETRIES):
        try:
            result = await _run_sql(execute_query, sql_to_run)
        except Exception as exc:
            result = f"Error: {exc}"

        if not _is_sql_error(result):
            sql = sql_to_run  # keep the (possibly corrected) SQL for the response
            break

        if attempt == _MAX_SQL_RETRIES:
            logger.error("SQL correction exhausted %d retries. Last error: %s", _MAX_SQL_RETRIES, result)
            sql = sql_to_run
            break

        logger.warning(
            "SQL execution failed (attempt %d/%d): %s",
            attempt + 1, 1 + _MAX_SQL_RETRIES, result,
        )
        sql_to_run = await _fix_sql(sql_to_run, question, result, table_names)
        logger.info("Corrected SQL (attempt %d): %s", attempt + 2, sql_to_run)
    logger.info("Query result: %s", result)

    # Step 5 — Rephrase the raw DB result into a natural language answer.
    # Guard: if the query ran without error but returned no rows, skip the
    # rephrase chain (it would hallucinate a confusing non-answer) and tell
    # the user directly so they know to refine the question.
    if _is_sql_error(result):
        answer = f"The query could not be executed after {_MAX_SQL_RETRIES} correction attempts. Last error: {result}"
        logger.warning("Returning error answer | thread_id=%s", thread_id)
        history.add_user_message(question)
        history.add_ai_message(answer)
        return {"answer": answer, "sql": sql}

    if not result or not result.strip():
        answer = (
            "The query ran successfully but returned no results. "
            "The database may not contain data matching that question — "
            "try rephrasing or ask a related question."
        )
        logger.warning("Empty query result | thread_id=%s | sql=%s", thread_id, sql)
        history.add_user_message(question)
        history.add_ai_message(answer)
        return {"answer": answer, "sql": sql}

    answer: str = await rephrase_answer.ainvoke({
        "question": question,
        "query": sql,
        "result": result,
    })
    logger.info("Rephrased answer: %s", answer)

    # Update conversation history so the next turn in this session can
    # reference what was asked and answered here.
    history.add_user_message(question)
    history.add_ai_message(answer)
    logger.info(
        "History updated | thread_id=%s | turns=%d",
        thread_id,
        len(history.messages) // 2,
    )

    return {"answer": answer, "sql": sql}
