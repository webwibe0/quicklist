from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True, secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None)
    database_url: str = "postgresql+psycopg://assistant:assistant@localhost:5432/assistant"
    app_environment: str = "local"
    auth_require_email_verification: bool = False
    auth_registration_enabled: bool = True
    auth_csrf_ip_limit: int = Field(default=100, ge=1, le=1000)
    mail_host: str = ""
    mail_port: int = Field(default=587, ge=1, le=65535)
    mail_ssl: bool = False
    mail_username: str = ""
    mail_password: SecretStr = SecretStr("")
    mail_from: str = ""
    support_email: str = ""
    interactive_budget_usd: float = Field(default=0, ge=0, le=100)
    # Explicit production policy. Old binding amounts remain historical metadata.
    ai_usage_mode: Literal['capped', 'accounting_only'] = 'capped'
    ai_total_budget_usd: float = Field(default=0, ge=0, le=4)
    # Zero retains statistics without a lifetime request-count ceiling.
    interactive_budget_max_calls: int = Field(default=0, ge=0, le=10000)
    interactive_budget_path: Path = ROOT / '.local' / 'interactive-budget.sqlite3'
    embedding_worker_in_web: bool = True
    worker_status_path: Path = ROOT / '.local' / 'worker-status.json'
    mail_ca_file: str = ''
    db_connect_timeout_seconds: int = Field(default=3, ge=2)
    app_timezone: str = "Europe/Moscow"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.6-luna"
    llm_timeout_seconds: float = 30
    llm_max_output_tokens: int = Field(default=4096, ge=512, le=8192)
    llm_max_input_bytes: int = Field(default=100000, ge=10000, le=200000)
    openai_stt_model: str = "gpt-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "marin"
    voice_timeout_seconds: float = 45
    auth_origin: str = "http://127.0.0.1:3000"
    auth_cookie_secure: bool = False
    auth_session_days: int = Field(default=7, ge=1, le=30)
    auth_login_ip_limit: int = Field(default=20, ge=1, le=100)
    auth_login_account_limit: int = Field(default=5, ge=1, le=20)
    google_calendar_enabled: bool = False
    google_client_id: str = ''
    google_client_secret: SecretStr = SecretStr('')
    google_token_key: SecretStr = SecretStr('')
    google_redirect_uri: str = 'http://127.0.0.1:8000/api/v1/calendar/google/callback'

    @model_validator(mode='after')
    def google_redirect(self):
        from urllib.parse import urlsplit
        value = urlsplit(self.google_redirect_uri)
        if (value.path != '/api/v1/calendar/google/callback' or value.query or value.fragment or value.username
                or value.scheme not in {'http', 'https'} or not value.hostname
                or (value.scheme == 'http' and value.hostname not in {'127.0.0.1', 'localhost', '::1'})):
            raise ValueError('Google callback must be the exact server callback URI; HTTPS outside loopback')
        return self
    result_limit: int = 50
    semantic_search_enabled: bool = False
    semantic_min_similarity: float = Field(default=0.7, ge=0, le=1)
    semantic_candidate_limit: int = Field(default=100, ge=1, le=500)
    embedding_production_indexing_approved: bool = False
    embedding_worker_enabled: bool = False
    embedding_real_api_enabled: bool = False
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = Field(default=1536, ge=1536, le=1536)
    embedding_batch_size: int = Field(default=16, ge=1, le=64)
    embedding_max_documents: int = Field(default=128, ge=1, le=1000)
    embedding_max_input_bytes: int = Field(default=8000, ge=1, le=8000)
    embedding_max_run_bytes: int = Field(default=64000, ge=1, le=1000000)
    embedding_timeout_seconds: float = Field(default=10, ge=0.1, le=60)
    embedding_lease_seconds: int = Field(default=60, ge=1, le=600)
    embedding_max_attempts: int = Field(default=3, ge=1, le=10)
    embedding_retry_base_seconds: float = Field(default=2, ge=1, le=60)
    embedding_retry_max_seconds: float = Field(default=300, ge=1, le=3600)
    embedding_poll_seconds: float = Field(default=10, ge=1, le=3600)
    embedding_allowed_cluster_id: str = ""
    # Lifetime allowance, never renewed by a restart. Zero blocks paid background work.
    embedding_budget_usd: float = Field(default=0, ge=0, le=1)
    embedding_budget_max_calls: int = Field(default=500, ge=1, le=10000)
    embedding_budget_path: Path = ROOT / '.local' / 'embedding-budget.sqlite3'
    embedding_min_call_interval_seconds: float = Field(default=30, ge=0, le=3600)

    @model_validator(mode="after")
    def total_ai_budget(self):
        # Fixed allocations keep the combined ceiling safe across processes without
        # a second ledger. Unknown reservations remain charged in each allocation.
        if self.ai_usage_mode == 'capped' and self.ai_total_budget_usd and (
            round(self.interactive_budget_usd * 1e9)
            + round(self.embedding_budget_usd * 1e9)
            > round(self.ai_total_budget_usd * 1e9)
        ):
            raise ValueError('Allocated caps exceed total AI budget')
        if (self.ai_total_budget_usd or self.ai_usage_mode == 'accounting_only') and (
            self.interactive_budget_path.resolve() == self.embedding_budget_path.resolve()
        ):
            raise ValueError('AI budget allocations require distinct ledger paths')
        return self

    @model_validator(mode="after")
    def secure_auth_origin(self):
        from urllib.parse import urlsplit
        origin = urlsplit(self.auth_origin)
        if (origin.scheme not in {"http", "https"} or not origin.hostname
                or origin.path not in {"", "/"} or origin.query or origin.fragment or origin.username):
            raise ValueError("AUTH_ORIGIN must be a single origin")
        if origin.hostname not in {"127.0.0.1", "localhost", "::1"} and (
                origin.scheme != "https" or not self.auth_cookie_secure):
            raise ValueError("Public auth requires HTTPS and Secure cookies")
        self.auth_origin = self.auth_origin.rstrip('/')
        if self.app_environment not in {'local', 'staging', 'production'}:
            raise ValueError('Invalid application environment')
        if self.app_environment != 'local' and (
                origin.scheme != 'https' or not self.auth_cookie_secure
                or not self.auth_require_email_verification):
            raise ValueError('Staging/production requires HTTPS, Secure cookies and email verification')
        if self.app_environment == 'production' and not (self.mail_host and self.mail_from and self.support_email):
            raise ValueError('Production requires SMTP sender and support contact')
        return self

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("result_limit")
    @classmethod
    def valid_limit(cls, value):
        if not 1 <= value <= 200:
            raise ValueError("RESULT_LIMIT must be between 1 and 200")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
