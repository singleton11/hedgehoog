from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "production"] = "development"
    mongodb_uri: SecretStr = SecretStr(
        "mongodb://hedgehoog:hedgehoog-local@127.0.0.1:27017/?authSource=admin"
    )
    mongodb_database: str = "hedgehoog"
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
