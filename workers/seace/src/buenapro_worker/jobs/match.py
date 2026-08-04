from __future__ import annotations

import logging
from typing import Any

from buenapro_worker.queue.repository import JobRepository


logger = logging.getLogger(__name__)


def route_contract_profiles_job(
    repo: JobRepository,
    *,
    id_contrato: int,
    profile_id: str | None = None,
) -> dict[str, int]:
    """Apply the cheap profile-aware gate after a TDR is validated.

    The PostgreSQL function is the same authoritative fit used by the product:
    level 2 means two visible dots (fit >= 60), not two numeric points.
    """
    rows = repo.conn.execute(
        """
        SELECT cp.id AS profile_id,
          COALESCE(ar.enabled, true) AS enabled,
          COALESCE(ar.min_fit_level, 2) AS min_fit_level,
          COALESCE(ar.max_daily_evaluations, 0) AS max_daily_evaluations,
          COALESCE(ar.min_hours_before_close, 0) AS min_hours_before_close,
          fit.business_line_id, fit.business_line_name, fit.keyword_points,
          fit.fit_points, fit.fit_score, fit.fit_level, fit.keyword_hits,
          c.fec_fin_cotizacion
        FROM seace_contracts c
        JOIN company_profiles cp ON cp.is_active = true
          AND (%s::uuid IS NULL OR cp.id = %s::uuid)
        LEFT JOIN automation_rules ar ON ar.profile_id = cp.id
        JOIN LATERAL profile_contract_fit(cp.id, c.id_contrato) fit ON true
        WHERE c.id_contrato = %s
          AND c.pipeline_state IN ('validated', 'normalized', 'matched')
          AND c.estado_codigo = 2
          AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion > now())
        ORDER BY fit.fit_level DESC, fit.fit_score DESC, cp.id
        """,
        (profile_id, profile_id, id_contrato),
    ).fetchall()

    stats = {"profiles": len(rows), "eligible": 0, "enqueued": 0, "limited": 0}
    for row in rows:
        if not row["enabled"] or int(row["fit_level"]) < int(row["min_fit_level"]):
            continue
        if row["fec_fin_cotizacion"] is not None:
            hours_left = repo.conn.execute(
                "SELECT extract(epoch FROM (%s::timestamptz - now())) / 3600 AS hours",
                (row["fec_fin_cotizacion"],),
            ).fetchone()
            if float(hours_left["hours"] or 0) < int(row["min_hours_before_close"]):
                continue

        stats["eligible"] += 1
        daily_limit = int(row["max_daily_evaluations"] or 0)
        if daily_limit and _automatic_jobs_today(repo, str(row["profile_id"])) >= daily_limit:
            stats["limited"] += 1
            continue

        payload: dict[str, Any] = {
            "id_contrato": id_contrato,
            "profile_id": str(row["profile_id"]),
            "business_line_id": str(row["business_line_id"]),
            "source": "automatic",
            "fit_points": int(row["fit_points"]),
            "fit_score": int(row["fit_score"]),
            "fit_level": int(row["fit_level"]),
            "keyword_hits": list(row["keyword_hits"] or []),
        }
        inserted = repo.enqueue(
            "analyze_match",
            payload,
            queue_name="llm",
            dedup_key=f"analyze_match:{row['profile_id']}:{id_contrato}",
            priority=4,
        )
        if inserted is not None:
            stats["enqueued"] += 1

    logger.info("route_contract_profiles_done", extra={"id_contrato": id_contrato, **stats})
    return stats


def enqueue_current_contract_sweep(
    repo: JobRepository,
    *,
    limit: int | None = None,
    profile_id: str | None = None,
) -> int:
    rows = repo.conn.execute(
        """
        SELECT c.id_contrato
        FROM seace_contracts c
        WHERE c.estado_codigo = 2
          AND c.pipeline_state IN ('validated', 'normalized', 'matched')
          AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion > now())
        ORDER BY c.fec_fin_cotizacion NULLS LAST, c.id_contrato
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    enqueued = 0
    for row in rows:
        inserted = repo.enqueue(
            "route_contract_profiles",
            {
                "id_contrato": int(row["id_contrato"]),
                "profile_id": profile_id,
                "source": "initial_sweep",
            },
            queue_name="match",
            dedup_key=f"route_contract_profiles:{profile_id or 'all'}:{row['id_contrato']}",
            priority=3,
        )
        enqueued += int(inserted is not None)
    return enqueued


def match_contract_job(repo: JobRepository, *, id_contrato: int) -> int:
    stats = route_contract_profiles_job(repo, id_contrato=id_contrato)
    return stats["enqueued"]


def match_profile_job(repo: JobRepository, *, profile_id: str) -> int:
    rows = repo.conn.execute(
        """
        SELECT DISTINCT c.id_contrato
        FROM seace_contracts c
        JOIN business_lines bl ON c.cubso_segmento = ANY(bl.cubso_segmentos)
        WHERE bl.profile_id = %s AND bl.is_active = true
          AND c.estado_codigo = 2
          AND c.pipeline_state IN ('validated', 'normalized', 'matched')
          AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion > now())
        ORDER BY c.id_contrato
        """,
        (profile_id,),
    ).fetchall()
    for row in rows:
        repo.enqueue(
            "route_contract_profiles",
            {"id_contrato": int(row["id_contrato"]), "profile_id": profile_id},
            queue_name="match",
            dedup_key=f"route_contract_profiles:{profile_id}:{row['id_contrato']}",
            priority=4,
        )
    logger.info("match_profile_routed", extra={"profile_id": profile_id, "contracts": len(rows)})
    return len(rows)


def _automatic_jobs_today(repo: JobRepository, profile_id: str) -> int:
    row = repo.conn.execute(
        """
        SELECT count(*) AS total
        FROM worker_jobs
        WHERE job_type = 'analyze_match'
          AND payload->>'source' = 'automatic'
          AND payload->>'profile_id' = %s
          AND created_at >= date_trunc('day', now())
        """,
        (profile_id,),
    ).fetchone()
    return int(row["total"] or 0)
