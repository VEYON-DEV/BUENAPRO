from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    database_url: str
    gemini_api_key: str
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_fallback_model: str = "gemini-2.5-flash"
    seace_base_url: str = "https://prod6.seace.gob.pe/v1/s8uit-services"
    seace_user_agent: str = "BuenaPro/0.1 (+https://veyon-solutions.local)"
    seace_timeout_seconds: float = 45.0
    seace_poll_interval_minutes: int = 30
    seace_lifecycle_interval_hours: int = 6
    seace_recent_closures_interval_hours: int = 2
    seace_recent_closures_days: int = 15
    seace_contract_test_interval_minutes: int = 60
    seace_concurrency: int = 3
    gemini_concurrency: int = 1
    seace_allowed_estado_contrato: str = "2"
    seace_allowed_codigo_objeto: str = "2"
    seace_allowed_segments: str = "43,81,78,80"
    worker_id: str = "local-worker"
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    telegram_bot_token: str = ""

    @staticmethod
    def _parse_csv_ints(value: str) -> list[int]:
        return [int(item.strip()) for item in value.split(",") if item.strip()]

    @property
    def allowed_estado_contrato(self) -> list[int]:
        return self._parse_csv_ints(self.seace_allowed_estado_contrato)

    @property
    def allowed_codigo_objeto(self) -> list[int]:
        return self._parse_csv_ints(self.seace_allowed_codigo_objeto)

    @property
    def allowed_segments(self) -> list[int]:
        return self._parse_csv_ints(self.seace_allowed_segments)

    @property
    def primary_estado_contrato(self) -> int:
        return self.allowed_estado_contrato[0]

    @property
    def primary_codigo_objeto(self) -> int:
        return self.allowed_codigo_objeto[0]
