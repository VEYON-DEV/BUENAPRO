#!/usr/bin/env python3
"""Recover one exact paid STOP body offline, retaining substantive review.

prepare freezes local input hashes, a conservative derived bundle and a fresh
read-only PostgreSQL snapshot. execute requires --go, its exact plan SHA and
committed runtime, then completes the SAME original evaluation and extraction
inside one transaction. No provider client, selector, source HTTP or Redis is
constructed. Old outcomes, payment receipts, reports and STOP remain immutable.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from buenapro_worker.extraction.gemini import (
    BASES_PROMPT_FILENAME, PARTICIPATION_INSTRUCTION, ExtractionResult,
    estimate_cost, parse_json_response, read_prompt,
)
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2.analysis import _document_input, extract_document
from buenapro_worker.prod2.batch_runner import private_write
from buenapro_worker.prod2.extraction_policy import validate_scope_payload
from buenapro_worker.prod2.input_policy import (
    prompt_version_for_input, read_verified_retrieved_text,
    schema_for_input, schema_version_for_input,
)
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1
from buenapro_worker.prod2.pdf_preparation import _result
from buenapro_worker.prod2.repository import complete_evaluation, hash_request
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.prod2.selected_metadata_publication import digest, file_sha, frozen, read_bound

POLICY = "din_known_commercial_review_recovery_v1"
CONVOCATION = 1048156
EVALUATION_ID = 2229
REQUEST_HASH = "124de02e9d20f1640667ca6f01a45ef688524f0a0d64a01068886ce5413b4b89"
SOURCE_PROFILE_ID = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"
ATTEMPT = "gemini-selected-4c22f98ed7314f90bb47b548e99f95ed"
RESPONSE_SHA = "bcd8e29c147e77c62b35860fdb528ebf4291cf4364cf7aa6003c6efa8957bea5"
RUN_MANIFEST_SHA = "04167b0206a5bbc3cd2d8a63485cc36e3a0a82d0ca8611a77fe474549051aea5"
RUN_FOLDER = ROOT / "tmp/prod2-sector-reading-completion-20261020/run-04"
EVIDENCE_SHAS = {
    ATTEMPT + ".started.private.json": "fafe3abb5428db60a57e21de4e084104ea0694fe260779a0d55b31e4d4ddfca9",
    ATTEMPT + ".response.private.json": RESPONSE_SHA,
    ATTEMPT + ".outcome.private.json": "eb790cd2a27a57849b455c395362d72211a9898958f18e21453ca21acc5011c0",
    ATTEMPT + ".cleanup.private.json": "595eeb27a2777e2e15a58eabd8640f4b990c17b0746465bf818fe140c757e614",
    "gemini_extraction-" + REQUEST_HASH + ".claim.private.json":
        "d8a97a75a7de5fb1a3cad798b586c5da2a3a8262e8311585b7d3d529f790775c",
}
PLAN_NAME = "known-commercial-review-recovery-v1.plan.private.json"
DERIVED_NAME = "known-commercial-review-recovery-v1.derived.private.json"
COMPLETE_NAME = "known-commercial-review-recovery-v1.complete.private.json"


class RecoveryError(ValueError):
    """Sanitized local guard; never include provider text or connection secrets."""


def require_folder(folder):
    path = Path(folder).absolute()
    if (path != RUN_FOLDER or path.is_symlink() or not path.is_dir()
            or any(parent.is_symlink() for parent in path.parents)
            or (path / str(CONVOCATION)).is_symlink()
            or not (path / str(CONVOCATION)).is_dir()):
        raise RecoveryError("Only the same exact run-04 folder is permitted")
    return path


def once(path, value):
    path = Path(path)
    if path.exists():
        if read_bound(path) != frozen(value):
            raise RecoveryError("Append-only recovery evidence conflicts")
    else:
        private_write(path, value)


def runtime_bindings():
    """Bind the existing reader's import surface plus this isolated script."""
    prior = read_bound(RUN_FOLDER / "runtime-bindings.private.json")
    names = set(prior) | {str(Path(__file__).relative_to(ROOT))}
    return {name: file_sha(ROOT / name) for name in sorted(names)}


def require_committed(bindings):
    for name, checksum in bindings.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or file_sha(ROOT / path) != checksum:
            raise RecoveryError("Runtime differs from the frozen recovery bindings")
        result = subprocess.run(["git", "show", "HEAD:" + name], cwd=ROOT, capture_output=True)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != checksum:
            raise RecoveryError("Commit the exact recovery runtime before execution")


def conservative_projection(receipt):
    text = receipt["response_text"]
    if (receipt["attempt_id"] != ATTEMPT or receipt["status"] != "response_received"
            or receipt["model"] != "gemini-2.5-flash-lite"
            or receipt["finish_reason"] != "STOP" or receipt["finish_reasons"] != ["STOP"]
            or receipt["provider_usage_known"] is not True
            or not isinstance(text, str)
            or hashlib.sha256(text.encode()).hexdigest() != receipt["response_text_sha256"]
            or len(text.encode()) != receipt["response_text_bytes"]):
        raise RecoveryError("The complete exact known STOP response is required")
    for field in ("input_tokens", "output_tokens"):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise RecoveryError("Known integer provider usage is required")
    expected_cost = estimate_cost(receipt["model"], receipt["input_tokens"], receipt["output_tokens"])
    if not math.isclose(receipt["cost_usd"], expected_cost, rel_tol=1e-12, abs_tol=1e-12):
        raise RecoveryError("Original paid cost differs from its known usage")
    raw = parse_json_response(text)
    schema = schema_for_input("bases_service", receipt["selection_context"])
    try:
        schema.model_validate(raw)
    except ValidationError as error:
        errors = error.errors(include_url=False, include_input=False, include_context=False)
        if (len(errors) != 1 or errors[0]["loc"] != ("commercial_classification",)
                or errors[0]["type"] != "value_error"
                or errors[0]["msg"] != "Value error, Every commercial label requires its own applicable evidence"):
            raise RecoveryError("Only the diagnosed commercial-label validation failure is supported") from None
    else:
        raise RecoveryError("A valid paid reading must not be reprojected here")
    projected = deepcopy(raw)
    projected["commercial_classification"] = {
        "taxonomy_version": "commercial_taxonomy_v1", "primary_area": "sin_clasificar",
        "secondary_areas": [], "work_types": [], "specialties": [],
        "support_status": "needs_review", "evidence": [],
    }
    validate_scope_payload(projected, receipt["selection_context"])
    schema.model_validate(projected)
    noncommercial = lambda value: {key: item for key, item in value.items()
                                   if key != "commercial_classification"}
    if noncommercial(projected) != noncommercial(raw):
        raise RecoveryError("Requirements, quotes or other paid fields changed")
    proof = {
        "policy_version": POLICY, "convocation_id": CONVOCATION,
        "attempt_id": ATTEMPT, "original_response_receipt_sha256": RESPONSE_SHA,
        "original_response_text_sha256": receipt["response_text_sha256"],
        "original_commercial_sha256": canonical_hash(raw["commercial_classification"]),
        "projected_commercial_sha256": canonical_hash(projected["commercial_classification"]),
        "unchanged_noncommercial_sha256": canonical_hash(noncommercial(raw)),
        "changed_paths": ["commercial_classification"],
        "classification_pending_review": True, "original_citations_preserved": True,
        "source_coverage_not_upgraded": True, "semantic_qa_complete": False,
        "new_provider_calls": 0, "original_input_modified": False,
    }
    return projected, proof


class OfflineReader:
    """Return only this verified paid projection through the real normalizer."""
    supports_selected_input_policies = True

    def __init__(self, prepared, local, receipt, projected):
        self.prepared, self.local_text_result = prepared, local
        self.receipt, self.projected = receipt, projected
        self.prompts = {"bases_service": read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION}

    def extract(self, pdf_bytes, *, mime, doc_class):
        if (mime != "application/pdf" or doc_class != "bases_service"
                or hashlib.sha256(pdf_bytes).hexdigest() != self.receipt["analysis_sha256"]):
            raise RecoveryError("Offline decoding changed the verified source or document class")
        context = self.receipt["selection_context"]
        extraction = schema_for_input(doc_class, context).model_validate(self.projected)
        return ExtractionResult(extraction=extraction, raw_json=deepcopy(self.projected),
            model=self.receipt["model"], prompt_version=prompt_version_for_input(doc_class, context),
            schema_version=schema_version_for_input(doc_class, context),
            input_tokens=self.receipt["input_tokens"], output_tokens=self.receipt["output_tokens"],
            cost_usd=self.receipt["cost_usd"], requires_human_review=True)


def local_material(folder, response_sha):
    folder = require_folder(folder)
    if response_sha != RESPONSE_SHA:
        raise RecoveryError("Explicit exact original response SHA is required")
    case = folder / str(CONVOCATION)
    evidence = []
    def bound(path, checksum=None):
        value = read_bound(path, checksum)
        evidence.append({"path": str(Path(path).absolute()), "sha256": file_sha(path)})
        return value
    manifest = bound(folder / "manifest.private.json", RUN_MANIFEST_SHA)
    bound(folder / "report.private.json",
          "716179d722090c33d3fec0b6155fdcb8c13b1a6a24dd19ebcbc997b5b8a186a0")
    bound(folder / "handoff-guards/dispatch-stop.private.json")
    source_manifest = bound(Path(manifest["original_folder"]) / "manifest.private.json",
                            manifest["original_manifest_sha256"])
    source = next(row for row in manifest["cases"] if row["convocation_id"] == CONVOCATION)
    for row in source["evidence"]:
        if file_sha(row["path"]) != row["sha256"]:
            raise RecoveryError("Original source/preparation evidence changed")
        evidence.append(row)
    record = bound(Path(manifest["original_folder"]) / str(CONVOCATION) / "outcome.private.json",
                   source["original_outcome_sha256"])
    receipts = {name: bound(case / name, checksum) for name, checksum in EVIDENCE_SHAS.items()}
    response = receipts[ATTEMPT + ".response.private.json"]
    started, outcome, cleanup = (receipts[ATTEMPT + suffix + ".private.json"]
                                 for suffix in (".started", ".outcome", ".cleanup"))
    fields = ("attempt_id", "model", "prompt_hash", "analysis_sha256", "document_set_hash",
              "document_set_identity", "selection_context", "page_count", "llm1_input",
              "max_output_tokens", "timeout_ms")
    if (started["status"] != "generation_reserved"
            or outcome["status"] != "response_parse_failure" or outcome["error_type"] != "ValidationError"
            or outcome["model_call_started"] is not True
            or any(started.get(key) != response.get(key) or outcome.get(key) != response.get(key) for key in fields)
            or any(outcome.get(key) != response.get(key) for key in
                   ("usage", "input_tokens", "output_tokens", "cost_usd", "finish_reason", "finish_reasons"))
            or any(value.get("remote_cleanup_pending") != 0
                   or value.get("remote_cleanup_unknown") is not False for value in (outcome, cleanup))):
        raise RecoveryError("Paid attempt identity, usage or cleanup is inconsistent")
    claim = receipts["gemini_extraction-" + REQUEST_HASH + ".claim.private.json"]
    if source_manifest["profile_id"] != SOURCE_PROFILE_ID or claim["id"] != EVALUATION_ID:
        raise RecoveryError("The original inherited reading owner must be preserved")
    preparation_manifest = bound(source["preparation"])
    prepared = _result(Path(source["preparation"]).parent, preparation_manifest,
                       preparation_manifest["max_analysis_bytes"])
    prepared["llm1_input"] = deepcopy(response["llm1_input"])
    if any(prepared[key] != response[key] for key in
           ("analysis_sha256", "document_set_hash", "document_set_identity", "selection_context", "page_count")):
        raise RecoveryError("Retained preparation differs from the original paid input")
    context_path = case / ("embedding-context-" + response["llm1_input"]["retrieval_hash"]) / "context.private.txt"
    local = {**deepcopy(prepared["llm1_input"]),
             "policy_version": prepared["llm1_input"]["retrieval_policy_version"],
             "context_text_path": str(context_path)}
    read_verified_retrieved_text(local, prepared)
    evidence.append({"path": str(context_path), "sha256": file_sha(context_path)})
    projected, proof = conservative_projection(response)
    document_context = {key: prepared[key] for key in
        ("document_scope", "document_set", "document_set_hash", "document_set_identity", "selection_context", "llm1_input")}
    first = prepared["document_set"][0]
    anchor_sha = first.get("archive_sha256", first["original_sha256"])
    settings = SimpleNamespace(gemini_model=response["model"],
                               gemini_fallback_model="gemini-2.5-flash")
    pdf = prepared["pdf_path"].read_bytes()
    inputs = _document_input(settings, pdf, record["object_type"], anchor_sha,
                             prepared["source_document_manifest"], document_context)
    evaluation_key = hash_request({"convocation": CONVOCATION, "kind": "gemini_extraction",
        "request_hash": REQUEST_HASH, "model": response["model"],
        "policy_version": "prod2_document_extraction_v1", "profile_id": SOURCE_PROFILE_ID})
    if (canonical_hash(inputs) != REQUEST_HASH or inputs["prompt_hash"] != response["prompt_hash"]
            or claim["evaluation_key"] != evaluation_key or claim["status"] != "claimed"):
        raise RecoveryError("Request/prompt/model/claim does not reproduce the original paid identity")
    bundle = extract_document(settings, pdf, object_type=record["object_type"],
        original_sha256=anchor_sha, manifest=prepared["source_document_manifest"],
        document_context=document_context, extractor=OfflineReader(prepared, local, response, projected))
    bundle.update(document_uuid=first["document_uuid"], document_sha256=anchor_sha,
                  analysis_bytes=prepared["analysis_bytes"], transport_parts=len(prepared["chunks"]))
    bundle["known_commercial_review_projection"] = deepcopy(proof)
    bundle["raw_extraction_json"]["known_commercial_review_projection"] = deepcopy(proof)
    audit = bundle["raw_extraction_json"]["retrieval_evidence_audit"]
    if (bundle["requires_human_review"] is not True
            or len(audit["errors"]) != 47 or audit["requires_human_review"] is not True
            or prepared["llm1_input"]["source_capture"]["text_coverage_complete"] is not False
            or bundle["raw_extraction_json"]["requirements"] != projected["requirements"]
            or bundle["raw_extraction_json"]["evidence_citations"] != projected["evidence_citations"]):
        raise RecoveryError("Known partial/citation review or substantive reading fields changed")
    return {"bundle": bundle, "proof": proof, "claim": claim, "record": record,
            "source_url": first["source_url"], "evidence": sorted(evidence, key=lambda row: row["path"])}


def pg_snapshot(conn, material, *, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    record, claim = material["record"], material["claim"]
    source = conn.execute("SELECT p.id_procedimiento,p.description,p.row_hash,p.object_type,"
        "p.opportunity_id::text,d.detail_hash,d.proposals_closes_at FROM prod2_discoveries p "
        "JOIN prod2_details d USING(id_convocatoria) WHERE p.id_convocatoria=%s" + suffix,
        (CONVOCATION,)).fetchone()
    if (not source or source["id_procedimiento"] != record["procedure_id"]
            or source["description"] != record["description"] or source["object_type"] != record["object_type"]
            or source["detail_hash"] != record["detail_hash"]
            or source["proposals_closes_at"] != datetime.fromisoformat(record["closes_at"])):
        raise RecoveryError("Current PostgreSQL source differs from the exact paid source")
    profile = conn.execute("SELECT id::text,tenant_id::text,profile_hash,is_active "
        "FROM company_profiles WHERE id=%s" + suffix, (SOURCE_PROFILE_ID,)).fetchone()
    if (not profile or profile["id"] != SOURCE_PROFILE_ID or profile["is_active"] is not True
            or not isinstance(profile["profile_hash"], str) or not profile["profile_hash"]):
        raise RecoveryError("The original reading profile is unavailable or inactive")
    evaluations = conn.execute("SELECT id,id_convocatoria,kind,request_hash,evaluation_key,model,"
        "policy_version,profile_id::text,claim_token::text,status,result_json,input_tokens,output_tokens,cost_usd "
        "FROM prod2_evaluations WHERE id_convocatoria=%s AND kind='gemini_extraction' ORDER BY id" + suffix,
        (CONVOCATION,)).fetchall()
    if len(evaluations) != 1:
        raise RecoveryError("Other historical or newer readings must remain preserved")
    row = evaluations[0]
    expected = {"id": EVALUATION_ID, "id_convocatoria": CONVOCATION, "kind": "gemini_extraction",
                "request_hash": REQUEST_HASH, "evaluation_key": claim["evaluation_key"],
                "model": material["bundle"]["model"], "policy_version": "prod2_document_extraction_v1",
                "profile_id": SOURCE_PROFILE_ID, "claim_token": claim["claim_token"]}
    if (any(row.get(key) != value for key, value in expected.items())
            or row["status"] not in {"running", "uncertain"}
            or any(row.get(key) is not None for key in ("result_json", "input_tokens", "output_tokens", "cost_usd"))):
        raise RecoveryError("Only the same incomplete original paid evaluation may be completed")
    existing = conn.execute("SELECT id,evaluation_id FROM prod2_document_extractions "
                            "WHERE id_convocatoria=%s" + suffix, (CONVOCATION,)).fetchall()
    if existing:
        raise RecoveryError("An existing reading is preserved; this recovery never replaces it")
    return frozen({"source": source, "profile": profile, "evaluation": row})


def prepare(conn, folder, response_sha):
    material = local_material(folder, response_sha)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        current = pg_snapshot(conn, material)
    case = Path(folder) / str(CONVOCATION)
    derived = {"version": POLICY, "bundle": material["bundle"], "proof": material["proof"],
               "original_cost_usd_known": material["bundle"]["cost_usd"], "new_provider_calls": 0}
    once(case / DERIVED_NAME, derived)
    plan = {"version": POLICY, "convocation_id": CONVOCATION, "evaluation_id": EVALUATION_ID,
            "request_hash": REQUEST_HASH, "source_profile_id": SOURCE_PROFILE_ID,
            "folder": str(Path(folder).absolute()), "response_sha256": RESPONSE_SHA,
            "evidence": material["evidence"], "derived_sha256": file_sha(case / DERIVED_NAME),
            "bundle_sha256": canonical_hash(material["bundle"]), "pg_snapshot": current,
            "runtime_bindings": runtime_bindings(), "new_provider_calls": 0,
            "requires_human_review": True, "old_stop_preserved": True}
    once(case / PLAN_NAME, plan)
    return plan


def execute(conn, folder, expected_plan_sha, response_sha, *, go=False):
    if go is not True:
        raise RecoveryError("Explicit --go is required for the same-evaluation PostgreSQL recovery")
    folder = require_folder(folder)
    case = folder / str(CONVOCATION)
    plan = read_bound(case / PLAN_NAME, expected_plan_sha)
    if (plan.get("version") != POLICY or plan.get("folder") != str(folder)
            or plan.get("convocation_id") != CONVOCATION or plan.get("evaluation_id") != EVALUATION_ID
            or plan.get("request_hash") != REQUEST_HASH or plan.get("source_profile_id") != SOURCE_PROFILE_ID
            or plan.get("response_sha256") != response_sha or response_sha != RESPONSE_SHA):
        raise RecoveryError("Frozen same-case recovery plan identity differs")
    require_committed(plan["runtime_bindings"])
    material = local_material(folder, response_sha)
    derived = read_bound(case / DERIVED_NAME, plan["derived_sha256"])
    if (material["evidence"] != plan["evidence"] or derived["bundle"] != material["bundle"]
            or derived["proof"] != material["proof"]
            or canonical_hash(material["bundle"]) != plan["bundle_sha256"]):
        raise RecoveryError("Derived reading or original source evidence changed")
    bundle, claim = material["bundle"], material["claim"]
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        locked = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()
        if locked["locked"] is not True:
            raise RecoveryError("A PROD2 coordinator is still active")
        if pg_snapshot(conn, material, lock=True) != plan["pg_snapshot"]:
            raise RecoveryError("Current source/profile/evaluation changed after preparing recovery")
        complete_evaluation(conn, EVALUATION_ID, claim["claim_token"], bundle,
            input_tokens=bundle["input_tokens"], output_tokens=bundle["output_tokens"], cost_usd=bundle["cost_usd"])
        saved = persist_completed_llm1(conn, convocation_id=CONVOCATION,
            document_uuid=bundle["document_uuid"], document_sha256=bundle["document_sha256"],
            source_url=material["source_url"], extraction={**bundle, "evaluation_id": EVALUATION_ID,
                                                       "allowed_models": [bundle["model"]]})
        if saved.get("requires_human_review") is not True or saved.get("projection_pending") is not True:
            raise RecoveryError("Recovered reading must retain human review and pending participation")
    result = {"version": POLICY, "status": "recovered_with_review", "convocation_id": CONVOCATION,
              "evaluation_id": EVALUATION_ID, "request_hash": REQUEST_HASH,
              "source_profile_id": SOURCE_PROFILE_ID, "plan_sha256": expected_plan_sha,
              "bundle_sha256": plan["bundle_sha256"], "original_response_sha256": RESPONSE_SHA,
              "known_original_cost_usd": bundle["cost_usd"], "new_provider_calls": 0,
              "new_inference_cost_usd": 0, "old_stop_preserved": True,
              "literal_review_errors": 47, "classification_pending_review": True,
              "source_coverage_not_upgraded": True, "semantic_qa_complete": False,
              "saved": saved, "completed_at": datetime.now(timezone.utc).isoformat()}
    once(case / COMPLETE_NAME, result)
    return result


@contextmanager
def database(args):
    if args.database_url_env:
        dsn = os.environ.get(args.database_url_env)
        if not dsn:
            raise RecoveryError("Explicit database environment variable is empty")
        yield dsn
    else:
        with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
            yield settings.database_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute"))
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--expected-response-sha256", required=True)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--database-url-env")
    parser.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    parser.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    args = parser.parse_args(argv)
    folder = require_folder(args.folder)
    if args.expected_response_sha256 != RESPONSE_SHA:
        raise RecoveryError("The explicit diagnosed original response SHA is required")
    if args.command == "execute":
        if not args.go or not args.expected_plan_sha256:
            raise RecoveryError("Execution requires --go and the exact prepared plan SHA")
        plan = read_bound(folder / str(CONVOCATION) / PLAN_NAME, args.expected_plan_sha256)
        require_committed(plan["runtime_bindings"])
    with database(args) as dsn, psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        if args.command == "prepare":
            plan = prepare(conn, folder, args.expected_response_sha256)
            print(json.dumps({"status": "prepared", "plan_sha256": file_sha(folder / str(CONVOCATION) / PLAN_NAME), "new_provider_calls": 0,
                              "business_pg_writes": 0, "convocation_id": CONVOCATION}))
        else:
            result = execute(conn, folder, args.expected_plan_sha256, args.expected_response_sha256, go=True)
            print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except RecoveryError as error:
        print(json.dumps({"status": "attention_required", "reason": str(error), "no_provider_calls": True}))
        raise SystemExit(2)
