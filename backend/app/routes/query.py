from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Any
import logging

import asyncio
from openai import RateLimitError

from app.agent import run_agent
from app.input_validator import validate_question

# Maximum time (seconds) a single /api/query request is allowed to run.
_REQUEST_TIMEOUT = 60

logger = logging.getLogger(__name__)
router = APIRouter()


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question")
    thread_id: str = Field(..., min_length=1, max_length=128, description="Session thread identifier")


class QueryResponse(BaseModel):
    answer: str
    sql: str
    # Phase 8 — Insight generation layer (always present on successful queries)
    insights: dict[str, Any] | None = None
    # Phase 9 — Visualization layer (only present when user asks for a chart)
    # TODO: When the MCP chart server is wired up, this field will be populated
    #       by the MCP tool response rather than the LLM viz_agent.
    chart_spec: dict[str, Any] | None = None


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Run NL2SQL agent",
    description="Accepts a natural-language question and returns an answer with the generated SQL.",
)
async def query_endpoint(body: QueryRequest) -> QueryResponse:
    """
    POST /api/query

    TODO: The actual agent logic lives in ``app/agent.py``.
          Extend ``run_agent`` there to plug in the LangGraph implementation.
    """
    logger.info("POST /api/query | thread_id=%s", body.thread_id)

    # Layer 1 — Input validation: sanitize and check for prompt injection
    # before the question reaches the LLM.  Returns a 400 so the client
    # knows it sent a bad request (not a server error).
    try:
        question = validate_question(body.question)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        result = await asyncio.wait_for(
            run_agent(question=question, thread_id=body.thread_id),
            timeout=_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("Request timed out after %ds for thread_id=%s", _REQUEST_TIMEOUT, body.thread_id)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Request timed out after {_REQUEST_TIMEOUT}s. The LLM provider may be overloaded.",
        )
    except RateLimitError as exc:
        logger.warning("Rate limit hit for thread_id=%s: %s", body.thread_id, exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="LLM rate limit exceeded. Please wait a moment and try again.",
        ) from exc
    except Exception as exc:
        # Log the full exception server-side; return a sanitized message to the client.
        logger.exception("Agent error for thread_id=%s", body.thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing your request.",
        ) from exc

    return QueryResponse(
        answer=result["answer"],
        sql=result["sql"],
        insights=result.get("insights"),
        chart_spec=result.get("chart_spec"),
    )
