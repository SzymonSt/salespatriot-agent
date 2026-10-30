from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

BACKEND_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = BACKEND_DIR.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr | None = None
    google_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    embedding_model: str = "gemini-embedding-001"
    embedding_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    profiles_dir: Path = REPOSITORY_ROOT / "data" / "profiles"
    knowledge_base_dir: Path = REPOSITORY_ROOT / "knowledge_base"

    @model_validator(mode="after")
    def validate_model_name(self) -> Self:
        if not self.gemini_model.strip():
            raise ValueError("GEMINI_MODEL must not be empty")
        if not self.embedding_model.strip():
            raise ValueError("EMBEDDING_MODEL must not be empty")
        return self

    def async_database_url(self) -> URL | None:
        if self.database_url is None:
            return None

        raw_url = self.database_url.get_secret_value().strip()
        if not raw_url or any(
            marker in raw_url
            for marker in ("[YOUR-PASSWORD]", "<PROJECT_REF>", "<URL_ENCODED_DATABASE_PASSWORD>", "<POOLER_HOST>")
        ):
            return None

        url = make_url(raw_url)
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+asyncpg")
        if url.drivername not in {"postgresql+asyncpg", "sqlite+aiosqlite"}:
            raise ValueError("DATABASE_URL must use PostgreSQL or SQLite with an async driver")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
