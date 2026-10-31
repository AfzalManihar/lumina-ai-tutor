"""Centralised, validated configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_GROQ_MODEL = "openai/gpt-oss-120b"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: str = "replace_with_a_random_secret"
    access_token_minutes: int = 15
    refresh_token_days: int = 14

    mongo_uri: str = "mongodb://127.0.0.1:27017"
    db_name: str = "lumina_ai_tutor"

    groq_api_key: str = ""
    groq_model: str = REQUIRED_GROQ_MODEL
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_seconds: float = 30.0
    llm_max_output_tokens: int = 2500

    embedding_provider: Literal["", "google", "hash"] = ""
    google_api_key: str = ""
    google_embedding_model: str = "models/gemini-embedding-001"
    vector_store_provider: Literal["", "mongo"] = ""

    rag_top_k: int = 5
    rag_chunk_size: int = 1000
    rag_chunk_overlap: int = 150
    rag_min_score: float = 0.15
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 300

    user_monthly_token_limit: int = 250_000
    plan_pro_monthly_token_limit: int = 1_000_000
    plan_premium_monthly_token_limit: int = 5_000_000
    plan_pro_price_inr: int = 499
    plan_premium_price_inr: int = 1499
    plan_duration_days: int = 30

    max_agent_steps: int = 6
    max_tool_calls: int = 4
    max_retries: int = 2
    conversation_history_limit: int = 12
    max_input_chars: int = 4000
    max_output_chars: int = 12000
    workflow_timeout_seconds: float = 90.0
    workflow_token_budget: int = 12000
    min_token_reserve: int = 1500

    memory_enabled: bool = True
    memory_retrieval_limit: int = 5

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_base_url: str = "https://api.razorpay.com/v1"

    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    cors_origins: str = "http://localhost:5173"
    auth_rate_limit_attempts: int = 5
    auth_rate_limit_window_seconds: int = 300
    api_rate_limit_per_minute: int = 120

    @field_validator("rag_chunk_size", "rag_top_k", "max_agent_steps", "max_tool_calls")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("RAG_CHUNK_OVERLAP must be smaller than RAG_CHUNK_SIZE")
        if self.groq_model != REQUIRED_GROQ_MODEL:
            raise ValueError(f"GROQ_MODEL must be '{REQUIRED_GROQ_MODEL}'; other models are not allowed")
        if self.app_env == "production":
            if self.app_secret_key in ("", "replace_with_a_random_secret") or len(self.app_secret_key) < 32:
                raise ValueError("APP_SECRET_KEY must be a random value of >= 32 chars in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def secret_values(self) -> list[str]:
        """Secrets that must never appear in output/logs."""
        vals = [
            self.groq_api_key, self.google_api_key, self.razorpay_key_secret,
            self.razorpay_webhook_secret, self.langfuse_secret_key,
        ]
        if self.app_secret_key and self.app_secret_key != "replace_with_a_random_secret":
            vals.append(self.app_secret_key)
        return [v for v in vals if v and len(v) >= 6]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
