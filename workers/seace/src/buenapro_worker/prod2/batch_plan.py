"""Pure metadata-load plan: publication dates are independent of exercise years."""
from __future__ import annotations

import calendar
import copy
from datetime import date, datetime
import hashlib
import json
from zoneinfo import ZoneInfo

LIMA = ZoneInfo("America/Lima")
PLAN_VERSION = "prod2-six-calendar-months-metadata-v1"
MEMBER_IDS = ["bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def months_before(day: date, months: int) -> date:
    if type(months) is not int or not 1 <= months <= 120:
        raise ValueError("months must be an integer from 1 to 120")
    absolute = day.year * 12 + day.month - 1 - months
    year, month = divmod(absolute, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def publication_window(as_of: datetime, *, months: int = 6,
                       published_from: str | None = None, published_to: str | None = None) -> tuple[str, str]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include its timezone")
    end = date.fromisoformat(published_to) if published_to else as_of.astimezone(LIMA).date()
    start = date.fromisoformat(published_from) if published_from else months_before(end, months)
    if start > end or end > as_of.astimezone(LIMA).date():
        raise ValueError("Publication interval is reversed or in the future")
    return start.isoformat(), end.isoformat()


def configured_keywords(config: dict) -> list[str]:
    keywords = [word for lane in config["lanes"] for word in lane["keywords"]]
    if not keywords or any(not isinstance(word, str) or not word.strip() or len(word) > 120 for word in keywords):
        raise ValueError("Invalid keyword inventory")
    folded = [word.casefold().strip() for word in keywords]
    if len(folded) != len(set(folded)):
        raise ValueError("Keyword inventory contains duplicates")
    return [word.strip() for word in keywords]


def freeze_plan(*, as_of: datetime, keywords: list[str], years: list[int], published_from: str,
                published_to: str, commercial_profiles: list[dict], profile_id: str,
                jev_model: str, jev_policy: str, keyword_config_hash: str,
                zero_result_scopes: list[dict] | None = None) -> dict:
    publication_window(as_of, published_from=published_from, published_to=published_to)
    if not years or any(type(year) is not int or not 2000 <= year <= 2100 for year in years) or len(years) != len(set(years)):
        raise ValueError("Exercise years must be unique observed/configured years")
    if not keywords or len(keywords) != len(set(keywords)):
        raise ValueError("Keywords must be nonempty and unique")
    if profile_id not in MEMBER_IDS or len(commercial_profiles) != 2:
        raise ValueError("This run needs the two authorized commercial members")
    return copy.deepcopy({"plan_version": PLAN_VERSION, "source": "seace_prod2", "surface": "Procedimientos de Selección",
            "as_of": as_of.astimezone(LIMA).isoformat(), "published_from": published_from,
            "published_to": published_to, "publication_granularity": "inclusive_calendar_day",
            "procedure_years": sorted(years, reverse=True), "year_filter_kind": "convocatoria_year",
            "keywords": keywords, "keyword_config_hash": keyword_config_hash,
            "object_types": ["good", "service"], "seace_version": "3",
            "phase_policy": "discovery_then_enrich", "save_rejected_jev": True,
            "ficha_for_all_unique_records": True, "pdf_downloads": False, "gemini": False,
            "profile_id": profile_id, "member_profile_ids": MEMBER_IDS,
            "commercial_profiles": commercial_profiles, "commercial_profiles_hash": digest(commercial_profiles),
            "jev_model": jev_model, "jev_policy_version": jev_policy,
            "zero_result_scopes": zero_result_scopes or [],
            "coverage_scope": "configured_keywords_objects_exercises_publication_interval",
            "source_atomic_snapshot": False})


def native_arguments(query: dict) -> dict:
    return {"year": query["year"], "object_type": {"good": "Bien", "service": "Servicio"}[query["object_type"]],
            "published_from": date.fromisoformat(query["published_from"]).strftime("%d/%m/%Y"),
            "published_to": date.fromisoformat(query["published_to"]).strftime("%d/%m/%Y")}


def page_capture(query: dict, result: dict, observed_at: datetime) -> dict:
    if result.get("search_verified") is not True or len(result.get("counter", [])) != 5:
        raise ValueError("An observed and verified native search page is required")
    counter, snapshot = result["counter"], result["snapshot"]
    if (counter[2], counter[4]) != (snapshot["total_rows"], snapshot["total_pages"]):
        raise ValueError("Native page and snapshot totals disagree")
    rows = [{"procedure_id": row["procedure_id"], "convocation_id": row["convocatoria_id"],
             "nomenclature": row["nomenclature"], "buyer_name": row["entity"],
             "published_at": row["published_at"], "description": row["description"],
             "object_label": row["object"], "reference_amount": row["amount_text"],
             "currency": row["currency"], "seace_version": row["seace_version"]} for row in result["rows"]]
    return {"query": query, "observed_at": observed_at.isoformat(),
            "page": {"number": counter[3], "total_pages": counter[4], "total_results": counter[2],
                     "page_size": snapshot["page_size"]}, "rows": rows}
