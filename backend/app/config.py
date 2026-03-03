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

    # OpenAI
    openai_api_key: str = Field(..., description="OpenAI API key")

    # MySQL database
    db_user: str = Field(..., description="Database username")
    db_password: str = Field(..., description="Database password")
    db_host: str = Field(..., description="Database host")
    db_name: str = Field(..., description="Database name")

    # CORS
    allowed_origins: list[str] = Field(
        default=["http://localhost:8085"],
        description="Comma-separated list of allowed CORS origins",
    )

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
