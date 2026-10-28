from __future__ import annotations

import json
import logging
from hashlib import sha256
from datetime import datetime, timezone

from buenapro_worker.jobs.send_notification import enqueue_match_notifications
from buenapro_worker.matching.analyzer import ANALYSIS_PROMPT_VERSION, MatchAnalyzer, read_analysis_prompt
from buenapro_worker.matching.profile_evidence import EVIDENCE_POLICY_VERSION, evidence_only_profile, guard_participation_actions
from buenapro_worker.matching.compact_context import COMPACT_PROFILE_VERSION, build_compact_match_profile
from buenapro_worker.matching.substantive_policy import split_match_requirements, bind_substantive_requirements, checklist_omitted_proposal_documents, guard_economic_comparisons
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)

# Evidence uncertainty does not itself cap capability at 49 points.
VERDICT_SCORE_BANDS = {
    "verde": (85, 100),
    "ambar": (50, 84),
    "gris": (0, 100),
    "rojo": (0, 29),
}


def clamp_score(verdict: str, score: int) -> int:
    low, high = VERDICT_SCORE_BANDS.get(verdict, (0, 100))
    return max(low, min(high, int(score)))


def _econ_capacity(profile: dict) -> float | None:
    values = []
    econ = profile.get("econ_experience_json") or {}
    if isinstance(econ, dict):
        if econ.get("no_global_sum") or any(e.get("documentary_summary", {}).get("sources") for e in profile.get("experience_json", []) if isinstance(e, dict)):
            return None
        # Area declarations are not interchangeable or additive capacity. A
        # specialty/currency/evidence comparison is required before arithmetic.
        if isinstance(econ.get("areas"), list) and econ["areas"]:
            return None
        for value in econ.values():
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
    return max(values) if values else 0.0


def _econ_exigido(facet_rows: list) -> float | None:
    amounts = []
    for row in facet_rows:
        if row["facet"] != "economic_experience":
            continue
        details = row["details_json"] or {}
        try:
            amounts.append(float(details.get("monto")))
        except (TypeError, ValueError):
            continue
    return max(amounts) if amounts else None


def _guard_area_economic_requirements(requisitos: list[dict]) -> None:
    for requirement in requisitos:
        if requirement.get("categoria") == "experiencia_economica":
            requirement.update(
                estado="requiere_revision",
                gap="La experiencia está desglosada por área; no hay un monto compatible acreditado automáticamente.",
                accion="Verificar rubro, especialidad, moneda y respaldo compatibles con la experiencia exigida en las bases.",
            )


def _apply_econ_rule(requisitos: list[dict], *, exigido: float | None, capacidad: float | None,
                     consorcio_status: str = "not_identified") -> None:
    """Regla determinista para experiencia económica: es aritmética, no juicio.
    Cubre todo -> cumple; >=30% -> accionable vía consorcio; <30% -> gap duro."""
    if capacidad is None:
        _guard_area_economic_requirements(requisitos)
        return
    if exigido is None or exigido <= 0:
        return
    ratio = capacidad / exigido
    for req in requisitos:
        if req.get("categoria") != "experiencia_economica":
            continue
        if ratio >= 1:
            req.update(estado="cumple", gap=None, accion=None)
        elif consorcio_status != "permitted":
            req.update(
                estado="requiere_revision" if consorcio_status != "prohibited" else "no_cumple",
                gap=f"Acreditas S/ {capacidad:,.0f} de S/ {exigido:,.0f} exigidos.",
                accion="Verificar autorización y condiciones en las bases" if consorcio_status != "prohibited"
                else "Acreditar experiencia propia exigida en las bases",
            )
        elif ratio >= 0.3:
            req.update(
                estado="cumple_con_accion",
                gap=f"Acreditas S/ {capacidad:,.0f} de S/ {exigido:,.0f} exigidos.",
                accion=f"Formar consorcio para cubrir S/ {exigido - capacidad:,.0f}.",
            )
        else:
            req.update(
                estado="no_cumple",
                gap=f"Acreditas S/ {capacidad:,.0f} de S/ {exigido:,.0f} exigidos (menos del 30%).",
                accion="Formar consorcio con un socio que cubra la mayor parte.",
            )


def _derive_verdict(requisitos: list[dict]) -> str:
    """Veredicto por reglas a partir de los estados por requisito (el LLM juzga
    cada requisito; la agregación es determinista)."""
    if not requisitos:
        return "gris"
    estados = [(str(req.get("estado")), bool(req.get("critico"))) for req in requisitos]
    if any(estado == "no_cumple" and critico for estado, critico in estados):
        return "rojo"
    if any(estado == "requiere_revision" and critico for estado, critico in estados):
        return "gris"
    if any(estado in {"cumple_con_accion", "no_cumple", "requiere_revision"} for estado, _ in estados):
        return "ambar"
    return "verde"

PROFILE_FIELDS = (
    "razon_social",
    "ruc",
    "identity_json",
    "team_json",
    "experience_json",
    "econ_experience_json",
    "equipment_json",
    "certifications_json",
    "hireable_roles_json",
)


def analyze_match_job(
    settings: Settings,
    repo: JobRepository,
    *,
    id_contrato: int,
    profile_id: str,
    force: bool = False,
    business_line_id: str | None = None,
    source: str = "worker",
    fit_points: int | None = None,
    fit_score: int | None = None,
    fit_level: int | None = None,
    keyword_hits: list[dict] | None = None,
    analyzer: MatchAnalyzer | None = None,
    notifications: bool = True,
    extra_meta: dict | None = None,
) -> dict[str, object]:
    """Evaluate one contract against one company.

    ``analyzer`` replaces the plain Gemini caller (the PROD6 runtime passes one with durable
    payment receipts). ``notifications=False`` creates neither notification rows nor
    ``send_notification`` jobs. ``extra_meta`` is merged into ``breakdown_json.meta``.
    """
    profile = repo.conn.execute(
        "SELECT * FROM company_profiles WHERE id = %s AND is_active = true",
        (profile_id,),
    ).fetchone()
    if profile is None:
        raise ValueError(f"Profile not found: {profile_id}")

    contract = repo.conn.execute(
        """
        SELECT c.codigo, c.descripcion, c.entidad_nombre, te.summary_json,
               te.id AS extraction_id, te.quality, te.requires_human_review
        FROM seace_contracts c
        LEFT JOIN LATERAL (
          SELECT tx.id, tx.summary_json, tx.quality, tx.requires_human_review
          FROM contract_documents d
          JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current = true
          WHERE d.id_contrato = c.id_contrato
          ORDER BY tx.created_at DESC
          LIMIT 1
        ) te ON true
        WHERE c.id_contrato = %s
        """,
        (id_contrato,),
    ).fetchone()
    if contract is None:
        raise ValueError(f"Contract not found: {id_contrato}")
    if not contract["extraction_id"] or contract["quality"] == "failed" or contract["requires_human_review"]:
        raise ValueError("Current valid documentary extraction required before LLM2")

    facet_rows = repo.conn.execute(
        """
        SELECT facet, label, required, details_json, facet_hash
        FROM requirement_facets
        WHERE id_contrato = %s AND extraction_id = %s AND is_current = true
        ORDER BY facet, id
        """,
        (id_contrato, contract["extraction_id"]),
    ).fetchall()

    facets_hash = canonical_hash(sorted(str(row["facet_hash"]) for row in facet_rows))
    profile_hash = str(profile["profile_hash"] or "")
    prompt_sha256 = sha256(read_analysis_prompt().encode()).hexdigest()

    existing = repo.conn.execute(
        "SELECT id, verdict, score, breakdown_json FROM matches WHERE profile_id = %s AND id_contrato = %s",
        (profile_id, id_contrato),
    ).fetchone()
    if existing is not None and not force:
        meta = (existing["breakdown_json"] or {}).get("meta") if isinstance(existing["breakdown_json"], dict) else None
        if (meta and meta.get("facets_hash") == facets_hash and meta.get("profile_hash") == profile_hash
                and meta.get("prompt_version") == ANALYSIS_PROMPT_VERSION
                and meta.get("prompt_sha256") == prompt_sha256
                and meta.get("evidence_policy_version") == EVIDENCE_POLICY_VERSION
                and meta.get("context_version") == COMPACT_PROFILE_VERSION
                and meta.get("semantic_guard_version") == "economic_comparison_guard_v1"
                and meta.get("extraction_id") == contract["extraction_id"]):
            if source == "automatic" and notifications:
                enqueue_match_notifications(
                    repo,
                    match_id=int(existing["id"]),
                    reason="automatic_evaluation",
                )
            logger.info("analyze_match_skipped_unchanged", extra={"id_contrato": id_contrato, "profile_id": profile_id})
            return {"skipped": "unchanged", "match_id": int(existing["id"])}

    requisitos, administrative_checklist = split_match_requirements(facet_rows)
    profile_evidence = evidence_only_profile({field: profile[field] for field in PROFILE_FIELDS})
    result = (analyzer or MatchAnalyzer(settings)).analyze(
        perfil=profile_evidence,
        oportunidad={
            "codigo": contract["codigo"],
            "descripcion": contract["descripcion"],
            "entidad": contract["entidad_nombre"],
            "resumen": contract["summary_json"],
        },
        requisitos=requisitos,
    )
    final_score, final_verdict, breakdown, missing = finish_match(
        result, requisitos=requisitos, administrative_checklist=administrative_checklist,
        profile_evidence=profile_evidence, summary_json=contract["summary_json"],
        meta={"prompt_sha256": prompt_sha256, "extraction_id": contract["extraction_id"],
              "profile_hash": profile_hash, "facets_hash": facets_hash},
    )
    breakdown["meta"].update(
        {
            "execution": "worker_auto" if source == "automatic" else source,
            "fit_points": fit_points,
            "fit_score": fit_score,
            "fit_level": fit_level,
            "keyword_hits": keyword_hits or [],
            **(extra_meta or {}),
        }
    )

    row = repo.conn.execute(
        """
        INSERT INTO matches (
          profile_id, id_contrato, business_line_id, score, verdict,
          breakdown_json, missing_actions_json, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, now())
        ON CONFLICT (profile_id, id_contrato)
        DO UPDATE SET
          business_line_id = COALESCE(EXCLUDED.business_line_id, matches.business_line_id),
          score = EXCLUDED.score,
          verdict = EXCLUDED.verdict,
          breakdown_json = EXCLUDED.breakdown_json,
          missing_actions_json = EXCLUDED.missing_actions_json,
          matched_at = now(),
          updated_at = now()
        RETURNING id
        """,
        (
            profile_id,
            id_contrato,
            business_line_id,
            final_score,
            final_verdict,
            json.dumps(breakdown, ensure_ascii=False),
            json.dumps(missing, ensure_ascii=False),
        ),
    ).fetchone()
    match_id = int(row["id"])

    repo.conn.execute(
        """
        INSERT INTO match_events (match_id,event_type,payload)
        VALUES (%s,%s,%s::jsonb)
        """,
        (
            match_id,
            "automatic_analysis" if source == "automatic" else "analysis",
            json.dumps({"score": final_score, "verdict": final_verdict, "source": source}),
        ),
    )

    if not notifications:
        pass
    elif source == "automatic":
        enqueue_match_notifications(repo, match_id=match_id, reason="automatic_evaluation")
    elif existing is None:
        enqueue_match_notifications(repo, match_id=match_id, reason="new_match")
    elif existing["verdict"] != final_verdict:
        enqueue_match_notifications(repo, match_id=match_id, reason="verdict_change")

    logger.info(
        "analyze_match_done",
        extra={
            "id_contrato": id_contrato,
            "profile_id": profile_id,
            "verdict": final_verdict,
            "score": final_score,
            "cost_usd": result.cost_usd,
        },
    )
    return {"match_id": match_id, "verdict": final_verdict, "score": final_score}


def _clip(text: object, limit: int) -> str | None:
    value = str(text).strip() if text else None
    if not value:
        return None
    return value if len(value) <= limit else f"{value[:limit].rsplit(' ', 1)[0]}…"


def finish_match(result, *, requisitos: list[dict], administrative_checklist: list, profile_evidence: dict,
                 summary_json: dict | None, meta: dict) -> tuple[int, str, dict, list]:
    """Score, verdict, breakdown and missing actions from one model answer.

    Shared by menores (``analyze_match_job``) and mayores (``prod2.evaluation``). ``meta`` carries
    the caller's provenance (prompt, extraction, profile and facet hashes) into ``breakdown.meta``.
    Raises ``ModelOutputMismatch`` when the answer skipped or reordered requirements.
    """
    analysis = result.analysis
    judged = [item.model_dump() for item in analysis.requisitos]
    requisitos = checklist_omitted_proposal_documents(judged, requisitos, administrative_checklist)
    requisitos_final = bind_substantive_requirements(
        judged, requisitos, build_compact_match_profile(profile_evidence),
    )
    validation_issues = guard_economic_comparisons(requisitos_final, requisitos, build_compact_match_profile(profile_evidence))
    validation_issues += [
        {"source_index": final["source_index"], "code": "unsupported_model_judgement"}
        for raw, final in zip(analysis.requisitos, requisitos_final, strict=True)
        if raw.estado != final["estado"] and not any(issue["source_index"] == final["source_index"] for issue in validation_issues)
    ]
    # A global/per-area amount is not compatible capacity. Keep grounded
    # comparisons and their limits; never substitute a max/sum or force every
    # documentary judgement back to review merely because areas exist.
    guarded_actions = guard_participation_actions(
        requisitos_final, list(analysis.acciones_recomendadas), summary_json,
    )

    # El veredicto se agrega por reglas desde los estados; el score del modelo
    # se acota a la banda del veredicto derivado.
    final_verdict = _derive_verdict(requisitos_final)
    final_score = clamp_score(final_verdict, analysis.score)

    # Truncado defensivo: el prompt exige brevedad, pero un modelo verboso no
    # debe romper la UI con parrafos.
    breakdown = {
        "resumen": ("La evaluación requiere contrastar experiencia y respaldos antes de publicar el puntaje. Revisa las condiciones pendientes indicadas abajo." if validation_issues else _clip(analysis.resumen, 320)),
        "administrative_checklist": administrative_checklist,
        "requisitos": [
            {
                **item,
                "requisito": _clip(item.get("requisito"), 90) or "Requisito",
                "gap": _clip(item.get("gap"), 140),
                "accion": _clip(item.get("accion"), 110),
            }
            for item in requisitos_final
        ],
        "acciones_recomendadas": [
            clipped for clipped in (_clip(item, 110) for item in guarded_actions[:4]) if clipped
        ],
        "meta": {
            "model": result.model,
            "evidence_policy_version": EVIDENCE_POLICY_VERSION,
            "prompt_version": result.prompt_version,
            "prompt_sha256": meta["prompt_sha256"],
            "context_version": COMPACT_PROFILE_VERSION,
            "extraction_id": meta["extraction_id"],
            "score_validated": not bool(validation_issues),
            "raw_model_score": analysis.score,
            "raw_model_verdict": analysis.veredicto,
            "raw_model_summary": analysis.resumen,
            "semantic_guard_version": "economic_comparison_guard_v1",
            "validation_issues": validation_issues,
            "profile_hash": meta["profile_hash"],
            "facets_hash": meta["facets_hash"],
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    missing = [
        {
            "facet": item.get("categoria"),
            "label": _clip(item.get("requisito"), 90) or "Requisito",
            "estado": item.get("estado"),
            "accion": _clip(item.get("accion"), 110),
            "gap": _clip(item.get("gap"), 140),
            "critico": item.get("critico"),
        }
        for item in requisitos_final
        if item.get("estado") != "cumple"
    ]
    strengths = [
        _clip(item.get("requisito"), 100)
        for item in requisitos_final
        if item.get("estado") == "cumple"
    ][:2]
    main_risk = next(
        (
            _clip(item.get("gap") or item.get("requisito"), 140)
            for item in requisitos_final
            if item.get("estado") != "cumple" and item.get("critico")
        ),
        None,
    ) or next(
        (
            _clip(item.get("gap") or item.get("requisito"), 140)
            for item in requisitos_final
            if item.get("estado") != "cumple"
        ),
        None,
    )
    breakdown["fortalezas"] = [item for item in strengths if item]
    breakdown["riesgo_principal"] = main_risk
    return final_score, final_verdict, breakdown, missing
