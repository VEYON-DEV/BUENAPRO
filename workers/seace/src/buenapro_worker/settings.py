from __future__ import annotations

import json

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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
    prod6_profile_poll_enabled: bool = False
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
    schedule_refresh_enabled: bool = False
    schedule_refresh_interval_minutes: int = Field(default=30, ge=1)
    # Legacy deployments may still configure hours. Explicit minutes win.
    schedule_refresh_interval_hours: int | None = Field(default=None, ge=1)
    schedule_refresh_limit: int = 500
    # Deep analysis advances gradually: no more than three newly queued PDFs
    # per poll and ten per day, with an additional per-profile final-score cap.
    prod4_document_analysis_enabled: bool = True
    prod4_document_enqueue_limit_per_poll: int = 3
    prod4_document_daily_limit: int = 10
    prod4_profile_evaluations_daily_default: int = 10
    prod4_max_analysis_pdf_bytes: int = 20 * 1024 * 1024
    prod4_technology_segments: str = "43,81"
    # Optional, versioned sector rules; never enable an entire broad segment
    # without a specific phrase/acronym signal. Empty preserves the IT radar.
    prod4_additional_relevance_rules: str = "[]"
    # Segment 81 also contains non-IT engineering and works consulting.
    prod4_service_cubso_prefixes: str = "811115,811116,811117,811118,811119,811120,811121,811122,811123,811124,811125"
    # A CUBSO 81 prefix alone is insufficient: it includes non-IT engineering,
    # biomedical equipment, physical security and administrative services.
    prod4_technology_terms: str = "software,licencia,informatic,tecnolog,comput,servidor,base de datos,transmision de datos,informix,nube,cloud,internet,conectividad,correo electronico,comunicaciones,telecom,ciberseguridad,seguridad perimetral,red interna,telemetria,gps,impresora,cableado estructurado,plataforma virtual,portal web,web,erp,workspace,office 365,microsoft 365,privileged access,antivirus,virtualizacion,etl,sistema integral,aplicaciones,gestion de datos"
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
        return sorted(set(segments).union(
            segment for rule in self.prod4_relevance_rules for segment in rule["segments"]
        ))

    @property
    def prod4_relevance_rules(self) -> list[dict]:
        rules = json.loads(self.prod4_additional_relevance_rules)
        if not isinstance(rules, list) or len(rules) > 32:
            raise ValueError("PROD4 additional relevance rules must be a list of at most 32 rules")
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("key"), str) or not rule["key"].strip():
                raise ValueError("Each PROD4 relevance rule requires a key")
            segments = rule.get("segments")
            objects = rule.get("objects")
            terms = rule.get("terms")
            if not isinstance(segments, list) or not segments or any(
                type(segment) is not int or not 10 <= segment <= 99 for segment in segments
            ):
                raise ValueError("PROD4 relevance rule segments must be two-digit CUBSO integers")
            if not isinstance(objects, list) or not objects or any(obj not in ("good", "service") for obj in objects):
                raise ValueError("PROD4 relevance rules only accept goods/services")
            if not isinstance(terms, list) or not terms or len(terms) > 24 or any(
                not isinstance(term, str) or not 3 <= len(term.strip()) <= 100 for term in terms
            ):
                raise ValueError("PROD4 relevance rules require 1–24 specific phrases/acronyms")
        return rules

    @property
    def prod4_service_prefixes(self) -> list[str]:
        return [part.strip() for part in self.prod4_service_cubso_prefixes.split(",") if part.strip()]

    @property
    def prod4_terms(self) -> list[str]:
        return [part.strip() for part in self.prod4_technology_terms.split(",") if part.strip()]

    @property
    def schedule_refresh_interval_seconds(self) -> int:
        if "schedule_refresh_interval_minutes" not in self.model_fields_set and self.schedule_refresh_interval_hours is not None:
            return self.schedule_refresh_interval_hours * 3600
        return self.schedule_refresh_interval_minutes * 60

    @property
    def primary_estado_contrato(self) -> int:
        return self.allowed_estado_contrato[0]

    @property
    def primary_codigo_objeto(self) -> int:
        return self.allowed_codigo_objeto[0]
