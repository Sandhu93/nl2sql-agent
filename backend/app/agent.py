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
  query rewrite            ← rewrite follow-ups into standalone questions
      │                      (skipped on first turn when history is empty)
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
  _rephrase_answer chain   ← (standalone question + SQL + result) → readable sentence
      │
      ▼
  {"answer": <sentence>, "sql": <clean SQL>}
  + history updated        ← original question + answer stored per-thread
"""

import asyncio
import logging
from typing import List

from langchain.chains import create_sql_query_chain
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, PromptTemplate
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.cricket_knowledge import retrieve_cricket_rules
from app.entity_resolver import resolve_player_mentions
from app.insights_agent import generate_insights
from app.prompts import _build_few_shot_prompt
from app.sql_helpers import _clean_sql, _is_sql_error, _run_sql, validate_sql, detect_semantic_sql_issue
from app.table_selector import get_table_details, get_table_names
from app.viz_agent import generate_chart_spec, wants_visualization

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_llm_with_fallbacks() -> ChatOpenAI:
    """
    Build the primary LLM (GPT-4o) and attach any configured fallback providers.

    Fallbacks are tried in order when the primary raises an exception (e.g. rate
    limit, network error, quota exceeded).  Each provider is only added when its
    API key / URL is present in settings AND its package is installed — a missing
    package logs a warning and is skipped rather than crashing the app.

    Fallback order (when all configured):
        1. Anthropic Claude  — strong SQL reasoning, reliable API
        2. Google Gemini     — good general-purpose fallback
        3. DeepSeek          — cheap, OpenAI-compatible API
        4. Ollama            — local, no cost, quality depends on model size
    """
    primary = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        api_key=settings.openai_api_key,
    )

    fallbacks = []

    if settings.anthropic_api_key:
        try:
            from langchain_anthropic import ChatAnthropic  # pip install langchain-anthropic
            fallbacks.append(
                ChatAnthropic(
                    model="claude-3-5-sonnet-20241022",
                    temperature=0,
                    api_key=settings.anthropic_api_key,
                )
            )
            logger.info("Fallback LLM registered: Anthropic Claude")
        except ImportError:
            logger.warning(
                "ANTHROPIC_API_KEY is set but langchain-anthropic is not installed. "
                "Run: pip install langchain-anthropic"
            )

    if settings.google_api_key:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI  # pip install langchain-google-genai
            fallbacks.append(
                ChatGoogleGenerativeAI(
                    model="gemini-2.0-flash",
                    temperature=0,
                    google_api_key=settings.google_api_key,
                    max_retries=2,
                )
            )
            logger.info("Fallback LLM registered: Google Gemini")
        except ImportError:
            logger.warning(
                "GOOGLE_API_KEY is set but langchain-google-genai is not installed. "
                "Run: pip install langchain-google-genai"
            )

    if settings.deepseek_api_key:
        # DeepSeek uses an OpenAI-compatible API — no extra package needed.
        fallbacks.append(
            ChatOpenAI(
                model="deepseek-chat",
                temperature=0,
                api_key=settings.deepseek_api_key,
                base_url="https://api.deepseek.com/v1",
            )
        )
        logger.info("Fallback LLM registered: DeepSeek")

    if settings.ollama_base_url:
        try:
            from langchain_ollama import ChatOllama  # pip install langchain-ollama
            fallbacks.append(
                ChatOllama(
                    model=settings.ollama_model,
                    base_url=settings.ollama_base_url,
                )
            )
            logger.info("Fallback LLM registered: Ollama (%s)", settings.ollama_model)
        except ImportError:
            logger.warning(
                "OLLAMA_BASE_URL is set but langchain-ollama is not installed. "
                "Run: pip install langchain-ollama"
            )

    if not fallbacks:
        logger.info("No fallback LLMs configured — using GPT-4o only")
        return primary

    logger.info("LLM fallback chain: GPT-4o → %d fallback(s) active", len(fallbacks))
    return primary.with_fallbacks(fallbacks)

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
_rewrite_query = None  # chain: {"history": list, "question": str} → standalone question str

# Per-thread conversation history (in-memory, lost on restart).
# Keyed by thread_id so each browser session has its own message history.
_conversation_histories: dict[str, ChatMessageHistory] = {}
# Per-thread recent insight chips for cross-turn dedupe.
_recent_follow_up_chips: dict[str, list[str]] = {}

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
                    "- `batsman_runs` is per-ball (0-6), never innings totals; for "
                    "filters like 50/100/119, aggregate by innings and use HAVING SUM(...)\n\n"
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
    """Return (generate_query, execute_query, rephrase_answer, select_table, rewrite_query), initialising them once."""
    global _db, _llm, _generate_query, _execute_query, _rephrase_answer, _select_table, _rewrite_query

    if _generate_query is not None:
        return _generate_query, _execute_query, _rephrase_answer, _select_table, _rewrite_query

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

    # --- LLM (primary + fallbacks) ---
    # GPT-4o is primary. Any configured fallback providers are chained via
    # .with_fallbacks() so the agent retries them automatically on API errors.
    _llm = _build_llm_with_fallbacks()
    llm = _llm  # local alias for readability within this function

    # --- Chain: natural language → SQL string (with few-shot examples) ---
    _generate_query = create_sql_query_chain(llm, _db, prompt=_build_few_shot_prompt())

    # --- Tool: executes a SQL string and returns the raw DB result ---
    _execute_query = QuerySQLDataBaseTool(db=_db)

    # --- Chain: (question + SQL + raw result) → natural language answer ---
    answer_prompt = PromptTemplate.from_template(
        """You are given a user question, the SQL query that was run, and the SQL result rows.
Your job is to write a clear, concise natural-language answer based ONLY on the SQL result.

RULES:
1. Present the data from the SQL result as the answer — do NOT question whether the query is correct.
2. Do NOT critique, analyse, or explain the SQL.
3. Do NOT say the answer "cannot be determined" if data is present — use what the result gives you.
4. If the result is a list of rows, present them clearly (e.g. as bullet points or a ranked list).

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

    # --- Chain: (history + question) → standalone question string ---
    # Rewrites ambiguous follow-up questions into fully self-contained questions
    # so every downstream step (table selector, SQL generator, fix_sql, rephrase)
    # receives an unambiguous question without needing history awareness itself.
    _rewrite_query = (
        ChatPromptTemplate.from_messages([
            (
                "system",
                "You are a question-rewriting assistant. Your ONLY job is to rewrite "
                "a follow-up question into a standalone question using context from the "
                "conversation history.\n\n"
                "STRICT RULES:\n"
                "1. Output ONLY a question — never a statement, never an answer, never a fact.\n"
                "2. If the question is already self-contained and unambiguous, return it "
                "EXACTLY as written (you may correct minor grammar only).\n"
                "3. Never answer the question — only rewrite it as a standalone question.\n"
                "4. Never add information that is not in the original question.\n"
                "5. Your output must always end with a question mark.\n\n"
                "EXAMPLES:\n"
                "History: 'Which teams won more than 5 matches in 2019?' / 'What about 2020?' "
                "→ 'Which teams won more than 5 matches in 2020?'\n"
                "History: anything / 'who has the most runouts' "
                "→ 'Who has the most runouts in IPL history?'\n"
                "History: anything / 'show me their top scorers' "
                "→ 'Who were the top run scorers for [the subject from history]?'",
            ),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{question}"),
        ])
        | llm
        | StrOutputParser()
    )

    return _generate_query, _execute_query, _rephrase_answer, _select_table, _rewrite_query


async def run_agent(question: str, thread_id: str) -> dict[str, str]:
    """
    Execute the NL2SQL pipeline with natural-language rephrasing.

    Pipeline:
        0. query_rewrite   — rewrite ambiguous follow-ups into standalone questions
                             (skipped on the first turn when history is empty)
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
        {
            "answer":     natural language sentence,
            "sql":        clean SQL string,
            "insights":   {"key_takeaway": str, "follow_up_chips": list[str]},
            "chart_spec": Vega-Lite spec dict or None (only when viz was requested),
        }
    """
    logger.info("run_agent | thread_id=%s | question=%r", thread_id, question)

    generate_query, execute_query, rephrase_answer, select_table, rewrite_query = _get_chain()

    # Retrieve (or create) the conversation history for this session.
    # history.messages is [] on the first turn, which is fine — the
    # MessagesPlaceholder simply adds nothing to the prompt.
    if thread_id not in _conversation_histories:
        _conversation_histories[thread_id] = ChatMessageHistory()
        logger.info("New conversation history created | thread_id=%s", thread_id)
    history = _conversation_histories[thread_id]

    # Step 0 — Rewrite follow-up questions into fully standalone queries.
    # The table-selector (Step 1) has no access to conversation history, so an
    # ambiguous follow-up like "What about 2020?" or "Show me their top scorers"
    # would cause it to pick wrong tables or nothing at all.  By rewriting the
    # question into a self-contained form first, every downstream step receives
    # an unambiguous question without needing to be aware of the history.
    #
    # On the first turn (empty history) we skip the LLM call entirely — there
    # is nothing to resolve and we save one round-trip.
    if history.messages:
        # Keep a wider rewrite window so follow-ups over longer threads still
        # resolve correctly. We avoid full history to reduce drift.
        rewrite_history = history.messages[-8:]
        standalone_question: str = await rewrite_query.ainvoke({
            "history": rewrite_history,
            "question": question,
        })
        # Safety guard: discard the rewrite if the LLM answered the question
        # instead of rewriting it.
        #
        # The "?" check is the reliable signal: hallucinated answers are statements,
        # not questions. A length ratio (e.g. 3×, 5×) is the wrong tool — short
        # follow-ups like "plot" or "you forgot to plot" legitimately expand into
        # full standalone questions that exceed any fixed multiplier.
        # We keep only a generous absolute ceiling (300 chars) to reject the rare
        # case where the LLM emits a multi-sentence paragraph as a single "question".
        _looks_like_answer = (
            not standalone_question.strip().endswith("?")
            or len(standalone_question) > 300
        )
        if _looks_like_answer:
            logger.warning(
                "Query rewrite produced a non-question — falling back to original. "
                "rewrite=%r", standalone_question,
            )
            standalone_question = question
        logger.info(
            "Query rewrite | original=%r | standalone=%r",
            question, standalone_question,
        )
    else:
        standalone_question = question  # first turn — nothing to resolve

    # Step 0b — Resolve entity aliases (e.g. full player names) into dataset
    # names so SQL generation can match the underlying schema reliably.
    resolved_question, player_name_mappings = resolve_player_mentions(standalone_question)
    if player_name_mappings:
        logger.info("Player name mappings applied: %s", player_name_mappings)

    # Steps 1 + 1b — Run table selection and cricket knowledge retrieval in
    # parallel. Both are independent of each other: table selection makes one
    # LLM API call; cricket retrieval makes one embedding API call + in-memory
    # vector search. Running them concurrently saves ~300–500 ms per request.
    #
    # TODO: If the cricket vector store is not yet initialised when the first
    #       request arrives, its cold-start embedding call also runs here,
    #       hidden inside asyncio.gather. That is intentional — the user's first
    #       question absorbs the one-time cost transparently.
    available_tables = set(_db.get_usable_table_names())
    raw_selection, cricket_context = await asyncio.gather(
        select_table.ainvoke({"question": resolved_question}),
        retrieve_cricket_rules(resolved_question, k=3),
    )

    # Step 1 — Validate table selection; fall back to all tables if needed.
    # Discard hallucinated names; keep only tables that actually exist in the DB.
    table_names = [t for t in raw_selection if t in available_tables]
    if not table_names:
        table_names = list(available_tables)
        logger.warning("Table selector returned no valid tables; falling back to all: %s", table_names)
    logger.info("Tables selected: %s", table_names)

    # Step 2 — Generate SQL using the selected tables' schemas + cricket context.
    # {cricket_context} carries the k=3 most relevant sections from
    # cricket_rules.md (e.g. bowling rules, eligibility rules, dismissal logic)
    # so the LLM generates cricket-correct SQL, not just schema-correct SQL.
    # We intentionally do NOT pass full chat history to SQL generation.
    # Follow-up references are already resolved in Step 0 (rewrite), and
    # long message history causes factual drift/hallucination in later turns.
    raw: str = await generate_query.ainvoke({
        "question": resolved_question,
        "table_names_to_use": table_names,
        "messages": [],
        "cricket_context": cricket_context,
    })
    logger.info("Raw LLM output: %s", raw)

    # Step 3 — Extract clean SQL from whatever the LLM returned.
    sql = _clean_sql(raw)
    logger.info("Cleaned SQL: %s", sql)

    # Layer 2 — SQL output validation: block any non-SELECT statement before
    # it reaches the database.  This is a defence-in-depth check — even if
    # prompt injection or a model error causes the LLM to emit a destructive
    # statement, it is stopped here.  Returns a safe answer to the user and
    # skips execution entirely.
    try:
        validate_sql(sql)
    except ValueError as exc:
        answer = (
            "Your question could not be answered because the generated query "
            "was not a read-only SELECT statement. Please rephrase your question."
        )
        logger.warning(
            "SQL validation blocked execution | thread_id=%s | reason=%s | sql=%r",
            thread_id, exc, sql,
        )
        history.add_user_message(question)
        history.add_ai_message(answer)
        return {"answer": answer, "sql": sql, "insights": None, "chart_spec": None}

    # Layer 2b — semantic SQL validation for high-confidence logical errors
    # that are syntactically valid but produce wrong/empty results.
    semantic_issue = detect_semantic_sql_issue(sql)
    semantic_attempts = 0
    while semantic_issue and semantic_attempts < _MAX_SQL_RETRIES:
        logger.warning(
            "Semantic SQL issue detected (attempt %d/%d): %s | sql=%r",
            semantic_attempts + 1,
            _MAX_SQL_RETRIES,
            semantic_issue,
            sql[:200],
        )
        sql = await _fix_sql(
            bad_sql=sql,
            question=resolved_question,
            error=f"Semantic validation error: {semantic_issue}",
            table_names=table_names,
        )
        validate_sql(sql)
        semantic_issue = detect_semantic_sql_issue(sql)
        semantic_attempts += 1

    if semantic_issue:
        answer = (
            "Your question could not be answered because the generated query "
            "had a logical issue in how cricket stats were computed. "
            "Please rephrase your question."
        )
        logger.warning(
            "Semantic SQL validation blocked execution | thread_id=%s | reason=%s | sql=%r",
            thread_id, semantic_issue, sql,
        )
        history.add_user_message(question)
        history.add_ai_message(answer)
        return {"answer": answer, "sql": sql, "insights": None, "chart_spec": None}

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
        sql_to_run = await _fix_sql(sql_to_run, standalone_question, result, table_names)
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
        return {"answer": answer, "sql": sql, "insights": None, "chart_spec": None}

    if not result or not result.strip():
        answer = (
            "The query ran successfully but returned no results. "
            "The database may not contain data matching that question — "
            "try rephrasing or ask a related question."
        )
        logger.warning("Empty query result | thread_id=%s | sql=%s", thread_id, sql)
        history.add_user_message(question)
        history.add_ai_message(answer)
        return {"answer": answer, "sql": sql, "insights": None, "chart_spec": None}

    # Steps 5a + 5b + 5c — run in parallel to hide LLM latency:
    #   5a. rephrase_answer  — convert raw DB rows into a natural language sentence
    #   5b. generate_insights — key takeaway + 3 follow-up question chips (Phase 8)
    #   5c. generate_chart_spec — Vega-Lite spec if user asked for a chart (Phase 9)
    #
    # TODO: If insights or viz add too much latency, gate them behind
    #       ENABLE_INSIGHTS / ENABLE_VIZ config flags in config.py.
    # Check both the original question and the rewritten standalone question because
    # the query rewriter may strip chart-related keywords (e.g. "show me a bar chart"
    # → "Who were the top 10 run scorers?"), causing viz intent to be silently lost.
    viz_requested = wants_visualization(question) or wants_visualization(standalone_question)
    recent_chips = _recent_follow_up_chips.get(thread_id, [])

    async def _maybe_chart() -> dict | None:
        """Run chart spec generation only when the question asks for a viz."""
        if not viz_requested:
            return None
        return await generate_chart_spec(standalone_question, result, _llm)

    answer, insights, chart_spec = await asyncio.gather(
        rephrase_answer.ainvoke({
            "question": standalone_question,
            "query": sql,
            "result": result,
        }),
        generate_insights(standalone_question, result, _llm, recent_chips=recent_chips),
        _maybe_chart(),
    )
    logger.info("Rephrased answer: %s", answer)
    logger.info(
        "Insights generated | key_takeaway=%r | chips=%d",
        insights.get("key_takeaway", "")[:60],
        len(insights.get("follow_up_chips", [])),
    )
    if chart_spec:
        logger.info("Chart spec generated | viz_requested=%s", viz_requested)

    # Update recent chips memory (last 2 turns ~= 6 chips max) for dedupe.
    chips = insights.get("follow_up_chips", []) if isinstance(insights, dict) else []
    merged_recent: list[str] = []
    for chip in [*recent_chips, *chips]:
        if chip and chip not in merged_recent:
            merged_recent.append(chip)
    _recent_follow_up_chips[thread_id] = merged_recent[-6:]

    # Update conversation history so the next turn in this session can
    # reference what was asked and answered here.
    history.add_user_message(question)
    history.add_ai_message(answer)
    logger.info(
        "History updated | thread_id=%s | turns=%d",
        thread_id,
        len(history.messages) // 2,
    )

    return {"answer": answer, "sql": sql, "insights": insights, "chart_spec": chart_spec}
