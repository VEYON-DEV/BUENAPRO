"""Provider auditing and durable document-selector payment reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import time

import httpx

from .batch_runner import private_write, read_private
from .document_selection import (CACHE_PREFIX, DOCUMENT_SELECTION_POLICY,
    document_selection_request_hash)
from . import selected_repository as repo
from .selected_control import LimitReached


class AuditedJevClient:
    """Record received usage before typed answer validation can fail."""
    def __init__(self, folder, limits, *, client=None, purpose="affinity", before_request=None):
        self.folder, self.limits, self.purpose = Path(folder), limits, purpose
        self.client = client or httpx.Client(timeout=60)
        self.owned = client is None
        self.before_request = before_request or (lambda: self.limits.before_request("jev"))

    def post(self, *args, **kwargs):
        self.before_request()
        start = time.monotonic()
        attempt_id = "jev-attempt-" + str(time.time_ns())
        record = {"provider": "jev", "purpose": self.purpose, "status": "error_unknown",
                  "usage_known": False, "attempt_id": attempt_id,
                  "started_at": datetime.now(timezone.utc).isoformat()}
        private_write(self.folder / (attempt_id + ".started.private.json"), record)
        try:
            response = self.client.post(*args, **kwargs)
            record.update(status="response_received", http_status=response.status_code)
            if response.status_code == 200:
                try:
                    data = response.json()
                    usage = data.get("usage", {})
                    if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")):
                        record.update(usage_known=True, input_tokens=usage["input_tokens"],
                            output_tokens=usage["output_tokens"], cost_usd=usage["input_tokens"] * .042 / 1_000_000)
                except (ValueError, TypeError, AttributeError):
                    pass
            return response
        finally:
            record["elapsed_seconds"] = time.monotonic() - start
            private_write(self.folder / (attempt_id + ".outcome.private.json"), record)

    def close(self):
        if self.owned:
            self.client.close()


def paid_document_selection(conn, folder, *, convocation_id, description, candidates,
                            selector, high_threshold=80, low_threshold=20):
    policy_version = getattr(selector, "policy_version", DOCUMENT_SELECTION_POLICY)
    digest = document_selection_request_hash(description, candidates, model=selector.model,
                                            policy_version=policy_version)
    claim = repo.claim_selection(conn, convocation_id=convocation_id, request_hash=digest,
        model=selector.model, policy_version=policy_version)
    stem = Path(folder) / ("selector-" + digest)
    owner_path, response_path = stem.with_suffix(".claim.private.json"), stem.with_suffix(".response.private.json")
    if claim["state"] == "cached":
        receipt, reused = claim["result"], True
    else:
        if claim["state"] == "claimed":
            private_write(owner_path, {**claim, "request_hash": digest})
        elif owner_path.exists() and response_path.exists():
            claim = read_private(owner_path)
            if claim.get("request_hash") != digest:
                raise RuntimeError("Selector private reservation mismatch")
        else:
            raise RuntimeError("Selector needs reconciliation; no automatic repayment")
        reused = response_path.exists()
        try:
            if reused:
                receipt = read_private(response_path)
            else:
                receipt = selector.select(description, candidates, high_threshold=high_threshold, low_threshold=low_threshold)
                private_write(response_path, receipt)
            repo.complete_selection(conn, request_hash=digest, claim_token=claim["claim_token"], result=receipt)
        except Exception:
            repo.uncertain_selection(conn, request_hash=digest, claim_token=claim["claim_token"])
            raise
    # Global cache answers have ordinal IDs: always reconstruct this exact plan.
    result = selector.select(description, candidates, high_threshold=high_threshold, low_threshold=low_threshold,
                             cache={CACHE_PREFIX + digest: receipt})
    return result, reused


@contextmanager
def provider_permission(conn, folder, limits, *, provider, kind, request_hash, evaluation_key=None):
    """An exhausted budget still permits exact cache/owned-receipt reconciliation."""
    permit = None
    try:
        try:
            permit = limits.reserve_request(provider)
        except LimitReached:
            if kind == "selector":
                row = conn.execute("SELECT * FROM prod2_document_selection_cache WHERE request_hash=%s", (request_hash,)).fetchone()
                stem = "selector-" + request_hash
            elif kind == "affinity":
                row = conn.execute("SELECT * FROM prod2_jev_decision_cache WHERE cache_key=%s", (request_hash,)).fetchone()
                stem = "jev-" + request_hash
            elif kind == "llm1":
                if not isinstance(evaluation_key, str) or len(evaluation_key) != 64:
                    raise ValueError("Exact LLM1 evaluation binding is required")
                row = conn.execute("SELECT * FROM prod2_evaluations WHERE evaluation_key=%s", (evaluation_key,)).fetchone()
                stem = "gemini_extraction-" + request_hash
            else:
                raise ValueError("Unknown paid stage")
            receipt, owner = Path(folder) / (stem + ".response.private.json"), Path(folder) / (stem + ".claim.private.json")
            recovered = False
            if row and row["status"] in {"running", "uncertain"} and receipt.exists() and owner.exists():
                claimed = read_private(owner)
                recovered = str(claimed.get("claim_token")) == str(row.get("claim_token"))
                if kind == "llm1":
                    recovered = recovered and claimed.get("id") == row.get("id")
            if (not row or row["status"] != "done") and not recovered:
                raise
        def issue():
            if permit is None:
                raise LimitReached("Budget permits cache reconciliation only")
            permit.issue()
        yield issue
    finally:
        if permit is not None:
            permit.close()


def audit_summary(folder):
    import math
    records, jev = [], {}
    for path in Path(folder).rglob("jev-attempt-*.private.json"):
        receipt = read_private(path)
        identity = receipt.get("attempt_id", path.name)
        # started/outcome are ONE provider attempt, including interrupted calls.
        if identity not in jev or path.name.endswith(".outcome.private.json"):
            jev[identity] = receipt
    records.extend(jev.values())
    model_attempts = {}
    paths = list(Path(folder).rglob("gemini-selected-*.private.json")) + list(Path(folder).rglob("openai-selected-*.private.json"))
    for path in paths:
        if not path.name.endswith((".started.private.json", ".response.private.json", ".outcome.private.json")):
            continue
        receipt = read_private(path)
        model_attempts.setdefault(receipt["attempt_id"], {})[path.name.split(".")[-3]] = receipt
    for attempt_id, parts in model_attempts.items():
        outcome, response, started = parts.get("outcome", {}), parts.get("response", {}), parts.get("started", {})
        # Pre-generation upload/conversion failures are not billed model calls.
        if not started and not response and not outcome.get("model_call_started"):
            continue
        record = outcome or response or started
        records.append({**record, "provider": "openai" if attempt_id.startswith("openai-selected-") else "gemini",
            "usage_known": response.get("provider_usage_known", outcome.get("provider_usage_known", False)),
            "cost_usd": response.get("cost_usd", outcome.get("cost_usd"))})
    embedding_attempts = {}
    for path in Path(folder).rglob("embedding-*.private.json"):
        if not path.name.endswith((".started.private.json", ".response.private.json", ".outcome.private.json")):
            continue
        receipt = read_private(path)
        if not isinstance(receipt.get("attempt_id"), str) or not receipt["attempt_id"]:
            continue  # Stage summaries/manifests are not provider attempts.
        stage = path.name.split(".")[-3]
        parts = embedding_attempts.setdefault(receipt["attempt_id"], {})
        if stage in parts and parts[stage] != receipt:
            parts["conflict"] = True
        parts[stage] = receipt
    embeddings = []
    for identity, parts in embedding_attempts.items():
        outcome, envelope, started = parts.get("outcome", {}), parts.get("response", {}), parts.get("started", {})
        response = envelope.get("response", {})
        response = response if isinstance(response, dict) else {}
        record = outcome or response or started
        # A committed HTTP response outranks an interrupted local completion.
        # A reservation without a terminal receipt has ambiguous dispatch and
        # must remain visible as potentially billed, never assumed free.
        request_sent = response.get("request_sent", outcome.get("request_sent"))
        free = request_sent is False and not response and not parts.get("conflict")
        known = response.get("provider_usage_known", outcome.get("provider_usage_known",
            outcome.get("usage_known", False)))
        cost = response.get("cost_usd", outcome.get("cost_usd"))
        tokens = response.get("input_tokens", outcome.get("input_tokens"))
        known = bool(known is True and type(tokens) is int and tokens >= 0
            and type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            and not parts.get("conflict"))
        embeddings.append({**record, "attempt_id": identity, "provider": "embedding",
            "request_sent": request_sent, "usage_known": True if free else known,
            "cost_usd": 0 if free else cost if known else None,
            "input_tokens": 0 if free else tokens if known else None,
            "pre_send_failure": free, "receipt_conflict": bool(parts.get("conflict"))})
    records.extend(embeddings)
    return {"attempts": len(records), "jev_calls": sum(r.get("provider") == "jev" for r in records),
            "llm1_calls": sum(r.get("provider") in {"gemini", "openai"} for r in records),
            "openai_calls": sum(r.get("provider") == "openai" for r in records),
            "embedding_attempts": len(embeddings),
            "embedding_calls": sum(r["request_sent"] is not False for r in embeddings),
            "embedding_unknown_dispatch_attempts": sum(r["request_sent"] is None for r in embeddings),
            "embedding_pre_send_failures": sum(r["pre_send_failure"] for r in embeddings),
            "embedding_usage_unknown_attempts": sum(not r["usage_known"] for r in embeddings),
            "embedding_receipt_conflicts": sum(r["receipt_conflict"] for r in embeddings),
            "embedding_input_tokens": sum(r["input_tokens"] or 0 for r in embeddings),
            "embedding_cost_usd": sum(r["cost_usd"] or 0 for r in embeddings),
            "known_cost_usd": sum(r.get("cost_usd") or 0 for r in records),
            "usage_unknown_attempts": sum(not r.get("usage_known", False) for r in records),
            "elapsed_seconds": sum(r.get("elapsed_seconds") or 0 for r in records)}
