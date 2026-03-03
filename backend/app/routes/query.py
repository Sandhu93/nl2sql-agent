from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
import logging

from app.agent import run_agent

logger = logging.getLogger(__name__)
router = APIRouter()


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question")
    thread_id: str = Field(..., min_length=1, max_length=128, description="Session thread identifier")


class QueryResponse(BaseModel):
    answer: str
    sql: str


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

    try:
        result = await run_agent(question=body.question, thread_id=body.thread_id)
    except Exception as exc:
        # Log the full exception server-side; return a sanitized message to the client.
        logger.exception("Agent error for thread_id=%s", body.thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing your request.",
        ) from exc

    return QueryResponse(answer=result["answer"], sql=result["sql"])
