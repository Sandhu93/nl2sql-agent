from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # OpenAI — primary LLM (required)
    openai_api_key: str = Field(..., description="OpenAI API key")

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
