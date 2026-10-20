"""Resumable native keyword discovery, with no DB writes or provider calls.

Immutable private captures retain observed passes, never an atomic SEACE snapshot.
Completed passes re-emit without HTTP; partial passes require a fresh first-page
anchor before continuing and terminal reconciliation before declaring coverage.
The caller owns global HTTP pacing and durable Redis/PG dedup in ``emit``.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
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
from .discovery import Capture, validate_capture


SWEEP_VERSION = "prod2_keyword_sweep_v1"


class SweepError(ValueError):
    """Sanitized explicit coverage failure; contains no portal body or session data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _query(query: dict) -> dict:
    return validate_capture({"query": query, "observed_at": _now().isoformat(),
        "page": {"number": 1, "total_pages": 1, "total_results": 0, "page_size": 1},
        "rows": []}, now=_now()).query


def _write(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise SweepError("unsafe_private_cache")
    if path.exists():
        if read_private(path) != value:
            raise SweepError("immutable_cache_conflict")
        return
    private_write(path, value)


def _checkpoint(folder: Path, **values) -> None:
    files = list(folder.glob("checkpoint-*.private.json"))
    numbers = [int(path.name.split("-")[1].split(".")[0]) for path in files]
    sequence = max(numbers, default=0) + 1
    _write(folder / f"checkpoint-{sequence:08d}.private.json", {
        "version": SWEEP_VERSION, "sequence": sequence, "observed_at": _now().isoformat(), **values})


def _native_capture(query: dict, raw: dict, *, page_number: int, allow_cap: bool = False) -> dict:
    """Check the filter snapshot and exact counter, even for a capped proof."""
    if raw.get("search_verified") is not True:
        raise SweepError("unverified_native_search")
    counter, snapshot = raw.get("counter"), raw.get("snapshot")
    if (not isinstance(counter, (list, tuple)) or len(counter) != 5
            or any(type(value) is not int for value in counter) or not isinstance(snapshot, dict)):
        raise SweepError("invalid_native_counter")
    arguments = native_arguments(query)
    expected = {"year": arguments["year"], "object_type": arguments["object_type"],
        "keyword": query["keyword"], "published_from": arguments["published_from"],
        "published_to": arguments["published_to"], "year_filter_kind": "convocatoria_year",
        "native_atomic_snapshot": False}
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise SweepError("native_filter_snapshot_mismatch")
    size, total, pages = snapshot.get("page_size"), snapshot.get("total_rows"), snapshot.get("total_pages")
    if (type(size) is not int or not 1 <= size <= 100 or type(total) is not int or total < 0
            or type(pages) is not int or pages != max(1, (total + size - 1) // size)
            or not 1 <= page_number <= pages):
        raise SweepError("invalid_native_snapshot")
    start, end = ((page_number - 1) * size + 1, min(page_number * size, total)) if total else (0, 0)
    if list(counter) != [start, end, total, page_number, pages]:
        raise SweepError("native_counter_mismatch")
    if (page_number == 1 and raw.get("counter_origin") != "native_search_counter") or (
            page_number > 1 and raw.get("counter_origin") not in {
                "native_search_counter", "initial_native_total_and_observed_indices"}):
        raise SweepError("native_counter_origin_unverified")
    value = page_capture(query, raw, _now())
    if total >= 499:
        if not allow_cap or page_number != 1:
            raise SweepError("query_became_capped")
        # Validate all row fields without inventing a persisted small query. The
        # stored proof below retains the REAL total/counter and is never a page.
        expected_rows = min(size, total)
        if len(value["rows"]) != expected_rows:
            raise SweepError("capped_first_page_incomplete")
        validator = deepcopy(value)
        validator["page"] = {"number": 1, "total_pages": 1, "total_results": expected_rows, "page_size": size}
        validate_capture(validator, now=_now())
    else:
        validate_capture(value, now=_now())
    return value


def _page(value: dict) -> dict:
    capture = validate_capture(value, now=_now())
    return {"version": SWEEP_VERSION, "capture": value, "page_hash": capture.page_hash}


def _read_pages(attempt: Path, query: dict) -> list[tuple[dict, Capture]]:
    if attempt.is_symlink() or not attempt.is_dir():
        raise SweepError("unsafe_private_cache")
    files = sorted(attempt.glob("page-*.private.json"))
    pages, seen, anchor = [], set(), None
    for number, path in enumerate(files, 1):
        if path.name != f"page-{number:06d}.private.json":
            raise SweepError("cached_page_gap")
        saved = read_private(path)
        capture = validate_capture(saved["capture"], now=_now())
        if (saved.get("version") != SWEEP_VERSION or saved.get("page_hash") != capture.page_hash
                or capture.query != query or capture.page["number"] != number):
            raise SweepError("cached_page_binding_mismatch")
        anchor = anchor or capture.page
        if any(capture.page[key] != anchor[key] for key in ("total_results", "total_pages", "page_size")):
            raise SweepError("cached_counter_changed")
        ids = {row["convocation_id"] for row in capture.rows}
        if seen.intersection(ids):
            raise SweepError("duplicate_id_across_pages")
        seen.update(ids)
        pages.append((saved, capture))
    return pages


def inspect_complete_cache(query: dict, folder: Path | str) -> dict:
    """Validate a whole completed pass offline, before permitting source I/O.

    Existence of the root directory is insufficient: every partition, page,
    reconciliation and completion marker must remain bound to this query.
    This helper neither creates files nor emits rows nor opens a source session.
    """
    query = _query(deepcopy(query))
    folder = Path(folder)
    root = folder / ("sweep-" + _hash(query))
    if folder.is_symlink() or root.is_symlink() or not root.is_dir():
        raise SweepError("completed_cache_missing")
    expected = {"version": SWEEP_VERSION, "query": query, "query_hash": _hash(query),
        "coverage_kind": "observed_pass", "source_atomic_snapshot": False}
    if read_private(root / "manifest.private.json") != expected:
        raise SweepError("completed_cache_binding_mismatch")
    evidence, observations = [], []

    def inspect(window):
        windows = root / "windows"
        node = windows / _hash(window)
        if windows.is_symlink() or node.is_symlink() or not node.is_dir():
            raise SweepError("completed_cache_missing")
        manifest = {"version": SWEEP_VERSION, "query": window, "query_hash": _hash(window)}
        if read_private(node / "manifest.private.json") != manifest:
            raise SweepError("completed_cache_binding_mismatch")
        split_path = node / "split.private.json"
        if split_path.exists():
            proof = read_private(split_path)
            capture = proof.get("native_capture")
            if (proof.get("version") != SWEEP_VERSION or proof.get("query") != window
                    or not isinstance(capture, dict) or capture.get("query") != window
                    or proof.get("proof_hash") != _hash(capture)
                    or capture["page"]["total_results"] < 499
                    or proof.get("children") != _split(window)):
                raise SweepError("completed_split_binding_mismatch")
            # A capped observation is proof of splitting, never complete coverage.
            validation = deepcopy(capture)
            size, total = capture["page"]["page_size"], capture["page"]["total_results"]
            if (type(size) is not int or type(total) is not int or not 1 <= size <= 100
                    or capture["page"]["number"] != 1
                    or capture["page"]["total_pages"] != (total + size - 1) // size):
                raise SweepError("completed_split_binding_mismatch")
            validation["page"] = {"number": 1, "total_pages": 1, "total_results": size, "page_size": size}
            validate_capture(validation, now=_now())
            evidence.append({"query_hash": _hash(window), "split_proof_hash": proof["proof_hash"]})
            for child in proof["children"]:
                inspect(child)
            return
        attempts = sorted(node.glob("attempt-*"))
        if not attempts:
            raise SweepError("completed_cache_missing")
        attempt = attempts[-1]
        if attempt.is_symlink() or not attempt.is_dir():
            raise SweepError("unsafe_private_cache")
        attempt_manifest = read_private(attempt / "manifest.private.json")
        if (attempt_manifest.get("version") != SWEEP_VERSION or attempt_manifest.get("query") != window
                or attempt_manifest.get("query_hash") != _hash(window)
                or set(attempt_manifest) != {"version", "query", "query_hash", "started_at"}):
            raise SweepError("completed_cache_binding_mismatch")
        pages = _read_pages(attempt, window)
        done = read_private(attempt / "complete.private.json")
        if not pages:
            raise SweepError("empty_complete_cache")
        first = pages[0][1]
        reconciliation = validate_capture(done["reconciliation_capture"], now=_now())
        ids = {row["convocation_id"] for _, capture in pages for row in capture.rows}
        if (done.get("version") != SWEEP_VERSION or done.get("query") != window
                or done.get("coverage_kind") != "observed_pass" or done.get("source_atomic_snapshot") is not False
                or len(pages) != first.page["total_pages"] or len(ids) != first.page["total_results"]
                or done.get("page_hashes") != [capture.page_hash for _, capture in pages]
                or reconciliation.page_hash != first.page_hash):
            raise SweepError("complete_cache_binding_mismatch")
        completed = datetime.fromisoformat(done["completed_at"])
        started = datetime.fromisoformat(attempt_manifest["started_at"])
        if (completed.tzinfo is None or completed.utcoffset() is None
                or started.tzinfo is None or started.utcoffset() is None or started > completed
                or completed > _now() + timedelta(minutes=5) or completed < reconciliation.observed_at):
            raise SweepError("complete_cache_timestamp_invalid")
        observations.append(done["completed_at"])
        evidence.append({"query_hash": _hash(window), "completion_hash": _hash(done),
            "attempt_manifest_hash": _hash(attempt_manifest), "page_hashes": done["page_hashes"]})

    inspect(query)
    return {"query": query, "coverage_kind": "observed_pass", "cache_evidence_hash": _hash(evidence),
        "source_observed_at": max(observations)}


def _split(query: dict) -> list[dict]:
    first, last = date.fromisoformat(query["published_from"]), date.fromisoformat(query["published_to"])
    if first == last:
        raise SweepError("capped_single_day_requires_review")
    middle = first + timedelta(days=(last - first).days // 2)
    return [{**query, "published_to": middle.isoformat()},
            {**query, "published_from": (middle + timedelta(days=1)).isoformat()}]


def _error_code(exc: Exception) -> str:
    if isinstance(exc, SweepError):
        return exc.code
    if isinstance(exc, Prod2ProtocolError):
        status = re.fullmatch(r"PROD2 HTTP status (403|429)", str(exc))
        if status:
            return "source_" + status[1]
    return "source_or_capture_error"


class _Sweep:
    def __init__(self, query, source_factory, emit, folder, stop_requested, max_pages, emit_many=None,
                 batch_fresh_pages=False):
        self.query, self.source_factory, self.emit, self.folder = query, source_factory, emit, folder
        self.emit_many, self.batch_fresh_pages = emit_many, batch_fresh_pages
        self.stop_requested, self.max_pages = stop_requested, max_pages
        self.pages, self.offered, self.emit_true, self.emit_false, self.windows = 0, 0, 0, 0, 0
        self.cached_pages, self.http_searches, self.http_pages = 0, 0, 0
        self.leaves, self.errors = [], []

    def stopped(self):
        return bool(self.stop_requested() if callable(self.stop_requested) else self.stop_requested)

    def offer(self, capture: Capture, *, cached: bool) -> None:
        if self.stopped():
            raise SweepError("stop_requested")
        if self.pages >= self.max_pages:
            raise SweepError("page_budget_exhausted")
        self.pages += 1
        self.cached_pages += int(cached)
        if self.emit_many is not None and (cached or self.batch_fresh_pages):
            # Captures contain at most 100 validated, already cached rows.
            # Opt-in fresh batches share replay's page-level STOP boundary.
            items = [{"row": deepcopy(row), "query": deepcopy(capture.query),
                "page_number": capture.page["number"], "observed_at": capture.observed_at.isoformat()}
                for row in capture.rows]
            self.offered += len(items)
            try:
                returned = self.emit_many(items)
            except Exception:
                raise SweepError("emit_callback_error") from None
            if (not isinstance(returned, (list, tuple)) or len(returned) != len(items)
                    or any(type(value) is not bool for value in returned)):
                raise SweepError("emit_must_return_bool")
            self.emit_true += sum(returned)
            self.emit_false += len(returned) - sum(returned)
            if self.stopped():
                raise SweepError("stop_requested")
            return
        for row in capture.rows:
            if self.stopped():
                raise SweepError("stop_requested")
            self.offered += 1
            try:
                returned = self.emit({"row": deepcopy(row), "query": deepcopy(capture.query),
                    "page_number": capture.page["number"], "observed_at": capture.observed_at.isoformat()})
            except Exception:
                raise SweepError("emit_callback_error") from None
            if type(returned) is not bool:
                raise SweepError("emit_must_return_bool")
            self.emit_true += int(returned)
            self.emit_false += int(not returned)

    def search(self, source, query):
        if self.stopped():
            raise SweepError("stop_requested")
        if self.http_searches >= 4 * self.max_pages + 16:
            raise SweepError("search_budget_exhausted")
        self.http_searches += 1
        return source.search(query["keyword"], **native_arguments(query))

    def run_window(self, query: dict) -> bool:
        self.windows += 1
        if self.windows > 2 * self.max_pages + 1:
            raise SweepError("partition_budget_exhausted")
        if self.stopped():
            raise SweepError("stop_requested")
        node = self.folder / "windows" / _hash(query)
        for directory in (self.folder / "windows", node):
            if directory.is_symlink():
                raise SweepError("unsafe_private_cache")
            directory.mkdir(mode=0o700, exist_ok=True)
        _write(node / "manifest.private.json", {"version": SWEEP_VERSION, "query": query, "query_hash": _hash(query)})
        split_path = node / "split.private.json"
        if split_path.exists():
            proof = read_private(split_path)
            if (proof.get("version") != SWEEP_VERSION or proof.get("query") != query
                    or proof.get("proof_hash") != _hash(proof["native_capture"])
                    or proof["native_capture"].get("query") != query
                    or proof["native_capture"]["page"]["total_results"] < 499
                    or proof.get("children") != _split(query)):
                raise SweepError("cached_split_binding_mismatch")
            complete = True
            for child in proof["children"]:
                complete = self.run_window(child) and complete
            return complete
        attempts = sorted(node.glob("attempt-*"))
        attempt = attempts[-1] if attempts else None
        pages = _read_pages(attempt, query) if attempt else []
        complete_path = attempt / "complete.private.json" if attempt else None
        if complete_path and complete_path.exists():
            done = read_private(complete_path)
            if not pages:
                raise SweepError("empty_complete_cache")
            first = pages[0][1]
            reconciliation = validate_capture(done["reconciliation_capture"], now=_now())
            if (done.get("version") != SWEEP_VERSION or done.get("query") != query
                    or len(pages) != first.page["total_pages"]
                    or done.get("page_hashes") != [capture.page_hash for _, capture in pages]
                    or reconciliation.page_hash != first.page_hash):
                raise SweepError("complete_cache_binding_mismatch")
            for _, capture in pages:
                self.offer(capture, cached=True)
            self.leaves.append({"query": query, "status": "complete", "cached": True,
                "pages": len(pages), "total_results": first.page["total_results"],
                "observed_at": done["completed_at"]})
            return True
        # Partial complete pages remain observations; replay is safe via emit's
        # durable dedup. They do NOT establish source coverage without HTTP.
        for _, capture in pages:
            self.offer(capture, cached=True)
        if self.stopped():
            raise SweepError("stop_requested")
        try:
            with self.source_factory() as source:
                first_raw = self.search(source, query)
                first_value = _native_capture(query, first_raw, page_number=1, allow_cap=True)
                if first_value["page"]["total_results"] >= 499:
                    # Preserve the REAL capped observation even when a one-day
                    # window cannot be split. This is evidence, never a full page.
                    _write(node / ("capped-" + _hash(first_value) + ".private.json"), {
                        "version": SWEEP_VERSION, "query": query, "native_capture": first_value,
                        "coverage_complete": False, "proof_hash": _hash(first_value)})
                    children = _split(query)
                    _write(split_path, {"version": SWEEP_VERSION, "query": query,
                        "native_capture": first_value, "proof_hash": _hash(first_value), "children": children})
                    _checkpoint(node, status="split", query_hash=_hash(query), children=children)
                    capped = True
                else:
                    capped = False
                    first = validate_capture(first_value, now=_now())
                    if pages and first.page_hash != pages[0][1].page_hash:
                        _checkpoint(node, status="anchor_changed", attempt=attempt.name)
                        # Leave the prior pass immutable. New pages use a new
                        # attempt; previously emitted observations are not coverage.
                        pages, attempt = [], None
                    if attempt is None:
                        number = len(attempts) + 1
                        attempt = node / f"attempt-{number:06d}-{uuid4().hex}"
                        attempt.mkdir(mode=0o700)
                        _write(attempt / "manifest.private.json", {"version": SWEEP_VERSION,
                            "query": query, "query_hash": _hash(query), "started_at": _now().isoformat()})
                    seen = {row["convocation_id"] for _, capture in pages for row in capture.rows}
                    for number in range(len(pages) + 1, first.page["total_pages"] + 1):
                        if self.stopped():
                            raise SweepError("stop_requested")
                        if self.pages >= self.max_pages:
                            raise SweepError("page_budget_exhausted")
                        if number == 1:
                            value = first_value
                        else:
                            self.http_pages += 1
                            value = _native_capture(query, source.search_page(number), page_number=number)
                        capture = validate_capture(value, now=_now())
                        if any(capture.page[key] != first.page[key] for key in ("total_results", "total_pages", "page_size")):
                            raise SweepError("counter_changed_during_pass")
                        ids = {row["convocation_id"] for row in capture.rows}
                        if seen.intersection(ids):
                            raise SweepError("duplicate_id_across_pages")
                        seen.update(ids)
                        saved = _page(value)
                        _write(attempt / f"page-{number:06d}.private.json", saved)
                        pages.append((saved, capture))
                        _checkpoint(node, status="partial", attempt=attempt.name,
                                    next_page=number + 1, first_page_hash=first.page_hash)
                        self.offer(capture, cached=False)
            # The discovery session has closed. A NEW session must agree with
            # the first-page rows/IDs/counts/hash before this pass can complete.
            if capped:
                complete = True
                for child in children:
                    complete = self.run_window(child) and complete
                return complete
            if self.stopped():
                raise SweepError("stop_requested")
            with self.source_factory() as verifier:
                checked = _native_capture(query, self.search(verifier, query), page_number=1)
            reconciliation = validate_capture(checked, now=_now())
            if reconciliation.page_hash != first.page_hash:
                raise SweepError("first_page_reconciliation_changed")
            if len(pages) != first.page["total_pages"] or len(seen) != first.page["total_results"]:
                raise SweepError("pass_identity_count_mismatch")
            completed_at = _now().isoformat()
            _write(attempt / "complete.private.json", {"version": SWEEP_VERSION, "query": query,
                "page_hashes": [capture.page_hash for _, capture in pages],
                "completed_at": completed_at, "reconciliation_capture": checked,
                "coverage_kind": "observed_pass", "source_atomic_snapshot": False})
            _checkpoint(node, status="complete", attempt=attempt.name,
                        next_page=first.page["total_pages"] + 1, first_page_hash=first.page_hash)
            self.leaves.append({"query": query, "status": "complete", "cached": False,
                "pages": len(pages), "total_results": first.page["total_results"], "observed_at": completed_at})
            return True
        except Exception as exc:
            code = _error_code(exc)
            status = ("stopped" if code == "stop_requested" else "blocked"
                      if code in {"source_403", "source_429", "capped_single_day_requires_review"} else "partial")
            _checkpoint(node, status=status,
                        attempt=attempt.name if attempt else None, error_code=code, error_type=type(exc).__name__)
            self.errors.append({"query": query, "code": code, "error_type": type(exc).__name__})
            self.leaves.append({"query": query, "status": status, "cached": False,
                "pages": len(pages), "error_code": code})
            if code in {"stop_requested", "page_budget_exhausted", "search_budget_exhausted", "partition_budget_exhausted",
                        "source_403", "source_429", "emit_callback_error", "emit_must_return_bool"}:
                raise SweepError(code) from None
            return False


def sweep_query(query: dict, *, source_factory, emit, folder: Path | str,
                stop_requested=False, max_pages: int = 10000, emit_many=None,
                batch_fresh_pages: bool = False) -> dict:
    """Paginate one frozen keyword/object/year/publication window and emit rows.

    ``source_factory()`` yields an independent native Prod2HttpClient session.
    ``emit(item)`` MUST return bool: False may mean a duplicate and never stops
    discovery. Only ``stop_requested`` interrupts. No accepted/rejected decision,
    canonical discovery persistence, provider call or latest-snapshot guarantee is
    made here. Reusing the folder intentionally replays an existing observed pass.
    Optional ``emit_many(items)`` replays cached pages by default. Explicit
    ``batch_fresh_pages=True`` also batches fresh pages after validation/cache
    persistence and requires ``emit_many``. It must return one strict bool per
    row in order; STOP is checked before/after each batch, and a failed batch
    never establishes coverage or confirms an unknown applied prefix.
    """
    query = _query(deepcopy(query))
    if type(max_pages) is not int or max_pages <= 0:
        raise ValueError("max_pages must be a positive integer")
    if not callable(source_factory) or not callable(emit) or not (callable(stop_requested) or type(stop_requested) is bool):
        raise ValueError("Invalid sweep callback")
    if emit_many is not None and not callable(emit_many):
        raise ValueError("Invalid batch sweep callback")
    if type(batch_fresh_pages) is not bool:
        raise ValueError("batch_fresh_pages must be a boolean")
    if batch_fresh_pages and emit_many is None:
        raise ValueError("Fresh page batching requires emit_many")
    folder = Path(folder)
    if folder.is_symlink():
        raise SweepError("unsafe_private_cache")
    root = folder / ("sweep-" + _hash(query))
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise SweepError("unsafe_private_cache")
    os.chmod(root, 0o700)
    runner = _Sweep(query, source_factory, emit, root, stop_requested, max_pages, emit_many,
                    batch_fresh_pages)
    lock_path = root / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    complete, owned = False, False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owned = True
        except BlockingIOError:
            runner.errors.append({"query": query, "code": "query_owned_by_another_worker"})
        if owned:
            try:
                _write(root / "manifest.private.json", {"version": SWEEP_VERSION, "query": query,
                    "query_hash": _hash(query), "coverage_kind": "observed_pass", "source_atomic_snapshot": False})
                complete = runner.run_window(query)
            except Exception as exc:
                code = exc.code if isinstance(exc, SweepError) else "cache_or_callback_error"
                if not any(error["code"] == code for error in runner.errors):
                    runner.errors.append({"query": query, "code": code, "error_type": type(exc).__name__})
        codes = {error["code"] for error in runner.errors}
        status = ("complete" if complete and not codes else "stopped" if "stop_requested" in codes
                  else "blocked" if codes.intersection({"capped_single_day_requires_review", "query_owned_by_another_worker",
                                                       "source_403", "source_429"})
                  else "partial")
        report = {"version": SWEEP_VERSION, "query": query, "query_hash": _hash(query), "status": status,
            "coverage_complete": status == "complete", "coverage_kind": "observed_pass", "source_atomic_snapshot": False,
            "pages_processed": runner.pages, "cached_pages_reemitted": runner.cached_pages,
            "rows_offered": runner.offered, "emit_returned_true": runner.emit_true,
            "emit_returned_false": runner.emit_false, "http_searches": runner.http_searches,
            "http_pages": runner.http_pages, "windows_visited": runner.windows,
            "leaf_windows": runner.leaves, "errors": runner.errors, "ended_at": _now().isoformat()}
        if owned:
            _checkpoint(root, status=status, report=report)
        return report
    finally:
        if owned:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
