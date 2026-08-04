from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from buenapro_worker.jobs.send_notification import enqueue_match_notifications
from buenapro_worker.matching.analyzer import MatchAnalyzer
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)

# El score debe vivir dentro de la banda de su veredicto; el prompt lo pide,
# pero el clamp lo garantiza (un "rojo 35" contradice al semaforo).
VERDICT_SCORE_BANDS = {
    "verde": (85, 100),
    "ambar": (50, 84),
    "gris": (30, 49),
    "rojo": (0, 29),
}


def clamp_score(verdict: str, score: int) -> int:
    low, high = VERDICT_SCORE_BANDS.get(verdict, (0, 100))
    return max(low, min(high, int(score)))


def _econ_capacity(profile: dict) -> float:
    values = []
    econ = profile.get("econ_experience_json") or {}
    if isinstance(econ, dict):
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


def _apply_econ_rule(requisitos: list[dict], *, exigido: float | None, capacidad: float) -> None:
    """Regla determinista para experiencia económica: es aritmética, no juicio.
    Cubre todo -> cumple; >=30% -> accionable vía consorcio; <30% -> gap duro."""
    if exigido is None or exigido <= 0:
        return
    ratio = capacidad / exigido
    for req in requisitos:
        if req.get("categoria") != "experiencia_economica":
            continue
        if ratio >= 1:
            req.update(estado="cumple", gap=None, accion=None)
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
) -> dict[str, object]:
    profile = repo.conn.execute(
        "SELECT * FROM company_profiles WHERE id = %s AND is_active = true",
        (profile_id,),
    ).fetchone()
    if profile is None:
        raise ValueError(f"Profile not found: {profile_id}")

    contract = repo.conn.execute(
        """
        SELECT c.codigo, c.descripcion, c.entidad_nombre, te.summary_json
        FROM seace_contracts c
        LEFT JOIN LATERAL (
          SELECT tx.summary_json
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

    facet_rows = repo.conn.execute(
        """
        SELECT facet, label, required, details_json, facet_hash
        FROM requirement_facets
        WHERE id_contrato = %s AND is_current = true
        ORDER BY facet, id
        """,
        (id_contrato,),
    ).fetchall()

    facets_hash = canonical_hash(sorted(str(row["facet_hash"]) for row in facet_rows))
    profile_hash = str(profile["profile_hash"] or "")

    existing = repo.conn.execute(
        "SELECT id, verdict, score, breakdown_json FROM matches WHERE profile_id = %s AND id_contrato = %s",
        (profile_id, id_contrato),
    ).fetchone()
    if existing is not None and not force:
        meta = (existing["breakdown_json"] or {}).get("meta") if isinstance(existing["breakdown_json"], dict) else None
        if meta and meta.get("facets_hash") == facets_hash and meta.get("profile_hash") == profile_hash:
            if source == "automatic":
                enqueue_match_notifications(
                    repo,
                    match_id=int(existing["id"]),
                    reason="automatic_evaluation",
                )
            logger.info("analyze_match_skipped_unchanged", extra={"id_contrato": id_contrato, "profile_id": profile_id})
            return {"skipped": "unchanged", "match_id": int(existing["id"])}

    requisitos = [
        {
            "tipo": row["facet"],
            "requisito": row["label"],
            "obligatorio": row["required"],
            "detalle": row["details_json"],
        }
        for row in facet_rows
        # Las penalidades no son requisitos del postor; el prompt tampoco las evalua.
        if row["facet"] != "penalty_condition"
    ]

    def _clip(text: object, limit: int) -> str | None:
        value = str(text).strip() if text else None
        if not value:
            return None
        return value if len(value) <= limit else f"{value[:limit].rsplit(' ', 1)[0]}…"

    # Re-analisis incremental: el analisis previo va como base para que el
    # modelo mantenga estados/score estables y solo ajuste lo que cambio.
    analisis_previo = None
    if existing is not None and isinstance(existing["breakdown_json"], dict):
        previous_reqs = existing["breakdown_json"].get("requisitos")
        if previous_reqs:
            analisis_previo = {
                "veredicto": existing["verdict"],
                "score": existing["score"],
                "requisitos": [
                    {key: item.get(key) for key in ("requisito", "categoria", "estado", "critico")}
                    for item in previous_reqs
                    if isinstance(item, dict)
                ],
            }

    result = MatchAnalyzer(settings).analyze(
        perfil={field: profile[field] for field in PROFILE_FIELDS},
        oportunidad={
            "codigo": contract["codigo"],
            "descripcion": contract["descripcion"],
            "entidad": contract["entidad_nombre"],
            "resumen": contract["summary_json"],
        },
        requisitos=requisitos,
        analisis_previo=analisis_previo,
    )
    analysis = result.analysis
    requisitos_final = [item.model_dump() for item in analysis.requisitos]

    # La comparación económica es aritmética: se corrige por regla, no por LLM.
    _apply_econ_rule(
        requisitos_final,
        exigido=_econ_exigido(facet_rows),
        capacidad=_econ_capacity({field: profile[field] for field in PROFILE_FIELDS}),
    )

    # El veredicto se agrega por reglas desde los estados; el score del modelo
    # se acota a la banda del veredicto derivado.
    final_verdict = _derive_verdict(requisitos_final)
    final_score = clamp_score(final_verdict, analysis.score)

    # Ancla determinista: si ningun requisito cambio de estado respecto al
    # analisis previo, el veredicto y score anteriores se mantienen tal cual
    # (la variabilidad del modelo no debe mover el numero sin causa).
    if analisis_previo is not None:
        def _states(items: list[dict]) -> set[tuple[str, str]]:
            result_set: set[tuple[str, str]] = set()
            for data in items:
                requisito = str(data.get("requisito") or "").strip().lower()
                estado = str(data.get("estado") or "")
                if requisito:
                    result_set.add((requisito, estado))
            return result_set

        if _states(requisitos_final) == _states(analisis_previo["requisitos"]):
            final_verdict = str(existing["verdict"])
            final_score = clamp_score(final_verdict, int(existing["score"]))

    # Truncado defensivo: el prompt exige brevedad, pero un modelo verboso no
    # debe romper la UI con parrafos.
    breakdown = {
        "resumen": _clip(analysis.resumen, 320),
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
            clipped for clipped in (_clip(item, 110) for item in analysis.acciones_recomendadas[:4]) if clipped
        ],
        "meta": {
            "model": result.model,
            "prompt_version": result.prompt_version,
            "profile_hash": profile_hash,
            "facets_hash": facets_hash,
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
    breakdown["meta"].update(
        {
            "execution": "worker_auto" if source == "automatic" else source,
            "fit_points": fit_points,
            "fit_score": fit_score,
            "fit_level": fit_level,
            "keyword_hits": keyword_hits or [],
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

    if source == "automatic":
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
