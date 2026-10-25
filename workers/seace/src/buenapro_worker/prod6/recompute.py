"""Recompute summary_json and the review flag of stored readings, without Gemini.

Local derivation only: it reads ``raw_extraction_json`` and the contract title, applies the same
``build_reading_summary`` as ``derive_summary_job`` and rewrites ``tdr_extractions.summary_json``,
``requires_human_review`` and the matching ``contract_filter_index`` columns. It never calls
Gemini, SEACE or JEV, never enqueues jobs and never touches ``raw_extraction_json``. It is
idempotent (a second run changes nothing) and dry-run by default.

    DATABASE_URL=... python -m buenapro_worker.prod6.recompute --since 2026-10-25          # dry run
    DATABASE_URL=... python -m buenapro_worker.prod6.recompute --since 2026-10-25 --apply
    DATABASE_URL=... python -m buenapro_worker.prod6.recompute --id 2298 --id 2303 --apply
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod6.review import build_reading_summary

SELECT_CURRENT = """
    SELECT e.id, e.raw_extraction_json, e.summary_json, e.prompt_version, e.quality,
           e.requires_human_review, c.id_contrato, c.codigo, c.descripcion
    FROM tdr_extractions e
    JOIN contract_documents d ON d.id = e.contract_document_id
    JOIN seace_contracts c ON c.id_contrato = d.id_contrato
    WHERE e.is_current AND e.quality <> 'failed'
      AND (%(all)s OR e.id = ANY(%(ids)s) OR e.created_at >= %(since)s)
    ORDER BY e.id
"""
UPDATE_EXTRACTION = """
    UPDATE tdr_extractions SET summary_json = %s::jsonb, requires_human_review = %s WHERE id = %s
"""
# Only an existing index row is refreshed; recomputing never creates one.
UPDATE_FILTER_INDEX = """
    UPDATE contract_filter_index
    SET plazo_ejecucion_dias = %s, tipo_pago = %s, penalidad_tope_pct = %s,
        entregables_count = %s, roles_requeridos = %s, facets = %s, documentos_clave = %s,
        updated_at = now()
    WHERE id_contrato = %s
"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def recompute_readings(conn: psycopg.Connection, *, ids: list[int] | None = None,
                       since: datetime | None = None, everything: bool = False,
                       apply: bool = False) -> dict[str, Any]:
    """Recompute the selected current readings; write only when ``apply`` is true.

    The caller owns the transaction: nothing is committed here.
    """
    if not (ids or since or everything):
        raise ValueError("Select readings with ids, since or everything")
    rows = conn.execute(SELECT_CURRENT, {
        "all": everything, "ids": ids or [],
        "since": since or datetime(9999, 1, 1, tzinfo=timezone.utc)}).fetchall()
    reasons: Counter[str] = Counter()
    modalities: Counter[str] = Counter()
    report: dict[str, Any] = {"mode": "apply" if apply else "dry_run", "selected": len(rows),
                              "changed": 0, "unchanged": 0, "flagged": 0, "newly_flagged": 0,
                              "no_longer_flagged": 0, "term_filled": 0, "readings": []}
    for row in rows:
        summary, flag = build_reading_summary(
            row["raw_extraction_json"], contract_title=row["descripcion"],
            prompt_version=row["prompt_version"], previous_summary=row["summary_json"],
            previous_flag=bool(row["requires_human_review"]), quality=row["quality"])
        old = row["summary_json"] if isinstance(row["summary_json"], dict) else {}
        changed = _canonical(summary) != _canonical(old) or flag != bool(row["requires_human_review"])
        report["changed" if changed else "unchanged"] += 1
        report["flagged"] += int(flag)
        report["newly_flagged"] += int(flag and not row["requires_human_review"])
        report["no_longer_flagged"] += int(row["requires_human_review"] and not flag)
        report["term_filled"] += int(old.get("plazo_ejecucion_dias") is None
                                     and summary.get("plazo_ejecucion_dias") is not None)
        modalities[summary["modalidad"]] += 1
        for reason in summary["review_reasons"]:
            reasons[reason["code"]] += 1
        report["readings"].append({
            "extraction_id": row["id"], "contract": row["codigo"], "changed": changed,
            "requires_human_review": flag, "was_flagged": bool(row["requires_human_review"]),
            "modalidad": summary["modalidad"], "plazo_ejecucion_dias": summary.get("plazo_ejecucion_dias"),
            "reasons": [reason["code"] for reason in summary["review_reasons"]]})
        if apply and changed:
            conn.execute(UPDATE_EXTRACTION, (json.dumps(summary, ensure_ascii=False), flag, row["id"]))
            conn.execute(UPDATE_FILTER_INDEX, (
                summary.get("plazo_ejecucion_dias"), summary.get("tipo_pago"),
                summary.get("penalidad_tope_pct"), summary.get("entregables_count"),
                summary.get("roles_requeridos") or [], summary.get("requirement_facets") or [],
                summary.get("documentos_clave") or [], row["id_contrato"]))
    report["reasons"] = dict(sorted(reasons.items()))
    report["modalidad"] = dict(sorted(modalities.items()))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, action="append", dest="ids", help="tdr_extractions.id (repeatable)")
    parser.add_argument("--since", type=lambda value: datetime.fromisoformat(value).replace(
        tzinfo=timezone.utc), help="Current readings created on/after this date (UTC), e.g. 2026-10-25")
    parser.add_argument("--all", action="store_true", dest="everything", help="Every current reading")
    parser.add_argument("--apply", action="store_true", help="Write the result (default: dry run)")
    parser.add_argument("--summary-only", action="store_true", help="Omit the per-reading list")
    args = parser.parse_args(argv)
    if not (args.ids or args.since or args.everything):
        parser.error("choose readings with --id, --since or --all")
    url = os.environ.get("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL is not set")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        report = recompute_readings(conn, ids=args.ids, since=args.since,
                                    everything=args.everything, apply=args.apply)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    if args.summary_only:
        report.pop("readings")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
