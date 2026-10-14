"""One explicitly authorized retry of an owned, uncertain PROD2 LLM1 claim.

Default mode is an offline plan. Execution never discovers or downloads data,
resets a claim, overwrites paid evidence, or invokes a model fallback.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import psycopg
from psycopg.rows import dict_row

import continue_prod2_pending as continuation
import run_prod2_selected as selected
from buenapro_worker.settings import Settings
from buenapro_worker.extraction.gemini import MODEL_PRICES_USD_PER_MILLION, estimate_cost
from buenapro_worker.prod2.accepted_runner import paid_llm1
from buenapro_worker.prod2.analysis import _document_input, extract_document, extraction_request_hash
from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.pdf_preparation import _result
from buenapro_worker.prod2.repository import hash_request, _uuid
from buenapro_worker.prod2.selected_extractor import SelectedGeminiExtractor

POLICY = "prod2_explicit_owned_llm1_retry_v1"
AUTHORIZATION_NAME = "llm1-retry-authorization.private.json"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-folder", type=Path, required=True)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--convocation-id", type=int, required=True)
    p.add_argument("--recovery-folder", type=Path, action="append", default=[],
                   help="Existing recovery audit folder bound to these exact 90; repeat for each folder")
    p.add_argument("--max-output-tokens", type=int, choices=[24576, 65536], default=65536)
    p.add_argument("--timeout-ms", type=int, choices=[180000, 300000], default=300000)
    p.add_argument("--allow-unknown-billing", action="store_true",
                   help="Requires explicit user authorization for retrying an attempt with unknown usage")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="Explicit paid execution after reviewing the offline plan")
    modes.add_argument("--dry-run", action="store_true", help="Offline plan; the default")
    p.add_argument("--local", action="store_true")
    p.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    p.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    return p


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def one(paths, label):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError("Expected exactly one original " + label)
    return paths[0]


def known_usage(receipt):
    valid = (receipt.get("provider_usage_known") is True
        and all(type(receipt.get(key)) is int and receipt[key] >= 0 for key in ("input_tokens", "output_tokens"))
        and type(receipt.get("cost_usd")) in {int, float}
        and math.isfinite(receipt["cost_usd"]) and receipt["cost_usd"] >= 0)
    return (valid and receipt.get("model") in MODEL_PRICES_USD_PER_MILLION
        and math.isclose(receipt["cost_usd"], estimate_cost(receipt["model"], receipt["input_tokens"], receipt["output_tokens"]),
            rel_tol=1e-12, abs_tol=1e-12))


def recovery_proof(folder, source, manifest):
    folder = continuation.safe_path(folder)
    value = read_private(folder / "manifest.private.json")
    ids = value.get("recovery_ids")
    if (value.get("recovery_policy") != "prod2_accepted_selected_recovery_v1"
            or value.get("original_folder") != str(source)
            or value.get("original_manifest_hash") != hash_request(manifest)
            or value.get("run_id") != manifest["run_id"]
            or value.get("no_discovery") is not True or value.get("no_redis") is not True
            or not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
            or any(type(conv) is not int or conv not in manifest["pending_ids"] for conv in ids)
            or any(value.get(key) != manifest.get(key) for key in ("profile_id", "gemini_model", "commercial_profiles",
                "published_from", "published_to", "resolved_years", "llm1_model_routing", "llm1_ficha_context_policy"))):
        raise ValueError("Recovery audit folder differs from the exact frozen continuation")
    for conv in ids:
        outcome = read_private(source / str(conv) / "outcome.private.json")
        if (outcome.get("convocation_id") != conv or outcome.get("accepted") is not True
                or outcome.get("status") in {"llm1_complete", "rejected_below_60"}):
            raise ValueError("Recovery audit includes an unaccepted or already completed original member")
    return {"folder": str(folder), "manifest_sha256": sha(folder / "manifest.private.json")}


def plan(args, settings=None):
    source, folder = continuation.safe_path(args.source_folder), continuation.safe_path(args.folder)
    continuation._run_id(folder.name)
    if (source == folder or source.is_relative_to(folder) or folder.is_relative_to(source)
            or type(args.convocation_id) is not int or args.convocation_id <= 0):
        raise ValueError("Retry requires a separate private folder and positive convocation ID")
    if folder.exists() and (not folder.is_dir() or (any(folder.iterdir()) and not (folder / "manifest.private.json").is_file())):
        raise ValueError("Retry folder must be new or own a frozen retry manifest")
    manifest = read_private(source / "manifest.private.json")
    original = continuation.safe_path(manifest["original_folder"])
    old = read_private(original / "manifest.private.json")
    if (manifest.get("continuation_policy") != continuation.POLICY or manifest.get("run_id") != source.name
            or manifest.get("continuation_folder") != str(source) or manifest.get("expected_pending") != 90
            or manifest.get("expected_seen") != 100 or len(manifest.get("previous_done", [])) != 10
            or manifest.get("original_manifest_hash") != hash_request(old)
            or continuation.original_proofs(original, old) != manifest["previous_done"]):
        raise ValueError("Retry source does not bind the exact authorized 90 and previous 10")
    continuation.validate_frozen(manifest, old)
    recoveries = sorted((recovery_proof(path, source, manifest) for path in args.recovery_folder), key=lambda v: v["folder"])
    if len({v["folder"] for v in recoveries}) != len(recoveries):
        raise ValueError("Recovery audit references must not be duplicated")
    conv = args.convocation_id
    if conv not in manifest["pending_ids"] or conv in {v["convocation_id"] for v in manifest["previous_done"]}:
        raise ValueError("Retry ID is outside the exact 90 or belongs to a previous member")
    item = next(v["payload"] for v in manifest["pending_snapshot"] if int(v["candidate_id"]) == conv)
    owner = continuation.safe_path(source / str(conv))
    outcome_path = owner / "outcome.private.json"
    outcome = read_private(outcome_path)
    if outcome.get("convocation_id") != conv or outcome.get("accepted") is not True or outcome.get("status") != "needs_review":
        raise ValueError("Retry requires an already accepted member with a preserved failed outcome")
    claim_path = one(owner.glob("gemini_extraction-*.claim.private.json"), "owned LLM1 claim")
    match = re.fullmatch(r"gemini_extraction-([0-9a-f]{64})\.claim\.private\.json", claim_path.name)
    if not match:
        raise ValueError("Original claim does not name an exact request hash")
    request_hash, claim = match[1], read_private(claim_path)
    if claim.get("status") != "claimed" or type(claim.get("id")) is not int or claim["id"] <= 0:
        raise ValueError("Original claim has invalid ownership evidence")
    _uuid(claim.get("claim_token"))
    attempt_path = one(owner.glob("gemini-selected-*.outcome.private.json"), "failed model attempt")
    attempt = read_private(attempt_path)
    attempt_id = attempt.get("attempt_id", "")
    if (not re.fullmatch(r"gemini-selected-[0-9a-f]{32}", attempt_id)
            or attempt_path.name != attempt_id + ".outcome.private.json"):
        raise ValueError("Original model receipt does not bind an exact attempt ID")
    started_path = owner / (attempt_id + ".started.private.json")
    started = read_private(started_path)
    if started.get("attempt_id") != attempt_id or started.get("model") != attempt.get("model"):
        raise ValueError("Original generation start receipt is missing or inconsistent")
    known = attempt.get("provider_usage_known") is True
    if not attempt.get("model_call_started") or not attempt.get("attempt_id"):
        raise ValueError("Retry requires evidence that the original paid generation started")
    if known:
        response_audit_path = owner / (attempt_id + ".response.private.json")
        response_audit = read_private(response_audit_path)
        if (attempt.get("status") != "response_parse_failure" or attempt.get("finish_reason") != "MAX_TOKENS"
                or not known_usage(attempt) or not known_usage(response_audit)
                or any(response_audit.get(key) != attempt.get(key) for key in
                    ("attempt_id", "model", "prompt_hash", "input_tokens", "output_tokens", "cost_usd", "finish_reason"))):
            raise ValueError("Known-usage retries are limited to MAX_TOKENS")
        if args.max_output_tokens != 65536:
            raise ValueError("MAX_TOKENS retry requires the explicit larger output limit")
    elif (attempt.get("status") != "error_unknown" or outcome.get("error_type") not in {"ReadTimeout", "TimeoutError"}
            or not args.allow_unknown_billing):
        raise ValueError("Unknown-billing timeout retry requires explicit authorization")
    prepared_folder = continuation.safe_path(Path(manifest["cache_folder"]) / ("selected-analysis-" + attempt["document_set_hash"]))
    pinned_path = prepared_folder / "manifest.private.json"
    pinned = read_private(pinned_path)
    prepared = _result(prepared_folder, pinned, pinned["max_analysis_bytes"])
    for key in ("analysis_sha256", "document_set_hash", "document_set_identity", "selection_context", "page_count"):
        if prepared.get(key) != attempt.get(key):
            raise ValueError("Prepared retry differs from the exact paid input")
    model, routing = selected.resolve_llm1_model(manifest, prepared["page_count"], manifest["gemini_model"])
    if model != attempt.get("model"):
        raise ValueError("Retry model differs from the frozen page routing")
    settings = settings or Settings(_env_file=ROOT / ".env.local")
    settings = settings.model_copy(update={"gemini_model": model})
    document_context = {k: prepared[k] for k in ("document_scope", "document_set", "document_set_hash",
                                               "document_set_identity", "selection_context")}
    first = prepared["document_set"][0]
    anchor, anchor_sha = first["document_uuid"], first.get("archive_sha256", first["original_sha256"])
    pdf = prepared["pdf_path"].read_bytes()
    inputs = _document_input(settings, pdf, item["query"]["object_type"], anchor_sha,
                             prepared["source_document_manifest"], document_context)
    expected_hash = extraction_request_hash(settings, pdf, object_type=item["query"]["object_type"],
        original_sha256=anchor_sha, manifest=prepared["source_document_manifest"], document_context=document_context)
    evaluation_key = hash_request({"convocation": conv, "kind": "gemini_extraction", "request_hash": expected_hash,
        "model": model, "policy_version": "prod2_document_extraction_v1", "profile_id": manifest["profile_id"]})
    if expected_hash != request_hash or inputs["prompt_hash"] != attempt.get("prompt_hash") or claim.get("evaluation_key") != evaluation_key:
        raise ValueError("Retry request, prompt or evaluation binding changed")
    response_path = owner / ("gemini_extraction-" + request_hash + ".response.private.json")
    frozen = {"policy": POLICY, "source_folder": str(source), "retry_folder": str(folder), "convocation_id": conv,
        "source_manifest_hash": hash_request(manifest), "original_outcome_sha256": sha(outcome_path),
        "owner_claim_sha256": sha(claim_path), "owner_attempt_sha256": sha(attempt_path),
        "owner_started_sha256": sha(started_path),
        "owner_response_audit_sha256": sha(response_audit_path) if known else None,
        "prepared_manifest_sha256": sha(pinned_path), "evaluation_id": claim["id"], "evaluation_key": evaluation_key,
        "request_hash": request_hash, "model": model, "page_count": prepared["page_count"],
        "analysis_sha256": prepared["analysis_sha256"], "prompt_hash": inputs["prompt_hash"],
        "model_routing": routing, "original_usage_known": known, "allow_unknown_billing": args.allow_unknown_billing,
        "max_output_tokens": args.max_output_tokens, "timeout_ms": args.timeout_ms, "max_attempts": 1,
        "recovery_audit_folders": recoveries,
        "global_max_llm1_calls": min(manifest["max_llm1_calls"], 90)}
    marker = folder / "manifest.private.json"
    authorization = owner / AUTHORIZATION_NAME
    for path in (marker, authorization):
        if path.exists() and read_private(path) != frozen:
            raise ValueError("This ID or folder already belongs to a different retry authorization")
    return {"frozen": frozen, "source_manifest": manifest, "item": item, "prepared": prepared, "pdf": pdf,
        "document_context": document_context, "anchor": anchor, "anchor_sha": anchor_sha, "settings": settings,
        "claim": claim, "owner": owner, "authorization": authorization, "response_path": response_path,
        "outcome_path": outcome_path, "claim_path": claim_path, "attempt_path": attempt_path, "pinned_path": pinned_path}


def validate_claim(row, data, *, done=False):
    frozen, claim = data["frozen"], data["claim"]
    if (not row or row.get("id") != claim["id"] or row.get("id_convocatoria") != frozen["convocation_id"]
            or row.get("kind") != "gemini_extraction" or row.get("request_hash") != frozen["request_hash"]
            or row.get("policy_version") != "prod2_document_extraction_v1"
            or row.get("evaluation_key") != frozen["evaluation_key"] or row.get("model") != frozen["model"]
            or str(row.get("profile_id")) != str(data["source_manifest"]["profile_id"])
            or str(row.get("claim_token")) != str(claim["claim_token"])
            or row.get("status") not in ({"uncertain", "done"} if done else {"uncertain"})):
        raise ValueError("Live LLM1 claim is not the exact owned uncertain reservation")


def budget(data):
    source = Path(data["frozen"]["source_folder"])
    roots = {source}
    recovery_refs = list(data["frozen"]["recovery_audit_folders"])
    for path in source.glob("*/" + AUTHORIZATION_NAME):
        value = read_private(path)
        if (value.get("policy") != POLICY or value.get("source_folder") != str(source)
                or path.parent.name != str(value.get("convocation_id"))
                or value.get("convocation_id") not in data["source_manifest"]["pending_ids"]
                or value.get("source_manifest_hash") != hash_request(data["source_manifest"])
                or value.get("global_max_llm1_calls") != data["frozen"]["global_max_llm1_calls"]):
            raise ValueError("Global retry authorization history is malformed")
        root = continuation.safe_path(value["retry_folder"])
        if read_private(root / "manifest.private.json") != value:
            raise ValueError("Global retry folder does not bind its owner authorization")
        roots.add(root)
        recovery_refs.extend(value.get("recovery_audit_folders", []))
    for proof in recovery_refs:
        if recovery_proof(proof["folder"], source, data["source_manifest"]) != proof:
            raise ValueError("Frozen recovery audit manifest changed")
        roots.add(continuation.safe_path(proof["folder"]))
    # Separate folders prevent copying an attempt into two counted roots.
    if any(a != b and (a.is_relative_to(b) or b.is_relative_to(a)) for a in roots for b in roots):
        raise ValueError("Retry audit roots must not overlap")
    attempts = set()
    issued_without_sdk = 0
    for root in roots:
        local = set()
        for path in root.rglob("gemini-selected-*.private.json"):
            if not path.name.endswith((".started.private.json", ".response.private.json", ".outcome.private.json")):
                continue
            record = read_private(path)
            if not (path.name.endswith((".started.private.json", ".response.private.json"))
                    or record.get("model_call_started")):
                continue
            identity = record.get("attempt_id", "")
            if not re.fullmatch(r"gemini-selected-[0-9a-f]{32}", identity) or not path.name.startswith(identity + "."):
                raise ValueError("Global generation receipt has an invalid attempt ID")
            local.add(identity)
        if attempts.intersection(local):
            raise ValueError("A paid attempt was copied across separate audit roots")
        if root != source and (root / "retry-issued.private.json").exists() and not local:
            # A kill between permit publication and SDK start still consumes the
            # one-shot permit; it is not reported as a known billed API call.
            issued_without_sdk += 1
        attempts.update(local)
    return len(attempts) + issued_without_sdk


def retry_bundle(data, folder):
    bundle = read_private(folder / "retry-response.private.json")
    frozen = data["frozen"]
    attempt_path = one(folder.glob("gemini-selected-*.outcome.private.json"), "retry model outcome")
    attempt = read_private(attempt_path)
    issued = read_private(folder / "retry-issued.private.json")
    identity = attempt.get("attempt_id", "")
    if (not re.fullmatch(r"gemini-selected-[0-9a-f]{32}", identity)
            or attempt_path.name != identity + ".outcome.private.json"
            or attempt.get("status") != "success" or not attempt.get("model_call_started")
            or not known_usage(attempt)
            or issued.get("policy") != POLICY or issued.get("evaluation_id") != frozen["evaluation_id"]
            or issued.get("request_hash") != frozen["request_hash"] or issued.get("max_attempts") != 1):
        raise ValueError("Retry response has no attributable successful paid attempt")
    received = read_private(folder / (identity + ".response.private.json"))
    started = read_private(folder / (identity + ".started.private.json"))
    if (not known_usage(received)
            or any(received.get(key) != attempt.get(key) for key in
                ("attempt_id", "model", "prompt_hash", "analysis_sha256", "input_tokens", "output_tokens", "cost_usd"))
            or any(started.get(key) != attempt.get(key) for key in
                ("attempt_id", "model", "prompt_hash", "analysis_sha256", "page_count", "max_output_tokens", "timeout_ms"))):
        raise ValueError("Retry start, usage and outcome receipts disagree")
    for key in ("model", "prompt_hash", "analysis_sha256", "page_count", "max_output_tokens", "timeout_ms"):
        if attempt.get(key) != frozen[key]:
            raise ValueError("Retry attempt differs from its exact input or explicit limits")
    if (bundle.get("cache_key") != "prod2:llm1:" + frozen["request_hash"]
            or bundle.get("document_uuid") != data["anchor"]
            or bundle.get("document_sha256") != data["anchor_sha"]
            or bundle.get("original_sha256") != data["anchor_sha"]
            or any(bundle.get(key) != attempt.get(key) for key in
                ("model", "prompt_hash", "analysis_sha256", "input_tokens", "output_tokens", "cost_usd"))
            or any(bundle.get(key) != value for key, value in data["document_context"].items())
            or bundle.get("requires_human_review") is not True
            or not isinstance(bundle.get("raw_extraction_json"), dict)):
        raise ValueError("Retry response does not bind the original request and paid evidence")
    if data["response_path"].exists() and read_private(data["response_path"]) != bundle:
        raise ValueError("Original owner response differs from the explicit retry response")
    return bundle, attempt


def main():
    args = parser().parse_args()
    os.umask(0o077)
    data = plan(args)
    if not args.execute:
        print(json.dumps({**data["frozen"], "dry_run": True, "provider_calls": 0, "database_writes": 0,
            "source_calls": 0, "redis_calls": 0, "original_response_present": data["response_path"].exists()}, ensure_ascii=False))
        return 0
    folder = Path(data["frozen"]["retry_folder"])
    with selected.runtime(args) as (runtime_settings, _):
        data = plan(args, runtime_settings)
        settings, frozen = data["settings"], data["frozen"]
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()["locked"]:
                raise RuntimeError("Another PROD2 coordinator owns the executor")
            try:
                row = conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s", (frozen["evaluation_id"],)).fetchone()
                validate_claim(row, data, done=data["response_path"].exists())
                member = conn.execute("SELECT * FROM prod2_accepted_run_members WHERE run_id=%s AND convocation_id=%s",
                    (data["source_manifest"]["run_id"], frozen["convocation_id"])).fetchone()
                if not member or member.get("accepted") is not True or member.get("opportunity_id") is None:
                    raise ValueError("Retry member is not durably accepted in the authorized run")
                if plan(args, runtime_settings)["frozen"] != frozen:
                    raise ValueError("Private retry inputs changed before execution")
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                for path in (folder / "manifest.private.json", data["authorization"]):
                    if not path.exists():
                        private_write(path, frozen)
                if (folder / "retry-response.private.json").exists():
                    bundle, _ = retry_bundle(data, folder)
                    if not data["response_path"].exists():
                        private_write(data["response_path"], bundle)
                elif data["response_path"].exists():
                    raise ValueError("Original owner response has no matching explicit retry evidence")
                if not data["response_path"].exists():
                    if (list(folder.glob("gemini-selected-*.started.private.json"))
                            or list(folder.glob("gemini-selected-*.outcome.private.json"))
                            or (folder / "retry-issued.private.json").exists()):
                        raise RuntimeError("The one explicit retry was already issued; reconcile its evidence")
                    if budget(data) >= frozen["global_max_llm1_calls"]:
                        raise RuntimeError("Original and retry LLM1 calls exhaust the frozen global budget")
                    def before_request():
                        validate_claim(conn.execute("SELECT * FROM prod2_evaluations WHERE id=%s",
                            (frozen["evaluation_id"],)).fetchone(), data)
                        if plan(args, runtime_settings)["frozen"] != frozen:
                            raise ValueError("Private retry evidence changed before paid generation")
                        if budget(data) >= frozen["global_max_llm1_calls"]:
                            raise RuntimeError("Global LLM1 budget exhausted before retry generation")
                        private_write(folder / "retry-issued.private.json", {"policy": POLICY,
                            "evaluation_id": frozen["evaluation_id"], "request_hash": frozen["request_hash"],
                            "issued_at": datetime.now(timezone.utc).isoformat(), "max_attempts": 1})
                    with SelectedGeminiExtractor(settings, data["prepared"], folder, before_request,
                            max_output_tokens=frozen["max_output_tokens"], timeout_ms=frozen["timeout_ms"]) as extractor:
                        bundle = extract_document(settings, data["pdf"], object_type=data["item"]["query"]["object_type"],
                            original_sha256=data["anchor_sha"], manifest=data["prepared"]["source_document_manifest"],
                            document_context=data["document_context"], extractor=extractor)
                    bundle.update(document_uuid=data["anchor"], document_sha256=data["anchor_sha"])
                    if bundle.get("cache_key") != "prod2:llm1:" + frozen["request_hash"]:
                        raise ValueError("Retry result does not bind the original exact request")
                    private_write(folder / "retry-response.private.json", bundle)
                    retry_bundle(data, folder)
                    private_write(data["response_path"], bundle)
                _, attempt = retry_bundle(data, folder)
                def forbidden():
                    raise RuntimeError("Reconciliation must never issue another model call")
                evaluation_id, bundle, hit = paid_llm1(conn, data["owner"], frozen["convocation_id"],
                    frozen["request_hash"], settings, data["source_manifest"]["profile_id"], forbidden)
                if evaluation_id != frozen["evaluation_id"] or bundle != read_private(data["response_path"]):
                    raise ValueError("Reconciled evaluation differs from the exact owned retry response")
                result = {"policy": POLICY, "convocation_id": frozen["convocation_id"], "evaluation_id": evaluation_id,
                    "request_hash": frozen["request_hash"], "status": "evaluation_reconciled",
                    "model": bundle["model"], "input_tokens": bundle["input_tokens"], "output_tokens": bundle["output_tokens"],
                    "cost_usd": bundle["cost_usd"], "cache_hit": hit, "original_outcome_preserved": True,
                    "requires_normal_recovery": True, "global_llm1_calls": budget(data),
                    "attempt_id": attempt["attempt_id"], "retry_response_sha256": sha(folder / "retry-response.private.json"),
                    "max_output_tokens": frozen["max_output_tokens"], "timeout_ms": frozen["timeout_ms"]}
                path = folder / "retry-result.private.json"
                if not path.exists():
                    private_write(path, result)
                else:
                    previous = read_private(path)
                    if any(previous.get(k) != result[k] for k in result if k != "global_llm1_calls"):
                        raise ValueError("Stored retry reconciliation result changed")
                    result = previous
                if sha(data["outcome_path"]) != frozen["original_outcome_sha256"] or sha(data["claim_path"]) != frozen["owner_claim_sha256"]:
                    raise ValueError("Original ownership or outcome evidence changed")
                print(json.dumps(result, ensure_ascii=False))
                return 0
            finally:
                if not conn.closed and not conn.broken:
                    conn.execute("SELECT pg_advisory_unlock(hashtext('prod2_accepted_executor'),0)")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
