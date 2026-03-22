from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from functools import lru_cache

_VALID_EMBEDDING_MODELS = {
    "text-embedding-3-small",
    "text-embedding-3-large",
    "text-embedding-ada-002",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # OpenAI — primary LLM (required)
    openai_api_key: str = Field(..., description="OpenAI API key")

    # Model routing — Phase 12 (latency optimisation)
    # SQL generation and SQL fixing need full GPT-4o reasoning.
    # Lighter tasks (query rewrite, table selection, answer rephrase, insights,
    # chart intent) can use GPT-4o-mini: ~3-5× faster, ~15× cheaper.
    openai_model: str = Field(
        default="gpt-4o",
        description="OpenAI model for SQL generation and SQL fixing (accuracy-critical)",
    )
    openai_fast_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model for lightweight tasks: query rewrite, table selection, answer rephrase, insights",
    )

    # Fallback LLM providers — all optional.
    # Add the API key for any provider you want to use as a fallback.
    # The backend will automatically include them in the fallback chain.
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key (Claude)")
    google_api_key: str | None = Field(default=None, description="Google API key (Gemini)")
    deepseek_api_key: str | None = Field(default=None, description="DeepSeek API key")

    # Ollama — local LLMs, no API key needed, just a running Ollama instance.
    ollama_base_url: str | None = Field(default=None, description="Ollama base URL, e.g. http://localhost:11434")
    ollama_model: str = Field(default="llama3.1", description="Ollama model name to use")

    # PostgreSQL database
    db_user: str = Field(..., description="Database username")
    db_password: str = Field(..., description="Database password")
    db_host: str = Field(..., description="Database host")
    db_port: int = Field(default=5432, description="Database port")
    db_name: str = Field(..., description="Database name")

    # MCP Chart Server (Phase 9.5)
    # Internal Docker URL when running via docker-compose.
    # Override in .env for local dev: MCP_CHART_SERVER_URL=http://localhost:8087
    # TODO: Set to None to disable chart generation entirely (e.g. resource-constrained envs).
    mcp_chart_server_url: str = Field(
        default="http://mcp_chart_server:8087",
        description="Base URL of the MCP chart server (SSE endpoint = /sse)",
    )

    # Rate limiting — Phase 10 (production hardening)
    # Per-IP request cap on /api/query. Override in .env for higher limits
    # during trusted internal testing, or lower limits for public exposure.
    rate_limit_per_minute: int = Field(
        default=20,
        description="Maximum requests per IP per minute on /api/query",
    )

    # LLM concurrency — Phase 11 (production hardening)
    # Maximum simultaneous in-flight LLM API calls across all requests.
    # At ~1,000 tokens/call and a 30,000 TPM limit, 5 concurrent calls is safe.
    # Lower for shared/free-tier keys; raise for dedicated high-TPM keys.
    llm_max_concurrency: int = Field(
        default=5,
        description="Maximum concurrent LLM API calls (semaphore cap)",
    )

    # Response cache — Phase 11 (production hardening)
    # TTL for cached first-turn question responses. Follow-up questions are
    # never cached because their answers depend on per-thread history.
    cache_ttl_seconds: int = Field(
        default=3600,  # 1 hour
        description="TTL (seconds) for cached first-turn question responses",
    )

    # Circuit breaker — Phase 11 (production hardening)
    # Opens the circuit (rejects all LLM calls) after this many consecutive
    # failures (primary + all fallbacks exhausted). Closes after the cooldown.
    llm_circuit_failure_threshold: int = Field(
        default=5,
        description="Consecutive LLM failures before circuit breaker opens",
    )
    llm_circuit_cooldown_seconds: int = Field(
        default=60,
        description="Seconds the circuit stays open before allowing a probe",
    )

    # Redis — Phase 10: persistent conversation history
    # Internal Docker URL when running via docker-compose.
    # Override in .env for local dev: REDIS_URL=redis://localhost:6379/0
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis connection URL (used for RedisChatMessageHistory)",
    )
    # Session TTL in seconds. After this period Redis auto-expires history and
    # chip keys so stale sessions don't accumulate indefinitely.
    redis_ttl_seconds: int = Field(
        default=86400,  # 24 hours
        description="TTL (seconds) for Redis session keys (history + chips)",
    )

    # ChromaDB persistence — Phase 13
    # Persistent vector stores for cricket rules RAG and few-shot examples.
    # Docker default: /app/chroma_data (mounted as a named volume in docker-compose).
    # Local dev override: CHROMA_PERSIST_DIR=./chroma_data
    # To force a full re-embed (e.g. after upgrading chromadb): delete this directory
    # and restart. The content-hash guard will detect the missing store and rebuild.
    chroma_persist_dir: str = Field(
        default="/app/chroma_data",
        description="Root directory for ChromaDB persistent vector stores",
    )

    # Embedding model — included in content hashes so a model change auto-triggers
    # a full re-embed of both ChromaDB stores (cricket rules + few-shot examples).
    # Changing this value invalidates both caches on next startup.
    # Must be one of the values in _VALID_EMBEDDING_MODELS — validated at startup
    # to fail fast with a clear error rather than an opaque OpenAI API failure.
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model used for ChromaDB vector stores",
    )

    @field_validator("openai_embedding_model")
    @classmethod
    def validate_embedding_model(cls, v: str) -> str:
        if v not in _VALID_EMBEDDING_MODELS:
            raise ValueError(
                f"OPENAI_EMBEDDING_MODEL must be one of {sorted(_VALID_EMBEDDING_MODELS)}, got '{v}'"
            )
        return v

    # Player name index TTL — Phase 13
    # The entity resolver loads the players table once and caches it in memory.
    # After this many seconds the cache is considered stale and the next request
    # triggers a silent background refresh from the DB. Prevents restarts being
    # required when new players are added to the players table mid-season.
    player_index_ttl_seconds: int = Field(
        default=3600,  # 1 hour
        description="TTL (seconds) for the in-memory player name index",
    )

    # CORS
    allowed_origins: list[str] = Field(
        default=["http://localhost:8085"],
        description="Comma-separated list of allowed CORS origins",
    )

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
