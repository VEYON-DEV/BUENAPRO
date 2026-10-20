"""Opt-in local admission and dispatch guards for a frozen ID selection.

The coordinator already owns the global PostgreSQL executor lock. These small
append-only receipts survive cleanup/restart; they are not paid cache entries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading

from .batch_plan import digest
from .batch_runner import private_write, read_private
from .selected_control import LimitReached

VERSION = "prod2_handoff_guards_v1"
TERMINAL = {"closed", "adjudicado", "cancelado", "desierto", "suspendido"}
BOUNDED_ID_NOT_LOCATED = "Accepted source row not found within bounded captured-page neighbors"


class BoundedNativeIdentityNotLocated(RuntimeError):
    """A bounded source lookup is inconclusive, never proof of deletion."""

    def __init__(self, proof):
        super().__init__("bounded_native_identity_not_located")
        self.proof = proof


def document_preparation_review(error, folder):
    """Recognize only the exact economical preparation review and its receipt.

    Source preparation may be free while retrieval reviews can follow known
    paid embeddings. This proof never changes the separate cost audit.
    """
    from .embedding_runtime import EmbeddingPreparationReview
    if type(error) is not EmbeddingPreparationReview:
        return None
    path = Path(folder) / "embedding-preparation-review.private.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = path.read_bytes()
        value = json.loads(data)
        if (not isinstance(value, dict) or value.get("full_pdf_fallback") is not False
                or not isinstance(value.get("corpus_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["corpus_hash"])):
            return None
        reason = value.get("reason")
        if reason in {"source_has_no_usable_text", "incomplete_source_text"}:
            if (set(value) != {"reason", "corpus_hash", "unresolved_pages", "provider_calls", "full_pdf_fallback"}
                    or type(value.get("provider_calls")) is not int or value["provider_calls"] != 0
                    or not isinstance(value.get("unresolved_pages"), list)):
                return None
        elif reason == "retrieval_context_needs_expansion_or_review":
            cost, retrieval = value.get("embedding_cost_usd"), value.get("retrieval_hash")
            if (set(value) != {"reason", "corpus_hash", "retrieval_hash", "embedding_cost_usd", "full_pdf_fallback"}
                    or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
                    or retrieval is not None and (
                        not isinstance(retrieval, str) or not re.fullmatch(r"[0-9a-f]{64}", retrieval))):
                return None
        else:
            return None
        return {"reason": reason, "receipt_sha256": hashlib.sha256(data).hexdigest()}
    except (ValueError, TypeError, OSError):
        return None


def preserve_by_id(conn, item, *, profile_id):
    """Read historical ownership before *any* source or affinity request.

    A valid canonical reading is preserved using the existing strict contract.
    Other profiles/uncertain selector or corpus payments are held for review,
    never relabeled as a free cache hit for this new policy.
    """
    from .prior_reading_guard import preserve_prior_reading, POLICY
    conv = item["row"]["convocation_id"]
    identity = conn.execute("""SELECT id_procedimiento,object_type,last_seen_at
        FROM prod2_discoveries WHERE id_convocatoria=%s""", (conv,)).fetchone()
    if identity and (identity["id_procedimiento"] != item["row"]["procedure_id"]
                     or identity["object_type"] != item["row"]["object_type"]):
        raise ValueError("Handoff historical canonical identity conflicts")
    prior = preserve_prior_reading(conn, convocation_id=conv, profile_id=profile_id, policy=POLICY)
    if prior is not None:
        return prior
    rows = conn.execute("""SELECT 'llm1' AS stage,id::text AS reservation_id,status
        FROM prod2_evaluations WHERE id_convocatoria=%s AND kind='gemini_extraction'
        UNION ALL SELECT 'selector',request_hash,status FROM prod2_document_selection_cache
          WHERE convocation_id=%s AND status<>'done'
        UNION ALL SELECT 'affinity',cache_key,status FROM prod2_jev_decision_cache
          WHERE convocation_id=%s AND status<>'done'
        UNION ALL SELECT DISTINCT 'embedding',a.attempt_id::text,a.status
          FROM prod2_embedding_corpora c JOIN prod2_embedding_chunks ch USING(corpus_hash)
          JOIN prod2_embedding_vectors v ON v.text_sha256=ch.text_sha256
            AND v.model=c.vector_model AND v.dimensions=c.vector_dimensions
          JOIN prod2_embedding_attempts a USING(attempt_id)
          WHERE c.convocation_id=%s AND a.status IN ('running','uncertain','partial_known')""",
        (conv, conv, conv, conv)).fetchall()
    reason = None
    if rows:
        reason = "historical_payment_or_other_profile_reading_requires_review"
    elif identity and identity["last_seen_at"] > datetime.fromisoformat(item["observed_at"]):
        reason = "canonical_listing_observed_after_frozen_mapping"
    if reason is None:
        return None
    return {"status": "needs_review", "llm1_prior_preserved": True,
        "requires_human_review": True, "llm1_cost_usd": 0,
        "prior_reading_preserved": {"policy_version": VERSION, "reason": reason,
            "convocation_id": conv, "prior_reservations": rows,
            "provider_calls": 0, "source_calls": 0, "business_writes": 0,
            "new_reading_policy_equivalence_claimed": False}}


def lifecycle_gate(assessment):
    """Only positively observed open procedures may incur document/model work."""
    if assessment.get("confirmed_open") is True and assessment.get("state") in {
            "registration_open", "submission_open"}:
        return None
    closed = assessment.get("state") in TERMINAL
    return {"status": "closed_without_reading" if closed else "opening_not_confirmed",
        "requires_human_review": not closed, "new_document_requests": 0,
        "reading_gate": {"policy_version": VERSION, "allowed": False,
            "reason": "terminal_or_closed" if closed else "opening_not_positively_confirmed",
            "assessment_sha256": assessment.get("assessment_sha256")}}


def lifecycle_fields(assessment):
    """Project the actual parsed submission deadline, never infer a date."""
    end = assessment.get("evidence", {}).get("windows", {}).get("submission", {}).get("end", {})
    closes = end.get("at")
    try:
        parsed = datetime.fromisoformat(closes)
        observed = datetime.fromisoformat(assessment["as_of"])
        valid = (parsed.tzinfo is not None and parsed.utcoffset() is not None
                 and observed.tzinfo is not None and observed.utcoffset() is not None and parsed > observed)
    except (ValueError, TypeError, KeyError):
        valid = False
    opened = assessment.get("confirmed_open") is True and lifecycle_gate(assessment) is None
    return {"temporal_state": assessment["state"], "temporal_observed_at": assessment["as_of"],
        "confirmed_open": opened, "requires_fresh_lifecycle": False,
        "temporal_reason_codes": assessment.get("reason_codes", []),
        "closes_at": closes if valid else None, "matching_eligible": opened and valid}


class HandoffGuards:
    def __init__(self, folder, *, binding, max_candidates, max_new_documents, stop):
        self.folder = Path(folder)
        self.binding_hash = digest(binding)
        self.max_candidates, self.max_new_documents = max_candidates, max_new_documents
        self.stop = stop
        self.lock = threading.RLock()
        self.budget_deferred = False
        self.root = self.folder / "handoff-guards"
        self.marker = self.root / "dispatch-stop.private.json"
        self.candidates = self._load("candidates")
        self.documents = self._load("documents")
        if len(self.candidates) > max_candidates or len(self.documents) > max_new_documents:
            raise ValueError("Handoff admission ceiling cannot shrink below existing receipts")
        if self.marker.exists():
            self._validate(read_private(self.marker))
            self.stop.set()

    def _validate(self, value):
        if (not isinstance(value, dict) or value.get("version") != VERSION
                or value.get("binding_hash") != self.binding_hash):
            raise ValueError("Handoff guard receipt binding changed")
        return value

    def _load(self, name):
        root = self.root / name
        if root.is_symlink() or self.root.is_symlink():
            raise ValueError("Handoff guard paths must be literal")
        result = {}
        for path in sorted(root.glob("*.private.json")):
            if path.is_symlink():
                raise ValueError("Handoff guard receipt must be literal")
            value = self._validate(read_private(path))
            identity = value.get("identity")
            if not isinstance(identity, dict) or path.stem.split(".")[0] != digest(identity):
                raise ValueError("Handoff guard identity changed")
            result[digest(identity)] = value
        return result

    def _admit(self, kind, identity, maximum):
        with self.lock:
            self.before_request()
            items = self.candidates if kind == "candidates" else self.documents
            key = digest(identity)
            if key in items:
                return items[key]
            if len(items) >= maximum:
                self.budget_deferred = True
                raise LimitReached("Handoff " + kind + " admission ceiling reached")
            value = {"version": VERSION, "binding_hash": self.binding_hash,
                "identity": identity, "admitted_at": datetime.now(timezone.utc).isoformat()}
            private_write(self.root / kind / (key + ".private.json"), value)
            items[key] = value
            return value

    def admit_candidate(self, convocation_id):
        if type(convocation_id) is not int or convocation_id <= 0:
            raise ValueError("Invalid handoff candidate")
        return self._admit("candidates", {"convocation_id": convocation_id}, self.max_candidates)

    def admit_document(self, document_uuid, *, member=None, archive_sha256=None):
        from .repository import _uuid, _sha
        identity = {"document_uuid": _uuid(document_uuid)}
        if member is not None:
            if not isinstance(member, str) or not member or len(member) > 4000:
                raise ValueError("Invalid bounded archive member identity")
            identity.update(member=member, archive_sha256=_sha(archive_sha256))
        return self._admit("documents", identity, self.max_new_documents)

    def bind_document(self, document_uuid, sha256, *, member=None, archive_sha256=None):
        """Bind actual bytes once; a replay can never silently replace a source."""
        from .repository import _sha
        with self.lock:
            admitted = self.admit_document(document_uuid, member=member, archive_sha256=archive_sha256)
            identity, sha = admitted["identity"], _sha(sha256)
            path = self.root / "document-hashes" / (digest(identity) + ".private.json")
            expected = {"version": VERSION, "binding_hash": self.binding_hash,
                        "identity": identity, "sha256": sha}
            if path.exists():
                if path.is_symlink() or read_private(path) != expected:
                    raise ValueError("Handoff admitted source hash changed")
            else:
                private_write(path, expected)

    def before_request(self):
        if self.stop.is_set() or self.marker.exists():
            self.stop.set()
            raise LimitReached("Frozen handoff dispatch stopped; preserve pending work")

    def observe_attempt(self, attempt):
        """Called at the provider boundary, before parsing/persistence/cleanup."""
        sent = attempt.get("request_sent") is not False and (
            attempt.get("provider") == "jev" or attempt.get("model_call_started") is True
            or attempt.get("request_sent") is True)
        known = attempt.get("usage_known", attempt.get("provider_usage_known")) is True
        if not sent or known:
            return
        with self.lock:
            self.stop.set()
            if not self.marker.exists():
                from .selected_extractor import SAFE_GEMINI_ERROR_STATUSES
                status, provider_code = attempt.get("http_status"), attempt.get("provider_error_code")
                private_write(self.marker, {"version": VERSION, "binding_hash": self.binding_hash,
                    "reason": "sent_provider_attempt_with_uncertain_usage", "request_sent": True,
                    "attempt_id": attempt.get("attempt_id"), "provider": attempt.get("provider", "gemini"),
                    "http_status": status if type(status) is int and 100 <= status <= 599 else None,
                    "provider_error_code": provider_code if isinstance(provider_code, str)
                        and provider_code in SAFE_GEMINI_ERROR_STATUSES else None,
                    "cost_usd": None,
                    "observed_at": datetime.now(timezone.utc).isoformat()})

    def check_audit(self, costs):
        if costs.get("usage_unknown_attempts", 0):
            self.observe_attempt({"provider": "jev", "attempt_id": "existing-run-audit",
                                  "usage_known": False})

    def embedding_client(self, client):
        guard = self
        class GuardedClient:
            @property
            def model(self):
                return client.model
            @property
            def dimensions(self):
                return client.dimensions
            @property
            def price_per_million(self):
                return client.price_per_million
            def embed(self, *args, **kwargs):
                from .embedding_client import EmbeddingRequestError
                try:
                    response = client.embed(*args, **kwargs)
                except Exception as error:
                    sent = error.request_sent if isinstance(error, EmbeddingRequestError) else True
                    guard.observe_attempt({"provider": "embedding", "request_sent": sent,
                        "provider_usage_known": False, "http_status": getattr(error, "http_status", None)})
                    raise
                guard.observe_attempt({"provider": "embedding", **response})
                return response
            def close(self):
                client.close()
        return GuardedClient()

    def stop_access(self, status):
        with self.lock:
            self.stop.set()
            if not self.marker.exists():
                private_write(self.marker, {"version": VERSION, "binding_hash": self.binding_hash,
                    "reason": "source_access_blocked", "http_status": status,
                    "observed_at": datetime.now(timezone.utc).isoformat()})

    def report(self):
        with self.lock:
            return {"policy_version": VERSION, "candidate_admissions": len(self.candidates),
                "document_admissions": len(self.documents), "max_candidates": self.max_candidates,
                "max_new_documents": self.max_new_documents,
                "document_unit": "unique_official_UUID_plus_archive_member_identity",
                "budget_deferred": self.budget_deferred,
                "dispatch_stopped": self.marker.exists()}
