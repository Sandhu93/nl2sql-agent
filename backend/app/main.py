import logging
import logging.config
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.limiter import limiter
from app.routes.query import router as query_router
from app.schema_watcher import run_schema_watcher

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
settings = get_settings()

# ---------------------------------------------------------------------------
# Lifespan — Phase 14: startup tasks (schema drift check)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    Startup tasks run before the first request is served; teardown tasks
    (after `yield`) run when the server shuts down.

    Current startup tasks:
      - Schema drift detection: hashes information_schema.columns for the 9
        known IPL tables, compares against Redis baseline, logs WARNING on
        drift. Also logs data coverage stats (max_year, match/delivery counts).

    TODO: Add retrieval-eval warm-up ping here when Phase 14b is complete.
    TODO: Add schema_watcher Prometheus counter export here (Phase 14b).
    """
    await run_schema_watcher()
    yield
    # TODO: Add graceful shutdown logic here (e.g. flush metrics) as needed.


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NL2SQL Agent API",
    description="Natural-language to SQL agent powered by LangGraph.",
    version="0.1.0",
    lifespan=lifespan,
    # Disable automatic docs in production if needed — leave enabled for dev.
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Rate limiting — Phase 10 (production hardening)
# slowapi requires the limiter on app.state so the @limiter.limit() decorator
# can find it at request time.  The SlowAPIMiddleware intercepts the request
# before it reaches the route handler and enforces the per-IP counter.
# ---------------------------------------------------------------------------
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# ---------------------------------------------------------------------------
# CORS — restricted to the frontend origin only
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

# ---------------------------------------------------------------------------
# Global error handler — never leak tracebacks to the client
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected error occurred. Please try again later."},
    )


# ---------------------------------------------------------------------------
# Rate limit exceeded handler — Phase 10
# Returns {"detail": "..."} consistent with our other 429 responses
# (e.g. OpenAI RateLimitError in routes/query.py).
# slowapi's built-in handler returns {"error": "..."} — we override it
# so the frontend only needs to handle one error shape.
# ---------------------------------------------------------------------------
@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning(
        "Rate limit exceeded | ip=%s | limit=%s | path=%s",
        request.client.host if request.client else "unknown",
        exc.limit,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": (
                f"Too many requests — you are limited to {exc.limit} on this endpoint. "
                "Please wait a moment and try again."
            )
        },
    )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get(
    "/health",
    summary="Health check",
    tags=["system"],
)
async def health() -> dict[str, str]:
    """Returns 200 OK when the service is up."""
    return {"status": "ok"}


# TODO: Register additional routers here as you expand the API.
app.include_router(query_router, prefix="/api", tags=["query"])

# ---------------------------------------------------------------------------
# Entry point (for local dev without Docker)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8086,
        reload=True,
    )
