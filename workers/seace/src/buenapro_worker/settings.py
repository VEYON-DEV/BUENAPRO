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
    prod4_enabled: bool = True
    prod4_base_url: str = "https://prod4.seace.gob.pe:8086/api/oportunidades"
    prod4_poll_interval_minutes: int = 30
    prod4_detail_limit: int = 100
    prod4_technology_segments: str = "43,81"
    # Segment 81 also contains non-IT engineering and works consulting.
    prod4_service_cubso_prefixes: str = "811115,811116,811117,811118,811119,811120,811121,811122,811123,811124,811125"
    worker_id: str = "local-worker"
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    telegram_bot_token: str = ""
    settings_encryption_key: str = ""
    app_base_url: str = "http://localhost:3001"

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
    def prod4_segments(self) -> list[int]:
        segments = self._parse_csv_ints(self.prod4_technology_segments)
        unsupported = set(segments) - {43, 81}
        if unsupported:
            raise ValueError(f"Unsupported PROD4 technology segments: {sorted(unsupported)}")
        return segments

    @property
    def prod4_service_prefixes(self) -> list[str]:
        return [part.strip() for part in self.prod4_service_cubso_prefixes.split(",") if part.strip()]

    @property
    def primary_estado_contrato(self) -> int:
        return self.allowed_estado_contrato[0]

    @property
    def primary_codigo_objeto(self) -> int:
        return self.allowed_codigo_objeto[0]
