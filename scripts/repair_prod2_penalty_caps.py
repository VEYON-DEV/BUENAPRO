"""Repair only derived penalty caps in the frozen PROD2 pending90 completion.

Dry-run is the default and uses PostgreSQL READ ONLY. Explicit --execute updates
one JSON key under the ingestion coordinator; no source, model, Redis or R2 calls.
Paid responses, raw extraction, facets, participation and the prior ten are frozen.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_prod2_pending as audit

POLICY = "prod2_exact_pending90_penalty_cap_only_v1"
CAP = "penalidad_tope_pct"


def receipt_fingerprint(local):
    """Hashes only; never publish paid evidence or personal document contents."""
    roots = list(dict.fromkeys([local["source_folder"], *local["recovery_roots"], *local["retry_roots"]]))
    values = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*.private.json")):
            audit.require(not path.is_symlink(), "repair_private_receipt_symlink")
            values[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return audit.fingerprint(values)


def require_idle(conn, ids, *, owns_coordinator):
    row = conn.execute("""SELECT
        (SELECT count(*) FROM prod2_evaluations WHERE id_convocatoria=ANY(%s) AND status='running') +
        (SELECT count(*) FROM prod2_document_selection_cache WHERE convocation_id=ANY(%s) AND status='running') +
        (SELECT count(*) FROM prod2_jev_decision_cache WHERE convocation_id=ANY(%s) AND status='running') AS n""", (ids, ids, ids)).fetchone()
    audit.require(row["n"] == 0, "repair_provider_still_running")
    if not owns_coordinator:
        active = conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
            AND classid=(hashtext('prod2_accepted_executor')::bigint & 4294967295)::oid
            AND objid=0 AND objsubid=2 AND granted) AS active""").fetchone()["active"]
        audit.require(active is False, "repair_coordinator_still_running")


def snapshot(conn, local, prior_folder, *, lock=False):
    prior = audit.prior_snapshot(conn, local["prior_ids"], prior_folder)
    audit.require(audit.fingerprint(prior) == audit.fingerprint(local["baseline"]["prior_snapshot"]),
        "repair_prior_10_changed")
    rows = conn.execute("SELECT * FROM prod2_document_extractions WHERE id_convocatoria=ANY(%s) "
        "AND is_current ORDER BY id_convocatoria" + (" FOR UPDATE" if lock else ""), (local["complete"],)).fetchall()
    audit.require(len(rows) == len(local["complete"]) and {int(row["id_convocatoria"]) for row in rows} == set(local["complete"]),
        "repair_current_completion_scope")
    evaluations = conn.execute("SELECT * FROM prod2_evaluations WHERE id=ANY(%s) ORDER BY id",
        ([row["evaluation_id"] for row in rows],)).fetchall()
    facets = conn.execute("SELECT * FROM prod2_requirement_facets WHERE id_convocatoria=ANY(%s) ORDER BY id", (local["ids"],)).fetchall()
    participation = conn.execute("""SELECT e.id_convocatoria,o.consortium_status,o.subcontracting_status,o.participation_terms_json
        FROM prod2_document_extractions e JOIN opportunities o ON o.id=e.opportunity_id
        WHERE e.id_convocatoria=ANY(%s) AND e.is_current ORDER BY e.id_convocatoria""", (local["ids"],)).fetchall()
    return {"rows": rows, "evaluations": evaluations, "facets": facets, "participation": participation,
        "prior": prior, "receipts_sha256": receipt_fingerprint(local)}


def protected_fingerprints(value):
    # Exclude exactly one derived JSON key, preserving every other extraction field.
    rows = [{**row, "summary_json": {key: item for key, item in row["summary_json"].items() if key != CAP}}
        for row in value["rows"]]
    return {"extractions_except_cap_sha256": audit.fingerprint(rows),
        "raw_sha256": audit.fingerprint([{"id": row["id"], "raw": row["raw_extraction_json"]} for row in value["rows"]]),
        "evaluations_sha256": audit.fingerprint(value["evaluations"]), "facets_sha256": audit.fingerprint(value["facets"]),
        "participation_sha256": audit.fingerprint(value["participation"]), "prior_10_sha256": audit.fingerprint(value["prior"]),
        "private_receipts_sha256": value["receipts_sha256"]}


def plan_changes(local, value):
    evaluations = {int(row["id"]): row for row in value["evaluations"]}
    audit.require(len(evaluations) == len(value["rows"]), "repair_evaluation_scope")
    prior_extractions = local["baseline"]["prior_snapshot"]["current_extraction_ids"].values()
    cutoff = max(int(identity) for identity in prior_extractions)
    changes = []
    for row in value["rows"]:
        conv, extraction = int(row["id_convocatoria"]), int(row["id"])
        outcome = local["outcomes"][conv]
        audit.require(conv in local["ids"] and conv not in local["prior_ids"] and row["is_current"] is True
            and outcome["status"] == "llm1_complete" and outcome.get("accepted") is True
            and extraction == outcome["extraction_id"] and extraction > cutoff, "repair_new_owned_current_extraction")
        evaluation = evaluations.get(int(row["evaluation_id"]))
        audit.require(evaluation is not None and evaluation["id_convocatoria"] == conv and evaluation["status"] == "done"
            and evaluation["kind"] == "gemini_extraction", "repair_paid_evaluation_binding")
        bundle = evaluation["result_json"]
        request_hash = evaluation["request_hash"]
        audit.require(isinstance(request_hash, str) and re.fullmatch(r"[0-9a-f]{64}", request_hash)
            and bundle.get("cache_key") == "prod2:llm1:" + request_hash
            and bundle["raw_extraction_json"] == row["raw_extraction_json"]
            and bundle["document_set_hash"] == outcome["document_set_hash"]
            and bundle["analysis_sha256"] == local["prepared"][conv]["analysis_sha256"]
            and row["model"] == evaluation["model"] == bundle["model"] == outcome["llm1_model"]
            and bundle["document_uuid"] == str(row["codigo_alfresco"])
            and bundle["original_sha256"] == row["sha256_original"], "repair_paid_raw_document_binding")
        audit.require(row["requires_human_review"] is True, "repair_human_review_gate")
        if conv in local["bundles"]:
            audit.require(local["bundles"][conv] == bundle, "repair_local_paid_receipt_binding")
        for folder in dict.fromkeys([local["source_folder"], local["outcome_folders"][conv]]):
            path = Path(folder) / str(conv) / ("gemini_extraction-" + request_hash + ".claim.private.json")
            if path.exists():
                audit.require(audit.read_private(path)["id"] == row["evaluation_id"], "repair_owned_evaluation_id")
        desired = audit.derive_summary(row["raw_extraction_json"])
        current, paid = row["summary_json"], bundle["summary_json"]
        audit.require(isinstance(current, dict) and isinstance(paid, dict), "repair_summary_shape")
        for existing in (current, paid):
            other_keys = (set(existing) | set(desired)) - {CAP}
            audit.require(all(key in existing and key in desired and existing[key] == desired[key] for key in other_keys),
                "repair_other_derived_summary_difference")
        if current.get(CAP) != desired.get(CAP) or (CAP in current) != (CAP in desired):
            changes.append({"convocation_id": conv, "extraction_id": extraction, "evaluation_id": row["evaluation_id"],
                "before": current.get(CAP), "after": desired[CAP], "row": row,
                "raw_sha256": audit.fingerprint(row["raw_extraction_json"]),
                "paid_evaluation_sha256": audit.fingerprint(evaluation), "summary_before_sha256": audit.fingerprint(current)})
    return changes, cutoff


def apply_changes(conn, changes):
    for change in changes:
        row = change["row"]
        # Parameterized exact-row CAS; neither raw nor paid results are assigned.
        result = conn.execute("""UPDATE prod2_document_extractions
            SET summary_json=jsonb_set(summary_json,'{penalidad_tope_pct}',%s::jsonb,true)
            WHERE id=%s AND id_convocatoria=%s AND evaluation_id=%s AND is_current
            AND summary_json=%s::jsonb AND raw_extraction_json=%s::jsonb RETURNING id""",
            (json.dumps(change["after"], allow_nan=False), change["extraction_id"], change["convocation_id"], change["evaluation_id"],
             json.dumps(row["summary_json"], ensure_ascii=False, allow_nan=False),
             json.dumps(row["raw_extraction_json"], ensure_ascii=False, allow_nan=False))).fetchone()
        audit.require(result is not None and result["id"] == change["extraction_id"], "repair_exact_row_cas_failed")


def report(local, before, changes, cutoff, *, executed):
    return {"policy": POLICY, "verified_at": datetime.now(timezone.utc).isoformat(), "dry_run": not executed,
        "source_run_id": local["manifest"]["run_id"], "authorized_count": len(local["ids"]),
        "completed_subset_count": len(local["complete"]), "unresolved_count": len(local["blocked"]),
        "prior_extraction_cutoff": cutoff, "planned_changes": len(changes), "updated_rows": len(changes) if executed else 0,
        "changes": [{key: item for key, item in value.items() if key != "row"} for value in changes],
        "protected_fingerprints": protected_fingerprints(before), "previous_10_unchanged": True,
        "raw_paid_facets_participation_receipts_unchanged": True, "provider_calls": 0, "source_calls": 0,
        "redis_calls": 0, "r2_calls": 0, "db_writes": len(changes) if executed else 0}


def run(args):
    audit.require(not args.output.exists(), "repair_output_already_exists")
    intent = args.output.with_name(args.output.stem + ".intent.private.json")
    audit.require(not args.execute or not intent.exists(), "repair_intent_already_exists")
    # Unresolved billing is preserved; this local read does not resolve/retry it.
    args.allow_incomplete = True
    local = audit.local_run(args)
    local["source_folder"] = args.folder.resolve()
    roots = [local["source_folder"], *local["recovery_roots"], *local["retry_roots"], args.prior_folder.resolve()]
    audit.require(not any(args.output.resolve().is_relative_to(Path(root).resolve()) for root in roots), "repair_output_inside_frozen_evidence")
    with audit.selected.runtime(args) as (settings, _redis_url):
        with audit.psycopg.connect(settings.database_url, row_factory=audit.dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE" if args.execute
                else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout='30s'")
            if args.execute:
                locked = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]
                audit.require(locked is True, "repair_coordinator_unavailable")
            require_idle(conn, local["ids"], owns_coordinator=args.execute)
            before = snapshot(conn, local, args.prior_folder, lock=args.execute)
            changes, cutoff = plan_changes(local, before)
            result = report(local, before, changes, cutoff, executed=args.execute)
            if args.execute:
                audit.private_write(intent, {**result, "status": "prepared_uncommitted", "db_writes": 0,
                    "updated_rows": 0, "commit_confirmation": str(args.output.resolve())})
                apply_changes(conn, changes)
                after = snapshot(conn, local, args.prior_folder)
                audit.require(protected_fingerprints(after) == protected_fingerprints(before), "repair_protected_data_changed")
                remaining, _ = plan_changes(local, after)
                audit.require(not remaining, "repair_derived_caps_still_differ")
                result["after_protected_fingerprints"] = protected_fingerprints(after)
                require_idle(conn, local["ids"], owns_coordinator=True)
    # Final success evidence is written only after the transaction commits.
    audit.private_write(args.output, result)
    print(json.dumps({key: item for key, item in result.items() if key not in
        {"changes", "protected_fingerprints", "after_protected_fingerprints"}}, ensure_ascii=False))
    return result


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--execute", action="store_true", help="Apply only the audited derived cap changes under the coordinator")
    value.add_argument("--original-folder", type=Path, required=True)
    value.add_argument("--folder", type=Path, required=True)
    value.add_argument("--recovery-folder", action="append", type=Path, default=[])
    value.add_argument("--retry-folder", action="append", type=Path, default=[])
    value.add_argument("--prior-folder", type=Path, default=ROOT / "tmp/prod2-selected-recovery-v3-20261006")
    value.add_argument("--preflight", type=Path, default=ROOT / "tmp/prod2-pending90-audit-20261006/preflight.private.json")
    value.add_argument("--baseline-file", type=Path, default=ROOT / "tmp/prod2-pending90-audit-20261006/baseline.private.json")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--local", action="store_true")
    value.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    value.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    return value


if __name__ == "__main__":
    os.umask(0o077)
    try:
        run(parser().parse_args())
    except Exception as error:
        print(json.dumps({"status": "repair_failed", "error_type": type(error).__name__,
            "error_code": str(error) if isinstance(error, audit.PendingVerificationError) else "private_repair_error"}))
        sys.exit(1)
