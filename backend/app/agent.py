"""
NL2SQL Agent — Step 6: Conversation Memory.

Pipeline
--------
  User question
      │
      ▼
  select_table chain       ← LLM picks which tables are relevant to the question
      │
      ▼
  create_sql_query_chain   ← LLM turns NL into SQL (only relevant tables in schema;
                             previous conversation turns injected via MessagesPlaceholder)
      │
      ▼
  _clean_sql()             ← strips markdown, prose, prefixes
      │
      ▼
  QuerySQLDataBaseTool     ← executes SQL (each statement separately if multiple)
      │
      ▼
  rephrase_answer chain    ← LLM converts raw result into a sentence
      │
      ▼
  {"answer": <natural language sentence>, "sql": <generated SQL>}
  + history updated        ← question + answer appended to per-thread ChatMessageHistory

Adaptations from the tutorial
------------------------------
  Tutorial                         This app
  ──────────────────────────────── ────────────────────────────────────────
  os.environ["OPENAI_API_KEY"]     settings.openai_api_key  (from .env)
  mysql+pymysql://...              settings.database_url    (postgresql+psycopg2)
  single global ChatMessageHistory per-thread dict keyed by thread_id
  chain.invoke(...)                3 explicit ainvoke() calls — lets us
                                   capture sql + answer separately for the
                                   API response shape {answer, sql}
  synchronous .invoke()            async .ainvoke()         (FastAPI async)
  module-level db init             lazy init on first call  (survives hot-reload)
  generic examples                 IPL-specific examples    (batsman, bowler, etc.)
  static examples in prompt        dynamic selection via SemanticSimilarityExampleSelector
                                   + ChromaDB + OpenAI Embeddings
  all table schemas in prompt      dynamic table selection via extraction chain
                                   + database_table_descriptions.csv
"""

import logging
import re
from pathlib import Path
from typing import List

import pandas as pd

from langchain.chains import create_sql_query_chain
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.vectorstores import Chroma
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
from langchain_core.output_parsers import StrOutputParser
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.prompts import (
    ChatPromptTemplate,
    FewShotChatMessagePromptTemplate,
    MessagesPlaceholder,
    PromptTemplate,
)
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Dynamic table selection — helpers and Pydantic model
# ---------------------------------------------------------------------------

# Path to the CSV that describes each table in plain English.
# Mounted into the container at /app/app/ via the backend volume.
_TABLE_DESCRIPTIONS_CSV = Path(__file__).parent / "database_table_descriptions.csv"


def get_table_details() -> str:
    """
    Read database_table_descriptions.csv and return a formatted string
    listing every table name and its plain-English description.
    This text is embedded in the table-selection prompt so the LLM can
    decide which tables are relevant without seeing the full schema.
    """
    df = pd.read_csv(_TABLE_DESCRIPTIONS_CSV)
    details = ""
    for _, row in df.iterrows():
        details += f"Table Name: {row['Table']}\nTable Description: {row['Description']}\n\n"
    return details



# ---------------------------------------------------------------------------
# Few-shot examples — IPL-specific question/SQL pairs that steer the LLM
# toward correct column names and PostgreSQL idioms for this dataset.
# Add more examples here to cover query patterns that the model gets wrong.
# ---------------------------------------------------------------------------

IPL_EXAMPLES = [
    {
        "input": "How many runs did Virat Kohli score in total?",
        "query": (
            "SELECT SUM(batsman_runs) AS total_runs "
            "FROM deliveries "
            "WHERE batsman = 'V Kohli';"
        ),
    },
    {
        "input": "Who are the top 5 highest run-scorers across all seasons?",
        "query": (
            "SELECT batsman, SUM(batsman_runs) AS total_runs "
            "FROM deliveries "
            "GROUP BY batsman "
            "ORDER BY total_runs DESC "
            "LIMIT 5;"
        ),
    },
    {
        "input": "Which bowlers have taken the most wickets?",
        "query": (
            "SELECT bowler, COUNT(*) AS total_wickets "
            "FROM deliveries "
            "WHERE dismissal_kind NOT IN ('run out', 'retired hurt', 'obstructing the field') "
            "  AND player_dismissed IS NOT NULL "
            "GROUP BY bowler "
            "ORDER BY total_wickets DESC "
            "LIMIT 10;"
        ),
    },
    {
        "input": "Which team has won the most IPL titles?",
        "query": (
            "SELECT winner, COUNT(*) AS titles "
            "FROM matches "
            "WHERE match_type = 'Final' "
            "GROUP BY winner "
            "ORDER BY titles DESC "
            "LIMIT 5;"
        ),
    },
    {
        "input": "Who has won the Player of the Match award the most times?",
        "query": (
            "SELECT player_of_match, COUNT(*) AS awards "
            "FROM matches "
            "WHERE player_of_match IS NOT NULL "
            "GROUP BY player_of_match "
            "ORDER BY awards DESC "
            "LIMIT 10;"
        ),
    },
    {
        "input": "How many sixes were hit in the 2016 season?",
        "query": (
            "SELECT COUNT(*) AS total_sixes "
            "FROM deliveries d "
            "JOIN matches m ON d.match_id = m.id "
            "WHERE m.season = 2016 "
            "  AND d.batsman_runs = 6;"
        ),
    },
    {
        "input": "What is the highest individual score in a single match?",
        "query": (
            "SELECT batsman, match_id, SUM(batsman_runs) AS runs_in_match "
            "FROM deliveries "
            "GROUP BY batsman, match_id "
            "ORDER BY runs_in_match DESC "
            "LIMIT 1;"
        ),
    },
    {
        "input": "Which venue has hosted the most matches?",
        "query": (
            "SELECT venue, COUNT(*) AS matches_hosted "
            "FROM matches "
            "GROUP BY venue "
            "ORDER BY matches_hosted DESC "
            "LIMIT 5;"
        ),
    },
]


def _build_few_shot_prompt() -> ChatPromptTemplate:
    """
    Assemble a ChatPromptTemplate with DYNAMIC few-shot example selection.

    Instead of sending all IPL_EXAMPLES on every request, a
    SemanticSimilarityExampleSelector embeds the user's question at call-time
    and retrieves the k=3 most semantically similar examples from a ChromaDB
    vector store.  This keeps the prompt compact and ensures the examples
    shown to the model are always the most relevant ones for the current query.

    Prompt structure
    ----------------
      [system]   Role + schema (table_info) + row limit (top_k)
      [human]    dynamically chosen example question
      [ai]       example SQL
      …          (k=3 examples, selected per query)
      [human]    actual user question
    """
    # Template for a single example turn (question → SQL)
    example_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", "{input}\nSQLQuery:"),
            ("ai", "{query}"),
        ]
    )

    # Embed all IPL_EXAMPLES into an in-memory Chroma vector store.
    # At query time the selector computes cosine similarity between the
    # incoming question embedding and each stored example, then returns the
    # k closest matches.  The vector store is rebuilt fresh each startup
    # (no persistence needed for this small example set).
    example_selector = SemanticSimilarityExampleSelector.from_examples(
        IPL_EXAMPLES,
        OpenAIEmbeddings(api_key=settings.openai_api_key),
        Chroma,
        k=3,
        input_keys=["input"],
    )
    logger.info("Dynamic example selector built | examples=%d | k=3", len(IPL_EXAMPLES))

    # Dynamic few-shot block: examples are chosen at call-time via the selector
    few_shot_prompt = FewShotChatMessagePromptTemplate(
        example_prompt=example_prompt,
        example_selector=example_selector,
        input_variables=["input", "top_k"],
    )

    # Full prompt: system context → dynamic few-shot block → conversation
    # history → user question.
    # MessagesPlaceholder injects the prior turns (HumanMessage / AIMessage
    # pairs) so the model can resolve follow-up questions like "and in 2017?".
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a PostgreSQL expert for an IPL (Indian Premier League) cricket "
                    "database. Given an input question, write a syntactically correct "
                    "PostgreSQL query to answer it. Unless the user specifies a different "
                    "number of results, limit your query to at most {top_k} rows using "
                    "LIMIT.\n\n"
                    "Only query columns that exist in the schema below. Pay attention to "
                    "which table each column belongs to. Wrap column and table names in "
                    "double quotes only when they are reserved words.\n\n"
                    "Relevant table schema:\n{table_info}\n\n"
                    "Here are the most relevant example questions and their SQL queries:"
                ),
            ),
            few_shot_prompt,
            MessagesPlaceholder(variable_name="messages"),
            ("human", "{input}\nSQLQuery:"),
        ]
    )


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


# ---------------------------------------------------------------------------
# SQL extraction helpers
# ---------------------------------------------------------------------------

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
    Called by run_agent() inside the retry loop when _run_sql() raises.

    Args:
        bad_sql:     The SQL string that caused the error.
        question:    The original natural-language question.
        error:       The exception message from psycopg2 / SQLDatabase.
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
    logger.info(
        "Table selector chain built | available_tables=%s",
        pd.read_csv(_TABLE_DESCRIPTIONS_CSV)["Table"].tolist(),
    )

    return _generate_query, _execute_query, _rephrase_answer, _select_table


async def run_agent(question: str, thread_id: str) -> dict[str, str]:
    """
    Execute the NL2SQL pipeline with natural-language rephrasing.

    Pipeline:
        1. select_table    — LLM picks relevant tables from descriptions CSV
        2. generate_query  — NL → raw LLM output (only relevant schemas shown)
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
    # TODO: In Step 6, this answer will come from the LangGraph agent's final
    #       message instead, removing the need for a separate rephrase chain.
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
