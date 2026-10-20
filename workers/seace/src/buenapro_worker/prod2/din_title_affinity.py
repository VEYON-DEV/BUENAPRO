"""DIN-only commercial title affinity; no qualification/matching publication.

Global JEV payment identity remains the existing exact provider request hash.
A separate canonical binding hash versions the profile/metadata projection
without paying again for unchanged provider input. No uncertain send is retried.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import httpx
import psycopg
from psycopg.rows import dict_row

from .analysis import JEV_MODEL, JEV_POLICY_VERSION, JevDescriptionAnalyzer, _commercial, _jev_payload, jev_request_hash
from .accepted_runner import paid_jev
from . import accepted_repository
from .repository import claim_evaluation, complete_evaluation
from .selected_metadata_publication import digest, encoded, file_sha, immutable_write, read_bound
from .batch_runner import private_write

POLICY = "din_title_commercial_affinity_v1"
PROFILE_ID = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"
TENANT_ID = "46ba7840-ae84-4de0-ac98-16fab9e5d5bb"
SELECTION_SHA = "5a3fd548248ee719d616f9bc1994dcdc1edebade84c234911d3c14d170e6f3db"
COUNT = 157
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
RUNTIME_FILES = ("scripts/run_din_selected_title_affinity.py",
    "workers/seace/src/buenapro_worker/prod2/din_title_affinity.py",
    "workers/seace/src/buenapro_worker/prod2/analysis.py",
    "workers/seace/src/buenapro_worker/prod2/accepted_runner.py",
    "workers/seace/src/buenapro_worker/prod2/accepted_repository.py",
    "workers/seace/src/buenapro_worker/prod2/repository.py",
    "workers/seace/src/buenapro_worker/prod2/selected_metadata_publication.py")


class AffinityStop(ValueError):
    """Sanitized fail-closed diagnostic; never raw transport exceptions."""


def atomic_receipt(path, value):
    """Publish complete evidence atomically before concurrent local accounting."""
    if Path(path).exists():
        if read_bound(path) != value:
            raise AffinityStop("Immutable DIN affinity receipt conflicts")
        return
    private_write(Path(path), value)


def connection(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def current_commercial(conn):
    profile = conn.execute("SELECT id,tenant_id,profile_hash,identity_json->>'descripcion' AS descripcion "
        "FROM company_profiles WHERE id=%s AND tenant_id=%s AND is_active", (PROFILE_ID, TENANT_ID)).fetchone()
    if not profile or not isinstance(profile["profile_hash"], str) or not profile["profile_hash"]:
        raise AffinityStop("Current active DIN profile identity/hash is unavailable")
    lines = conn.execute("SELECT nombre FROM business_lines WHERE profile_id=%s AND is_active ORDER BY nombre",
                         (PROFILE_ID,)).fetchall()
    commercial = _commercial([{"descripcion": profile["descripcion"], "lineas": [row["nombre"] for row in lines]}])
    if not commercial[0]["descripcion"] or not commercial[0]["lineas"]:
        raise AffinityStop("DIN commercial description and active business lines are required")
    return {"profile_id": PROFILE_ID, "tenant_id": TENANT_ID, "profile_hash": profile["profile_hash"],
            "commercial_profiles": commercial, "commercial_profile_hash": digest(commercial)}


def selected_rows(conn, ids):
    if len(ids) != COUNT or len(set(ids)) != COUNT:
        raise AffinityStop("Only the exact 157 selected metadata IDs are permitted")
    rows = conn.execute("SELECT p.id_convocatoria,p.opportunity_id,p.description,p.row_hash "
        "FROM prod2_discoveries p JOIN prod2_feed_publications fp ON fp.id_convocatoria=p.id_convocatoria "
        "AND fp.source_row_hash=p.row_hash AND fp.selection_sha256=%s "
        "WHERE p.id_convocatoria=ANY(%s) ORDER BY p.id_convocatoria", (SELECTION_SHA, ids)).fetchall()
    if len(rows) != COUNT or {row["id_convocatoria"] for row in rows} != set(ids):
        raise AffinityStop("All 157 current metadata publications must exist before freezing DIN affinity")
    return [{"convocation_id": row["id_convocatoria"], "opportunity_id": str(row["opportunity_id"]),
             "description": row["description"], "source_row_hash": row["row_hash"],
             "source_description_sha256": hashlib.sha256(row["description"].encode()).hexdigest()} for row in rows]


def canonical_binding(case, profile):
    provider_hash = jev_request_hash(case["description"], commercial_profiles=profile["commercial_profiles"])
    binding = {"scope": POLICY, "provider_request_hash": provider_hash, "profile_hash": profile["profile_hash"],
               "commercial_profile_hash": profile["commercial_profile_hash"], "source_row_hash": case["source_row_hash"],
               "id_convocatoria": str(case["convocation_id"])}
    return {**case, "provider_request_hash": provider_hash, "canonical_binding_hash": digest(binding)}


def committed_bindings(root):
    root = Path(root)
    bindings = {}
    for name in RUNTIME_FILES:
        actual = file_sha(root / name)
        try:
            committed = subprocess.check_output(["git", "show", "HEAD:"+name], cwd=root, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            raise AffinityStop("DIN affinity runtime must be committed before prepare/execute") from None
        if hashlib.sha256(committed).hexdigest() != actual:
            raise AffinityStop("DIN affinity runtime contains uncommitted changes")
        bindings[name] = actual
    return bindings


def freeze(conn, selection_path, *, root, run_id):
    selected = read_bound(selection_path, SELECTION_SHA)
    ids = [row["convocation_id"] for row in selected["candidates"]]
    profile = current_commercial(conn)
    cases = [canonical_binding(row, profile) for row in selected_rows(conn, ids)]
    payloads = {case["provider_request_hash"]: _jev_payload(case["description"], profile["commercial_profiles"], JEV_MODEL)
                for case in cases}
    if any(len(encoded(payload)) > 60_000 for payload in payloads.values()):
        raise AffinityStop("Complete commercial request exceeds the local finite size guard; never truncate")
    return {"version": POLICY, "run_id": run_id, "profile": profile, "model": JEV_MODEL,
        "provider_policy_version": JEV_POLICY_VERSION, "selection_path": str(Path(selection_path).resolve()),
        "selection_sha256": SELECTION_SHA, "prepared_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases, "runtime_bindings": committed_bindings(root), "max_requests": COUNT,
        "budget_usd": "0.10", "analysis_basis": "title", "requires_eligibility_analysis": False,
        "not_qualification_or_win_probability": True}


def validate_plan(plan, expected_sha, *, root=None):
    if (digest(plan) != expected_sha or plan.get("version") != POLICY or plan.get("model") != JEV_MODEL
            or plan.get("provider_policy_version") != JEV_POLICY_VERSION or plan.get("selection_sha256") != SELECTION_SHA
            or plan.get("max_requests") != COUNT or plan.get("budget_usd") != "0.10"):
        raise AffinityStop("Frozen DIN title-affinity scope/model/budget changed")
    profile = plan["profile"]
    if (profile["profile_id"] != PROFILE_ID or profile["tenant_id"] != TENANT_ID
            or len(profile["commercial_profiles"]) != 1
            or digest(profile["commercial_profiles"]) != profile["commercial_profile_hash"]):
        raise AffinityStop("DIN-only commercial profile binding changed")
    selected = read_bound(plan["selection_path"], SELECTION_SHA)
    if {case["convocation_id"] for case in plan["cases"]} != {row["convocation_id"] for row in selected["candidates"]} or len(plan["cases"]) != COUNT:
        raise AffinityStop("DIN title-affinity cases differ from the exact selection")
    for case in plan["cases"]:
        if canonical_binding(case, profile) != case:
            raise AffinityStop("DIN title or canonical/provider request binding changed")
        if hashlib.sha256(case["description"].encode()).hexdigest() != case["source_description_sha256"]:
            raise AffinityStop("DIN source title hash changed")
    if root is not None and committed_bindings(root) != plan["runtime_bindings"]:
        raise AffinityStop("DIN affinity runtime differs from frozen committed code")


class ReplayClient:
    """Reparse one exact durable response through the existing analyzer offline."""
    def __init__(self, payload, raw):
        self.payload, self.raw = payload, raw
    def post(self, url, *, headers, json):
        if url != ENDPOINT or json != self.payload:
            raise AffinityStop("Offline replay request differs from the exact paid payload")
        return httpx.Response(200, json=self.raw)
    def close(self):
        pass


def check_dispatch_ownership(owner, owner_backend_pid, worker, cases_and_claims, provider_hash,
                             provider_token, profile, stop, owner_guard):
    """Revalidate the live coordinator and every paid owner immediately pre-send.

    A connection per consumer is not proof that the coordinator's profile lock
    still exists. The final proof comes from the original owner's exact backend
    and pg_locks key after current profile/source/lease checks on the worker.
    """
    if stop.is_set() or owner.closed or owner.broken or worker.closed or worker.broken:
        raise AffinityStop("Coordinator or worker connection unavailable before provider dispatch")
    try:
        with worker.transaction():
            provider = worker.execute("SELECT status,claim_token,model,policy_version,lease_until>clock_timestamp() AS live "
                "FROM prod2_jev_decision_cache WHERE cache_key=%s FOR SHARE", (provider_hash,)).fetchone()
            if (not provider or provider["status"] != "running" or not provider["live"]
                    or str(provider["claim_token"]) != provider_token or provider["model"] != JEV_MODEL
                    or provider["policy_version"] != JEV_POLICY_VERSION):
                raise AffinityStop("Provider reservation ownership or lease changed before dispatch")
            ids = [claim["id"] for _, claim in cases_and_claims]
            canonical = {row["id"]: row for row in worker.execute("SELECT id,id_convocatoria,kind,model,policy_version,"
                "status,claim_token,profile_id,request_hash,lease_until>clock_timestamp() AS live FROM prod2_evaluations "
                "WHERE id=ANY(%s) FOR SHARE", (ids,)).fetchall()}
            for case, claim in cases_and_claims:
                row = canonical.get(claim["id"])
                if (row is None or row["status"] != "running" or not row["live"]
                        or str(row["claim_token"]) != claim["claim_token"]
                        or str(row["profile_id"]) != PROFILE_ID or row["request_hash"] != case["canonical_binding_hash"]
                        or row["kind"] != "jev_affinity" or row["model"] != JEV_MODEL or row["policy_version"] != POLICY
                        or row["id_convocatoria"] != case["convocation_id"]):
                    raise AffinityStop("Canonical reservation ownership or lease changed before dispatch")
            if current_commercial(worker) != profile:
                raise AffinityStop("DIN commercial profile changed immediately before dispatch")
            convs = [case["convocation_id"] for case, _ in cases_and_claims]
            sources = {row["id_convocatoria"]: row for row in worker.execute(
                "SELECT p.id_convocatoria,p.opportunity_id,p.description,p.row_hash FROM prod2_discoveries p "
                "JOIN prod2_feed_publications fp ON fp.id_convocatoria=p.id_convocatoria "
                "AND fp.source_row_hash=p.row_hash AND fp.selection_sha256=%s "
                "WHERE p.id_convocatoria=ANY(%s) FOR SHARE OF p,fp", (SELECTION_SHA, convs)).fetchall()}
            for case, _ in cases_and_claims:
                row = sources.get(case["convocation_id"])
                if (row is None or str(row["opportunity_id"]) != case["opportunity_id"]
                        or row["description"] != case["description"] or row["row_hash"] != case["source_row_hash"]):
                    raise AffinityStop("Selected source identity changed immediately before dispatch")
        # The original coordinator's session, not a consumer session, must prove
        # its exact two-key advisory ownership as the last PG check before HTTP.
        with owner_guard:
            if owner.closed or owner.broken or stop.is_set():
                raise AffinityStop("Coordinator became unavailable before provider dispatch")
            proof = owner.execute("SELECT pg_backend_pid() AS backend_pid, EXISTS (SELECT 1 FROM pg_locks "
                "WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND mode='ExclusiveLock' "
                "AND classid=hashtext('dinaut_three_dot_llm2')::oid AND objid=hashtext(%s)::oid AND objsubid=2) AS locked",
                (PROFILE_ID,)).fetchone()
            if not proof["locked"] or proof["backend_pid"] != owner_backend_pid:
                raise AffinityStop("Original DIN coordinator no longer owns its exact profile lock")
    except psycopg.Error:
        raise AffinityStop("PostgreSQL ownership proof unavailable before provider dispatch") from None


def verify_result(case, profile, result):
    payload = _jev_payload(case["description"], profile["commercial_profiles"], JEV_MODEL)
    if not isinstance(result, dict) or not isinstance(result.get("raw_response"), dict):
        raise AffinityStop("A known original JEV response is required; never invent a score")
    expected = JevDescriptionAnalyzer("offline-validation", client=ReplayClient(payload, result["raw_response"])).classify(
        case["description"], commercial_profiles=profile["commercial_profiles"])
    for key in ("cache_key", "model", "policy_version", "affinity_index", "affinity_confidence", "domain", "information",
                "needs_detail", "input_tokens", "output_tokens", "cost_usd", "raw_response"):
        if result.get(key) != expected.get(key):
            raise AffinityStop("Saved JEV response does not reproduce its claimed affinity/usage")
    if expected["cache_key"] != "prod2:jev:" + case["provider_request_hash"]:
        raise AffinityStop("JEV response belongs to another commercial request")
    return expected


class CaptureClient:
    def __init__(self, client, folder, case, profile, stop, pace, guard):
        self.client, self.folder, self.case, self.profile, self.stop, self.pace = client, folder, case, profile, stop, pace
        self.guard = guard
    def post(self, url, *, headers, json):
        key = self.case["provider_request_hash"]
        expected = _jev_payload(self.case["description"], self.profile["commercial_profiles"], JEV_MODEL)
        if url != ENDPOINT or json != expected:
            raise AffinityStop("Paid JEV title request differs from frozen DIN-only input")
        if (self.folder / f"raw-{key}.private.json").exists():
            raise AffinityStop("An original sent response exists; never send it again")
        claim = read_bound(self.folder / f"jev-{key}.claim.private.json")
        if claim.get("request_hash") != key:
            raise AffinityStop("Original provider claim differs from the frozen request")
        self.pace()
        if self.stop.is_set():
            raise AffinityStop("A prior sent failure stopped new commercial requests")
        try:
            self.guard(claim["claim_token"])
        except Exception as error:
            self.stop.set()
            atomic_receipt(self.folder / f"guard-denied-{key}.private.json", {
                "provider_request_hash": key, "sent": False,
                "reason": str(error) if isinstance(error, AffinityStop) else "Pre-send ownership guard failed",
                "at": datetime.now(timezone.utc).isoformat()})
            raise AffinityStop("Pre-send ownership guard rejected this provider dispatch") from None
        marker = {"provider_request_hash": key, "request_sha256": digest(expected),
                  "claim_token": claim["claim_token"], "started_at": datetime.now(timezone.utc).isoformat()}
        atomic_receipt(self.folder / f"started-{key}.private.json", marker)
        try:
            response = self.client.post(url, headers=headers, json=json)
        except Exception as error:
            self.stop.set()
            atomic_receipt(self.folder / f"raw-{key}.private.json", {**marker, "http_status": None,
                "body": None, "transport_error_type": type(error).__name__})
            raise AffinityStop("Commercial provider send is uncertain; no automatic retry") from None
        atomic_receipt(self.folder / f"raw-{key}.private.json", {**marker, "http_status": response.status_code,
            "body": response.text, "received_at": datetime.now(timezone.utc).isoformat()})
        if response.status_code != 200:
            self.stop.set()
        return response
    def close(self):
        self.client.close()


def materialized_result(case, profile, result, *, reused):
    verify_result(case, profile, result)
    return {**result, "profile_id": PROFILE_ID, "request_context": {
        "description": case["description"], "commercial_profiles": profile["commercial_profiles"]},
        "meta": {"scope": POLICY, "analysis_basis": "title", "source_key": "seace_prod2",
            "id_convocatoria": str(case["convocation_id"]), "profile_id": PROFILE_ID, "tenant_id": TENANT_ID,
            "profile_hash": profile["profile_hash"], "commercial_profile_hash": profile["commercial_profile_hash"],
            "commercial_profile": profile["commercial_profiles"][0], "source_row_hash": case["source_row_hash"],
            "source_description_sha256": case["source_description_sha256"],
            "provider_request_hash": case["provider_request_hash"], "canonical_binding_hash": case["canonical_binding_hash"],
            "provider_policy_version": JEV_POLICY_VERSION, "provider_cache_reused": reused,
            "not_qualification_or_win_probability": True}}


def local_accounting(folder, plan):
    """Unique original sends, including known raw usage after local parse failures."""
    requests = {case["provider_request_hash"]: case for case in plan["cases"]}
    input_tokens, output_tokens, sent, unknown = 0, 0, 0, 0
    for key, case in requests.items():
        started = Path(folder) / "provider" / f"started-{key}.private.json"
        raw_path = Path(folder) / "provider" / f"raw-{key}.private.json"
        if not started.exists():
            continue
        sent += 1
        if not raw_path.exists():
            unknown += 1
            continue
        raw = read_bound(raw_path)
        expected = _jev_payload(case["description"], plan["profile"]["commercial_profiles"], JEV_MODEL)
        if raw.get("provider_request_hash") != key or raw.get("request_sha256") != digest(expected):
            raise AffinityStop("Paid raw accounting receipt differs from frozen provider input")
        try:
            body = json.loads(raw["body"])
            usage = body["usage"]
            if any(type(usage.get(field)) is not int or usage[field] < 0 for field in ("input_tokens", "output_tokens")):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            unknown += 1
            continue
        input_tokens += usage["input_tokens"]
        output_tokens += usage["output_tokens"]
    return {"sent_provider_requests_local": sent, "input_tokens_local": input_tokens, "output_tokens_local": output_tokens,
            "known_cost_usd_local": str(Decimal(input_tokens)*Decimal("0.042")/Decimal(1000000)),
            "unknown_sent_attempts_local": unknown}


def execute(dsn, plan, folder, expected_sha, api_key, *, root=None, limit=10, concurrency=2, client_factory=None):
    validate_plan(plan, expected_sha, root=root)
    if not api_key or type(limit) is not int or not 1 <= limit <= COUNT or concurrency not in (1, 2):
        raise AffinityStop("Finite DIN affinity requires a credential, limit 1..157 and at most two slots")
    folder = Path(folder)
    paid_folder = folder / "provider"
    paid_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = plan["profile"]
    stop, pacing_lock, owner_guard, last_start = threading.Event(), threading.Lock(), threading.Lock(), [0.0]
    def pace():
        with pacing_lock:
            delay = 1.0 - (time.monotonic() - last_start[0])
            if delay > 0:
                time.sleep(delay)
            last_start[0] = time.monotonic()

    def work(cases):
        if stop.is_set():
            return {"status": "skipped_after_stop", "count": len(cases)}
        leader = cases[0]
        key = leader["provider_request_hash"]
        with connection(dsn) as conn:
            claims = []
            for case in cases:
                claim_path = folder / "canonical-claims" / f"{case['canonical_binding_hash']}.private.json"
                claim = claim_evaluation(conn, case["convocation_id"], kind="jev_affinity", request_hash=case["canonical_binding_hash"],
                    model=JEV_MODEL, policy_version=POLICY, profile_id=PROFILE_ID, lease_seconds=3600)
                if claim["status"] == "cached":
                    expected = materialized_result(case, profile, claim["result_json"],
                        reused=claim["result_json"].get("meta", {}).get("provider_cache_reused", False))
                    if expected != claim["result_json"]:
                        raise AffinityStop("Completed canonical affinity does not match the frozen metadata/profile")
                    continue
                if claim["status"] == "claimed":
                    immutable_write(claim_path, claim)
                elif claim_path.exists():
                    original_claim = read_bound(claim_path)
                    row = conn.execute("SELECT claim_token FROM prod2_evaluations WHERE id=%s", (claim["id"],)).fetchone()
                    if original_claim["id"] != claim["id"] or str(row["claim_token"]) != original_claim["claim_token"]:
                        raise AffinityStop("Incomplete canonical affinity has a different owner")
                    claim = original_claim
                else:
                    raise AffinityStop("Canonical affinity is busy/uncertain; no automatic repayment")
                saved_path = folder / "canonical-responses" / f"{case['canonical_binding_hash']}.private.json"
                if saved_path.exists():
                    saved = read_bound(saved_path)
                    cached = saved.get("meta", {}).get("provider_cache_reused")
                    if type(cached) is not bool or materialized_result(case, profile, saved, reused=cached) != saved:
                        raise AffinityStop("Saved canonical affinity response does not bind this original owner/input")
                    complete_evaluation(conn, claim["id"], claim["claim_token"], saved,
                        input_tokens=0 if cached else saved["input_tokens"], output_tokens=0 if cached else saved["output_tokens"],
                        cost_usd=0 if cached else saved["cost_usd"])
                    continue
                claims.append((case, claim))
            if not claims:
                return {"status": "canonical_cached", "count": len(cases), "provider_request_hash": key}
            raw_path = paid_folder / f"raw-{key}.private.json"
            normalized_path = paid_folder / f"jev-{key}.response.private.json"
            # A known exact raw response can repair local parsing/completion without HTTP.
            if raw_path.exists() and not normalized_path.exists():
                raw = read_bound(raw_path)
                original_provider_claim = read_bound(paid_folder / f"jev-{key}.claim.private.json")
                expected_payload = _jev_payload(leader["description"], profile["commercial_profiles"], JEV_MODEL)
                if (raw["http_status"] != 200 or raw["provider_request_hash"] != key
                        or raw["request_sha256"] != digest(expected_payload)
                        or raw["claim_token"] != original_provider_claim["claim_token"]):
                    raise AffinityStop("Original provider send is unresolved; no HTTP retry")
                data = json.loads(raw["body"])
                result = JevDescriptionAnalyzer("offline-recovery", client=ReplayClient(expected_payload, data)).classify(
                    leader["description"], commercial_profiles=profile["commercial_profiles"])
                immutable_write(normalized_path, {**result, "profile_id": PROFILE_ID})
            client = client_factory() if client_factory else httpx.Client(timeout=90)
            guard = lambda token: check_dispatch_ownership(owner, owner_backend_pid, conn, claims,
                key, token, profile, stop, owner_guard)
            capture = CaptureClient(client, paid_folder, leader, profile, stop, pace, guard)
            try:
                analyzer = JevDescriptionAnalyzer(api_key, client=capture)
                _, result, reused = paid_jev(conn, paid_folder, {"convocation_id": leader["convocation_id"],
                    "description": leader["description"]}, profile["commercial_profiles"], PROFILE_ID, analyzer, accepted_repository)
                verify_result(leader, profile, result)
                if result["input_tokens"] > 32768:
                    # Keep the known paid response, but do not admit more work past the local reserve estimate.
                    stop.set()
                for index, (case, claim) in enumerate(claims):
                    cached = reused or index > 0
                    output = materialized_result(case, profile, result, reused=cached)
                    immutable_write(folder / "canonical-responses" / f"{case['canonical_binding_hash']}.private.json", output)
                    complete_evaluation(conn, claim["id"], claim["claim_token"], output,
                        input_tokens=0 if cached else output["input_tokens"], output_tokens=0 if cached else output["output_tokens"],
                        cost_usd=0 if cached else output["cost_usd"])
                return {"status": "done", "count": len(cases), "provider_request_hash": key, "reused": reused,
                        "new_known_cost_usd": 0 if reused else result["cost_usd"]}
            except Exception:
                stop.set()
                for _, claim in claims:
                    conn.execute("UPDATE prod2_evaluations SET status='uncertain',updated_at=now() "
                        "WHERE id=%s AND claim_token=%s AND status='running'", (claim["id"], claim["claim_token"]))
                raise
            finally:
                capture.close()

    outcomes = []
    with connection(dsn) as owner:
        locked = owner.execute("SELECT pg_try_advisory_lock(hashtext('dinaut_three_dot_llm2'),hashtext(%s)) AS locked, "
                               "pg_backend_pid() AS backend_pid", (PROFILE_ID,)).fetchone()
        if not locked["locked"]:
            raise AffinityStop("Another DIN LLM2/affinity coordinator owns this profile")
        owner_backend_pid = locked["backend_pid"]
        try:
            with owner.transaction():
                owner.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                if current_commercial(owner) != profile:
                    raise AffinityStop("DIN commercial profile changed after freezing")
                live = selected_rows(owner, [case["convocation_id"] for case in plan["cases"]])
                if [canonical_binding(case, profile) for case in live] != plan["cases"]:
                    raise AffinityStop("Selected metadata/title changed after freezing")
            groups = {}
            for case in plan["cases"]:
                groups.setdefault(case["provider_request_hash"], []).append(case)
            global_receipts = owner.execute("SELECT cache_key,status,result_json,claim_token FROM prod2_jev_decision_cache "
                "WHERE cache_key=ANY(%s)", (list(groups),)).fetchall()
            for row in global_receipts:
                case = groups[row["cache_key"]][0]
                if row["status"] == "done":
                    verify_result(case, profile, row["result_json"])
                else:
                    raw_path = paid_folder / f"raw-{row['cache_key']}.private.json"
                    claim_path = paid_folder / f"jev-{row['cache_key']}.claim.private.json"
                    if not raw_path.exists() or not claim_path.exists():
                        raise AffinityStop("A historical provider request is running/uncertain; no new inference")
                    raw, original = read_bound(raw_path), read_bound(claim_path)
                    payload = _jev_payload(case["description"], profile["commercial_profiles"], JEV_MODEL)
                    if (raw.get("http_status") != 200 or raw.get("provider_request_hash") != row["cache_key"]
                            or raw.get("request_sha256") != digest(payload)
                            or raw.get("claim_token") != original.get("claim_token")
                            or str(row["claim_token"]) != original.get("claim_token")):
                        raise AffinityStop("Original provider send is unresolved; no new inference")
                    try:
                        data = json.loads(raw["body"])
                        recovered = JevDescriptionAnalyzer("offline-preflight", client=ReplayClient(payload, data)).classify(
                            case["description"], commercial_profiles=profile["commercial_profiles"])
                        verify_result(case, profile, recovered)
                    except Exception:
                        owner.execute("UPDATE prod2_jev_decision_cache SET status='uncertain',"
                            "blocked_reason='raw_response_requires_review',updated_at=now() "
                            "WHERE cache_key=%s AND claim_token=%s AND status='running'",
                            (row["cache_key"], original["claim_token"]))
                        raise AffinityStop("Original HTTP 200 response is malformed; preserve it without new inference") from None
                    recovered["profile_id"] = PROFILE_ID
                    normalized_path = paid_folder / f"jev-{row['cache_key']}.response.private.json"
                    atomic_receipt(normalized_path, recovered)
                    accepted_repository.complete_jev(owner, request_hash=row["cache_key"],
                        claim_token=original["claim_token"], result=recovered)
            canonical_receipts = owner.execute("SELECT id,request_hash,status,claim_token FROM prod2_evaluations "
                "WHERE kind='jev_affinity' AND profile_id=%s AND policy_version=%s AND request_hash=ANY(%s)",
                (PROFILE_ID, POLICY, [case["canonical_binding_hash"] for case in plan["cases"]])).fetchall()
            for row in canonical_receipts:
                if row["status"] != "done":
                    path = folder / "canonical-claims" / f"{row['request_hash']}.private.json"
                    if not path.exists():
                        raise AffinityStop("An incomplete canonical affinity has no original local owner receipt")
                    original = read_bound(path)
                    if original.get("id") != row["id"] or original.get("claim_token") != str(row["claim_token"]):
                        raise AffinityStop("Incomplete canonical affinity has a different original owner")
            pending_groups = list(groups.values())
            # Finite limit counts semantic groups admitted, including exact-cache checks.
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures, index = {}, 0
                while futures or (index < min(limit, len(pending_groups)) and not stop.is_set()):
                    while len(futures) < concurrency and index < min(limit, len(pending_groups)) and not stop.is_set():
                        accounting = local_accounting(folder, plan)
                        reserve_usd = Decimal(32768)*Decimal("0.042")/Decimal(1000000)
                        if Decimal(accounting["known_cost_usd_local"]) + reserve_usd*(len(futures)+1) > Decimal(plan["budget_usd"]):
                            stop.set()
                            break
                        group = pending_groups[index]
                        futures[pool.submit(work, group)] = group
                        index += 1
                    if not futures:
                        break
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        group = futures.pop(future)
                        try:
                            outcomes.append(future.result())
                        except Exception as error:
                            stop.set()
                            with connection(dsn) as failed:
                                for case in group:
                                    claim_path = folder / "canonical-claims" / f"{case['canonical_binding_hash']}.private.json"
                                    if claim_path.exists():
                                        claim = read_bound(claim_path)
                                        failed.execute("UPDATE prod2_evaluations SET status='uncertain',updated_at=now() "
                                            "WHERE id=%s AND claim_token=%s AND status='running'",
                                            (claim["id"], claim["claim_token"]))
                            outcomes.append({"status": "stopped", "provider_request_hash": group[0]["provider_request_hash"],
                                             "error_type": type(error).__name__})
            report = {"scope": POLICY, "status": "attention_required" if stop.is_set() else "pilot_complete" if index < len(groups) else "complete",
                "plan_sha256": expected_sha, "selected": COUNT, "groups_admitted": index, "outcomes": outcomes,
                "canonical_cases_done_this_invocation": sum(outcome.get("count", 0) for outcome in outcomes if outcome["status"] in {"done", "canonical_cached"}),
                "new_known_cost_usd": str(sum((Decimal(str(outcome.get("new_known_cost_usd", 0))) for outcome in outcomes), Decimal(0))),
                "no_automatic_retry": True, "eligibility_matches_created": 0, "source_calls": 0,
                "budget_is_invoice_guarantee": False, **local_accounting(folder, plan)}
            # Losing the coordinator session is itself a stop, but known replies
            # from consumers remain drainable and are counted through fresh PG.
            with connection(dsn) as readback:
                canonical = readback.execute("SELECT status,count(*)::int AS count FROM prod2_evaluations "
                    "WHERE kind='jev_affinity' AND profile_id=%s AND policy_version=%s AND request_hash=ANY(%s) GROUP BY status",
                    (PROFILE_ID, POLICY, [case["canonical_binding_hash"] for case in plan["cases"]])).fetchall()
            report["canonical_statuses"] = {row["status"]: row["count"] for row in canonical}
            immutable_write(folder / "reports" / f"execute-{time.time_ns()}.private.json", report)
            return report
        finally:
            if not owner.closed and not owner.broken:
                try:
                    owner.execute("SELECT pg_advisory_unlock(hashtext('dinaut_three_dot_llm2'),hashtext(%s))", (PROFILE_ID,))
                except psycopg.Error:
                    pass  # A broken original session has already released its advisory locks.
