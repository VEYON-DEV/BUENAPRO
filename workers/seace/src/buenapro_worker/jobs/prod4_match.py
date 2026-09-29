from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from buenapro_worker.jobs.analyze_match import (
    PROFILE_FIELDS,
    _derive_verdict,
    _econ_capacity,
    _econ_exigido,
    clamp_score,
)
from buenapro_worker.matching.analyzer import MatchAnalyzer
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)
PROD4_MATCH_PROMPT_VERSION = "match_analysis_prod4_v1"


def _daily_prod4_evaluations(repo: JobRepository, profile_id: str) -> int:
    row = repo.conn.execute(
        """SELECT count(*) AS total FROM worker_jobs
           WHERE job_type = 'analyze_prod4_match'
             AND payload->>'source' = 'automatic'
             AND payload->>'profile_id' = %s
             AND created_at >= date_trunc('day', now())""",
        (profile_id,),
    ).fetchone()
    return int(row["total"] or 0)


def _guard_prod4_economic_claim(
    requisitos: list[dict], *, exigido: float | None, capacidad: float
) -> None:
    """A numerical shortfall cannot be marked compliant from model inference."""
    if exigido is None or exigido <= 0 or capacidad >= exigido:
        return
    for requirement in requisitos:
        if requirement.get("categoria") == "experiencia_economica" and requirement.get("estado") == "cumple":
            requirement.update(
                estado="requiere_revision",
                gap=f"Perfil registra S/ {capacidad:,.0f} de S/ {exigido:,.0f}.",
                accion="Verificar experiencia acreditable y bases.",
            )


def route_prod4_profiles_job(
    settings: Settings, repo: JobRepository, *, id_procedimiento: int, extraction_id: int
) -> dict[str, int]:
    """Route only validated, document-backed opportunities with a relevant profile fit."""
    rows = repo.conn.execute(
        """
        SELECT cp.id AS profile_id, p.opportunity_id,
               COALESCE(ar.enabled, true) AS enabled,
               COALESCE(ar.min_fit_level, 2) AS min_fit_level,
               COALESCE(ar.max_daily_evaluations, 0) AS max_daily_evaluations,
               COALESCE(ar.min_hours_before_close, 0) AS min_hours_before_close,
               fit.business_line_id, fit.fit_points, fit.fit_score, fit.fit_level,
               fit.keyword_hits, p.proposals_closes_at
        FROM prod4_processes p
        JOIN prod4_document_extractions e ON e.id_procedimiento = p.id_procedimiento
          AND e.id = %s AND e.is_current = true AND e.quality <> 'failed'
          AND e.requires_human_review = false AND e.facet_count > 0
        JOIN company_profiles cp ON cp.is_active = true
        LEFT JOIN automation_rules ar ON ar.profile_id = cp.id
        JOIN LATERAL profile_prod4_fit(cp.id, p.id_procedimiento) fit ON true
        WHERE p.id_procedimiento = %s AND p.technology_relevant = true
          AND p.missing_since IS NULL AND p.document_analysis_status = 'extracted'
          AND (p.proposals_closes_at IS NULL OR p.proposals_closes_at > now())
          AND fit.business_line_id IS NOT NULL
        ORDER BY fit.fit_level DESC, fit.fit_score DESC, cp.id
        """,
        (extraction_id, id_procedimiento),
    ).fetchall()
    stats = {"profiles": len(rows), "eligible": 0, "enqueued": 0, "limited": 0}
    for row in rows:
        if not row["enabled"] or int(row["fit_level"]) < max(2, int(row["min_fit_level"])):
            continue
        deadline = row["proposals_closes_at"]
        if deadline is not None:
            hours_left = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left < int(row["min_hours_before_close"]):
                continue
        stats["eligible"] += 1
        profile_id = str(row["profile_id"])
        daily_limit = int(row["max_daily_evaluations"] or settings.prod4_profile_evaluations_daily_default)
        repo.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('prod4_profile_eval'), hashtext(%s))",
            (profile_id,),
        )
        if daily_limit and _daily_prod4_evaluations(repo, profile_id) >= daily_limit:
            stats["limited"] += 1
            continue
        payload: dict[str, Any] = {
            "id_procedimiento": id_procedimiento,
            "extraction_id": extraction_id,
            "profile_id": profile_id,
            "business_line_id": str(row["business_line_id"]),
            "source": "automatic",
            "fit_points": int(row["fit_points"]),
            "fit_score": int(row["fit_score"]),
            "fit_level": int(row["fit_level"]),
            "keyword_hits": list(row["keyword_hits"] or []),
        }
        if repo.enqueue(
            "analyze_prod4_match", payload, queue_name="llm",
            dedup_key=f"analyze_prod4_match:{profile_id}:{id_procedimiento}:{extraction_id}",
            priority=5,
        ) is not None:
            stats["enqueued"] += 1
    logger.info("route_prod4_profiles_done", extra={"id_procedimiento": id_procedimiento, **stats})
    return stats


def _clip(value: object, limit: int) -> str | None:
    text = str(value).strip() if value else ""
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit].rsplit(' ', 1)[0]}…"


def analyze_prod4_match_job(
    settings: Settings,
    repo: JobRepository,
    *,
    id_procedimiento: int,
    extraction_id: int,
    profile_id: str,
    business_line_id: str | None = None,
    source: str = "worker",
    fit_points: int | None = None,
    fit_score: int | None = None,
    fit_level: int | None = None,
    keyword_hits: list[dict] | None = None,
    analyzer: MatchAnalyzer | None = None,
) -> dict[str, Any]:
    profile = repo.conn.execute(
        "SELECT * FROM company_profiles WHERE id = %s AND is_active = true",
        (profile_id,),
    ).fetchone()
    if profile is None:
        return {"skipped": "inactive_profile"}
    opportunity = repo.conn.execute(
        """SELECT p.opportunity_id, p.nomenclatura, p.description, p.buyer_name,
                  e.summary_json, e.sha256_original
           FROM prod4_processes p
           JOIN prod4_document_extractions e ON e.id_procedimiento = p.id_procedimiento
             AND e.id = %s AND e.is_current = true AND e.quality <> 'failed'
             AND e.requires_human_review = false AND e.facet_count > 0
           WHERE p.id_procedimiento = %s AND p.technology_relevant = true
             AND p.missing_since IS NULL AND p.document_analysis_status = 'extracted'
             AND (p.proposals_closes_at IS NULL OR p.proposals_closes_at > now())""",
        (extraction_id, id_procedimiento),
    ).fetchone()
    if opportunity is None:
        return {"skipped": "not_current_or_unvalidated"}
    facets = repo.conn.execute(
        """SELECT facet, label, required, details_json, facet_hash
           FROM prod4_requirement_facets
           WHERE id_procedimiento = %s AND extraction_id = %s AND is_current = true
           ORDER BY facet, id""",
        (id_procedimiento, extraction_id),
    ).fetchall()
    if not facets or not any(
        row["required"] and row["facet"] in {
            "economic_experience", "general_experience", "specific_experience",
            "key_personnel", "equipment", "insurance", "license",
            "company_certification", "education", "training",
            "professional_registration",
        }
        for row in facets
    ):
        return {"skipped": "insufficient_requirements"}
    facets_hash = canonical_hash(sorted(str(row["facet_hash"]) for row in facets))
    profile_hash = str(profile["profile_hash"] or "")
    existing = repo.conn.execute(
        """SELECT id, verdict, score, breakdown_json FROM opportunity_matches
           WHERE profile_id = %s AND opportunity_id = %s""",
        (profile_id, opportunity["opportunity_id"]),
    ).fetchone()
    previous = existing["breakdown_json"] if existing and isinstance(existing["breakdown_json"], dict) else {}
    previous_meta = previous.get("meta") or {}
    if (
        previous_meta.get("profile_hash") == profile_hash
        and previous_meta.get("facets_hash") == facets_hash
        and previous_meta.get("extraction_id") == extraction_id
    ):
        return {"skipped": "unchanged", "match_id": int(existing["id"])}

    requisitos = [
        {"tipo": row["facet"], "requisito": row["label"],
         "obligatorio": row["required"], "detalle": row["details_json"]}
        for row in facets if row["facet"] != "penalty_condition"
    ]
    analisis_previo = None
    if existing and previous.get("requisitos"):
        analisis_previo = {
            "veredicto": existing["verdict"], "score": existing["score"],
            "requisitos": [
                {key: item.get(key) for key in ("requisito", "categoria", "estado", "critico")}
                for item in previous["requisitos"] if isinstance(item, dict)
            ],
        }
    result = (analyzer or MatchAnalyzer(
        settings,
        prompt_filename="match_analysis_prod4_v1.txt",
        prompt_version=PROD4_MATCH_PROMPT_VERSION,
    )).analyze(
        perfil={field: profile[field] for field in PROFILE_FIELDS},
        oportunidad={
            "codigo": opportunity["nomenclatura"],
            "descripcion": opportunity["description"],
            "entidad": opportunity["buyer_name"],
            "resumen": opportunity["summary_json"],
        },
        requisitos=requisitos,
        analisis_previo=analisis_previo,
    )
    analysis = result.analysis
    requisitos_final = [item.model_dump() for item in analysis.requisitos]
    _guard_prod4_economic_claim(
        requisitos_final,
        exigido=_econ_exigido(facets),
        capacidad=_econ_capacity({field: profile[field] for field in PROFILE_FIELDS}),
    )
    verdict = _derive_verdict(requisitos_final)
    score = clamp_score(verdict, analysis.score)
    if analisis_previo is not None:
        def states(items: list[dict]) -> set[tuple[str, str]]:
            return {
                (str(item.get("requisito") or "").strip().lower(), str(item.get("estado") or ""))
                for item in items if item.get("requisito")
            }
        if states(requisitos_final) == states(analisis_previo["requisitos"]):
            verdict = str(existing["verdict"])
            score = clamp_score(verdict, int(existing["score"]))
    breakdown = {
        "resumen": _clip(analysis.resumen, 320),
        "requisitos": [
            {**item, "requisito": _clip(item.get("requisito"), 90) or "Requisito",
             "gap": _clip(item.get("gap"), 140), "accion": _clip(item.get("accion"), 110)}
            for item in requisitos_final
        ],
        "acciones_recomendadas": [
            clipped for clipped in (_clip(item, 110) for item in analysis.acciones_recomendadas[:4])
            if clipped
        ],
        "meta": {
            "source": "seace_prod4", "execution": source,
            "document_sha256": opportunity["sha256_original"],
            "extraction_id": extraction_id,
            "profile_hash": profile_hash, "facets_hash": facets_hash,
            "model": result.model, "prompt_version": result.prompt_version,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "fit_points": fit_points, "fit_score": fit_score, "fit_level": fit_level,
            "keyword_hits": keyword_hits or [],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    missing = [
        {"facet": item.get("categoria"), "label": _clip(item.get("requisito"), 90) or "Requisito",
         "estado": item.get("estado"), "accion": _clip(item.get("accion"), 110),
         "gap": _clip(item.get("gap"), 140), "critico": item.get("critico")}
        for item in requisitos_final if item.get("estado") != "cumple"
    ]
    row = repo.conn.execute(
        """INSERT INTO opportunity_matches (
             profile_id, opportunity_id, business_line_id, score, verdict,
             breakdown_json, missing_actions_json, matched_at, updated_at
           ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, now(), now())
           ON CONFLICT (profile_id, opportunity_id) DO UPDATE SET
             business_line_id = COALESCE(EXCLUDED.business_line_id, opportunity_matches.business_line_id),
             score = EXCLUDED.score, verdict = EXCLUDED.verdict,
             breakdown_json = EXCLUDED.breakdown_json,
             missing_actions_json = EXCLUDED.missing_actions_json,
             matched_at = now(), updated_at = now()
           RETURNING id""",
        (profile_id, opportunity["opportunity_id"], business_line_id, score, verdict,
         json.dumps(breakdown, ensure_ascii=False), json.dumps(missing, ensure_ascii=False)),
    ).fetchone()
    logger.info(
        "analyze_prod4_match_done",
        extra={"id_procedimiento": id_procedimiento, "profile_id": profile_id,
               "score": score, "verdict": verdict, "cost_usd": result.cost_usd},
    )
    return {"match_id": int(row["id"]), "score": score, "verdict": verdict}
