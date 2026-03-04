"""
Prompt templates and IPL few-shot examples for SQL generation.

IPL_EXAMPLES  — 8 hand-crafted (question, SQL) pairs that cover the most
                common query patterns for the IPL dataset.

_build_few_shot_prompt()  — assembles the full ChatPromptTemplate used by
                create_sql_query_chain, with dynamic example selection via
                SemanticSimilarityExampleSelector + ChromaDB.
"""

import logging

from langchain_community.vectorstores import Chroma
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
from langchain_core.prompts import (
    ChatPromptTemplate,
    FewShotChatMessagePromptTemplate,
    MessagesPlaceholder,
)
from langchain_openai import OpenAIEmbeddings

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
      [human/ai] conversation history (MessagesPlaceholder)
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
