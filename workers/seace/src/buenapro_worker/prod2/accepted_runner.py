"""Paid stages and pinned documents for the bounded accepted-only runner.

There is deliberately no LLM2, matching, notification or scheduler entry point.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time

from .analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from .batch_runner import private_write, read_private
from .repository import claim_evaluation, complete_evaluation
from buenapro_worker.local_document_conversion import prepare_pdf


def accepted(result: dict) -> bool:
    score = result.get("affinity_index")
    if type(score) not in (int, float) or not 0 <= score <= 100:
        raise ValueError("Invalid Jev affinity index")
    return score >= 60


def paid_jev(conn, folder, row, commercial, profile_id, analyzer, repository):
    request_hash = jev_request_hash(row["description"], commercial_profiles=commercial)
    claim = repository.claim_jev(conn, convocation_id=row["convocation_id"],
        request_hash=request_hash, model=JEV_MODEL, policy_version=JEV_POLICY_VERSION)
    stem = folder / ("jev-" + request_hash)
    owner_path = stem.with_suffix(".claim.private.json")
    response_path = stem.with_suffix(".response.private.json")
    if claim["state"] == "cached":
        result, reused = claim["result"], True
    else:
        if claim["state"] == "claimed":
            private_write(owner_path, claim)
        elif owner_path.exists() and response_path.exists():
            owner = read_private(owner_path)
            if owner["request_hash"] != request_hash:
                raise RuntimeError("Private Jev reservation mismatch")
            claim = owner
        else:
            raise RuntimeError("Jev reservation needs reconciliation; no automatic repayment")
        reused = response_path.exists()
        if reused:
            result = read_private(response_path)
        else:
            try:
                result = analyzer.classify(row["description"], commercial_profiles=commercial)
            except Exception as error:
                private_write(stem.with_suffix(".failure.private.json"),
                    {"error_type": type(error).__name__, "provider_usage_known": False,
                     "ended_at": datetime.now(timezone.utc).isoformat()})
                conn.execute("UPDATE prod2_jev_decision_cache SET status='uncertain',"
                    "blocked_reason='provider_response_unavailable',updated_at=now() "
                    "WHERE cache_key=%s AND claim_token=%s AND status='running'",
                    (request_hash, claim["claim_token"]))
                raise
            result["profile_id"] = profile_id
            private_write(response_path, result)
        repository.complete_jev(conn, request_hash=request_hash,
                                claim_token=claim["claim_token"], result=result)
    if (result.get("cache_key") != "prod2:jev:" + request_hash or
        result.get("model") != JEV_MODEL or result.get("policy_version") != JEV_POLICY_VERSION):
        raise RuntimeError("Jev receipt does not bind this request")
    accepted(result)
    return request_hash, {**result, "profile_id": profile_id}, reused


def paid_llm1(conn, folder, convocation_id, request_hash, settings, profile_id, callback):
    from .luna_policy import provider_model
    claim = claim_evaluation(conn, convocation_id, kind="gemini_extraction",
        request_hash=request_hash, model=provider_model(settings),
        policy_version="prod2_document_extraction_v1", profile_id=profile_id,
        lease_seconds=3600)
    if claim["status"] == "cached":
        result, reused = claim["result_json"], True
    else:
        stem = folder / ("gemini_extraction-" + request_hash)
        owner_path = stem.with_suffix(".claim.private.json")
        response_path = stem.with_suffix(".response.private.json")
        if claim["status"] == "claimed":
            private_write(owner_path, claim)
        elif claim["status"] in {"busy", "uncertain"} and owner_path.exists() and response_path.exists():
            owner = read_private(owner_path)
            if owner["id"] != claim["id"]:
                raise RuntimeError("LLM1 private reservation mismatch")
            claim = owner
        else:
            raise RuntimeError("LLM1 requires reconciliation; no automatic repayment")
        reused = response_path.exists()
        if reused:
            result = read_private(response_path)
        else:
            try:
                result = callback()
            except Exception as error:
                from .selected_extractor import SelectedProviderRejected
                if isinstance(error, SelectedProviderRejected):
                    # Gemini answered an HTTP error: nothing was billed. The reservation goes back to
                    # pending and its owner receipt is archived, so a later attempt may claim it.
                    conn.execute("UPDATE prod2_evaluations SET status='pending',claim_token=NULL,claimed_at=NULL,"
                        "lease_until=NULL,updated_at=now() WHERE id=%s AND claim_token=%s AND status='running'",
                        (claim["id"], claim["claim_token"]))
                    owner_path.rename(owner_path.with_name(
                        owner_path.name.replace(".claim.", f".claim-released-{time.time_ns()}.")))
                    raise
                conn.execute("UPDATE prod2_evaluations SET status='uncertain',updated_at=now() "
                    "WHERE id=%s AND claim_token=%s AND status='running'", (claim["id"], claim["claim_token"]))
                raise
            private_write(response_path, result)
        complete_evaluation(conn, claim["id"], claim["claim_token"], result,
            input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
            cost_usd=result.get("cost_usd"))
    if result.get("cache_key") != "prod2:llm1:" + request_hash:
        raise RuntimeError("LLM1 receipt does not bind this exact document/prompt request")
    return claim["id"], result, reused


def prepare_analysis_pdf(folder: Path, original: Path, extension: str, original_sha: str,
                         *, historical_cache: Path | None = None):
    """Reuse verified paid derivative bytes, not a fresh archive reconversion."""
    marker = folder / "analysis-pdf.private.json"
    if marker.exists():
        pinned = read_private(marker)
        target = (folder / pinned["relative_path"]).resolve()
        if (not target.is_relative_to(folder.resolve()) or pinned["original_sha256"] != original_sha
            or hashlib.sha256(target.read_bytes()).hexdigest() != pinned["analysis_sha256"]):
            raise RuntimeError("Pinned derivative no longer matches original/bytes")
        return target, pinned["manifest"]
    historical_marker = historical_cache / "analysis-pdf.private.json" if historical_cache else None
    if historical_marker and historical_marker.exists():
        old = read_private(historical_marker)
        if old["original_sha256"] == original_sha:
            source = (historical_cache / old["relative_path"]).resolve()
            if not source.is_relative_to(historical_cache.resolve()):
                raise RuntimeError("Historical derivative escapes its private directory")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != old["analysis_sha256"]:
                raise RuntimeError("Historical paid derivative hash mismatch")
            target = folder / "analysis.pdf"
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(content)
            private_write(marker, {**old, "relative_path": target.name})
            return target, old["manifest"]
    target = prepare_pdf(original, extension, max_bytes=20 * 1024 * 1024)
    manifest_path = folder / "local-document-manifest.json"
    manifest = read_private(manifest_path) if manifest_path.exists() else None
    private_write(marker, {"relative_path": str(target.resolve().relative_to(folder.resolve())),
        "original_sha256": original_sha, "analysis_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "manifest": manifest})
    return target, manifest
