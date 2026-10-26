"""Automatic evaluation (LLM2) of the contracts JEV accepted for a PROD6 company.

A (contract, company) pair is evaluated automatically only when the company's current JEV
decision for that contract is ``accepted`` (``prod6_profile_decisions``; JEV replaces the keyword
gate of the legacy ``route_contract_profiles``), the company's ``automation_rules`` allow it, the
contract is open and its current reading is valid and not under review. Everything else stays
available as a manual evaluation from the web.

``enqueue_evaluations`` is the only producer of ``analyze_match`` jobs in the PROD6 runtime: the
hook after ``derive_summary`` and the ``backfill_evaluations`` command both call it, and
``ScopedRepository.enqueue`` accepts no other origin.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from buenapro_worker.jobs.match import _automatic_jobs_today
from buenapro_worker.prod6.screening import POLICY_VERSION

AUTO_ORIGIN = "prod6_jev_accepted_v1"

# Newest decision of each contract x company that is not 'superseded' (accepted and rejected rows
# are history; a newer row of another card/profile hash replaces them). The current reading is the
# one analyze_match uses: the latest current extraction of the contract.
ROWS_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (d.id_contrato, d.profile_id)
         d.id AS decision_id, d.id_contrato, d.profile_id, d.decision, d.decided_by, d.score,
         d.keyword_lines
  FROM prod6_profile_decisions d
  WHERE d.policy_version = %(policy)s AND d.profile_id = ANY(%(profiles)s::uuid[])
    AND d.decision <> 'superseded'
    AND (%(contract)s::bigint IS NULL OR d.id_contrato = %(contract)s::bigint)
  ORDER BY d.id_contrato, d.profile_id, d.id DESC
)
SELECT l.decision_id, l.id_contrato, l.profile_id, l.decision, l.decided_by, l.score, l.keyword_lines,
       c.codigo, c.estado_codigo, c.objeto_codigo, c.fec_fin_cotizacion, c.cubso_segmento,
       (c.estado_codigo = 2 AND c.objeto_codigo IN (1, 2)
        AND (c.fec_fin_cotizacion IS NULL OR c.fec_fin_cotizacion > now())) AS contract_open,
       extract(epoch FROM (c.fec_fin_cotizacion - now())) / 3600 AS hours_left,
       (cp.id IS NOT NULL) AS profile_active,
       COALESCE(ar.enabled, true) AS rule_enabled,
       COALESCE(ar.max_daily_evaluations, 0) AS max_daily,
       COALESCE(ar.min_hours_before_close, 0) AS min_hours,
       te.extraction_id, te.quality, te.requires_human_review,
       (SELECT count(*) FROM requirement_facets f WHERE f.id_contrato = l.id_contrato
          AND f.extraction_id = te.extraction_id AND f.is_current) AS facets,
       m.id AS match_id, m.breakdown_json->'meta'->>'extraction_id' AS match_extraction_id
FROM latest l
JOIN seace_contracts c ON c.id_contrato = l.id_contrato
LEFT JOIN company_profiles cp ON cp.id = l.profile_id AND cp.is_active
LEFT JOIN automation_rules ar ON ar.profile_id = l.profile_id
LEFT JOIN LATERAL (
  SELECT tx.id AS extraction_id, tx.quality, tx.requires_human_review
  FROM contract_documents d
  JOIN tdr_extractions tx ON tx.contract_document_id = d.id AND tx.is_current
  WHERE d.id_contrato = c.id_contrato
  ORDER BY tx.created_at DESC LIMIT 1
) te ON true
LEFT JOIN matches m ON m.profile_id = l.profile_id AND m.id_contrato = l.id_contrato
WHERE (NOT %(only_accepted)s OR (l.decision = 'accepted' AND l.decided_by = 'jev'))
ORDER BY c.fec_fin_cotizacion NULLS LAST, l.id_contrato, l.profile_id
"""

BUSINESS_LINE_SQL = """
SELECT bl.id FROM business_lines bl JOIN seace_contracts c ON c.id_contrato = %s
WHERE bl.profile_id = %s AND bl.is_active AND c.cubso_segmento = ANY(bl.cubso_segmentos)
ORDER BY (bl.nombre = ANY(%s)) DESC, bl.nombre LIMIT 1
"""


def evaluation_rows(conn, *, profile_ids: list[str], contract_id: int | None = None,
                    only_accepted: bool = False) -> list[dict[str, Any]]:
    return conn.execute(ROWS_SQL, {"policy": POLICY_VERSION, "profiles": list(profile_ids),
                                   "contract": contract_id, "only_accepted": only_accepted}).fetchall()


def skip_reason(row: dict[str, Any], *, include_stale: bool = False) -> str | None:
    """Why this pair is not evaluated, or None. Free: no job and no request is made here.

    ``include_stale`` also accepts a pair whose match was made from an older reading of the
    document (the document changed); a match of unknown origin is never overwritten.
    """
    if row["decision"] != "accepted" or row["decided_by"] != "jev":
        return "decision_not_accepted"
    if not row["profile_active"]:
        return "profile_inactive"
    if not row["contract_open"]:
        return "contract_closed"
    if not row["rule_enabled"]:
        return "rule_disabled"
    if row["extraction_id"] is None:
        return "reading_missing"
    if row["quality"] == "failed":
        return "reading_failed"
    if row["requires_human_review"]:
        return "reading_requires_review"
    if not row["facets"]:
        return "no_requirements"
    if row["match_id"] is not None:
        known = row["match_extraction_id"]
        if known == str(row["extraction_id"]):
            return "match_current"
        if not (include_stale and known):
            return "match_exists"
    if row["hours_left"] is not None and float(row["hours_left"]) < int(row["min_hours"]):
        return "too_close_to_close"
    return None


def queued_job_exists(conn, dedup_key: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM worker_jobs WHERE dedup_key = %s AND status IN ('pending', 'claimed') LIMIT 1",
        (dedup_key,)).fetchone())


def enqueue_evaluations(repo, *, profile_ids: list[str], contract_id: int | None = None,
                        only_accepted: bool = False, include_stale: bool = False, apply: bool = True,
                        limit: int | None = None) -> dict[str, Any]:
    """Queue ``analyze_match`` for every pair that passes ``skip_reason`` and the daily limit.

    ``repo`` is the runtime's ``ScopedRepository``. With ``apply=False`` nothing is written:
    ``planned`` lists what would be queued (``already_queued`` ones excluded). ``limit`` caps the
    pairs queued in this call.
    """
    rows = evaluation_rows(repo.conn, profile_ids=profile_ids, contract_id=contract_id,
                           only_accepted=only_accepted)
    skipped: Counter[str] = Counter()
    stats: dict[str, Any] = {"candidates": len(rows), "enqueued": 0, "already_queued": 0,
                             "daily_limited": 0, "planned": [], "skipped": skipped}
    today: Counter[str] = Counter()  # Pairs planned in this call, per company, for the daily limit.
    for row in rows:
        reason = skip_reason(row, include_stale=include_stale)
        if reason:
            skipped[reason] += 1
            continue
        if limit is not None and stats["enqueued"] + len(stats["planned"]) >= limit:
            skipped["call_limit"] += 1
            continue
        profile = str(row["profile_id"])
        daily = int(row["max_daily"] or 0)  # 0 means no daily limit.
        if daily and _automatic_jobs_today(repo, profile) + today[profile] >= daily:
            stats["daily_limited"] += 1
            continue
        dedup_key = f"analyze_match:{profile}:{row['id_contrato']}"
        line = repo.conn.execute(BUSINESS_LINE_SQL, (row["id_contrato"], profile,
                                                     list(row["keyword_lines"] or []))).fetchone()
        item = {"id_contrato": int(row["id_contrato"]), "profile_id": profile}
        if apply:
            job_id = repo.enqueue("analyze_match", {
                **item, "business_line_id": str(line["id"]) if line else None,
                "source": "automatic", "origin": AUTO_ORIGIN, "decision_id": int(row["decision_id"]),
                "jev_score": float(row["score"]) if row["score"] is not None else None,
                "extraction_id": int(row["extraction_id"])},
                queue_name="llm", dedup_key=dedup_key, priority=4)
            if job_id is None:
                stats["already_queued"] += 1
            else:
                stats["enqueued"] += 1  # The database now counts the job against the daily limit.
        elif queued_job_exists(repo.conn, f"{repo.dedup_prefix()}{dedup_key}"):
            stats["already_queued"] += 1
        else:
            today[profile] += 1
            stats["planned"].append({**item, "reason": "no_match" if row["match_id"] is None
                                     else "reading_changed", "close": row["fec_fin_cotizacion"],
                                     "score": row["score"]})
    stats["skipped"] = dict(skipped)
    return stats
