"""Automatic evaluation (LLM2) of PROD2 readings (licitaciones mayores) for each company.

JEV accepts a PROD2 convocatoria once, for the commercial union of VEYON and DIN (the canonical
``prod2_evaluations`` row of kind ``jev_affinity``), and names its domain. The domain picks the
companies evaluated: ``industrial_electrica`` only DIN; ``informatica`` and ``ambiguo`` both, since
DIN also sells software. A (convocatoria, company) pair is evaluated when JEV accepted it (affinity
index >= 60), the company's ``automation_rules`` allow it (enabled, daily limit shared with menores,
hours before close), the convocatoria is open, its current reading has requirements and the company
has no match yet from that reading, profile and prompt.

Readings under human review are evaluated as well: every PROD2 reading of selected documents is
flagged by design. The match is stored with ``persist_match(allow_source_review=True)`` and the web
shows it as a provisional estimate. The evaluation itself is the menores one (``finish_match``:
8 most related experiences, substantive requirements only, verdict by rules) with a mayores prompt.

Payments follow ``prod6.evaluation_guard`` (receipts before sending, answer saved before parsing,
saturation fallback) plus the PROD2 ledger: each pair claims a ``gemini_match`` reservation.
* An HTTP error answered by the provider releases the reservation (nothing billed) and ends the run.
* An answer that skips or reorders requirements marks that reservation ``failed``: only that pair.
* A send whose outcome is uncertain leaves the receipt guard open and the reservation
  ``uncertain``: the run stops and no later run sends until an operator reviews it.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from buenapro_worker.jobs.analyze_match import PROFILE_FIELDS, finish_match
from buenapro_worker.jobs.match import _automatic_jobs_today
from buenapro_worker.matching.profile_evidence import evidence_only_profile
from buenapro_worker.matching.substantive_policy import ModelOutputMismatch, is_administrative_requirement
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod6.evaluation_guard import GuardedMatchAnalyzer
from buenapro_worker.prod6.reading import GuardedReadingError, ProviderRejectedError, ReadingBlockedError

from .batch_plan import MEMBER_IDS
from .repository import claim_evaluation, complete_evaluation, hash_request, persist_match

POLICY_VERSION = "prod2_match_v1"
AUTO_ORIGIN = "prod2_jev_domain_v1"
JEV_POLICY = "prod2_commercial_union_v1"
VEYON, DIN = MEMBER_IDS
DOMAIN_PROFILES = {"industrial_electrica": (DIN,), "informatica": (VEYON, DIN), "ambiguo": (VEYON, DIN)}
# Outcomes that end a run: the next one starts from a clean provider/payment state.
STOPS = {"provider_rejected", "payment_uncertain", "receipts_blocked"}
# Bases list many conditions of one kind (8 accepted specialties, one row per role and degree);
# the model merges them and skips indexes, so each kind goes as ONE requirement with its
# conditions in ``detalle``. Personnel degrees and registrations belong to the key personnel.
FAMILY = {"education": "key_personnel", "training": "key_personnel", "professional_registration": "key_personnel"}
FAMILY_LABELS = {
    "economic_experience": "Experiencia del postor: monto facturado",
    "specific_experience": "Experiencia del postor en la especialidad",
    "general_experience": "Experiencia general del postor",
    "key_personnel": "Personal clave: perfil, formación y experiencia",
    "equipment": "Equipamiento e infraestructura",
    "insurance": "Seguros",
    "license": "Licencias y autorizaciones",
    "company_certification": "Certificaciones de la empresa",
    "legal_capacity": "Habilitación legal",
}

ROWS_SQL = """
WITH jev AS (
  SELECT DISTINCT ON (ev.id_convocatoria) ev.id_convocatoria, ev.result_json->>'domain' AS domain,
         (ev.result_json->>'affinity_index')::numeric AS affinity
  FROM prod2_evaluations ev
  WHERE ev.kind = 'jev_affinity' AND ev.status = 'done' AND ev.policy_version = %(jev_policy)s
  ORDER BY ev.id_convocatoria, ev.completed_at DESC NULLS LAST, ev.id DESC
)
SELECT e.id AS extraction_id, e.id_convocatoria, e.opportunity_id::text AS opportunity_id, e.summary_json,
       e.quality, e.requires_human_review, e.facet_count,
       j.domain, j.affinity, d.proposals_closes_at,
       (d.proposals_closes_at IS NOT NULL AND d.proposals_closes_at > now()) AS open,
       extract(epoch FROM (d.proposals_closes_at - now())) / 3600 AS hours_left,
       disc.nomenclature, disc.description, disc.buyer_name,
       p.profile_id::text AS profile_id, (cp.id IS NOT NULL) AS profile_active,
       encode(sha256(convert_to(cp.profile_hash, 'UTF8')), 'hex') AS profile_hash,
       COALESCE(ar.enabled, true) AS rule_enabled,
       COALESCE(ar.max_daily_evaluations, 0) AS max_daily,
       COALESCE(ar.min_hours_before_close, 0) AS min_hours,
       EXISTS (SELECT 1 FROM opportunity_matches m
               JOIN prod2_match_provenance pr ON pr.match_id = m.id
               JOIN prod2_evaluations mev ON mev.id = pr.evaluation_id
               WHERE m.profile_id = p.profile_id AND m.opportunity_id = e.opportunity_id
                 AND pr.extraction_id = e.id
                 AND pr.profile_hash = encode(sha256(convert_to(cp.profile_hash, 'UTF8')), 'hex')
                 AND mev.kind = 'gemini_match' AND mev.status = 'done'
                 AND mev.policy_version = %(policy)s) AS match_current
FROM prod2_document_extractions e
JOIN prod2_documents doc ON doc.id_convocatoria = e.id_convocatoria
  AND doc.codigo_alfresco = e.codigo_alfresco AND doc.is_listed
JOIN prod2_discoveries disc ON disc.id_convocatoria = e.id_convocatoria
LEFT JOIN prod2_details d ON d.id_convocatoria = e.id_convocatoria
LEFT JOIN jev j ON j.id_convocatoria = e.id_convocatoria
CROSS JOIN unnest(%(profiles)s::uuid[]) AS p(profile_id)
LEFT JOIN company_profiles cp ON cp.id = p.profile_id AND cp.is_active
LEFT JOIN automation_rules ar ON ar.profile_id = p.profile_id
WHERE e.is_current
  AND (%(convocations)s::bigint[] IS NULL OR e.id_convocatoria = ANY(%(convocations)s::bigint[]))
  AND (%(open_only)s IS NOT TRUE OR d.proposals_closes_at > now())
ORDER BY d.proposals_closes_at NULLS LAST, e.id_convocatoria, p.profile_id
"""

TODAY_SQL = """
SELECT count(*) AS total FROM prod2_evaluations
WHERE kind = 'gemini_match' AND policy_version = %s AND profile_id = %s
  AND created_at >= date_trunc('day', now())
"""

FACETS_SQL = """
SELECT facet, label, required, details_json, facet_hash FROM prod2_requirement_facets
WHERE id_convocatoria = %s AND extraction_id = %s AND is_current
ORDER BY facet, id
"""


class Prod2GuardedMatchAnalyzer(GuardedMatchAnalyzer):
    receipt_kind = "prod2_match_analysis_v1"


def profile_snapshot(profile_hash: str | None) -> str | None:
    """SHA256 of ``company_profiles.profile_hash`` (32 hex), the form PROD2 provenance stores."""
    return sha256(profile_hash.encode("utf-8")).hexdigest() if profile_hash else None


def split_prod2_requirements(facets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Same shape as ``split_match_requirements``, one requirement per kind of condition.

    Penalties are not capacity; proposal documents (the annexes) and administrative rows are
    paperwork to prepare. A kind with a single condition keeps it as it is.
    """
    families: dict[str, list[dict]] = {}
    checklist = []
    for row in facets:
        if row["facet"] == "penalty_condition":
            continue
        if row["facet"] == "proposal_document" or is_administrative_requirement(row):
            checklist.append({"tipo": row["facet"], "requisito": row["label"], "estado": "preparar",
                              "score_impact": False})
            continue
        families.setdefault(FAMILY.get(row["facet"], row["facet"]), []).append(row)
    requisitos = []
    for index, (kind, rows) in enumerate(families.items(), 1):
        if len(rows) == 1 and rows[0]["facet"] == kind:
            row = rows[0]
            requisitos.append({"source_index": index, "tipo": kind, "requisito": row["label"],
                               "obligatorio": row["required"], "detalle": row["details_json"]})
            continue
        requisitos.append({"source_index": index, "tipo": kind,
                           "requisito": FAMILY_LABELS.get(kind, "Otras condiciones de capacidad"),
                           "obligatorio": any(row["required"] for row in rows),
                           "detalle": {"condiciones": [{"tipo": row["facet"], "requisito": row["label"],
                                                         "obligatorio": row["required"], "detalle": row["details_json"]}
                                                        for row in rows]}})
    return requisitos, checklist


def candidate_rows(conn, *, convocations: list[int] | None = None, open_only: bool = True) -> list[dict]:
    return conn.execute(ROWS_SQL, {"jev_policy": JEV_POLICY, "policy": POLICY_VERSION,
                                   "profiles": list(MEMBER_IDS), "convocations": convocations,
                                   "open_only": open_only}).fetchall()


def skip_reason(row: dict) -> str | None:
    """Why this pair is not evaluated, or None. Free: nothing is claimed or sent here."""
    if row["domain"] is None or row["affinity"] is None:
        return "jev_missing"
    if float(row["affinity"]) < 60:
        return "jev_rejected"
    if row["profile_id"] not in DOMAIN_PROFILES.get(row["domain"], ()):
        return "domain_not_for_company"
    if not row["profile_active"]:
        return "profile_inactive"
    if not row["profile_hash"]:
        return "profile_hash_missing"
    if not row["rule_enabled"]:
        return "rule_disabled"
    if not row["open"]:
        return "closed"
    if row["quality"] == "failed":
        return "reading_failed"
    if not row["facet_count"]:
        return "no_requirements"
    if row["match_current"]:
        return "match_current"
    if row["hours_left"] is not None and float(row["hours_left"]) < int(row["min_hours"]):
        return "too_close_to_close"
    return None


def _used_today(conn, profile_id: str) -> int:
    menores = _automatic_jobs_today(SimpleNamespace(conn=conn), profile_id)
    return menores + int(conn.execute(TODAY_SQL, (POLICY_VERSION, profile_id)).fetchone()["total"] or 0)


def plan(conn, *, convocations: list[int] | None = None, max_calls: int) -> dict[str, Any]:
    """The pairs a run would evaluate, after every free check and the daily limits."""
    skipped: Counter[str] = Counter()
    planned, used = [], {}
    for row in candidate_rows(conn, convocations=convocations):
        reason = skip_reason(row)
        if reason:
            skipped[reason] += 1
            continue
        profile = row["profile_id"]
        used.setdefault(profile, _used_today(conn, profile))
        daily = int(row["max_daily"] or 0)  # 0 means no daily limit.
        if daily and used[profile] >= daily:
            skipped["daily_limited"] += 1
            continue
        if len(planned) >= max_calls:
            skipped["call_limit"] += 1
            continue
        used[profile] += 1
        planned.append(row)
    return {"planned": planned, "skipped": dict(skipped)}


def _set_status(conn, claim: dict, status: str, reason: str, result=None) -> None:
    """Close this run's own reservation as ``pending`` (released), ``failed`` or ``uncertain``."""
    usage = (result.input_tokens, result.output_tokens, result.cost_usd) if result else (None, None, None)
    if status == "pending":
        conn.execute("""UPDATE prod2_evaluations SET status='pending', claim_token=NULL, claimed_at=NULL,
            lease_until=NULL, updated_at=now() WHERE id=%s AND claim_token=%s AND status='running'""",
            (claim["id"], claim["claim_token"]))
        return
    conn.execute("""UPDATE prod2_evaluations SET status=%s, result_json=%s::jsonb,
        input_tokens=COALESCE(%s, input_tokens), output_tokens=COALESCE(%s, output_tokens),
        cost_usd=COALESCE(%s, cost_usd), updated_at=now()
        WHERE id=%s AND claim_token=%s AND status='running'""",
        (status, json.dumps({"reason": reason}), *usage, claim["id"], claim["claim_token"]))


def evaluate_pair(conn, settings, analyzer: GuardedMatchAnalyzer, row: dict) -> dict[str, Any]:
    convocation, profile_id, extraction_id = int(row["id_convocatoria"]), row["profile_id"], int(row["extraction_id"])
    profile = conn.execute("SELECT * FROM company_profiles WHERE id = %s AND is_active", (profile_id,)).fetchone()
    if profile is None or profile_snapshot(profile["profile_hash"]) != row["profile_hash"]:
        return {"outcome": "skipped", "reason": "profile_changed"}
    facet_rows = conn.execute(FACETS_SQL, (convocation, extraction_id)).fetchall()
    requisitos, checklist = split_prod2_requirements(facet_rows)
    profile_evidence = evidence_only_profile({field: profile[field] for field in PROFILE_FIELDS})
    meta = {"prompt_sha256": sha256(analyzer.prompt.encode("utf-8")).hexdigest(), "extraction_id": extraction_id,
            "profile_hash": row["profile_hash"],
            "facets_hash": canonical_hash(sorted(str(item["facet_hash"]) for item in facet_rows))}
    request_hash = hash_request({**meta, "convocation": convocation, "profile_id": profile_id,
                                 "model": settings.gemini_model, "policy_version": POLICY_VERSION})
    claim = claim_evaluation(conn, convocation, kind="gemini_match", request_hash=request_hash,
                             model=settings.gemini_model, policy_version=POLICY_VERSION, profile_id=profile_id)
    if claim["status"] == "cached":
        stored = claim["result_json"]
    elif claim["status"] == "uncertain":
        return {"outcome": "payment_uncertain", "reason": "expired_reservation", "evaluation_id": claim["id"]}
    elif claim["status"] != "claimed":
        return {"outcome": "skipped", "reason": "reservation_" + claim["status"], "evaluation_id": claim["id"]}
    else:
        try:
            result = analyzer.analyze(perfil=profile_evidence, requisitos=requisitos, oportunidad={
                "codigo": row["nomenclature"], "descripcion": row["description"],
                "entidad": row["buyer_name"], "resumen": row["summary_json"]})
        except ProviderRejectedError as error:
            _set_status(conn, claim, "pending", "provider_rejected")
            return {"outcome": "provider_rejected", "reason": f"http_{error.code}", "evaluation_id": claim["id"]}
        except ReadingBlockedError:
            _set_status(conn, claim, "pending", "receipts_blocked")  # Nothing was sent.
            return {"outcome": "receipts_blocked", "reason": "guard_open", "evaluation_id": claim["id"]}
        except GuardedReadingError:
            _set_status(conn, claim, "uncertain", "sent_request_without_durable_response")
            return {"outcome": "payment_uncertain", "reason": "guard_opened", "evaluation_id": claim["id"]}
        try:
            score, verdict, breakdown, missing = finish_match(
                result, requisitos=requisitos, administrative_checklist=checklist,
                profile_evidence=profile_evidence, summary_json=row["summary_json"], meta=meta)
        except ModelOutputMismatch as error:
            # Temperature 0 repeats the answer: resending would only pay again for the same gap.
            _set_status(conn, claim, "failed", error.code, result)
            return {"outcome": "model_output_invalid", "reason": error.code, "evaluation_id": claim["id"],
                    "cost_usd": result.cost_usd}
        breakdown["meta"].update({"execution": "worker_auto", "evaluation_origin": AUTO_ORIGIN,
                                  "jev_domain": row["domain"], "jev_affinity": float(row["affinity"])})
        stored = {"extraction_id": extraction_id, "profile_hash": row["profile_hash"], "score": score,
                  "verdict": verdict, "breakdown": breakdown, "missing": missing, "model": result.model}
        complete_evaluation(conn, claim["id"], claim["claim_token"], stored, input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens, cost_usd=result.cost_usd)
    saved = persist_match(conn, convocation, profile_id=profile_id, extraction_id=extraction_id,
                          evaluation_id=claim["id"], profile_hash=stored["profile_hash"], score=stored["score"],
                          verdict=stored["verdict"], breakdown_json=stored["breakdown"],
                          missing_actions_json=stored["missing"], allow_source_review=True)
    paid = claim["status"] == "claimed"  # A replay of a completed reservation costs nothing now.
    return {"outcome": "evaluated" if paid else "replayed", **saved, "evaluation_id": claim["id"],
            "cost_usd": stored["breakdown"]["meta"].get("cost_usd") if paid else 0}


def run(conn, settings, receipt_dir: Path, *, max_calls: int, convocations: list[int] | None = None,
        analyzer: GuardedMatchAnalyzer | None = None) -> dict[str, Any]:
    """Evaluate the planned pairs in order; stop at the first provider rejection or payment doubt."""
    planned = plan(conn, convocations=convocations, max_calls=max_calls)
    report: dict[str, Any] = {"policy_version": POLICY_VERSION, "skipped": planned["skipped"],
                              "planned": len(planned["planned"]), "outcomes": [], "stopped": None,
                              "cost_usd": 0.0}
    if not planned["planned"]:
        return report
    analyzer = analyzer or Prod2GuardedMatchAnalyzer(settings, receipt_dir, prompt_version=POLICY_VERSION)
    try:
        for row in planned["planned"]:
            outcome = evaluate_pair(conn, settings, analyzer, row)
            report["outcomes"].append({"id_convocatoria": int(row["id_convocatoria"]),
                                       "profile_id": row["profile_id"], **outcome})
            report["cost_usd"] += float(outcome.get("cost_usd") or 0)
            if outcome["outcome"] in STOPS:
                report["stopped"] = outcome["outcome"]
                break
    finally:
        analyzer.close()
    return report
