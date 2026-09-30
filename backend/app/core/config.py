"""Application settings — validated at startup via Pydantic Settings.

All environment variables are typed and required. The app will refuse to start
if any required variable is missing or malformed. No silent failures.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",  # crash if unknown env vars are passed — no silent misconfig
    )

    # ── Runtime ──────────────────────────────────────────────────────────────
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    PORT: int = Field(default=8000, ge=1, le=65535)
    DEBUG: bool = False

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1, le=100)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0, le=100)

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn

    # ── Auth / JWT ────────────────────────────────────────────────────────────
    JWT_ACCESS_SECRET: str = Field(min_length=32)
    JWT_REFRESH_SECRET: str = Field(min_length=32)
    JWT_ACCESS_EXPIRE_MINUTES: int = Field(default=30, ge=5)
    JWT_REFRESH_EXPIRE_DAYS: int = Field(default=30, ge=1)
    OTP_EXPIRE_MINUTES: int = Field(default=10, ge=1)

    # ── CORS / Hosts ──────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] | str = []
    ALLOWED_HOSTS: list[str] | str = ["*"]

    # ── Paystack ──────────────────────────────────────────────────────────────
    PAYSTACK_SECRET_KEY: str
    PAYSTACK_PUBLIC_KEY: str
    PAYSTACK_BASE_URL: AnyHttpUrl = AnyHttpUrl("https://api.paystack.co")

    # ── AWS S3 / Object Storage ───────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_S3_BUCKET: str
    AWS_S3_REGION: str = "us-east-1"
    AWS_S3_ENDPOINT_URL: AnyHttpUrl | None = None  # set for Cloudflare R2

    # ── Firebase (Push Notifications) ─────────────────────────────────────────
    FIREBASE_CREDENTIALS_JSON: str  # path to service account JSON file

    # ── Google Maps ───────────────────────────────────────────────────────────
    GOOGLE_MAPS_API_KEY: str

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str | None = None

    # ── Platform Config ───────────────────────────────────────────────────────
    COMMISSION_RATE: float = Field(default=0.10, ge=0.0, le=1.0)  # 10%
    MAX_HELPER_MATCH_RADIUS_KM: float = Field(default=20.0, ge=1.0)
    DEFAULT_MATCH_RADIUS_KM: float = Field(default=5.0, ge=1.0)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [host.strip() for host in v.split(",")]
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT == "test"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance. Use as a FastAPI dependency."""
    return Settings()
