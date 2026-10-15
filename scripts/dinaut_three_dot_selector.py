"""Read-only SQL and local readiness classification. No DB/provider access.

Caller must execute in READ ONLY transaction and freeze returned identities.
The template is reused verbatim from the frontend to avoid another affinity
algorithm. This module does not import any provider or invoke any pipeline.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
from buenapro_worker.matching.readiness import match_readiness

PROFILE_ID = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"
TENANT_ID = "46ba7840-ae84-4de0-ac98-16fab9e5d5bb"
PARAMS = {"profile_id": PROFILE_ID, "tenant_id": TENANT_ID}


def prod2_fit_lateral() -> str:
    path = ROOT / "apps/web/server/services/keywordTextScoring.ts"
    content = path.read_text()
    found = re.search(r"return `([\s\S]+?)`;", content)
    if not found:
        raise ValueError("Canonical keyword SQL template unavailable")
    sql = found[1].replace("$${profileParam}", "%(profile_id)s").replace("${descriptionSql}", "p.description")
    if "${" in sql:
        raise ValueError("Unresolved canonical SQL template")
    return sql


def prod2_sql() -> str:
    return """
    SELECT 'seace_prod2'::text AS source_key, p.id_convocatoria AS source_id,
      p.opportunity_id::text, p.nomenclature AS code, p.description,
      p.buyer_name, d.proposals_closes_at AS closes_at,
      fit.business_line_id::text, fit.business_line_name,
      fit.keyword_points, fit.fit_points, fit.fit_level, fit.keyword_hits,
      ex.id AS extraction_id, ex.sha256_original AS document_sha256,
      ex.quality, ex.requires_human_review, ex.is_current,
      ex.raw_extraction_json, ex.summary_json,
      COALESCE(facets.rows, '[]'::jsonb) AS facets,
      matched.id AS existing_match_id,
      matched.breakdown_json->'meta' AS existing_match_meta,
      COALESCE(reservations.rows, '[]'::jsonb) AS prior_match_reservations
    FROM prod2_discoveries p
    JOIN prod2_details d USING(id_convocatoria)
    JOIN company_profiles cp ON cp.id=%(profile_id)s::uuid
      AND cp.tenant_id=%(tenant_id)s::uuid AND cp.is_active
    """ + prod2_fit_lateral() + """
    LEFT JOIN LATERAL (
      SELECT e.* FROM prod2_document_extractions e
      WHERE e.id_convocatoria=p.id_convocatoria AND e.is_current
      ORDER BY e.created_at DESC,e.id DESC LIMIT 1
    ) ex ON true
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(to_jsonb(f) ORDER BY f.facet,f.id) AS rows
      FROM prod2_requirement_facets f
      WHERE f.id_convocatoria=p.id_convocatoria
        AND f.extraction_id=ex.id AND f.is_current
    ) facets ON true
    LEFT JOIN opportunity_matches matched ON matched.profile_id=cp.id
      AND matched.opportunity_id=p.opportunity_id
    LEFT JOIN LATERAL (
      SELECT jsonb_agg(jsonb_build_object('id',e.id,'kind',e.kind,
        'status',e.status,'model',e.model,'policy_version',e.policy_version,
        'request_hash',e.request_hash) ORDER BY e.id) AS rows
      FROM prod2_evaluations e
      WHERE e.id_convocatoria=p.id_convocatoria AND e.profile_id=cp.id
        AND e.kind='gemini_match'
    ) reservations ON true
    WHERE fit.fit_level=3
    ORDER BY d.proposals_closes_at DESC NULLS LAST,p.id_convocatoria
    """


PROD6_SQL = """
SELECT 'seace_prod6'::text AS source_key, c.id_contrato AS source_id,
  c.opportunity_id::text, c.codigo AS code,c.descripcion AS description,
  c.entidad_nombre AS buyer_name,c.fec_fin_cotizacion AS closes_at,
  fit.business_line_id::text, fit.business_line_name,fit.keyword_points,
  fit.fit_points,fit.fit_score,fit.fit_level,fit.keyword_hits,
  ex.id AS extraction_id,ex.sha256_original AS document_sha256,
  ex.quality,ex.requires_human_review,ex.is_current,
  ex.raw_extraction_json,ex.summary_json,
  COALESCE(facets.rows,'[]'::jsonb) AS facets,
  matched.id AS existing_match_id,
  matched.breakdown_json->'meta' AS existing_match_meta,
  COALESCE(jobs.rows,'[]'::jsonb) AS prior_worker_jobs
FROM seace_contracts c
JOIN company_profiles cp ON cp.id=%(profile_id)s::uuid
  AND cp.tenant_id=%(tenant_id)s::uuid AND cp.is_active
CROSS JOIN LATERAL profile_contract_fit(cp.id,c.id_contrato) fit
LEFT JOIN LATERAL (
  SELECT tx.*,d.sha256_original FROM contract_documents d
  JOIN tdr_extractions tx ON tx.contract_document_id=d.id AND tx.is_current
  WHERE d.id_contrato=c.id_contrato
  ORDER BY tx.created_at DESC,tx.id DESC LIMIT 1
) ex ON true
LEFT JOIN LATERAL (
  SELECT jsonb_agg(to_jsonb(f) ORDER BY f.facet,f.id) AS rows
  FROM requirement_facets f WHERE f.id_contrato=c.id_contrato
    AND f.extraction_id=ex.id AND f.is_current
) facets ON true
LEFT JOIN matches matched ON matched.profile_id=cp.id AND matched.id_contrato=c.id_contrato
LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('id',j.id,'status',j.status,
    'attempts',j.attempts,'claimed_at',j.claimed_at,'finished_at',j.finished_at)
    ORDER BY j.id) AS rows
  FROM worker_jobs j WHERE j.job_type='analyze_match'
    AND j.payload->>'profile_id'=cp.id::text
    AND j.payload->>'id_contrato'=c.id_contrato::text
) jobs ON true
WHERE fit.fit_level=3
ORDER BY c.fec_fin_cotizacion DESC NULLS LAST,c.id_contrato
"""


def classify(row: dict) -> dict:
    """Keep snapshot readiness separate from dispatch disposition.

    Existing matches and any prior matching evaluation/job are retained for
    review/reconciliation by the caller. No uncertainty is retried implicitly.
    New executor's own durable receipts must ALSO be checked before dispatch.
    """
    source = {key: row.get(key) for key in (
        "extraction_id", "is_current", "quality", "requires_human_review",
        "raw_extraction_json", "summary_json")}
    ready, reason = match_readiness(source, row.get("facets") or [], provisional_source_review=True)
    reservations = row.get("prior_match_reservations") or []
    jobs = row.get("prior_worker_jobs") or []
    if int(row.get("fit_level") or 0) != 3:
        disposition = "exclude_not_three_dots"
    elif row.get("existing_match_id") is not None:
        disposition = "preserve_existing_match"
    elif any(item.get("status") in {"running", "uncertain"} for item in reservations):
        disposition = "preserve_running_or_uncertain"
    elif any(item.get("status") == "done" for item in reservations):
        disposition = "reconcile_completed_without_match"
    elif reservations:
        disposition = "inspect_prior_reservation_before_dispatch"
    elif jobs:
        disposition = "inspect_prior_worker_job_before_dispatch"
    elif not ready:
        disposition = "exclude_not_ready"
    else:
        disposition = "new_ready_candidate"
    return {"ready": ready, "readiness_reason": reason, "disposition": disposition}


def self_test() -> None:
    base = {"fit_level": 3,"extraction_id": 7,"is_current": True,
      "quality": "auto","requires_human_review": True,
      "raw_extraction_json": {"present": True},"summary_json": {"present": True},
      "facets": [{"extraction_id": 7,"is_current": True,"facet": "specific_experience",
                  "label": "Experiencia en software","required": True,"details_json": {}}]}
    assert classify(base)["disposition"] == "new_ready_candidate"
    assert "provisional" in classify(base)["readiness_reason"]
    assert classify({**base,"extraction_id": None})["disposition"] == "exclude_not_ready"
    assert classify({**base,"fit_level": 2})["disposition"] == "exclude_not_three_dots"
    assert classify({**base,"raw_extraction_json": {}})["disposition"] == "exclude_not_ready"
    assert classify({**base,"existing_match_id": 4})["disposition"] == "preserve_existing_match"
    for status in ("running","uncertain"):
        assert classify({**base,"prior_match_reservations": [{"status":status}]})["disposition"] == "preserve_running_or_uncertain"
    assert classify({**base,"prior_match_reservations": [{"status":"done"}]})["disposition"] == "reconcile_completed_without_match"
    assert classify({**base,"prior_worker_jobs": [{"status":"dead"}]})["disposition"] == "inspect_prior_worker_job_before_dispatch"
    for sql in (prod2_sql(),PROD6_SQL):
        assert "fit.fit_level=3" in sql
        assert "cp.tenant_id=%(tenant_id)s::uuid" in sql
        assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CALL)\b",sql,re.I)
    print("selector offline self-test: passed; no DB/provider access")


if __name__ == "__main__":
    self_test()
