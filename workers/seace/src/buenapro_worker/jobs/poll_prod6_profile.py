"""Bounded, profile-driven discovery; no tenant-wide historical sweep."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from buenapro_worker.jobs.poll_search import LIMA_TZ, canonical_hash, parse_lima_datetime, upsert_search_item
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def matches_profile_scope(description: str, lines: list[dict[str, Any]], segment: int) -> bool:
    """A phrase hit or two distinct configured words warrants document reading.

    This is discovery relevance, NOT evidence of legal/technical compliance.
    Matching stays within each business line instead of mixing unrelated words.
    """
    text = f" {normalize(description)} "
    for line in lines:
        if str(segment) not in [str(value) for value in line.get("cubso_segmentos", [])]:
            continue
        phrases = {normalize(value) for value in line.get("keyword_phrases", []) if value.strip()}
        terms = {normalize(value) for value in line.get("keyword_terms", []) if value.strip()}
        # Backward compatibility with lines populated before split keywords.
        if not phrases and not terms:
            for value in line.get("keywords", []):
                normalized = normalize(value)
                (phrases if " " in normalized else terms).add(normalized)
        if any(phrase and f" {phrase} " in text for phrase in phrases):
            return True
        if sum(bool(term) and f" {term} " in text for term in terms) >= 2:
            return True
    return False


def poll_prod6_profile(
    settings: Settings,
    repo: JobRepository,
    *,
    profile_id: str,
    anio: int | None = None,
    max_pages_per_segment: int = 10,
    max_candidates: int = 200,
    batch_id: str | None = None,
) -> dict[str, int]:
    """Discover current-year vigente goods/services from active profile lines.

    Existing SEACE contracts remain global and deduplicated by official ID.
    Only profile-scoped candidates reach process_contract / LLM1. The ordinary
    worker subsequently matches other tenants as usual; no LLM2 is run here.
    """
    if max_pages_per_segment < 1 or max_candidates < 1:
        raise ValueError("Polling limits must be positive")
    profile = repo.conn.execute(
        "SELECT id FROM company_profiles WHERE id = %s AND is_active = true", (profile_id,),
    ).fetchone()
    if not profile:
        raise ValueError("Active company profile not found")
    lines = list(repo.conn.execute(
        """SELECT cubso_segmentos, keyword_phrases, keyword_terms, keywords
           FROM business_lines WHERE profile_id = %s AND is_active = true""",
        (profile_id,),
    ).fetchall())
    segments = sorted({int(value) for line in lines for value in line["cubso_segmentos"] if str(value).isdigit()})
    if not segments or not any(line.get("keywords") or line.get("keyword_phrases") or line.get("keyword_terms") for line in lines):
        raise ValueError("Profile needs active segments and keywords")
    year = anio or datetime.now(LIMA_TZ).year
    stats = {"seen": 0, "candidates": 0, "changed": 0, "enqueued": 0, "skipped": 0,
             "pages": 0, "capped_segments": 0, "limit_reached": 0}
    seen: set[int] = set()
    with SeaceClient(settings) as client:
        for objeto in (1, 2):
            for segment in segments:
                for page in range(1, max_pages_per_segment + 1):
                    response = client.search_contracts(
                        anio=year, estado=2, objeto=objeto, segmento=segment, page=page, page_size=100,
                    )
                    stats["pages"] += 1
                    for item in response.data:
                        stats["seen"] += 1
                        publication = parse_lima_datetime(item.fec_publica)
                        close = parse_lima_datetime(item.fec_fin_cotizacion)
                        # PROD6 sometimes ignores anio: enforce it against publication.
                        if (item.estado_codigo != 2 or item.objeto_codigo != objeto
                                or publication is None or publication.astimezone(LIMA_TZ).year != year
                                or (close is not None and close <= datetime.now(timezone.utc))
                                or not matches_profile_scope(item.descripcion, lines, segment)):
                            stats["skipped"] += 1
                            continue
                        if item.id_contrato in seen:
                            continue
                        seen.add(item.id_contrato)
                        stats["candidates"] += 1
                        changed = upsert_search_item(repo, item, anio=year, segment=segment)
                        stats["changed"] += int(changed)
                        # Existing candidates without extraction can be recovered,
                        # but complete documents must not re-run every 30 minutes.
                        if not changed:
                            extracted = repo.conn.execute(
                                """SELECT 1 FROM tdr_extractions e
                                   JOIN contract_documents d ON d.id = e.contract_document_id
                                   WHERE d.id_contrato = %s AND e.is_current = true
                                     AND e.quality != 'failed' LIMIT 1""",
                                (item.id_contrato,),
                            ).fetchone()
                            if extracted:
                                continue
                        payload: dict[str, Any] = {"id_contrato": item.id_contrato, "segment": segment}
                        if batch_id:
                            payload["batch_id"] = batch_id
                        job = repo.enqueue(
                            "process_contract", payload, queue_name="io", priority=2,
                            dedup_key=f"process_contract:{batch_id or 'profile'}:{item.id_contrato}:"
                                      f"{canonical_hash(item.model_dump(mode='json', by_alias=True))}",
                        )
                        stats["enqueued"] += int(job is not None)
                        if stats["candidates"] >= max_candidates:
                            stats["limit_reached"] = 1
                            return stats
                    total_pages = (response.pageable.total_elements + response.pageable.page_size - 1) // response.pageable.page_size
                    if not response.data or page >= total_pages:
                        break
                    if page == max_pages_per_segment:
                        stats["capped_segments"] += 1
    return stats
