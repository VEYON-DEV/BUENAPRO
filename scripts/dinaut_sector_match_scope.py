"""Exact, manual DIN IA/industrial scope. Pure planning and one-shot guards.

This scope never enables ingestion. Only the explicitly selected existing
readings enter matching; legacy cohort identities and fingerprints stay intact.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading

from dinaut_batch_scope import SECTOR_SCOPE, iso_snapshot
from dinaut_three_dot_selector import PROFILE_ID, TENANT_ID
from match_provider_journal import ProviderBlocked, fingerprint, private_json

VERSION = "dinaut_sector_selection_v1"
SECTORS = frozenset({"ia_automatizacion", "industrial"})
MAX_LIFECYCLE_AGE_SECONDS = 6 * 3600
LIFECYCLE_POLICY = "confirmed_open_sha_bound_6h_v1"
LIFECYCLE_FIELDS = ("confirmed_open","temporal_state","temporal_observed_at",
    "requires_fresh_lifecycle","matching_eligible","closes_at")
DOCUMENT_SQL = """SELECT id::text,tenant_id::text,profile_id::text,title,filename,
    mime_type,size_bytes,document_type,description,tags,metadata_json,valid_until,
    amount,entity_name,usable_for_applications,created_at,updated_at,
    encode(sha256(content),'hex') AS content_sha256
    FROM company_documents WHERE tenant_id=%s AND profile_id=%s
      AND usable_for_applications ORDER BY id"""


def sector_folder(value, root: Path) -> Path:
    if value is None:
        raise ValueError("An explicit immutable sector batch folder is required")
    path = Path(value).absolute()
    base = root.resolve()/"tmp"/SECTOR_SCOPE.folder_name
    if (path.parent.resolve() != base or not re.fullmatch(r"pilot|batch-[0-9]{2,4}", path.name)
            or path.is_symlink() or base.is_symlink()):
        raise ValueError("Use a direct pilot or batch-NN folder inside the DIN sector scope")
    return path.resolve()


def validate_selection(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ValueError("Unsupported exact sector selection version")
    if (value.get("profile_id"),value.get("tenant_id")) != (PROFILE_ID,TENANT_ID):
        raise ValueError("The manual sector batch is DIN-only")
    cases=value.get("cases")
    if not isinstance(cases,list) or not 1 <= len(cases) <= SECTOR_SCOPE.maximum_cases:
        raise ValueError("A finite nonempty selected-ID list is required")
    seen=set()
    for case in cases:
        if not isinstance(case,dict):raise ValueError("Invalid selected identity")
        identity=(case.get("source_key"),case.get("source_id"))
        if (identity[0] not in SECTOR_SCOPE.allowed_sources or type(identity[1]) is not int
                or not 1 <= identity[1] <= 9223372036854775807 or identity in seen):
            raise ValueError("Unsupported, duplicate or invalid selected identity")
        seen.add(identity)
        categories=case.get("sector_categories")
        if (not isinstance(categories,list) or not categories
                or any(not isinstance(item,str) or item not in SECTORS for item in categories)
                or len(categories) != len(set(categories))):
            raise ValueError("Explicit IA/industrial sector proof is required")
        reason=case.get("selection_reason")
        if not isinstance(reason,str) or not reason.strip() or len(reason)>10000:
            raise ValueError("A concrete selection reason is required")
    return value


def read_selection(path: Path, expected_sha256: str) -> tuple[dict,str,bytes]:
    if not re.fullmatch(r"[0-9a-f]{64}",expected_sha256 or ""):
        raise ValueError("An exact SHA-256 of the selected-ID input is required")
    raw=path.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:raise ValueError("Selected-ID input SHA-256 mismatch")
    return validate_selection(json.loads(raw)),digest,raw


def reader_evidence(selection: dict, *, snapshot: Path | None = None):
    evidence=selection.get("reader_evidence")
    if not isinstance(evidence,dict) or not re.fullmatch(r"[0-9a-f]{64}",evidence.get("sha256") or ""):
        raise ValueError("A SHA-bound final reader report is required before matching")
    path=snapshot if snapshot is not None else Path(evidence.get("path") or "")
    if path.is_symlink() or not path.is_file() or path.stat().st_size>50_000_000:
        raise ValueError("Invalid final reader evidence file")
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=evidence["sha256"]:raise ValueError("Final reader report SHA mismatch")
    report=json.loads(raw);records=report.get("records")
    if not isinstance(records,list):raise ValueError("Reader report has no per-ID outcomes")
    by_id={}
    for record in records:
        if not isinstance(record,dict):raise ValueError("Invalid reader outcome")
        source=record.get("source_key") or ("seace_prod2" if "convocation_id" in record else None)
        identity=record.get("convocation_id") if source=="seace_prod2" else record.get("source_id",record.get("id_contrato"))
        if source not in SECTOR_SCOPE.allowed_sources or type(identity) is not int:raise ValueError("Unbound reader outcome identity")
        key=(source,identity)
        if key in by_id:raise ValueError("Duplicate reader outcome identity")
        by_id[key]=record
    for case in selection["cases"]:
        record=by_id.get((case["source_key"],case["source_id"]))
        proof=case.get("lifecycle")
        if (not record or not isinstance(proof,dict)
                or proof!={key:record.get(key) for key in LIFECYCLE_FIELDS}):
            raise ValueError("Selected lifecycle proof differs from the SHA-bound reader outcome")
    return report,raw


def _aware(value):
    if not isinstance(value,str):raise ValueError("An aware official timestamp is required")
    result=datetime.fromisoformat(value.replace("Z","+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError("Naive lifecycle timestamp")
    return result


def lifecycle_disposition(case: dict, *, now: datetime | None = None) -> tuple[bool,str]:
    proof=(case.get("selection") or {}).get("lifecycle") or {}
    if (proof.get("confirmed_open") is not True or proof.get("matching_eligible") is not True
            or proof.get("requires_fresh_lifecycle") is not False
            or proof.get("temporal_state") not in {"registration_open","submission_open"}):
        return False,"needs_fresh_confirmed_open_lifecycle"
    try:
        at=now or datetime.now(timezone.utc)
        observed=_aware(proof.get("temporal_observed_at"))
        closes=_aware(proof.get("closes_at"))
        live_close=_aware(case.get("closes_at"))
    except (ValueError,TypeError):return False,"missing_or_unbound_official_deadline"
    if closes != live_close:return False,"official_deadline_changed"
    age=(at-observed).total_seconds()
    if age < -60 or age > MAX_LIFECYCLE_AGE_SECONDS:return False,"needs_fresh_lifecycle"
    if closes<=at:return False,"closed_at_matching_cutoff"
    return True,"confirmed_open_recent_official_deadline"


def verify_live_lifecycle(case: dict, live: dict, *, now=None):
    valid,reason=lifecycle_disposition({**live,"selection":case["selection"]},now=now)
    if not valid:raise ProviderBlocked("New matching payment blocked: "+reason)


def validate_document_ownership(profile: dict, documents: list[dict]) -> None:
    owned={str(item["id"]):item for item in documents}
    if any((str(item.get("profile_id")),str(item.get("tenant_id"))) != (PROFILE_ID,TENANT_ID)
           for item in documents):
        raise ValueError("Company evidence outside DIN ownership")
    # All documentary cards are backed by the actual DIN library, including SHA.
    for contract in profile.get("experience_json") or []:
        if not isinstance(contract,dict):continue
        summary=contract.get("documentary_summary") or {}
        if not isinstance(summary,dict):continue
        for source in summary.get("sources") or []:
            if not isinstance(source,dict):raise ValueError("Invalid DIN documentary source")
            doc=owned.get(str(source.get("document_id")))
            if not doc or source.get("sha256") != doc.get("content_sha256"):
                raise ValueError("DIN documentary reference is foreign, missing or changed")


def verify_live_company_documents(conn, frozen: dict) -> None:
    documents=iso_snapshot(conn.execute(DOCUMENT_SQL+" FOR SHARE",(TENANT_ID,PROFILE_ID)).fetchall())
    if fingerprint(documents) != fingerprint(frozen["company_documents"]):
        raise ProviderBlocked("DIN documentary library changed since the frozen batch")
    validate_document_ownership(frozen["profile"],documents)


def sector_disposition(case: dict | None, *, now: datetime | None = None) -> tuple[bool,str]:
    """Only current ready readings; preserve all historical matching attempts."""
    from buenapro_worker.matching.readiness import match_readiness
    if not case:return False,"source_not_found"
    ready,reason=match_readiness(case,case.get("facets") or [],provisional_source_review=True)
    if not ready:return False,reason
    if case.get("existing_match_id") is not None:return False,"existing_match_preserved"
    if case.get("prior_match_reservations"):return False,"historical_matching_reservation_preserved"
    if case.get("prior_worker_jobs"):return False,"historical_matching_job_preserved"
    if case.get("prior_provider_events"):return False,"historical_provider_attempt_preserved"
    valid,reason=lifecycle_disposition(case,now=now)
    if not valid:return False,reason
    from buenapro_worker.extraction.commercial_classification import normalize_commercial_classification
    from buenapro_worker.normalization.commercial_categories import derive_commercial_categories
    raw=case.get("raw_extraction_json") or {}
    summary=case.get("summary_json") or {}
    block=raw.get("commercial_classification",summary.get("commercial_classification"))
    if block is not None:
        try:classification=normalize_commercial_classification(block)
        except ValueError:classification=None
        if classification and classification["primary_area"] != "sin_clasificar":
            if not SECTORS.intersection(derive_commercial_categories(classification)):
                return False,"current_classification_outside_selected_sectors"
    return True,reason


def validate_frozen_sector_inputs(frozen: dict, receipt: dict, folder: Path) -> None:
    selection,digest,_=read_selection(folder/"selection.private.json",receipt["selection_sha256"])
    reader_evidence(selection,snapshot=folder/"reader-evidence.private.json")
    if receipt.get("scope") != SECTOR_SCOPE.name or frozen.get("receipt") != {
            key:value for key,value in receipt.items() if key != "inputs_sha256"}:
        raise ValueError("Frozen manual sector receipt binding changed")
    if (receipt.get("lifecycle_policy") != LIFECYCLE_POLICY
            or receipt.get("max_lifecycle_age_seconds") != MAX_LIFECYCLE_AGE_SECONDS):
        raise ValueError("Frozen lifecycle freshness policy changed")
    wanted={(item["source_key"],item["source_id"]):item for item in selection["cases"]}
    actual=frozen.get("cases") or []
    if len(actual) != len(wanted) or len({(c["source_key"],c["source_id"]) for c in actual}) != len(actual):
        raise ValueError("Frozen cases differ from the exact selected-ID input")
    for case in actual:
        proof=wanted.get((case["source_key"],case["source_id"]))
        if not proof or case.get("selection") != proof:raise ValueError("Selected identity proof changed")
        ready,reason=sector_disposition(case,now=_aware(receipt["cutoff_utc"]))
        if case.get("ready") is not ready or case.get("readiness_reason") != reason:
            raise ValueError("Frozen readiness or history disposition changed")
    if (str(frozen["profile"].get("id")),str(frozen["profile"].get("tenant_id"))) != (PROFILE_ID,TENANT_ID):
        raise ValueError("Frozen company identity differs from DIN")
    if frozen["profile"].get("is_active") is not True:raise ValueError("DIN profile is inactive")
    if any(str(line.get("profile_id")) != PROFILE_ID or line.get("is_active") is not True
           for line in frozen["business_lines"]):
        raise ValueError("Frozen business line is not active DIN evidence")
    validate_document_ownership(frozen["profile"],frozen["company_documents"])


class SectorProviderControl:
    """At most two sends; first failure holds every subsequent new request.

    In-flight calls finish through the existing durable journal. A saved hold is
    never cleared automatically; replay-only can finish exact cached responses.
    """
    def __init__(self,folder: Path,*,check_owner=lambda:None,slots=2,replay_only=False):
        if type(slots) is not int or not 1 <= slots <= 2:raise ValueError("One or two model slots required")
        self.folder=folder;self.check_owner=check_owner;self.replay_only=replay_only
        self.stop=threading.Event();self.slots=threading.BoundedSemaphore(slots)
        self.mutex=threading.RLock()
        if (folder/"provider-stop.private.json").exists():self.stop.set()

    def fail(self,reason: str):
        with self.mutex:
            self.stop.set()
            path=self.folder/"provider-stop.private.json"
            if not path.exists():
                private_json(path,{"status":"held","reason":reason,
                    "at_utc":datetime.now(timezone.utc).isoformat(),"automatic_retry":False},exclusive=True)

    def require_send(self):
        with self.mutex:
            if self.stop.is_set() or self.replay_only:
                raise ProviderBlocked("Sector batch held; no new provider sends")
            try:self.check_owner()
            except Exception:
                self.fail("coordinator_connection_lost")
                raise ProviderBlocked("Sector coordinator ownership lost") from None

    def guarded_post(self,post=None):
        def invoke(*args,**kwargs):
            import requests
            with self.slots:
                self.require_send()
                try:response=(post or requests.post)(*args,**kwargs)
                except Exception:
                    self.fail("provider_transport_uncertain")
                    raise
                if response.status_code != 200:self.fail("provider_http_rejected")
                return response
        return invoke


_CONTROL=None


@contextmanager
def active_provider_control(control):
    global _CONTROL
    previous=_CONTROL;_CONTROL=control
    try:yield control
    finally:_CONTROL=previous


def provider_control() -> SectorProviderControl:
    if _CONTROL is None:raise ProviderBlocked("No sector coordinator provider guard")
    return _CONTROL
