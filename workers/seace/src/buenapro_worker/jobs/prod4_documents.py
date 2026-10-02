from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from buenapro_worker.documents.pdf import sha256_bytes
from buenapro_worker.extraction.gemini import GeminiExtractor, prompt_version_for_doc_class
from buenapro_worker.normalization.facets import derive_facets, derive_summary, facet_hash
from buenapro_worker.normalization.participation_repository import persist_participation
from buenapro_worker.prod4.client import DocumentTooLargeError, Prod4Client
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def _document_priority(row: dict[str, Any]) -> tuple[int, datetime, str]:
    label = f"{row.get('document_type') or ''} {row.get('name') or ''}".lower()
    if "base" in label and "integrad" in label:
        rank = 4
    elif "base" in label:
        rank = 3
    elif any(word in label for word in ("especificaci", "términos de referencia", "terminos de referencia", "tdr", "eett")):
        rank = 2
    else:
        rank = 0
    published_at = row.get("published_at") or datetime.min.replace(tzinfo=timezone.utc)
    return rank, published_at, str(row.get("codigo_alfresco"))


def select_official_requirements_pdf(
    rows: list[dict[str, Any]], *, supported_extensions: set[str] | None = None,
) -> dict[str, Any] | None:
    """Choose the current authoritative version, THEN require PDF support.

    An older administrative PDF cannot replace newer integrated DOCX/RAR
    bases. A PDF twin with the same stage and publication time is supported.
    """
    candidates = [row for row in rows if _document_priority(row)[0] > 0]
    if not candidates:
        return None
    latest = max(_document_priority(row)[:2] for row in candidates)
    supported = {extension.lower().lstrip(".") for extension in supported_extensions or {"pdf"}}
    available = [row for row in candidates if _document_priority(row)[:2] == latest
                 and str(row.get("extension") or "").lower().lstrip(".") in supported]
    # Prefer the PDF twin when one exists; converters remain explicitly local.
    pdfs = [row for row in available if str(row.get("extension") or "").lower().lstrip(".") == "pdf"]
    return max(pdfs or available, key=_document_priority) if available else None


def mark_unsupported_current_document(
    repo: JobRepository, procedure_id: int, rows: list[dict[str, Any]], *,
    supported_extensions: set[str] | None = None,
) -> bool:
    """Persist a format limitation, distinct from absence of an official file."""
    official = [row for row in rows if _document_priority(row)[0] > 0]
    if not official or select_official_requirements_pdf(official, supported_extensions=supported_extensions) is not None:
        return False
    current = max(official, key=_document_priority)
    # A PDF-only server cannot redownload this format, but a local reader may
    # already have converted and analyzed this exact official source. Preserve
    # that result until the authoritative document identity actually changes.
    saved = repo.conn.execute(
        """SELECT id, sha256_original FROM prod4_document_extractions
           WHERE id_procedimiento = %s AND codigo_alfresco = %s
             AND is_current = true AND quality <> 'failed'
           ORDER BY id DESC LIMIT 1""",
        (procedure_id, current["codigo_alfresco"]),
    ).fetchone()
    saved_sha = saved.get("sha256_original") if isinstance(saved, dict) else None
    if isinstance(saved_sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", saved_sha):
        return False
    # Also covers an already-queued old PDF job that executes after a source
    # refresh: neither a stale extraction nor its verdict remains current.
    repo.conn.execute("UPDATE prod4_document_extractions SET is_current = false WHERE id_procedimiento = %s AND is_current = true", (procedure_id,))
    repo.conn.execute("UPDATE prod4_requirement_facets SET is_current = false WHERE id_procedimiento = %s AND is_current = true", (procedure_id,))
    repo.conn.execute("DELETE FROM opportunity_matches WHERE opportunity_id = (SELECT opportunity_id FROM prod4_processes WHERE id_procedimiento = %s)", (procedure_id,))
    repo.conn.execute(
        """UPDATE opportunities SET consortium_status = 'not_identified',
             subcontracting_status = 'not_identified', participation_terms_json = '{}'::jsonb,
             updated_at = now() WHERE id = (SELECT opportunity_id FROM prod4_processes WHERE id_procedimiento = %s)""",
        (procedure_id,),
    )
    _mark_skipped(repo, procedure_id, current["codigo_alfresco"], "unsupported_current_format")
    return True


def _client_extensions(client: Any) -> set[str] | None:
    declared = getattr(client, "supported_extensions", None)
    if isinstance(declared, (set, frozenset, tuple, list)) and all(isinstance(value, str) for value in declared):
        return set(declared)
    return None


def _original_document_sha256(client: Any, content: bytes, extension: str) -> str:
    original_sha = getattr(client, "original_sha256", None)
    if isinstance(original_sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", original_sha):
        return original_sha.lower()
    if extension.lower().lstrip(".") != "pdf":
        raise ValueError("A converted document requires a verified original SHA-256")
    return sha256_bytes(content)


def _profile_has_relevant_fit(repo: JobRepository, procedure_id: int) -> bool:
    row = repo.conn.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM company_profiles cp
          LEFT JOIN automation_rules ar ON ar.profile_id = cp.id
          JOIN LATERAL profile_prod4_fit(cp.id, %s) fit ON true
          WHERE cp.is_active = true AND fit.business_line_id IS NOT NULL
            AND COALESCE(ar.enabled, true) = true
            AND fit.fit_level >= GREATEST(2, COALESCE(ar.min_fit_level, 2))
        ) AS eligible
        """,
        (procedure_id,),
    ).fetchone()
    return bool(row and row["eligible"])


def _document_jobs_today(repo: JobRepository) -> int:
    row = repo.conn.execute(
        """SELECT count(*) AS total FROM worker_jobs
           WHERE job_type = 'extract_prod4_document'
             AND created_at >= date_trunc('day', now())"""
    ).fetchone()
    return int(row["total"] or 0)


def enqueue_prod4_document_if_eligible(
    settings: Settings, repo: JobRepository, procedure_id: int
) -> bool:
    if settings.prod4_document_daily_limit < 0:
        raise ValueError("prod4_document_daily_limit cannot be negative")
    # Serializes competing poll/sweep transactions before the count + insert.
    repo.conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod4_document_daily_budget'))")
    if _document_jobs_today(repo) >= settings.prod4_document_daily_limit:
        return False
    process = repo.conn.execute(
        """SELECT opportunity_id, document_analysis_status, analyzed_document_code
           FROM prod4_processes WHERE id_procedimiento = %s
             AND technology_relevant = true AND missing_since IS NULL""",
        (procedure_id,),
    ).fetchone()
    if process is None:
        return False
    docs = repo.conn.execute(
        """SELECT codigo_alfresco, name, document_type, extension, published_at
           FROM prod4_documents WHERE id_procedimiento = %s""",
        (procedure_id,),
    ).fetchall()
    selected = select_official_requirements_pdf([dict(row) for row in docs])
    if selected is None:
        mark_unsupported_current_document(repo, procedure_id, [dict(row) for row in docs])
        return False
    code = selected["codigo_alfresco"]
    if process["analyzed_document_code"] == code and process["document_analysis_status"] in {"extracted", "skipped"}:
        return False
    if not _profile_has_relevant_fit(repo, procedure_id):
        return False
    # A new bases PDF invalidates verdicts from the previous document. Keep
    # preliminary affinity visible while the new document is being checked.
    if process["analyzed_document_code"] != code:
        repo.conn.execute(
            "DELETE FROM opportunity_matches WHERE opportunity_id = %s",
            (process["opportunity_id"],),
        )
    return repo.enqueue(
        "extract_prod4_document",
        {"id_procedimiento": procedure_id, "codigo_alfresco": str(code)},
        queue_name="llm",
        dedup_key=f"extract_prod4_document:{procedure_id}:{code}",
        priority=5,
    ) is not None


def enqueue_current_prod4_document_sweep(
    settings: Settings, repo: JobRepository, *, limit: int = 10
) -> dict[str, int]:
    if limit < 0:
        raise ValueError("limit cannot be negative")
    pending_row = repo.conn.execute(
        """SELECT count(*) AS total FROM worker_jobs
           WHERE job_type = 'extract_prod4_document' AND status IN ('pending', 'claimed')"""
    ).fetchone()
    pending = int(pending_row["total"] or 0)
    budget = max(0, min(limit - pending, settings.prod4_document_daily_limit - _document_jobs_today(repo)))
    rows = repo.conn.execute(
        """SELECT id_procedimiento FROM prod4_processes
           WHERE technology_relevant = true AND missing_since IS NULL AND raw_detail IS NOT NULL
           ORDER BY proposals_closes_at NULLS LAST, id_procedimiento"""
    ).fetchall()
    checked = 0
    enqueued = 0
    for row in rows:
        if enqueued >= budget:
            break
        checked += 1
        enqueued += int(enqueue_prod4_document_if_eligible(settings, repo, int(row["id_procedimiento"])))
    return {"checked": checked, "enqueued": enqueued, "already_pending": pending}


def _mark_skipped(repo: JobRepository, procedure_id: int, code: UUID, reason: str) -> None:
    repo.conn.execute(
        """UPDATE prod4_processes
           SET document_analysis_status = 'skipped', document_analysis_reason = %s,
               document_analysis_checked_at = now(), analyzed_document_code = %s
           WHERE id_procedimiento = %s""",
        (reason, code, procedure_id),
    )


def _has_substantive_requirements(facets: list[dict[str, Any]]) -> bool:
    substantive = {
        "economic_experience", "general_experience",
        "specific_experience", "key_personnel", "equipment", "insurance",
        "license", "company_certification", "education", "training",
        "professional_registration",
    }
    return any(facet["required"] and facet["facet"] in substantive for facet in facets)


def extract_prod4_document_job(
    settings: Settings,
    repo: JobRepository,
    *,
    id_procedimiento: int,
    codigo_alfresco: str,
    client: Prod4Client | None = None,
    extractor: GeminiExtractor | None = None,
    enqueue_matching: bool = True,
) -> dict[str, Any]:
    code = UUID(codigo_alfresco)
    process = repo.conn.execute(
        """SELECT opportunity_id, object_type, technology_relevant, missing_since
           FROM prod4_processes WHERE id_procedimiento = %s""",
        (id_procedimiento,),
    ).fetchone()
    if process is None or not process["technology_relevant"] or process["missing_since"] is not None:
        return {"skipped": "not_current"}
    docs = repo.conn.execute(
        """SELECT codigo_alfresco, name, document_type, extension, published_at
           FROM prod4_documents WHERE id_procedimiento = %s""",
        (id_procedimiento,),
    ).fetchall()
    supported_extensions = _client_extensions(client)
    selected = select_official_requirements_pdf([dict(row) for row in docs], supported_extensions=supported_extensions)
    if selected is None and mark_unsupported_current_document(
        repo, id_procedimiento, [dict(row) for row in docs], supported_extensions=supported_extensions,
    ):
        return {"skipped": "unsupported_current_format"}
    if selected is None or selected["codigo_alfresco"] != code:
        return {"skipped": "document_superseded"}
    if not _profile_has_relevant_fit(repo, id_procedimiento):
        return {"skipped": "no_relevant_profile"}

    doc_class = "bases_good" if process["object_type"] == "good" else "bases_service"
    prompt_version = prompt_version_for_doc_class(doc_class)
    current = repo.conn.execute(
        """SELECT id FROM prod4_document_extractions
           WHERE id_procedimiento = %s AND codigo_alfresco = %s
             AND prompt_version = %s AND is_current = true AND quality <> 'failed'""",
        (id_procedimiento, code, prompt_version),
    ).fetchone()
    if current:
        return {"skipped": "already_extracted", "extraction_id": int(current["id"])}

    def download(source: Prod4Client) -> bytes:
        content = source.download_document(code, max_bytes=settings.prod4_max_analysis_pdf_bytes)
        if not content.startswith(b"%PDF-"):
            raise ValueError("Document transport did not produce a PDF")
        if len(content) > settings.prod4_max_analysis_pdf_bytes:
            raise DocumentTooLargeError("Converted PDF exceeds configured analysis size limit")
        return content

    try:
        if client is None:
            with Prod4Client(settings) as source:
                pdf_bytes = download(source)
                original_sha256 = _original_document_sha256(source, pdf_bytes, selected["extension"])
        else:
            pdf_bytes = download(client)
            original_sha256 = _original_document_sha256(client, pdf_bytes, selected["extension"])
    except DocumentTooLargeError:
        _mark_skipped(repo, id_procedimiento, code, "pdf_exceeds_analysis_limit")
        return {"skipped": "pdf_exceeds_analysis_limit"}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in {401, 403}:
            raise
        # A protected origin must not become an endless retry/queue loop.
        # Keep the preliminary fit and expose the access limitation instead.
        _mark_skipped(repo, id_procedimiento, code, "official_document_access_denied")
        logger.warning(
            "prod4_document_access_denied",
            extra={"id_procedimiento": id_procedimiento, "status_code": exc.response.status_code},
        )
        return {"skipped": "official_document_access_denied"}
    except ValueError as exc:
        _mark_skipped(repo, id_procedimiento, code, "official_document_not_pdf")
        logger.warning("prod4_document_not_pdf", extra={"id_procedimiento": id_procedimiento, "error": str(exc)})
        return {"skipped": "official_document_not_pdf"}

    result = (extractor or GeminiExtractor(settings)).extract(
        pdf_bytes, mime="application/pdf", doc_class=doc_class
    )
    raw = result.raw_json
    summary = derive_summary(raw)
    facets = derive_facets(raw)
    # A scanned/incomplete or generic bases document cannot establish a final
    # eligibility verdict. Keep only preliminary affinity in that case.
    can_match = bool(facets) and _has_substantive_requirements(facets) and not result.requires_human_review
    repo.conn.execute(
        """UPDATE prod4_document_extractions SET is_current = false
           WHERE id_procedimiento = %s AND is_current = true""",
        (id_procedimiento,),
    )
    extraction = repo.conn.execute(
        """INSERT INTO prod4_document_extractions (
             id_procedimiento, opportunity_id, codigo_alfresco, sha256_original,
             model, prompt_version, schema_version, input_tokens, output_tokens,
             cost_usd, raw_extraction_json, summary_json, requires_human_review,
             facet_count, is_current
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s::jsonb, %s::jsonb, %s, %s, true) RETURNING id""",
        (
            id_procedimiento, process["opportunity_id"], code, original_sha256,
            result.model, result.prompt_version, result.schema_version,
            result.input_tokens, result.output_tokens, result.cost_usd,
            json.dumps(raw, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
            result.requires_human_review, len(facets),
        ),
    ).fetchone()
    extraction_id = int(extraction["id"])
    persist_participation(repo, opportunity_id=process["opportunity_id"], raw=raw,
                          extraction_id=extraction_id, source="seace_prod4",
                          document_sha256=original_sha256)
    repo.conn.execute(
        """UPDATE prod4_requirement_facets SET is_current = false
           WHERE id_procedimiento = %s AND is_current = true""",
        (id_procedimiento,),
    )
    for facet in facets:
        repo.conn.execute(
            """INSERT INTO prod4_requirement_facets (
                 id_procedimiento, opportunity_id, extraction_id, facet, label,
                 required, details_json, evidence_json, facet_hash, is_current
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, true)""",
            (
                id_procedimiento, process["opportunity_id"], extraction_id,
                facet["facet"], facet["label"], facet["required"],
                json.dumps(facet["details"], ensure_ascii=False),
                json.dumps(facet["evidence"], ensure_ascii=False), facet_hash(facet),
            ),
        )
    reason = None if can_match else "requirements_incomplete_or_review_required"
    repo.conn.execute(
        """UPDATE prod4_processes
           SET document_analysis_status = %s, document_analysis_reason = %s,
               document_analysis_checked_at = now(), analyzed_document_code = %s
           WHERE id_procedimiento = %s""",
        ("extracted" if can_match else "skipped", reason, code, id_procedimiento),
    )
    if can_match and enqueue_matching:
        repo.enqueue(
            "route_prod4_profiles",
            {"id_procedimiento": id_procedimiento, "extraction_id": extraction_id},
            queue_name="match",
            dedup_key=f"route_prod4_profiles:{id_procedimiento}:{extraction_id}",
            priority=4,
        )
    logger.info(
        "prod4_document_extracted",
        extra={"id_procedimiento": id_procedimiento, "extraction_id": extraction_id,
               "facets": len(facets), "match_eligible": can_match, "cost_usd": result.cost_usd},
    )
    return {"extraction_id": extraction_id, "facets": len(facets), "match_eligible": can_match}
