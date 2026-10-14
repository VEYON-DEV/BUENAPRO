"""Frozen-run proof that one exact native year/object/date scope was empty.

Only two independently created JSF/HTTP sessions with verified zero counters can
authorize skipping keyword queries. Nonempty/error responses never authorize it.
Proofs describe an observed empty scope, not future emptiness, other years, or an
atomic national snapshot. The caller owns pacing and chooses a NEW folder/run
when it needs a fresh observation. No DB, Redis, model or document operations.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .batch_plan import native_arguments, page_capture
from .batch_runner import private_write, read_private
from .client import Prod2ProtocolError
from .discovery import validate_capture


PROBE_VERSION = "prod2_observed_empty_scope_v1"


class EmptyScopeProofError(ValueError):
    """Sanitized failure: never contains portal bodies, cookies or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _query(value):
    if not isinstance(value, dict) or value.get("keyword") != "":
        raise EmptyScopeProofError("empty_keyword_required")
    # The discovery importer intentionally prohibits empty keywords. Validate
    # the same query contract with a local sentinel, never persisted or searched.
    validator = {**value, "keyword": "EMPTY_SCOPE_VALIDATION"}
    normalized = validate_capture({"query": validator, "observed_at": _now().isoformat(),
        "page": {"number": 1, "total_pages": 1, "total_results": 0, "page_size": 1},
        "rows": []}, now=_now()).query
    return {**normalized, "keyword": ""}


def _snapshot(query, snapshot):
    if not isinstance(snapshot, dict):
        raise EmptyScopeProofError("native_snapshot_missing")
    expected = {"keyword": "", **native_arguments(query),
                "year_filter_kind": "convocatoria_year", "native_atomic_snapshot": False}
    if (any(snapshot.get(key) != value for key, value in expected.items())
            or snapshot.get("native_atomic_snapshot") is not False):
        raise EmptyScopeProofError("native_filter_snapshot_mismatch")
    total, size, pages = (snapshot.get(key) for key in ("total_rows", "page_size", "total_pages"))
    if (type(total) is not int or total < 0 or type(size) is not int or not 1 <= size <= 100
            or type(pages) is not int or pages != max(1, (total + size - 1) // size)):
        raise EmptyScopeProofError("native_counter_snapshot_invalid")
    # Whitelist only public filter/count metadata; unknown fields can be session
    # tokens and are never included in private proofs or provider inputs.
    return {**expected, "total_rows": total, "page_size": size, "total_pages": pages}


def _observe(source, query):
    raw = source.search("", **native_arguments(query), allow_empty_keyword=True)
    if raw.get("search_verified") is not True or raw.get("counter_origin") != "native_search_counter":
        raise EmptyScopeProofError("unverified_native_search")
    snapshot = _snapshot(query, raw.get("snapshot"))
    total, size, pages = snapshot["total_rows"], snapshot["page_size"], snapshot["total_pages"]
    counter = raw.get("counter")
    expected = [1, min(size, total), total, 1, pages] if total else [0, 0, 0, 1, 1]
    if (not isinstance(counter, (list, tuple)) or len(counter) != 5
            or any(type(value) is not int for value in counter) or list(counter) != expected
            or not isinstance(raw.get("rows"), list) or len(raw["rows"]) != min(size, total)):
        raise EmptyScopeProofError("native_counter_rows_mismatch")
    # Independently validate first-page IDs/object/date fields when nonempty.
    # A clipped validation page is not a persisted proof or complete query.
    observed = _now()
    validator = page_capture({**query, "keyword": "EMPTY_SCOPE_VALIDATION"}, raw, observed)
    validator["page"] = {"number": 1, "total_pages": 1,
                         "total_results": len(raw["rows"]), "page_size": size}
    rows = validate_capture(validator, now=_now()).rows
    return {"observed_at": observed.isoformat(), "counter": list(counter), "snapshot": snapshot,
        "snapshot_hash": _hash(snapshot), "row_count": len(rows),
        "row_identity_hash": _hash([[row["procedure_id"], row["convocation_id"]] for row in rows]),
        "search_verified": True, "counter_origin": "native_search_counter"}


def _verified(proof, query):
    if not isinstance(proof, dict):
        raise EmptyScopeProofError("proof_binding_mismatch")
    value = {key: item for key, item in proof.items() if key != "proof_hash"}
    if (proof.get("version") != PROBE_VERSION or proof.get("query") != query
            or proof.get("scope_hash") != _hash(query) or proof.get("proof_hash") != _hash(value)
            or type(proof.get("scope_zero_verified")) is not bool
            or proof.get("source_atomic_snapshot") is not False
            or proof.get("coverage_kind") != "observed_empty_scope"):
        raise EmptyScopeProofError("proof_binding_mismatch")
    observations = proof.get("observations")
    if not isinstance(observations, list):
        raise EmptyScopeProofError("proof_observations_invalid")
    for observation in observations:
        snapshot = _snapshot(query, observation.get("snapshot"))
        total, size, pages = snapshot["total_rows"], snapshot["page_size"], snapshot["total_pages"]
        counter = observation.get("counter")
        expected = [1, min(size, total), total, 1, pages] if total else [0, 0, 0, 1, 1]
        if (observation.get("snapshot") != snapshot or observation.get("snapshot_hash") != _hash(snapshot)
                or observation.get("search_verified") is not True
                or observation.get("counter_origin") != "native_search_counter"
                or not isinstance(counter, list) or len(counter) != 5
                or any(type(item) is not int for item in counter) or counter != expected
                or type(observation.get("row_count")) is not int
                or observation["row_count"] != min(size, total)):
            raise EmptyScopeProofError("proof_observation_binding_mismatch")
        observed = datetime.fromisoformat(observation["observed_at"])
        if (observed.tzinfo is None or observed.utcoffset() is None
                or observed > _now() + timedelta(minutes=5)):
            raise EmptyScopeProofError("proof_timestamp_invalid")
    if proof.get("scope_zero_verified") is True:
        if (proof.get("status") != "zero_verified" or proof.get("sessions_independent") is not True
                or len(observations) != 2 or type(proof.get("total_results")) is not int
                or proof["total_results"] != 0 or proof.get("errors") != []
                or proof.get("observed_at") != observations[-1]["observed_at"]
                or any(observation["counter"] != [0, 0, 0, 1, 1]
                    or observation.get("row_count") != 0
                    or observation.get("row_identity_hash") != _hash([])
                    or observation["snapshot"]["total_rows"] != 0 for observation in observations)
                or observations[0]["snapshot_hash"] != observations[1]["snapshot_hash"]):
            raise EmptyScopeProofError("invalid_zero_proof")
    return proof


def scope_is_verified_zero(proof: dict, query: dict) -> bool:
    """Permit omission ONLY for the exact year/object/publication dates/version.

    ``query`` may contain any keyword; no earlier year or wider window is implied.
    Invalid/mutated proofs return False and never authorize omission.
    """
    try:
        normalized = _query({**query, "keyword": ""})
        return _verified(proof, normalized).get("scope_zero_verified") is True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def inspect_zero_scope_cache(query: dict, folder: Path | str, proof_hash: str) -> dict:
    """Read and validate the exact zero proof offline; never regenerate it."""
    query = _query({**query, "keyword": ""})
    folder = Path(folder)
    directory = folder / "empty-scopes"
    if (folder.is_symlink() or directory.is_symlink() or not directory.is_dir()
            or not isinstance(proof_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", proof_hash)):
        raise EmptyScopeProofError("completed_zero_cache_missing")
    proof = _verified(read_private(directory / (_hash(query) + ".private.json")), query)
    if proof.get("scope_zero_verified") is not True or proof["proof_hash"] != proof_hash:
        raise EmptyScopeProofError("completed_zero_cache_binding_mismatch")
    return {"scope_proof_hash": proof["proof_hash"],
        "source_observed_at": max(row["observed_at"] for row in proof["observations"])}


def _error_code(exc):
    if isinstance(exc, EmptyScopeProofError):
        return exc.code
    if isinstance(exc, Prod2ProtocolError):
        match = re.fullmatch(r"PROD2 HTTP status (403|429)", str(exc))
        if match:
            return "source_" + match[1]
    return "source_or_proof_error"


def _proof(query, observations, *, status, independent=False, error=None):
    value = {"version": PROBE_VERSION, "query": query, "scope_hash": _hash(query),
        "status": status, "scope_zero_verified": status == "zero_verified",
        "sessions_independent": independent, "observations": observations,
        "total_results": observations[-1]["snapshot"]["total_rows"] if observations else None,
        "observed_at": observations[-1]["observed_at"] if observations else _now().isoformat(),
        "coverage_kind": "observed_empty_scope", "source_atomic_snapshot": False,
        "errors": [error] if error else []}
    return {**value, "proof_hash": _hash(value)}


def probe_empty_scope(query: dict, *, source_factory, folder: Path | str) -> dict:
    """Return an immutable exact-scope proof or an explicit non-authorizing result.

    Verified nonempty first pages return False without an unnecessary second
    request. A zero requires two fresh independent sessions and identical native
    filter/count snapshots. Failures are recorded separately so a rerun may retry
    unpaid source I/O. Completed proofs are cached only within this frozen run.
    """
    query = _query(deepcopy(query))
    if not callable(source_factory):
        raise ValueError("source_factory must be callable")
    folder = Path(folder)
    if folder.is_symlink():
        raise EmptyScopeProofError("unsafe_private_cache")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = folder / "empty-scopes"
    if directory.is_symlink():
        raise EmptyScopeProofError("unsafe_private_cache")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / (_hash(query) + ".private.json")
    observations = []
    if path.exists():
        try:
            return _verified(read_private(path), query)
        except Exception as exc:
            return _proof(query, [], status="failed", error={"code": _error_code(exc), "error_type": type(exc).__name__})
    fd = os.open(directory / (_hash(query) + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    owned = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owned = True
        except BlockingIOError:
            return _proof(query, [], status="blocked", error={"code": "scope_owned_by_another_worker"})
        if path.exists():
            return _verified(read_private(path), query)
        try:
            with source_factory() as first_source:
                observations.append(_observe(first_source, query))
            if observations[0]["snapshot"]["total_rows"]:
                result = _proof(query, observations, status="nonempty")
            else:
                with source_factory() as second_source:
                    if second_source is first_source or (getattr(first_source, "client", None) is not None
                            and getattr(first_source, "client", None) is getattr(second_source, "client", None)):
                        raise EmptyScopeProofError("independent_sessions_required")
                    observations.append(_observe(second_source, query))
                if observations[1]["snapshot"]["total_rows"]:
                    raise EmptyScopeProofError("scope_changed_between_sessions")
                if observations[0]["snapshot_hash"] != observations[1]["snapshot_hash"]:
                    raise EmptyScopeProofError("scope_snapshot_changed_between_sessions")
                result = _proof(query, observations, status="zero_verified", independent=True)
            _verified(result, query)
            private_write(path, result)
            return result
        except Exception as exc:
            code = _error_code(exc)
            result = _proof(query, observations, status="blocked" if code in {"source_403", "source_429"} else "failed",
                            error={"code": code, "error_type": type(exc).__name__})
            private_write(directory / ("failed-" + uuid4().hex + ".private.json"), result)
            return result
    finally:
        if owned:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
