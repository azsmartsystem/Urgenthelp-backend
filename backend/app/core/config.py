"""Application settings — validated at startup via Pydantic Settings.

All environment variables are typed and required. The app will refuse to start
if any required variable is missing or malformed. No silent failures.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file (backend/.env), not the process CWD, so the
# app starts correctly whether uvicorn is launched from the repo root or backend/.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_BACKEND_ROOT / ".env", Path(".env")),
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
    OTP_COOLDOWN_SECONDS: int = Field(default=60, ge=1)
    OTP_MAX_REQUESTS_PER_WINDOW: int = Field(default=3, ge=1)
    OTP_RATE_LIMIT_WINDOW_MINUTES: int = Field(default=10, ge=1)
    DEV_STATIC_OTP: str | None = Field(default=None, pattern=r"^\d{6}$")

    # ── CORS / Hosts ──────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] | str = []
    ALLOWED_HOSTS: list[str] | str = ["*"]

    # ── Paystack ──────────────────────────────────────────────────────────────
    PAYSTACK_SECRET_KEY: str
    PAYSTACK_PUBLIC_KEY: str
    PAYSTACK_BASE_URL: AnyHttpUrl = AnyHttpUrl("https://api.paystack.co")

    # ── AWS S3 / Object Storage ───────────────────────────────────────────────
    STORAGE_PROVIDER: Literal["s3", "local", "memory"] = "local"
    LOCAL_STORAGE_DIR: str = "./storage"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_S3_BUCKET: str | None = None
    AWS_S3_REGION: str = "us-east-1"
    AWS_S3_ENDPOINT_URL: AnyHttpUrl | None = None  # set for Cloudflare R2

    # ── Firebase (Push Notifications) ─────────────────────────────────────────
    FIREBASE_CREDENTIALS_JSON: str  # path to service account JSON file

    # ── Google Maps ───────────────────────────────────────────────────────────
    GOOGLE_MAPS_API_KEY: str

    # ── Email (transactional: password reset, verification, receipts) ─────────
    EMAIL_PROVIDER_API_KEY: str | None = None  # Resend / SendGrid / SES key
    EMAIL_FROM_ADDRESS: str = "no-reply@UrgentHelp.com"
    EMAIL_VERIFICATION_EXPIRE_HOURS: int = Field(default=24, ge=1)
    PASSWORD_RESET_EXPIRE_MINUTES: int = Field(default=30, ge=5, le=120)
    EMAIL_COOLDOWN_SECONDS: int = Field(default=60, ge=1)
    EMAIL_MAX_REQUESTS_PER_WINDOW: int = Field(default=3, ge=1)
    EMAIL_RATE_LIMIT_WINDOW_MINUTES: int = Field(default=10, ge=1)

    # ── SMS / Multi-Provider (Termii, Twilio, Mock) ───────────────────────────
    SMS_PROVIDER: Literal["termii", "twilio", "mock"] = "termii"
    # Termii
    TERMII_API_KEY: str | None = None
    TERMII_SENDER_ID: str = "UrgentHelp"
    TERMII_BASE_URL: AnyHttpUrl = AnyHttpUrl("https://api.ng.termii.com")
    TERMII_CHANNEL: Literal["generic", "dnd", "whatsapp"] = "dnd"
    # Twilio
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_FROM_NUMBER: str | None = None

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str | None = None

    # ── Platform Config ───────────────────────────────────────────────────────
    COMMISSION_RATE: float = Field(default=0.10, ge=0.0, le=1.0)  # 10%
    MAX_HELPER_MATCH_RADIUS_KM: float = Field(default=20.0, ge=1.0)
    DEFAULT_MATCH_RADIUS_KM: float = Field(default=5.0, ge=1.0)

    @field_validator("DEV_STATIC_OTP", mode="before")
    @classmethod
    def normalize_dev_static_otp(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def validate_dev_static_otp(self) -> Self:
        if self.DEV_STATIC_OTP is not None and self.ENVIRONMENT == "production":
            raise ValueError("DEV_STATIC_OTP cannot be set when ENVIRONMENT is 'production'.")
        return self

    @model_validator(mode="after")
    def validate_storage_settings(self) -> Self:
        if self.STORAGE_PROVIDER == "s3":
            missing = [
                name
                for name, val in (
                    ("AWS_ACCESS_KEY_ID", self.AWS_ACCESS_KEY_ID),
                    ("AWS_SECRET_ACCESS_KEY", self.AWS_SECRET_ACCESS_KEY),
                    ("AWS_S3_BUCKET", self.AWS_S3_BUCKET),
                )
                if not isinstance(val, str) or not val.strip()
            ]
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"{joined} must be non-empty when STORAGE_PROVIDER is 's3'.")
        elif self.STORAGE_PROVIDER == "local" and not self.LOCAL_STORAGE_DIR.strip():
            raise ValueError(
                "LOCAL_STORAGE_DIR must be non-empty when STORAGE_PROVIDER is 'local'."
            )
        elif self.STORAGE_PROVIDER == "memory" and self.ENVIRONMENT == "production":
            raise ValueError(
                "STORAGE_PROVIDER cannot be 'memory' when ENVIRONMENT is 'production'."
            )
        return self

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
