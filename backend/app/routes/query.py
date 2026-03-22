from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from typing import Any
import uuid
import logging

import asyncio
from openai import RateLimitError

from app.agent import run_agent, LLMCircuitOpenError
from app.config import get_settings
from app.input_validator import validate_question
from app.limiter import limiter

# Maximum time (seconds) a single /api/query request is allowed to run.
_REQUEST_TIMEOUT = 60

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


# ---------------------------------------------------------------------------
# API key authentication — Phase 16
# ---------------------------------------------------------------------------

def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    FastAPI dependency that enforces API key auth on protected endpoints.

    Behaviour:
      - API_KEY not set in .env  → auth disabled, all requests pass through
      - API_KEY set, header missing  → HTTP 401
      - API_KEY set, header wrong    → HTTP 403
      - API_KEY set, header matches  → pass through

    HTTP 401 signals "you need to authenticate"; 403 signals "wrong credentials".
    The /health endpoint is intentionally excluded (no Depends here).
    """
    if settings.api_key is None:
        return  # auth disabled in dev / when API_KEY is not configured
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Include X-API-Key header.",
        )
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500, description="Natural-language question (max 500 chars)")
    thread_id: str = Field(..., min_length=1, max_length=128, description="Session thread identifier (UUID v4)")

    @field_validator("thread_id")
    @classmethod
    def thread_id_must_be_uuid4(cls, v: str) -> str:
        """Reject non-UUID thread_id values to prevent Redis key injection.

        Without this guard a malicious client could pass e.g. 'schema_hash' as
        thread_id, causing run_agent() to write conversation history under the
        key nl2sql:schema_hash — overwriting the schema drift baseline used by
        schema_watcher.py and corrupting startup checks.
        """
        try:
            parsed = uuid.UUID(v, version=4)
        except ValueError:
            raise ValueError("thread_id must be a valid UUID v4")
        # uuid.UUID normalises the string; if the input was a UUID v4 the
        # canonical form will round-trip correctly.
        if str(parsed) != v.lower():
            raise ValueError("thread_id must be a valid UUID v4")
        return v


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
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def query_endpoint(
    request: Request,
    body: QueryRequest,
    _: None = Depends(verify_api_key),
) -> QueryResponse:
    """
    POST /api/query

    Rate limited to RATE_LIMIT_PER_MINUTE requests per IP per minute (default: 20).
    Exceeding the limit returns HTTP 429. The limit is enforced via Redis so it
    is consistent across multiple backend replicas.

    TODO: The actual agent logic lives in ``app/agent.py``.
          Extend ``run_agent`` there to plug in the LangGraph implementation.
    """
    logger.info("POST /api/query | thread_id=%s | ip=%s", body.thread_id, request.client.host if request.client else "unknown")

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
    except LLMCircuitOpenError as exc:
        logger.warning("LLM circuit open | thread_id=%s: %s", body.thread_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
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
