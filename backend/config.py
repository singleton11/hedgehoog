from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class TaskSettings(BaseSettings):
    """Database and broker settings shared by the API and background worker."""
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )

    mongodb_uri: SecretStr = SecretStr(
        "mongodb://hedgehoog:hedgehoog-local@127.0.0.1:27017/?authSource=admin"
    )
    mongodb_database: str = "hedgehoog"
    celery_broker_url: SecretStr = SecretStr(
        "amqp://hedgehoog:hedgehoog-local@127.0.0.1:5672//"
    )
    # Yahoo is the free experimental default. Legacy Marketstack is retained
    # only as an explicit adapter choice, never as an automatic fallback.
    market_data_provider: Literal["yfinance", "marketstack"] = "yfinance"
    market_data_max_calls_per_job: int = Field(default=40, ge=1, le=200)
    market_data_timeout_seconds: int = Field(default=15, ge=1, le=60)
    # Shared across acquisition processes, not a per-task sleep.
    yahoo_request_interval_seconds: float = Field(default=0.05, ge=0, le=60, allow_inf_nan=False)
    marketstack_api_key: SecretStr = SecretStr("")
    # Local conservative cap, independent of provider usage outside this app.
    # Disabled by default: explicitly configure a budget before spending quota.
    marketstack_monthly_budget: int = Field(default=0, ge=0, le=1_000_000)
    marketstack_max_calls_per_job: int = Field(default=12, ge=1, le=100)


class Settings(TaskSettings):
    environment: Literal["development", "production"] = "development"
    jwt_secret: SecretStr
    jwt_expire_minutes: int = Field(default=60, ge=1, le=1440)
    cookie_secure: bool = False
    admin_login: str = Field(default="admin", min_length=1, max_length=128)
    admin_password: SecretStr = SecretStr("admin")
    allowed_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

    @model_validator(mode="after")
    def validate_security(self):
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters; run make setup")
        password = self.admin_password.get_secret_value()
        if not password:
            raise ValueError("ADMIN_PASSWORD cannot be empty")
        if self.environment == "production":
            if password == "admin" or len(password) < 12:
                raise ValueError("Set a strong ADMIN_PASSWORD (at least 12 characters) in production")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production (HTTPS required)")
        return self
