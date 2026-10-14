"""One owned metadata task at a time; no document downloads or Gemini calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from .analysis import jev_request_hash
from .batch_plan import native_arguments, page_capture
from .client import SEARCH_URL, Prod2ProtocolError
from .discovery import validate_capture, CaptureError
from .participation import decide
from .repository import claim_evaluation, complete_evaluation, persist_detail


def read_private(path: Path):
    if path.is_symlink():
        raise ValueError("Private cache must not be a symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def private_write(path: Path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Private cache directory must not be a symlink")
    temporary = path.parent / (".writing-" + uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False, default=str)
            output.flush()
            os.fsync(output.fileno())
        # Publish a complete receipt atomically, without overwriting an owner or
        # frozen manifest. A killed writer leaves only an unreferenced temp file.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def paid_jev_stage(conn, folder: Path, conv: int, description: str, manifest: dict, jev):
    """Reserve before paying. A saved response resolves our own uncertain claim."""
    request_hash = jev_request_hash(description, commercial_profiles=manifest["commercial_profiles"],
                                    model=manifest["jev_model"])
    def verified(result):
        if (not isinstance(result, dict) or result.get("model") != manifest["jev_model"]
                or result.get("policy_version") != manifest["jev_policy_version"]
                or result.get("cache_key") != "prod2:jev:" + request_hash):
            raise RuntimeError("Jev receipt does not bind the frozen model, policy and exact request")
        return result
    claim = claim_evaluation(conn, conv, kind="jev_affinity", request_hash=request_hash,
                             model=manifest["jev_model"], policy_version=manifest["jev_policy_version"],
                             profile_id=manifest["profile_id"], lease_seconds=3600)
    if claim["status"] == "cached":
        return claim["id"], verified(claim["result_json"]), True
    stem = folder / f"jev-{request_hash}"
    owner_path = stem.with_suffix(".claim.private.json")
    response_path = stem.with_suffix(".response.private.json")
    if claim["status"] == "claimed":
        private_write(owner_path, claim)
    elif claim["status"] in {"busy", "uncertain"} and owner_path.exists() and response_path.exists():
        owner = read_private(owner_path)
        if owner["id"] != claim["id"]:
            raise RuntimeError("Private provider receipt does not bind this reservation")
        claim = owner
    else:
        raise RuntimeError("Provider reservation requires reconciliation; automatic payment retry refused")
    reused = response_path.exists()
    if reused:
        result = read_private(response_path)
    else:
        try:
            result = jev.classify(description, commercial_profiles=manifest["commercial_profiles"])
        except Exception as error:
            failure = stem.with_suffix(".failure.private.json")
            if not failure.exists():
                private_write(failure, {"error_type": type(error).__name__, "provider_usage_known": False,
                                        "ended_at": datetime.now(timezone.utc).isoformat()})
            conn.execute("""UPDATE prod2_evaluations SET status='uncertain',updated_at=now()
                WHERE id=%s AND claim_token=%s AND status='running'""", (claim["id"], claim["claim_token"]))
            raise RuntimeError("Jev response unavailable; billing is uncertain and no retry was made") from None
        private_write(response_path, result)
    verified(result)
    complete_evaluation(conn, claim["id"], claim["claim_token"], result,
                        input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
                        cost_usd=result.get("cost_usd"))
    return claim["id"], result, reused


def discover_task(conn, task: dict, repo, source_factory) -> str:
    identity, token, query = task["task_id"], task["owner_token"], task["query"]
    with source_factory() as source:
        first = source.search(query["keyword"], **native_arguments(query))
        if first["counter"][2] >= 499:
            repo.split_capped_task(conn, identity, token, total_results=first["counter"][2])
            return "split"
        first_capture = page_capture(query, first, datetime.now(timezone.utc))
        signature = validate_capture(first_capture).page_hash
        next_page = task["checkpoint"].get("next_page", 1)
        if next_page > 1:
            saved = repo.discovery_checkpoint(conn, identity, token)["saved_pass"]
            if saved is None or saved["first_page_hash"] != signature:
                repo.restart_discovery_task(conn, identity, token)
                return "restarted"
        for number in range(next_page, first["counter"][4] + 1):
            repo.renew_lease(conn, identity, token, lease_seconds=3600)
            try:
                result = first if number == 1 else source.search_page(number)
            except Prod2ProtocolError as error:
                if str(error) not in {"Search counter changed during pagination",
                    "Search paginator counts changed during pagination", "Search rows changed when revisiting a page",
                    "Duplicate convocatoria across search pages", "Search counter does not match extracted rows"}:
                    raise
                repo.restart_discovery_task(conn, identity, token)
                return "restarted"
            capture = page_capture(query, result, datetime.now(timezone.utc))
            validate_capture(capture)
            if number == first["counter"][4]:
                # A separate fresh JSF session must agree before the last DB page
                # can complete the pass. This is reconciliation, not an atomic snapshot.
                with source_factory() as verifier:
                    again = verifier.search(query["keyword"], **native_arguments(query))
                    if again["counter"][2] >= 499:
                        repo.restart_discovery_task(conn, identity, token)
                        return "restarted"
                    checked = validate_capture(page_capture(query, again, datetime.now(timezone.utc)))
                    if checked.page_hash != signature:
                        repo.restart_discovery_task(conn, identity, token)
                        return "restarted"
            try:
                repo.persist_page(conn, identity, token, capture)
            except CaptureError as error:
                if str(error) not in {"run: query/totals changed; use a new run_id",
                    "run: page contents changed; use a new run_id", "run: duplicate convocation across pages"}:
                    raise
                repo.restart_discovery_task(conn, identity, token)
                return "restarted"
    return "complete"


def _locate_ficha(source, query, row, page_number: int):
    result = source.search(query["keyword"], **native_arguments(query))
    pages = result["counter"][4]
    if result["counter"][2] >= 499:
        raise RuntimeError("Ficha locator query became capped; discovery requires a new bounded pass")
    order = list(dict.fromkeys([1, *([page_number] if 1 < page_number <= pages else []), *range(2, pages + 1)]))
    for page in order:
        if page != 1:
            source.search_page(page)
        index = next((i for i, candidate in enumerate(source.rows)
                      if candidate["convocatoria_id"] == row["convocation_id"]
                      and candidate["procedure_id"] == row["procedure_id"]), None)
        if index is not None:
            candidate = source.rows[index]
            if any(candidate[native] != row[canonical] for native, canonical in (
                ("nomenclature", "nomenclature"), ("entity", "buyer_name"),
                ("description", "description"), ("object", "object_label"))):
                raise RuntimeError("Ficha identity/description changed; reimport current discovery before any payment")
            return source.detail(index, all_documents=True)
    raise RuntimeError("Frozen record is absent from its verified source query")


def enrich_task(conn, task: dict, repo, manifest: dict, folder: Path, source_factory, jev) -> dict:
    identity, token, conv = task["task_id"], task["owner_token"], task["id_convocatoria"]
    locator = repo.discovery_locator(conn, task["run_id"], conv)
    record_folder = folder / "records" / str(conv)
    path = record_folder / "detail.private.json"
    repo.renew_lease(conn, identity, token, lease_seconds=3600)
    if path.exists():
        receipt = read_private(path)
    else:
        # Check the source identity before paying Jev. This call retrieves ficha
        # HTML and document metadata only; it never downloads an attachment.
        with source_factory() as source:
            detail = _locate_ficha(source, locator["query_manifest"], locator["row_manifest"], locator["page_number"])
        receipt = {"observed_at": datetime.now(timezone.utc).isoformat(),
                   "detail": {key: value for key, value in detail.items()
                              if key not in {"http_status", "redirect_statuses"}}}
        private_write(path, receipt)
    saved = persist_detail(conn, receipt["detail"], observed_at=datetime.fromisoformat(receipt["observed_at"]),
                           source_url=SEARCH_URL)
    assessment = decide(receipt["detail"], datetime.now(timezone.utc))
    participation = repo.save_participation_assessment(conn, conv, detail_hash=saved["detail_hash"], assessment=assessment)
    repo.update_checkpoint(conn, identity, token, {"detail_hash": saved["detail_hash"],
        "participation_assessment_id": participation["id"]})
    evaluation_id, result, cached = paid_jev_stage(conn, record_folder, conv, locator["row_manifest"]["description"], manifest, jev)
    outcome = {"jev_evaluation_id": evaluation_id, "detail_hash": saved["detail_hash"],
               "participation_assessment_id": participation["id"], "jev_cache_hit": cached,
               "affinity_index": result["affinity_index"], "temporal_state": assessment["state"],
               "documents_downloaded": 0, "gemini_calls": 0}
    repo.complete_task(conn, identity, token, outcome)
    return outcome
