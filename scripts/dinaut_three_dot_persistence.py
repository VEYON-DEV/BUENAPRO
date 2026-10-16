"""Scoped publisher for the explicitly authorized DIN three-dot batch.

No provider calls, queue operations, source edits or notifications. The caller
owns the frozen manifest and durable private provider journal. A source_reader
must return a fresh case using the same selector as the frozen manifest.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from psycopg.pq import TransactionStatus

from buenapro_worker.jobs.analyze_match import PROFILE_FIELDS
from buenapro_worker.matching.readiness import match_readiness
from buenapro_worker.prod2.repository import claim_evaluation
from dinaut_batch_scope import iso_snapshot


class PublicationConflict(ValueError):
    """Frozen evidence or a prior publication changed; do not repay/retry."""


SOURCE_FIELDS = (
    "source_key", "source_id", "opportunity_id", "code", "description", "buyer_name",
    "closes_at", "business_line_id", "business_line_name", "keyword_points",
    "fit_points", "fit_level", "keyword_hits", "extraction_id", "document_sha256",
    "quality", "requires_human_review", "is_current", "raw_extraction_json",
    "summary_json", "facets",
)
SOURCES = {"seace_prod2", "seace_prod6"}


def _require_idle(conn: Any) -> None:
    info = getattr(conn, "info", None)
    if info is not None and info.transaction_status != TransactionStatus.IDLE:
        raise PublicationConflict("A preexisting transaction could hide a reservation or publication commit")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def profile_fingerprint(profile: dict) -> str:
    """Hash raw PROFILE_FIELDS, matching the input snapshot, not the DB MD5."""
    return fingerprint({key: profile.get(key) for key in PROFILE_FIELDS})


def source_fingerprint(case: dict) -> str:
    return fingerprint({key: case.get(key) for key in SOURCE_FIELDS})


def _source(case: dict) -> str:
    source = case.get("source_key")
    if source not in SOURCES:
        raise PublicationConflict("Unsupported batch source")
    if type(case.get("source_id")) is not int or case["source_id"] < 1:
        raise PublicationConflict("Invalid batch source identity")
    return source


def _ready(case: dict, expected_fit_level: int = 3) -> None:
    if type(expected_fit_level) is not int or expected_fit_level not in {2, 3}:
        raise PublicationConflict("Unsupported explicitly selected affinity scope")
    ready, _reason = match_readiness(case, case.get("facets") or [],
                                     provisional_source_review=True)
    if not ready or case.get("fit_level") != expected_fit_level:
        raise PublicationConflict("Case is no longer ready in the explicitly selected affinity scope")


def create_evaluation_before_dispatch(conn: Any, case: dict, *, profile_id: str,
        request_hash: str, model: str, policy_version: str, expected_fit_level: int = 3) -> dict | None:
    """PROD2 legacy kind, no model-name allowlist. Caller must COMMIT before I/O.

    PROD6 has no compatible reservation table: its caller must use the durable
    private journal. Never fabricate a PROD2 convocation for a minor contract.
    """
    if expected_fit_level != 3:
        _ready(case, expected_fit_level)
    if _source(case) == "seace_prod6":
        return None
    _ready(case, expected_fit_level)
    _require_idle(conn)
    return claim_evaluation(conn, case["source_id"], kind="gemini_match",
        request_hash=request_hash, model=model, policy_version=policy_version,
        profile_id=profile_id, lease_seconds=3600)


def _lock_source(conn: Any, case: dict) -> None:
    prod2 = _source(case) == "seace_prod2"
    parent, identity = ("prod2_discoveries", "id_convocatoria") if prod2 else ("seace_contracts", "id_contrato")
    # Parent FOR UPDATE also blocks FK-backed new child evidence until commit.
    if not conn.execute(f"SELECT {identity} FROM {parent} WHERE {identity}=%s FOR UPDATE",
                        (case["source_id"],)).fetchone():
        raise PublicationConflict("Source identity disappeared")
    table = "prod2_document_extractions" if prod2 else "tdr_extractions"
    if not conn.execute(f"SELECT id FROM {table} WHERE id=%s AND is_current FOR SHARE",
                        (case["extraction_id"],)).fetchone():
        raise PublicationConflict("Extraction is no longer current")
    facets = "prod2_requirement_facets" if prod2 else "requirement_facets"
    conn.execute(f"SELECT id FROM {facets} WHERE {identity}=%s AND extraction_id=%s AND is_current FOR SHARE",
                 (case["source_id"], case["extraction_id"])).fetchall()
    if prod2:
        if not conn.execute("SELECT id_convocatoria FROM prod2_details WHERE id_convocatoria=%s FOR SHARE",
                            (case["source_id"],)).fetchone():
            raise PublicationConflict("PROD2 detail snapshot disappeared")
        listed = conn.execute("""SELECT d.codigo_alfresco FROM prod2_documents d
            JOIN prod2_document_extractions e USING(id_convocatoria,codigo_alfresco)
            WHERE e.id=%s AND d.is_listed AND e.sha256_original=%s FOR SHARE OF d""",
            (case["extraction_id"], case["document_sha256"])).fetchone()
        if not listed:
            raise PublicationConflict("PROD2 document is no longer listed")
    else:
        document = conn.execute("""SELECT d.id FROM contract_documents d
            JOIN tdr_extractions e ON e.contract_document_id=d.id
            WHERE e.id=%s AND d.id_contrato=%s AND d.sha256_original=%s FOR SHARE OF d""",
            (case["extraction_id"], case["source_id"], case["document_sha256"])).fetchone()
        if not document:
            raise PublicationConflict("Minor-contract document revision changed")


def publish_case(conn: Any, case: dict, *, profile_id: str, tenant_id: str,
        expected_profile_fingerprint: str, score: int, verdict: str, breakdown: dict,
        missing_actions: list, source_reader: Callable[[Any, dict], dict | None],
        backup_callback: Callable[[dict], None], evaluation_id: int | None = None,
        expected_previous: dict | None = None, expected_provenance: dict | None = None,
        allowed_replacements: frozenset[tuple[str, int]] = frozenset(),
        expected_fit_level: int = 3, event_type: str = "dinaut_three_dot_analysis") -> dict:
    """Publish atomically after paid receipts exist, with evidence and match CAS.

    backup_callback must durably fsync a private full-row snapshot before it
    returns, including an explicit absent-row marker for new publications.
    A callback failure aborts publication. No remote I/O belongs in this call.
    Use a dictionary-row connection and no preexisting caller transaction.
    """
    source = _source(case)
    _ready(case, expected_fit_level)
    if expected_fit_level == 2:
        if (event_type != "dinaut_two_dot_50_analysis" or allowed_replacements or expected_previous is not None
                or case.get("existing_match_id") is not None):
            raise PublicationConflict("The two-dot cohort authorizes only new matches without replacement")
    _require_idle(conn)
    if type(score) is not int or not 0 <= score <= 100 or verdict not in {"verde", "ambar", "rojo", "gris"}:
        raise PublicationConflict("Invalid provisional score or verdict")
    if not isinstance(breakdown, dict) or not isinstance(missing_actions, list):
        raise PublicationConflict("Invalid publication JSON")
    meta = breakdown.get("meta")
    if not isinstance(meta, dict) or meta.get("score_validated") is not False:
        raise PublicationConflict("This batch requires an explicitly provisional score")
    if type(meta.get("source_review_required")) is not bool or meta["source_review_required"] != case["requires_human_review"]:
        raise PublicationConflict("Source review flag was not preserved")
    # Validate finite JSON before writing a backup or opening the transaction.
    fingerprint(breakdown)
    fingerprint(missing_actions)
    prod2 = source == "seace_prod2"
    table, identity, match_identity = ("opportunity_matches", "opportunity_id", case["opportunity_id"]) if prod2 else ("matches", "id_contrato", case["source_id"])
    with conn.transaction():
        profile = conn.execute("SELECT * FROM company_profiles WHERE id=%s AND tenant_id=%s AND is_active FOR UPDATE",
                               (profile_id, tenant_id)).fetchone()
        if expected_fit_level==2:
            profile=iso_snapshot(profile)
        if not profile or profile_fingerprint(profile) != expected_profile_fingerprint:
            raise PublicationConflict("Company input snapshot changed")
        # Existing keyword edits are independent of company_profiles.profile_hash.
        # The profile parent lock excludes new FK-backed lines while these locks
        # freeze edits/deletions before the canonical affinity selector runs.
        conn.execute("SELECT id FROM business_lines WHERE profile_id=%s FOR SHARE",
                     (profile_id,)).fetchall()
        _lock_source(conn, case)
        live = source_reader(conn, case)
        if not live or source_fingerprint(live) != source_fingerprint(case):
            raise PublicationConflict("Source, affinity or documentary snapshot changed")
        _ready(live, expected_fit_level)
        if case.get("business_line_id") is not None and not conn.execute(
                "SELECT id FROM business_lines WHERE id=%s AND profile_id=%s AND is_active FOR SHARE",
                (case["business_line_id"], profile_id)).fetchone():
            raise PublicationConflict("Business line does not belong to the active profile")
        previous = conn.execute(f"SELECT * FROM {table} WHERE profile_id=%s AND {identity}=%s FOR UPDATE",
                                (profile_id, match_identity)).fetchone()
        provenance = None
        if previous and prod2:
            provenance = conn.execute("SELECT * FROM prod2_match_provenance WHERE match_id=%s FOR UPDATE",
                                      (previous["id"],)).fetchone()
        if fingerprint(previous) != fingerprint(expected_previous):
            raise PublicationConflict("Previous match changed or appeared concurrently")
        if prod2 and fingerprint(provenance) != fingerprint(expected_provenance):
            raise PublicationConflict("Previous match provenance changed")
        if previous and (source, int(previous["id"])) not in allowed_replacements:
            raise PublicationConflict("Replacing this existing match is outside the frozen allowlist")
        payload = {**breakdown, "meta": {**meta,
            "source_key": source, "extraction_id": case["extraction_id"],
            "document_sha256": case["document_sha256"],
            "profile_hash": expected_profile_fingerprint}}
        if prod2:
            reservation = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s FOR UPDATE",
                                       (evaluation_id,)).fetchone()
            if not reservation or (reservation["id_convocatoria"], reservation["kind"], reservation["status"], str(reservation["profile_id"])) != (case["source_id"], "gemini_match", "done", profile_id):
                raise PublicationConflict("Missing completed PROD2 profile reservation")
            binding = reservation.get("result_json") or {}
            if (binding.get("extraction_id"), binding.get("profile_hash")) != (case["extraction_id"], expected_profile_fingerprint):
                raise PublicationConflict("Reservation does not bind these paid inputs")
            payload["meta"].update(id_convocatoria=case["source_id"], evaluation_id=evaluation_id,
                                    prompt_version=reservation["policy_version"])
        else:
            payload["meta"]["id_contrato"] = case["source_id"]
        backup_callback({"source_key": source, "source_id": case["source_id"],
            "profile_id": profile_id, "previous_match": previous,
            "previous_provenance": provenance, "source_fingerprint": source_fingerprint(case),
            "profile_fingerprint": expected_profile_fingerprint,
            "new_publication_sha256": fingerprint({"score": score, "verdict": verdict,
                "breakdown": payload, "missing_actions": missing_actions})})
        args = (case.get("business_line_id"), score, verdict,
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
                json.dumps(missing_actions, ensure_ascii=False, allow_nan=False))
        if previous:
            row = conn.execute(f"""UPDATE {table} SET business_line_id=%s,score=%s,verdict=%s,
                breakdown_json=%s::jsonb,missing_actions_json=%s::jsonb,matched_at=now(),updated_at=now()
                WHERE id=%s AND profile_id=%s RETURNING id""",
                (*args, previous["id"], profile_id)).fetchone()
        else:
            row = conn.execute(f"""INSERT INTO {table}(profile_id,{identity},business_line_id,score,
                verdict,breakdown_json,missing_actions_json,matched_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,now(),now())
                ON CONFLICT(profile_id,{identity}) DO NOTHING RETURNING id""",
                (profile_id, match_identity, *args)).fetchone()
        if not row:
            raise PublicationConflict("Concurrent publication won; paid response retained")
        match_id = int(row["id"])
        if prod2:
            conn.execute("""INSERT INTO prod2_match_provenance(match_id,id_convocatoria,extraction_id,evaluation_id,profile_hash)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(match_id) DO UPDATE SET
                id_convocatoria=EXCLUDED.id_convocatoria,extraction_id=EXCLUDED.extraction_id,
                evaluation_id=EXCLUDED.evaluation_id,profile_hash=EXCLUDED.profile_hash,matched_at=now()""",
                (match_id, case["source_id"], case["extraction_id"], evaluation_id, expected_profile_fingerprint))
        else:
            conn.execute("INSERT INTO match_events(match_id,event_type,payload) VALUES(%s,%s,%s::jsonb)",
                (match_id, event_type, json.dumps({"score": score,"verdict": verdict,
                    "source": source,"extraction_id": case["extraction_id"],
                    "profile_hash": expected_profile_fingerprint}, ensure_ascii=False)))
    return {"match_id": match_id, "replaced": previous is not None,
            "source_key": source, "source_id": case["source_id"], "score": score,
            "score_validated": False}
