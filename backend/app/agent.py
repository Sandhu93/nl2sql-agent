"""
NL2SQL Agent — Step 3: Enhancing NL2SQL Models with Few-Shot Examples.

Pipeline
--------
  User question
      │
      ▼
  create_sql_query_chain   ← LLM turns NL into SQL (guided by few-shot examples)
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

Adaptations from the tutorial
------------------------------
  Tutorial                         This app
  ──────────────────────────────── ────────────────────────────────────────
  os.environ["OPENAI_API_KEY"]     settings.openai_api_key  (from .env)
  mysql+pymysql://...              settings.database_url    (postgresql+psycopg2)
  chain.invoke(...)                3 explicit ainvoke() calls — lets us
                                   capture sql + answer separately for the
                                   API response shape {answer, sql}
  synchronous .invoke()            async .ainvoke()         (FastAPI async)
  module-level db init             lazy init on first call  (survives hot-reload)
  generic examples                 IPL-specific examples    (batsman, bowler, etc.)

TODO (next tutorial steps)
--------------------------
  Step 4 – Replace with LangGraph create_react_agent + MemorySaver for
           multi-turn conversation history (thread_id will be used there).
"""

import logging
import re

from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import (
    ChatPromptTemplate,
    FewShotChatMessagePromptTemplate,
    PromptTemplate,
)
from langchain.chains import create_sql_query_chain
from langchain_openai import ChatOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

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
    Assemble the ChatPromptTemplate that wraps few-shot examples around the
    user's question.  This is passed to create_sql_query_chain() so the LLM
    sees concrete IPL examples before it writes any SQL.

    Prompt structure
    ----------------
      [system]   Role + schema (table_info) + row limit (top_k)
      [human]    example question  ← repeated for every example in IPL_EXAMPLES
      [ai]       example SQL
      …
      [human]    actual user question
    """
    # Template for a single example turn (question → SQL)
    example_prompt = ChatPromptTemplate.from_messages(
        [
            ("human", "{input}\nSQLQuery:"),
            ("ai", "{query}"),
        ]
    )

    # Expands every entry in IPL_EXAMPLES into (human, ai) message pairs
    few_shot_prompt = FewShotChatMessagePromptTemplate(
        example_prompt=example_prompt,
        examples=IPL_EXAMPLES,
        input_variables=["input"],
    )

    # Full prompt: system context → few-shot block → user question
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a PostgreSQL expert for an IPL (Indian Premier League) cricket "
                    "database.  Given an input question, write a syntactically correct "
                    "PostgreSQL query to answer it.  Unless the user specifies a different "
                    "number of results, limit your query to at most {top_k} rows using "
                    "LIMIT.\n\n"
                    "Only query columns that exist in the schema below.  Pay attention to "
                    "which table each column belongs to.  Wrap column and table names in "
                    "double quotes only when they are reserved words.\n\n"
                    "Relevant table schema:\n{table_info}\n\n"
                    "Here are some example questions with their correct SQL queries:"
                ),
            ),
            few_shot_prompt,
            ("human", "{input}\nSQLQuery:"),
        ]
    )


# ---------------------------------------------------------------------------
# Lazy singletons — initialised on the first request so that a missing DB
# or bad API key surfaces as a clear runtime error, not a startup crash.
# TODO: Move to module-level once the environment is stable in production.
# ---------------------------------------------------------------------------
_db: SQLDatabase | None = None
_generate_query = None
_execute_query: QuerySQLDataBaseTool | None = None
_rephrase_answer = None


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


def _get_chain():
    """Return (generate_query, execute_query, rephrase_answer), initialising them once."""
    global _db, _generate_query, _execute_query, _rephrase_answer

    if _generate_query is not None:
        return _generate_query, _execute_query, _rephrase_answer

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
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        api_key=settings.openai_api_key,
    )

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

    return _generate_query, _execute_query, _rephrase_answer


async def run_agent(question: str, thread_id: str) -> dict[str, str]:
    """
    Execute the NL2SQL pipeline with natural-language rephrasing.

    Pipeline:
        1. generate_query  — NL → raw LLM output (may contain prose/markdown)
        2. _clean_sql()    — extract pure SQL statements
        3. _run_sql()      — execute each statement, combine results
        4. rephrase_answer — (question + SQL + result) → readable sentence

    Args:
        question:  Natural-language question from the user.
        thread_id: Session identifier — unused in the basic model.
                   Will drive LangGraph's per-thread memory in Step 4.

    Returns:
        {"answer": <natural language sentence>, "sql": <clean SQL>}
    """
    logger.info("run_agent | thread_id=%s | question=%r", thread_id, question)

    generate_query, execute_query, rephrase_answer = _get_chain()

    # Step 1 — Generate SQL (raw LLM output — may include markdown / prose).
    raw: str = await generate_query.ainvoke({"question": question})
    logger.info("Raw LLM output: %s", raw)

    # Step 2 — Extract clean SQL from whatever the LLM returned.
    sql = _clean_sql(raw)
    logger.info("Cleaned SQL: %s", sql)

    # Step 3 — Execute (handles multiple semicolon-separated statements).
    result: str = await _run_sql(execute_query, sql)
    logger.info("Query result: %s", result)

    # Step 4 — Rephrase the raw DB result into a natural language answer.
    # TODO: In Step 4, this answer will come from the LangGraph agent's final
    #       message instead, removing the need for a separate rephrase chain.
    answer: str = await rephrase_answer.ainvoke({
        "question": question,
        "query": sql,
        "result": result,
    })
    logger.info("Rephrased answer: %s", answer)

    return {"answer": answer, "sql": sql}
