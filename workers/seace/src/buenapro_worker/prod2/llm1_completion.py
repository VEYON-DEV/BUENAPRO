"""Finish a completed, document-bound LLM1 read without invoking any model.

The caller supplies ``evaluation_id`` and its configured ``allowed_models`` in
the extraction bundle. The paid reservation must already be ``done``. One outer
transaction encloses extraction persistence and canonical participation, also
when the connection uses autocommit. Historical receipts are never promoted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import PurePath
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from psycopg.rows import dict_row

from buenapro_worker.extraction.schemas import BasesGoodsExtractionV2, EettExtractionV1, TdrExtractionV2
from buenapro_worker.normalization.participation import derive_participation
from buenapro_worker.normalization.participation_repository import persist_participation
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2.analysis import extraction_for_persistence
from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY, MAX_ANALYSIS_PAGES
from buenapro_worker.prod2.pdf_preparation import SELECTED_DOCUMENTS_POLICY, _candidates, PdfPreparationError, MAX_SOURCE_BYTES
from buenapro_worker.prod2.selected_extractor import SELECTED_TRANSPORT_POLICY
from buenapro_worker.local_document_conversion import safe_member, WORD_FOOTNOTES_AT_END_POLICY
from buenapro_worker.prod2 import document_selection
from buenapro_worker.prod2 import repository
from buenapro_worker.prod2.document_events import event_hash
from buenapro_worker.prod2.archive_inventory import POLICY_7Z, inventory_policy


PROJECTION_POLICY = "prod2_llm1_participation_projection_v1"
SET_PROJECTION_POLICY = "prod2_llm1_direct_participation_projection_v1"
SELECTED_PROJECTION_POLICY = "prod2_llm1_selected_participation_projection_v1"
PRESERVE_FOREIGN_POLICY = "prod2_preserve_foreign_canonical_participation_v1"
RETRIEVED_PROJECTION_POLICY = "prod2_retrieved_participation_preserved_v1"
_CANONICAL_FIELDS = ("consortium_status", "subcontracting_status", "participation_terms_json", "updated_at")
_SET_FIELDS = ("document_scope", "document_set", "excluded_documents", "document_set_hash")
_SELECTED_FIELDS = ("document_scope", "document_set", "document_set_hash", "document_set_identity", "selection_context", "selected_transport_policy")
_DOCUMENT_FIELDS = {"document_uuid", "filename", "stage", "type", "published_at", "source_url"}
_BOUND_FIELDS = (
    "cache_key", "original_sha256", "analysis_sha256", "doc_class", "model", "prompt_version",
    "prompt_hash", "schema_version", "raw_extraction_json", "requires_human_review",
    "input_tokens", "output_tokens", "cost_usd",
)


class Llm1CompletionError(repository.Prod2PersistenceError):
    """A receipt is missing, stale or not attributable; no payment retry is allowed."""


def canonical_participation_snapshot(row: dict) -> dict:
    """Freeze all fields the canonical writer could change, including its timestamp."""
    if not isinstance(row, dict) or any(key not in row for key in _CANONICAL_FIELDS):
        raise Llm1CompletionError("Complete canonical participation snapshot is required")
    stamp = row["updated_at"]
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise Llm1CompletionError("Canonical participation timestamp must be attributable")
    return {**{key: deepcopy(row[key]) for key in _CANONICAL_FIELDS[:-1]},
            "updated_at": stamp.astimezone(timezone.utc).isoformat()}


def _validate_foreign_preservation(proof, *, canonical, convocation, opportunity,
                                   reservation, code, sha, context, result):
    """Explicit opt-in permits persistence, never a competing canonical projection."""
    fields = {"policy_version", "convocation_id", "opportunity_id", "evaluation_id",
              "request_hash", "document_uuid", "document_sha256", "paid_result_hash",
              "canonical_snapshot", "canonical_snapshot_hash"}
    previous = canonical["participation_terms_json"]
    if (not isinstance(proof, dict) or set(proof) != fields
            or proof["policy_version"] != PRESERVE_FOREIGN_POLICY
            or type(proof["convocation_id"]) is not int or proof["convocation_id"] != convocation
            or type(proof["evaluation_id"]) is not int or proof["evaluation_id"] != reservation["id"]
            or proof["opportunity_id"] != opportunity
            or proof["request_hash"] != reservation["request_hash"]
            or proof["document_uuid"] != code or proof["document_sha256"] != sha
            or proof["paid_result_hash"] != canonical_hash(reservation["result_json"])
            or context is None or context["document_scope"] != SELECTED_DOCUMENTS_POLICY
            or result.requires_human_review is not True
            or not isinstance(previous, dict) or previous.get("source") != "seace_prod4"
            or type(previous.get("extraction_id")) is not int or previous["extraction_id"] <= 0
            or previous.get("document_sha256") != sha):
        raise Llm1CompletionError("Explicit foreign canonical preservation proof is invalid")
    snapshot = canonical_participation_snapshot(canonical)
    if (proof["canonical_snapshot"] != snapshot
            or proof["canonical_snapshot_hash"] != canonical_hash(snapshot)):
        raise Llm1CompletionError("Canonical participation changed since the explicit preservation snapshot")
    return snapshot


def _source_reference(value: str, code: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname != "alfprod.seace.gob.pe":
        return repository._source_url(value)
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment
            or parsed.path != "/alfresco/service/osce/downloadDoc"
            or set(parameters) != {"id"} or len(parameters["id"]) != 1
            or repository._uuid(parameters["id"][0]) != code):
        raise Llm1CompletionError("Expected the exact official public document reference without tickets")
    return value


def _one(cursor, statement, params=()):
    cursor.execute(statement, params)
    return cursor.fetchone()


def _publication(row) -> tuple[str, datetime | None] | None:
    value = row.get("published_at")
    if isinstance(value, datetime) and value.tzinfo is not None:
        local = value.astimezone(repository.LIMA)
        return local.date().isoformat(), local
    text = row.get("published_at_text")
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat(), None
    except ValueError:
        return None


def _not_older(candidate, previous):
    left, right = _publication(candidate), _publication(previous)
    if left is None or right is None or left[0] < right[0]:
        raise Llm1CompletionError("Document chronology cannot establish a current participation projection")
    if left[0] == right[0] and (left[1] is None or right[1] is None or left[1] <= right[1]):
        raise Llm1CompletionError("Document chronology is older or ambiguous")


def _newer_paid(reservation, previous):
    if (not reservation.get("completed_at") or not previous.get("completed_at")
            or reservation["completed_at"] <= previous["completed_at"]):
        raise Llm1CompletionError("An older paid extraction cannot replace the current document analysis")


def _document_set_context(extraction, code, sha):
    """Validate the exact loose-document envelope; the anchor has no authority."""
    raw = extraction["raw_extraction_json"]
    if extraction.get("document_scope") == SELECTED_DOCUMENTS_POLICY or raw.get("document_scope") == SELECTED_DOCUMENTS_POLICY:
        return _selected_context(extraction, code, sha)
    scoped = any(field in extraction or field in raw for field in _SET_FIELDS)
    if not scoped:
        return None
    if (extraction.get("document_scope") != DIRECT_DOCUMENTS_POLICY
            or any(field not in extraction or raw.get(field) != extraction[field] for field in _SET_FIELDS)):
        raise Llm1CompletionError("Document-set context is missing or differs from the raw receipt")
    direct, excluded = extraction["document_set"], extraction["excluded_documents"]
    if not isinstance(direct, list) or not direct or not isinstance(excluded, list):
        raise Llm1CompletionError("Expected loose documents and explicit archive exclusions")
    seen = set()
    for rows, included in ((direct, True), (excluded, False)):
        ids = []
        for row in rows:
            expected = _DOCUMENT_FIELDS | ({"original_sha256"} if included else {"reason"})
            if not isinstance(row, dict) or set(row) != expected:
                raise Llm1CompletionError("Document-set metadata is incomplete or ambiguous")
            uuid = repository._uuid(row["document_uuid"])
            if uuid != row["document_uuid"] or uuid in seen:
                raise Llm1CompletionError("Document-set UUIDs must be unique and canonical")
            seen.add(uuid)
            ids.append(uuid)
            for field in ("filename", "stage", "type", "published_at"):
                repository._text(row[field], limit=1000, empty=field != "filename")
            _source_reference(row["source_url"], uuid)
            if urlsplit(row["source_url"]).hostname != "alfprod.seace.gob.pe":
                raise Llm1CompletionError("Document-set provenance requires each official document resolver")
            suffix = PurePath(row["filename"]).suffix.lower()
            if included:
                repository._sha(row["original_sha256"])
                if suffix not in {".pdf", ".doc", ".docx"}:
                    raise Llm1CompletionError("Only loose PDF and Word documents may be analyzed")
            elif suffix not in {".zip", ".rar"} or row["reason"] != "archive_deferred":
                raise Llm1CompletionError("Only ZIP/RAR may be explicitly deferred")
        if ids != sorted(ids):
            raise Llm1CompletionError("Document-set ordering must be stable")
    if (direct[0]["document_uuid"], direct[0]["original_sha256"]) != (code, sha):
        raise Llm1CompletionError("Storage anchor must be the first sorted loose-document UUID")
    context = {field: extraction[field] for field in _SET_FIELDS}
    expected_hash = canonical_hash({field: context[field] for field in _SET_FIELDS[:-1]})
    if repository._sha(context["document_set_hash"]) != expected_hash:
        raise Llm1CompletionError("Document-set identity does not match its exact metadata and revisions")
    manifest = raw.get("source_document_manifest")
    if not isinstance(manifest, list) or len(manifest) != len(direct):
        raise Llm1CompletionError("Page manifest must cover every included document")
    previous, page_count = {}, 0
    for doc, record in zip(direct, manifest):
        allowed = set(doc) | {"analysis_sha256", "start_page", "end_page"}
        if (not isinstance(record, dict) or set(record) not in (allowed, allowed | {"duplicate_of"})
                or any(record.get(field) != value for field, value in doc.items())):
            raise Llm1CompletionError("Page manifest differs from the bound document metadata")
        repository._sha(record["analysis_sha256"])
        start, end = record["start_page"], record["end_page"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= MAX_ANALYSIS_PAGES:
            raise Llm1CompletionError("Page manifest requires precise bounded global ranges")
        if "duplicate_of" in record:
            original = previous.get(record["duplicate_of"])
            if (not original or "duplicate_of" in original or any(
                    record[field] != original[field] for field in ("analysis_sha256", "start_page", "end_page"))):
                raise Llm1CompletionError("Duplicate pages require an earlier identical derivative and range")
        else:
            if start != page_count + 1:
                raise Llm1CompletionError("Page ranges must be contiguous without overlap or gaps")
            page_count = end
        previous[doc["document_uuid"]] = record
    for term in derive_participation(raw).values():
        if term["page"] is not None and term["page"] > page_count:
            raise Llm1CompletionError("Participation citation is outside the combined PDF page manifest")
    if (len(direct) > 1 or excluded) and extraction["requires_human_review"] is not True:
        raise Llm1CompletionError("Mixed or deferred documents require authority and coverage review")
    return {**context, "source_document_manifest": manifest, "analysis_page_count": page_count}


def _word_conversion_provenance(receipt, analysis_sha, max_bytes):
    """Validate the explicit derivative receipt; original Word SHA stays in its row."""
    fields = {"policy", "original_placement", "derived_placement", "only_placement_attribute_changed",
              "footnote_count", "native_fodt_sha256", "derived_fodt_sha256",
              "conversion_source_sha256", "pdf_sha256", "pdf_bytes"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or receipt["policy"] != WORD_FOOTNOTES_AT_END_POLICY
            or receipt["original_placement"] not in ("page", "document")
            or receipt["derived_placement"] != "document"
            or receipt["only_placement_attribute_changed"] is not True
            or type(receipt["footnote_count"]) is not int or receipt["footnote_count"] < 0
            or type(receipt["pdf_bytes"]) is not int or not 0 < receipt["pdf_bytes"] <= max_bytes):
        raise Llm1CompletionError("Explicit Word conversion provenance is incomplete or unsupported")
    for field in ("native_fodt_sha256", "derived_fodt_sha256", "conversion_source_sha256", "pdf_sha256"):
        repository._sha(receipt[field])
    if (receipt["pdf_sha256"] != analysis_sha
            or (receipt["native_fodt_sha256"] == receipt["derived_fodt_sha256"])
               != (receipt["original_placement"] == "document")):
        raise Llm1CompletionError("Explicit Word conversion provenance does not bind its derivative SHA")
    # Conversion may first sanitize external DOCX templates. Its source SHA need
    # not equal the immutable downloaded member SHA, already validated in rows.


def _selected_context(extraction, code, sha):
    """Bind original parents, selected member bytes and one-based page provenance."""
    raw = extraction["raw_extraction_json"]
    if (extraction.get("document_scope") != SELECTED_DOCUMENTS_POLICY
            or any(field not in extraction or raw.get(field) != extraction[field] for field in _SELECTED_FIELDS)):
        raise Llm1CompletionError("Selected preparation context differs from its raw receipt")
    context = {field: extraction[field] for field in _SELECTED_FIELDS}
    repository._json(context)  # Reject nonfinite or non-JSON receipt metadata.
    from .luna_policy import LUNA_SELECTED_TRANSPORT_POLICY
    expected_transport = LUNA_SELECTED_TRANSPORT_POLICY if extraction.get("model") == "gpt-6-luna" else SELECTED_TRANSPORT_POLICY
    if context["selected_transport_policy"] != expected_transport:
        raise Llm1CompletionError("Selected extraction transport policy is unsupported")
    identity = context["document_set_identity"]
    identity_fields = {"document_scope", "document_set", "selection_context", "max_source_bytes",
                       "max_analysis_bytes", "max_pages", "optimize_lossless"}
    word_policy = identity.get("word_conversion_policy") if isinstance(identity, dict) else None
    if (not isinstance(identity, dict)
            or set(identity) not in (identity_fields, identity_fields | {"word_conversion_policy"})
            or ("word_conversion_policy" in identity and word_policy != WORD_FOOTNOTES_AT_END_POLICY)
            or any(identity.get(field) != context[field] for field in ("document_scope", "document_set", "selection_context"))
            or type(identity["max_source_bytes"]) is not int or not 0 < identity["max_source_bytes"] <= MAX_SOURCE_BYTES
            or type(identity["max_analysis_bytes"]) is not int or not 0 < identity["max_analysis_bytes"] < 50_000_000
            or type(identity["max_pages"]) is not int or identity["max_pages"] != MAX_ANALYSIS_PAGES
            or type(identity["optimize_lossless"]) is not bool
            or repository._sha(context["document_set_hash"]) != canonical_hash(identity)):
        raise Llm1CompletionError("Selected preparation identity does not bind its exact inputs and policy")
    try:
        rows = _candidates(context["document_set"])
    except PdfPreparationError:
        raise Llm1CompletionError("Selected candidate source attribution is invalid") from None
    selection = context["selection_context"]
    if not isinstance(selection, dict):
        raise Llm1CompletionError("Expected a complete document selection snapshot")
    from .reading_policy import reading_policy
    try:
        archive_policy = inventory_policy(selection)
        reading_policy(selection)
        from .extraction_policy import get_scope_policy
        from .input_policy import validate_input_snapshot
        get_scope_policy(selection)
        proof = validate_input_snapshot(selection, extraction.get("llm1_input"),
            extraction["analysis_sha256"], raw.get("source_document_manifest", []))
        if raw.get("llm1_input") != proof:
            raise ValueError("Input proof differs from raw receipt")
        if proof is not None:
            context["llm1_input"] = proof
    except ValueError:
        raise Llm1CompletionError("Selected LLM1 scope, reading policy or input proof is invalid") from None
    if "ficha_description" in selection:
        from .analysis import ficha_reading_description
        try:
            if ficha_reading_description(selection["ficha_description"]) != selection["ficha_description"]:
                raise ValueError()
        except ValueError:
            raise Llm1CompletionError("Selected ficha description must be canonical sanitized public text") from None
    inventory, candidates, decisions = (selection.get(field) for field in ("official_inventory", "candidates", "decisions"))
    if not isinstance(inventory, list) or not inventory or not isinstance(candidates, list) or not candidates or not isinstance(decisions, list):
        raise Llm1CompletionError("Selected snapshot must preserve full inventory, candidates and decisions")
    repository._sha(selection.get("selector_request_hash"))
    repository._sha(selection.get("selector_receipt_hash"))
    thresholds = selection.get("thresholds")
    try:
        valid_thresholds = (isinstance(thresholds, dict) and set(thresholds) == {"high", "low"}
                            and document_selection._finite(thresholds["low"], 0, 100)
                            < document_selection._finite(thresholds["high"], 0, 100))
    except document_selection.DocumentSelectionError:
        valid_thresholds = False
    if not valid_thresholds:
        raise Llm1CompletionError("Selected snapshot thresholds are missing or invalid")
    parents = {}
    for parent in inventory:
        if not isinstance(parent, dict) or set(parent) not in (_DOCUMENT_FIELDS, _DOCUMENT_FIELDS | {"original_sha256"}):
            raise Llm1CompletionError("Official parent inventory metadata is incomplete")
        uuid = repository._uuid(parent["document_uuid"])
        if uuid != parent["document_uuid"] or uuid in parents:
            raise Llm1CompletionError("Official parent inventory must have unique canonical UUIDs")
        for field in ("filename", "type", "stage", "published_at"):
            repository._text(parent[field], limit=1000, empty=field != "filename")
        _source_reference(parent["source_url"], uuid)
        if urlsplit(parent["source_url"]).hostname != "alfprod.seace.gob.pe":
            raise Llm1CompletionError("Official parent provenance requires its public document resolver")
        if "original_sha256" in parent:
            repository._sha(parent["original_sha256"])
        parents[uuid] = parent
    indexed, selected_ids, seen_paths = {}, set(), set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str) or not candidate["candidate_id"]:
            raise Llm1CompletionError("Selection candidate identity is invalid")
        cid = candidate["candidate_id"]
        if cid in indexed or candidate.get("document_uuid") not in parents:
            raise Llm1CompletionError("Selection candidates must have unique IDs and listed official parents")
        parent = parents[candidate["document_uuid"]]
        if (candidate.get("source_kind") not in {"loose", "archive_member"}
                or any(candidate.get(field) != parent[field] for field in ("type", "stage", "published_at", "source_url"))
                or not isinstance(candidate.get("filename"), str)):
            raise Llm1CompletionError("Selection candidate metadata differs from its official parent")
        try:
            safe_member(candidate["filename"])
            if PurePath(candidate["filename"]).name != candidate["filename"] or "\\" in candidate["filename"]:
                raise ValueError()
            if candidate["source_kind"] == "loose":
                if candidate["filename"] != parent["filename"]:
                    raise ValueError()
                if "original_sha256" in candidate and candidate["original_sha256"] != parent.get("original_sha256"):
                    raise ValueError()
            else:
                path = candidate["member_path"]
                if not isinstance(path, str):
                    raise ValueError()
                safe_member(path)
                supported_parent = PurePath(parent["filename"]).suffix.lower() in {".zip", ".rar"} or (
                    PurePath(parent["filename"]).suffix.lower() == ".7z" and archive_policy == POLICY_7Z
                    and candidate.get("archive_inventory_policy") == POLICY_7Z)
                if (PurePath(path.replace("\\", "/")).name != candidate["filename"]
                        or candidate.get("parent_filename") != parent["filename"]
                        or not supported_parent
                        or candidate.get("archive_sha256") != parent.get("original_sha256")):
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise Llm1CompletionError("Selection candidate source path or revision is invalid") from None
        indexed[cid] = candidate
    for decision in decisions:
        if (not isinstance(decision, dict) or decision.get("candidate_id") not in indexed
                or decision.get("decision") not in {"selected", "pending", "excluded"}):
            raise Llm1CompletionError("Selected snapshot decisions do not bind its candidate inventory")
        if decision["decision"] == "selected":
            selected_ids.add(decision["candidate_id"])
    if (len(decisions) != len(indexed) or len({d["candidate_id"] for d in decisions}) != len(indexed)
            or selected_ids != {row["candidate_id"] for row in rows}):
        raise Llm1CompletionError("Included candidates differ from the complete selection decisions")
    for row in rows:
        candidate, parent = indexed[row["candidate_id"]], parents[row["document_uuid"]]
        common = {"candidate_id", "document_uuid", "source_kind", "filename", "type", "stage", "published_at", "source_url"}
        if (not common.issubset(candidate) or any(row.get(field) != candidate[field] for field in common)
                or any(row.get(field) != value for field, value in candidate.items() if field in row)):
            raise Llm1CompletionError("Selected candidate metadata differs from the selection input snapshot")
        if any(repository._text(row[field], limit=1000, empty=True) != repository._text(parent[field], limit=1000, empty=True)
               for field in ("type", "stage", "published_at", "source_url")):
            raise Llm1CompletionError("Selected candidate does not inherit its official parent metadata")
        _source_reference(row["source_url"], row["document_uuid"])
        parent_sha = parent.get("original_sha256")
        if row["source_kind"] == "loose":
            if row["filename"] != parent["filename"] or row["original_sha256"] != parent_sha:
                raise Llm1CompletionError("Loose candidate does not bind its official downloaded parent")
            path_identity = (row["document_uuid"], "")
        else:
            if row["parent_filename"] != parent["filename"] or row["archive_sha256"] != parent_sha:
                raise Llm1CompletionError("Archive member does not bind its official downloaded container")
            path_identity = (row["document_uuid"], row["member_path"].replace("\\", "/"))
        if path_identity in seen_paths:
            raise Llm1CompletionError("The same source path must not masquerade as distinct selected candidates")
        seen_paths.add(path_identity)
    if code not in {row["document_uuid"] for row in rows} or parents[code].get("original_sha256") != sha:
        raise Llm1CompletionError("Storage anchor must bind a selected official parent and its root SHA")
    manifest = raw.get("source_document_manifest")
    if not isinstance(manifest, list) or len(manifest) != len(rows):
        raise Llm1CompletionError("Selected page manifest must cover every included candidate")
    previous, page_count = {}, 0
    for row, record in zip(rows, manifest):
        allowed = set(row) | {"analysis_sha256", "start_page", "end_page", "page_offset"}
        word_derivative = word_policy == WORD_FOOTNOTES_AT_END_POLICY and PurePath(row["filename"]).suffix.lower() in {".doc", ".docx"}
        if word_derivative:
            allowed.add("word_conversion")
        if (not isinstance(record, dict) or set(record) not in (allowed, allowed | {"duplicate_of"})
                or any(record.get(field) != value for field, value in row.items())):
            raise Llm1CompletionError("Selected page manifest differs from the prepared source snapshot")
        repository._sha(record["analysis_sha256"])
        if word_derivative:
            _word_conversion_provenance(record["word_conversion"], record["analysis_sha256"], identity["max_source_bytes"])
        start, end, offset = (record[field] for field in ("start_page", "end_page", "page_offset"))
        if (type(start) is not int or type(end) is not int or type(offset) is not int
                or not 1 <= start <= end <= MAX_ANALYSIS_PAGES or offset != start - 1):
            raise Llm1CompletionError("Selected page ranges and offsets must be precise and bounded")
        if "duplicate_of" in record:
            original = previous.get(record["duplicate_of"])
            if not original or "duplicate_of" in original or any(record[field] != original[field]
                    for field in ("analysis_sha256", "start_page", "end_page", "page_offset")):
                raise Llm1CompletionError("Selected duplicates require an earlier identical candidate derivative")
        elif start != page_count + 1:
            raise Llm1CompletionError("Selected page ranges have a gap or overlap")
        else:
            page_count = end
        previous[row["candidate_id"]] = record
    if any(term["page"] is not None and term["page"] > page_count for term in derive_participation(raw).values()):
        raise Llm1CompletionError("Participation citation exceeds the selected PDF global pages")
    if extraction["requires_human_review"] is not True:
        raise Llm1CompletionError("Selected coverage requires independent authority review")
    return {**context, "source_document_manifest": manifest, "analysis_page_count": page_count}


def _latest_revision(cursor, convocation, code, sha):
    cursor.execute("""SELECT sha256_original,downloaded_at FROM prod2_document_downloads
        WHERE id_convocatoria=%s AND codigo_alfresco=%s ORDER BY downloaded_at DESC""", (convocation, code))
    revisions = cursor.fetchall()
    if not revisions or revisions[0]["sha256_original"] != sha or any(
        row["downloaded_at"] == revisions[0]["downloaded_at"] and row["sha256_original"] != sha
        for row in revisions
    ):
        raise Llm1CompletionError("Extraction is not bound to the latest unambiguous downloaded revision")


def _current_publication_inventory(cursor, convocation, detail, indexed):
    """Reconcile publication rows independently of downloadable resource UUIDs.

    Historical captures without the additive event array retain their original
    one-publication-per-resource contract. New captures must agree with both the
    complete native row total and the separately persisted publication history.
    This check does not alter any paid document or selector request identity.
    """
    raw = detail.get("raw_detail")
    if raw is not None and not isinstance(raw, dict):
        raise Llm1CompletionError("Current official publication metadata is incomplete")
    if not isinstance(raw, dict) or "document_events" not in raw:
        if type(detail["document_inventory_total"]) is not int or detail["document_inventory_total"] != len(indexed):
            raise Llm1CompletionError("Document-set coverage differs from the full current official inventory")
        return
    events = raw["document_events"]
    if (not isinstance(events, list) or type(detail["document_inventory_total"]) is not int
            or detail["document_inventory_total"] != len(events)):
        raise Llm1CompletionError("Current official publication inventory is incomplete")
    expected, first = {}, {}
    fields = {"uuid", "filename", "stage", "type", "published_at_text", "published_at"}
    for event in events:
        if not isinstance(event, dict) or set(event) != fields:
            raise Llm1CompletionError("Current official publication metadata is incomplete")
        code = repository._uuid(event["uuid"])
        if code != event["uuid"] or code not in indexed:
            raise Llm1CompletionError("Publication does not link to a current official resource")
        for field in ("filename", "stage", "type", "published_at_text"):
            if repository._text(event[field], limit=100 if field == "published_at_text" else 1000,
                                empty=field != "filename") != event[field]:
                raise Llm1CompletionError("Current official publication metadata is not canonical")
        if event["published_at"] != repository._official_time(event["published_at_text"]):
            raise Llm1CompletionError("Current official publication timestamp is contradictory")
        resource = indexed[code]
        if event["filename"] != resource["name"]:
            raise Llm1CompletionError("Publication filename contradicts its official resource")
        digest = event_hash(event)
        if digest in expected:
            raise Llm1CompletionError("Current official publication inventory repeats an exact event")
        expected[digest] = event
        first.setdefault(code, event)
    if set(first) != set(indexed):
        raise Llm1CompletionError("Current official resource is missing its publication history")
    for code, event in first.items():
        resource = indexed[code]
        if any(event[field] != resource[column] for field, column in (
                ("stage", "stage_name"), ("type", "document_type"), ("published_at_text", "published_at_text"))):
            raise Llm1CompletionError("Official resource differs from its first publication representative")
    cursor.execute("""SELECT codigo_alfresco,event_hash,filename,seace_version,document_type,stage_name,
        published_at,published_at_text,is_listed FROM prod2_document_events
        WHERE id_convocatoria=%s AND is_listed ORDER BY codigo_alfresco,event_hash FOR UPDATE""", (convocation,))
    stored = cursor.fetchall()
    if len(stored) != len(expected) or {row["event_hash"] for row in stored} != set(expected):
        raise Llm1CompletionError("Persisted official publication history is incomplete or changed")
    for row in stored:
        event = expected[row["event_hash"]]
        timestamp = datetime.fromisoformat(event["published_at"]) if event["published_at"] else None
        if (not row["is_listed"] or row["seace_version"] != "3" or str(row["codigo_alfresco"]) != event["uuid"]
                or row["published_at"] != timestamp
                or any(row[column] != event[field] for field, column in (
                    ("filename", "filename"), ("stage", "stage_name"), ("type", "document_type"),
                    ("published_at_text", "published_at_text")))):
            raise Llm1CompletionError("Persisted official publication metadata contradicts the current capture")


def _current_document_set(cursor, convocation, context, *, description=None):
    detail = _one(cursor, """SELECT document_inventory_complete,document_inventory_total,raw_detail
        FROM prod2_details WHERE id_convocatoria=%s FOR UPDATE""", (convocation,))
    if not detail or detail["document_inventory_complete"] is not True:
        raise Llm1CompletionError("Current official document inventory is incomplete")
    cursor.execute("""SELECT codigo_alfresco,name,document_type,stage_name,published_at,published_at_text,
        source_url,is_listed FROM prod2_documents WHERE id_convocatoria=%s AND is_listed
        ORDER BY codigo_alfresco FOR UPDATE""", (convocation,))
    inventory = cursor.fetchall()
    selected_scope = context["document_scope"] == SELECTED_DOCUMENTS_POLICY
    if selected_scope and "ficha_description" in context["selection_context"]:
        from .analysis import ficha_reading_description
        try:
            expected = ficha_reading_description(description)
            raw = detail.get("raw_detail")
            if (context["selection_context"]["ficha_description"] != expected
                    or (isinstance(raw, dict) and "description" in raw
                        and ficha_reading_description(raw["description"]) != expected)):
                raise ValueError()
        except ValueError:
            raise Llm1CompletionError("Selected ficha description differs from current official discovery/detail") from None
    bound = (context["selection_context"]["official_inventory"] if selected_scope
             else context["document_set"] + context["excluded_documents"])
    indexed = {str(row["codigo_alfresco"]): row for row in inventory}
    if len(indexed) != len(inventory):
        raise Llm1CompletionError("Current official resource inventory repeats a UUID")
    _current_publication_inventory(cursor, convocation, detail, indexed)
    if (len(indexed) != len(bound)
            or set(indexed) != {row["document_uuid"] for row in bound}):
        raise Llm1CompletionError("Document-set coverage differs from the full current official inventory")
    for doc in bound:
        row = indexed[doc["document_uuid"]]
        values = {"filename": row["name"], "stage": row["stage_name"], "type": row["document_type"],
                  "published_at": row["published_at_text"]}
        if any(repository._text(doc[field], limit=1000, empty=True) != value for field, value in values.items()):
            raise Llm1CompletionError("Current official document metadata changed since the paid input")
        if not row["is_listed"] or row["source_url"] != doc["source_url"]:
            raise Llm1CompletionError("Current official document provenance changed")
    downloaded = [row for row in bound if "original_sha256" in row] if selected_scope else context["document_set"]
    for doc in downloaded:
        _latest_revision(cursor, convocation, doc["document_uuid"], doc["original_sha256"])
    if selected_scope:
        _current_selection_receipt(cursor, context["selection_context"], description)
    return indexed


def _current_selection_receipt(cursor, selection, description):
    """Validate the global paid cache and reconstruct the current plan without HTTP."""
    digest = selection["selector_request_hash"]
    receipt = _one(cursor, """SELECT * FROM prod2_document_selection_cache
        WHERE request_hash=%s FOR SHARE""", (digest,))
    if (not receipt or receipt.get("status") != "done" or not receipt.get("completed_at")
            or receipt.get("request_hash") != digest
            or receipt.get("policy_version") not in {document_selection.DOCUMENT_SELECTION_POLICY,
                                                     document_selection.DOCUMENT_SELECTION_POLICY_V2,
                                                     document_selection.DOCUMENT_SELECTION_POLICY_V3}):
        raise Llm1CompletionError("Selected documents have no completed durable selector receipt")
    result = receipt.get("result_json")
    if (not isinstance(result, dict) or result.get("model") != receipt.get("model")
            or result.get("policy_version") != receipt["policy_version"]
            or result.get("request_hash") != digest or result.get("cache_key") != document_selection.CACHE_PREFIX + digest
            or canonical_hash(result.get("raw_response")) != selection["selector_receipt_hash"]):
        raise Llm1CompletionError("Selected snapshot does not bind the exact paid selector response")
    try:
        policy_version = receipt["policy_version"]
        payload, mappings, unsupported = document_selection._inputs(description, selection["candidates"], receipt["model"],
                                                                    policy_version=policy_version)
        expected_hash = canonical_hash({"policy_version": policy_version, "payload": payload})
        if expected_hash != digest:
            raise Llm1CompletionError("Selector request does not bind current description and candidate metadata")
        results = document_selection._answers(result["raw_response"], payload, policy_version=policy_version)
        thresholds = selection["thresholds"]
        decisions = document_selection._plan(results, mappings, unsupported, thresholds["high"] / 100, thresholds["low"] / 100,
                                             candidates=selection["candidates"], policy_version=policy_version)
        by_id = {decision["candidate_id"]: decision for decision in decisions}
        decisions = [by_id[row["candidate_id"]] for row in selection["candidates"]]
    except document_selection.DocumentSelectionError:
        raise Llm1CompletionError("Paid selector primitives or input context are invalid") from None
    if decisions != selection["decisions"]:
        raise Llm1CompletionError("Selected snapshot decisions differ from the current threshold plan")


class _ProjectionConnection:
    """Add stable attribution to the existing participation writer's payload."""
    def __init__(self, conn, metadata):
        self.conn, self.metadata = conn, metadata

    def execute(self, statement, params):
        if len(params) != 4 or "UPDATE opportunities" not in statement:
            raise Llm1CompletionError("Unexpected canonical participation writer contract")
        payload = json.loads(params[2])
        payload.update(self.metadata)
        return self.conn.execute(statement, (*params[:2], json.dumps(payload, ensure_ascii=False), params[3]))


def persist_completed_llm1(conn, *, convocation_id: int, document_uuid: str,
                           extraction: dict, document_sha256: str, source_url: str,
                           preserve_foreign_participation: dict | None = None) -> dict:
    """Project the current paid LLM1 result; perform no HTTP, LLM2, matching or alerts.

    Existing canonical evidence from another source/document requires explicit
    reconciliation. Missing clauses normalize to ``not_identified``. The helper
    checks receipt attribution and schema, not whether a quoted clause actually
    occurs at its reported PDF page. Loose-document sets retain every UUID and
    global page range; unresolved authority or coverage projects pending terms
    while preserving the model's raw reading unchanged.
    The versioned, exact snapshot opt-in saves a selected reading as pending
    while leaving foreign canonical participation and its timestamp untouched.
    """
    convocation = repository._id(convocation_id)
    code, sha = repository._uuid(document_uuid), repository._sha(document_sha256)
    source = _source_reference(source_url, code)
    if not isinstance(extraction, dict):
        raise Llm1CompletionError("Expected a completed extraction bundle")
    evaluation_id = repository._id(extraction.get("evaluation_id"))
    models = extraction.get("allowed_models")
    if not isinstance(models, (list, tuple)) or not models or any(
        not isinstance(model, str) or not (model.startswith("gemini-") or model == "gpt-6-luna") for model in models
    ):
        raise Llm1CompletionError("Configured LLM1 model allowlist is required")
    if extraction.get("model") == "gpt-6-luna":
        from .luna_policy import validate_luna_bundle
        try:
            validate_luna_bundle(extraction)
        except (KeyError, TypeError, ValueError) as error:
            raise Llm1CompletionError("Luna extraction provider identity is invalid") from error
    if extraction.get("original_sha256") != sha:
        raise Llm1CompletionError("Extraction does not bind the original document SHA")
    for field, expected in (("document_uuid", code), ("document_sha256", sha)):
        if field in extraction and extraction[field] != expected:
            raise Llm1CompletionError("Extraction document attribution changed")
    if any(field not in extraction for field in _BOUND_FIELDS):
        raise Llm1CompletionError("Incomplete exact extraction receipt")
    repository._sha(extraction["analysis_sha256"])
    repository._sha(extraction["prompt_hash"])
    result = extraction_for_persistence(extraction)
    from .extraction_policy import validate_scope_payload, get_scope_policy
    from .input_policy import (schema_for_input as schema_for_scope,
        prompt_version_for_input as prompt_version_for_scope,
        schema_version_for_input as schema_version_for_scope, is_retrieved_input)
    selection = extraction.get("selection_context")
    retrieved = is_retrieved_input(selection)
    try:
        schema = schema_for_scope(extraction["doc_class"], selection)
        validate_scope_payload(result.raw_json, selection)
        if get_scope_policy(selection):
            expected_coverage = {"policy_version": get_scope_policy(selection),
                "not_evaluated": ["payment", "requirements.proposal_documents"]}
            if (extraction["prompt_version"] != prompt_version_for_scope(extraction["doc_class"], selection)
                    or extraction["schema_version"] != schema_version_for_scope(extraction["doc_class"], selection)
                    or result.raw_json.get("extraction_coverage") != expected_coverage):
                raise ValueError("Focused extraction versions or coverage differ")
    except ValueError as error:
        raise Llm1CompletionError("Invalid extraction scope or schema version") from error
    schema.model_validate(result.raw_json)
    context = _document_set_context(extraction, code, sha)
    if retrieved:
        proof = (context or {}).get("llm1_input", {})
        text = result.raw_json.get("retrieval_context_text")
        from .input_policy import validate_retrieved_evidence
        import hashlib
        if (not isinstance(text, str) or result.requires_human_review is not True
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != proof.get("context_sha256")
                or len(text.encode("utf-8")) != proof.get("context_bytes")
                or any(block["text"] not in text for block in proof["selected_blocks"])):
            raise Llm1CompletionError("Retrieved extraction must retain exact sent text and human review")
        audit = result.raw_json.get("retrieval_evidence_audit")
        validation_raw = deepcopy(result.raw_json)
        if isinstance(audit, dict) and "paid_participation" in audit:
            validation_raw["participation"] = audit["paid_participation"]
        expected_audit = validate_retrieved_evidence(validation_raw, proof)
        if (not isinstance(audit, dict)
                or any(audit.get(key) != value for key, value in expected_audit.items())):
            raise Llm1CompletionError("Retrieved evidence validation differs from the sent literal blocks")

    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        discovery = _one(cursor, "SELECT opportunity_id,description FROM prod2_discoveries WHERE id_convocatoria=%s FOR UPDATE", (convocation,))
        if not discovery:
            raise Llm1CompletionError("Unknown canonical discovery")
        opportunity = str(discovery["opportunity_id"])
        canonical = _one(cursor, "SELECT consortium_status,subcontracting_status,participation_terms_json,updated_at FROM opportunities WHERE id=%s FOR UPDATE", (opportunity,))
        if not canonical:
            raise Llm1CompletionError("Canonical opportunity is missing")
        reservation = _one(cursor, "SELECT * FROM prod2_evaluations WHERE id=%s FOR UPDATE", (evaluation_id,))
        if not reservation or (reservation["id_convocatoria"], reservation["kind"], reservation["status"]) != (
            convocation, "gemini_extraction", "done",
        ):
            raise Llm1CompletionError("Extraction has no completed attributable reservation")
        binding = reservation["result_json"]
        if extraction.get("model") == "gpt-6-luna" and (
                reservation.get("model") != "gpt-6-luna"
                or not isinstance(binding, dict)
                or binding.get("provider_request") != extraction.get("provider_request")
                or not isinstance(extraction.get("llm1_fallback_from"), dict)
                or binding.get("llm1_fallback_from") != extraction.get("llm1_fallback_from")):
            raise Llm1CompletionError("Luna extraction requires its own provider reservation")
        if (not isinstance(binding, dict) or binding.get("document_uuid") != code
                or binding.get("document_sha256") != sha
                or extraction["cache_key"] != "prod2:llm1:" + reservation["request_hash"]
                or any(binding.get(field) != extraction[field] for field in _BOUND_FIELDS)
                or (context is not None and any(binding.get(field) != extraction[field]
                    for field in (_SELECTED_FIELDS if context["document_scope"] == SELECTED_DOCUMENTS_POLICY else _SET_FIELDS)))):
            raise Llm1CompletionError("Paid receipt does not bind the exact input, version and extraction result")
        if context is not None and "llm1_input" in context and binding.get("llm1_input") != context["llm1_input"]:
            raise Llm1CompletionError("Paid receipt does not bind the exact text/PDF input proof")
        if context is not None:
            candidate = _current_document_set(cursor, convocation, context, description=discovery.get("description"))[code]
        else:
            candidate = _one(cursor, """SELECT codigo_alfresco,published_at,published_at_text,is_listed
                FROM prod2_documents WHERE id_convocatoria=%s AND codigo_alfresco=%s FOR UPDATE""", (convocation, code))
            if not candidate or not candidate["is_listed"]:
                raise Llm1CompletionError("Document is not listed in the current official ficha")
            _latest_revision(cursor, convocation, code, sha)
        if retrieved:
            _verify_retrieved_database(conn, context["llm1_input"])
        current = _one(cursor, """SELECT ex.id,ex.evaluation_id,ex.sha256_original,ex.created_at,
            ex.codigo_alfresco,d.published_at,d.published_at_text,ev.completed_at
            FROM prod2_document_extractions ex JOIN prod2_documents d
              USING(id_convocatoria,codigo_alfresco)
            JOIN prod2_evaluations ev ON ev.id=ex.evaluation_id
            WHERE ex.id_convocatoria=%s AND ex.is_current FOR UPDATE OF ex""", (convocation,))
        if current and current["evaluation_id"] != evaluation_id:
            if context is not None:
                _newer_paid(reservation, current)
            elif str(current["codigo_alfresco"]) != code:
                _not_older(candidate, current)
            else:
                _newer_paid(reservation, current)
        previous = canonical["participation_terms_json"] or {}
        if not isinstance(previous, dict):
            raise Llm1CompletionError("Canonical participation provenance is malformed")
        preserved = None
        if retrieved:
            # Retrieval is an explicit partial reading. Persist its paid result
            # and facets without changing any pre-existing canonical field,
            # regardless of the canonical source or its current status.
            preserved = canonical_participation_snapshot(canonical)
        elif preserve_foreign_participation is not None:
            preserved = _validate_foreign_preservation(preserve_foreign_participation,
                canonical=canonical, convocation=convocation, opportunity=opportunity,
                reservation=reservation, code=code, sha=sha, context=context, result=result)
        if previous and previous.get("source") != "seace_prod2" and (
                context is not None or previous.get("document_sha256") != sha) and preserved is None:
            raise Llm1CompletionError("Canonical participation from another source requires reconciliation")
        if not retrieved and previous.get("source") == "seace_prod2" and previous.get("extraction_id"):
            prior = _one(cursor, """SELECT ex.id,ex.id_convocatoria,ex.codigo_alfresco,ex.evaluation_id,
                d.published_at,d.published_at_text,ev.completed_at FROM prod2_document_extractions ex
                JOIN prod2_documents d USING(id_convocatoria,codigo_alfresco)
                JOIN prod2_evaluations ev ON ev.id=ex.evaluation_id
                WHERE ex.id=%s""", (previous["extraction_id"],))
            if not prior:
                raise Llm1CompletionError("Canonical participation extraction provenance is missing")
            if context is not None and prior["id_convocatoria"] != convocation:
                raise Llm1CompletionError("Canonical participation belongs to a different convocation")
            if prior["evaluation_id"] != evaluation_id:
                if context is not None:
                    _newer_paid(reservation, prior)
                elif str(prior["codigo_alfresco"]) != code or prior["id_convocatoria"] != convocation:
                    _not_older(candidate, prior)
                else:
                    _newer_paid(reservation, prior)

        saved = repository.persist_extraction(conn, convocation, code, sha, evaluation_id=evaluation_id,
                                             result=result, allowed_models=tuple(models))
        stored = _one(cursor, """SELECT id_convocatoria,opportunity_id,codigo_alfresco,
            sha256_original,is_current,quality,raw_extraction_json,requires_human_review,created_at
            FROM prod2_document_extractions WHERE id=%s FOR UPDATE""", (saved["extraction_id"],))
        if (not stored or not stored["is_current"] or stored["quality"] == "failed"
                or (stored["id_convocatoria"], str(stored["opportunity_id"]), str(stored["codigo_alfresco"]), stored["sha256_original"]) != (
                    convocation, opportunity, code, sha,
                ) or stored["raw_extraction_json"] != result.raw_json
                or stored["requires_human_review"] != result.requires_human_review):
            raise Llm1CompletionError("Historical or altered extraction cannot project canonical participation")
        if preserved is not None:
            # All paid/context/current-revision guards ran above. No canonical
            # UPDATE is permitted, even for a missing clause or pending status.
            after = _one(cursor, "SELECT consortium_status,subcontracting_status,participation_terms_json,updated_at FROM opportunities WHERE id=%s FOR UPDATE", (opportunity,))
            if canonical_participation_snapshot(after) != preserved:
                raise Llm1CompletionError("Foreign canonical participation changed during reading persistence")
            terms = derive_participation(stored["raw_extraction_json"])
            return {"extraction_id": saved["extraction_id"],
                "subcontratacion": terms["subcontratacion"], "consorcio": terms["consorcio"],
                "requires_human_review": stored["requires_human_review"], "projection_pending": True,
                "canonical_participation_preserved": True,
                "projection_pending_reason": "retrieved_context_requires_evidence_review" if retrieved else
                    "foreign_canonical_participation_requires_reconciliation",
                "preservation_policy_version": RETRIEVED_PROJECTION_POLICY if retrieved else PRESERVE_FOREIGN_POLICY,
                "preserved_canonical_snapshot_hash": canonical_hash(preserved)}
        metadata = {"source_url": source, "document_uuid": code, "convocation_id": convocation,
                    "projection_policy_version": PROJECTION_POLICY,
                    "request_hash": reservation["request_hash"], "model": result.model,
                    "prompt_version": result.prompt_version, "schema_version": result.schema_version}
        projection_raw = stored["raw_extraction_json"]
        pending = context is not None and stored["requires_human_review"]
        if context is not None:
            selected_scope = context["document_scope"] == SELECTED_DOCUMENTS_POLICY
            metadata.update(context, projection_policy_version=SELECTED_PROJECTION_POLICY if selected_scope else SET_PROJECTION_POLICY,
                            projection_pending=pending, storage_anchor_only=True,
                            coverage="selected_documents_only" if selected_scope else
                            "loose_documents_only_archives_deferred" if context["excluded_documents"] else "loose_documents_only")
            if pending:
                # No clause or status is invented. The original paid reading
                # remains in raw_extraction_json; canonical authority is pending.
                projection_raw = {}
        repo = SimpleNamespace(conn=_ProjectionConnection(conn, metadata))
        persist_participation(repo, opportunity_id=opportunity, raw=projection_raw,
                              extraction_id=saved["extraction_id"], source="seace_prod2", document_sha256=sha)
        terms = derive_participation(projection_raw)
        output = {"extraction_id": saved["extraction_id"], "subcontratacion": terms["subcontratacion"],
                  "consorcio": terms["consorcio"], "requires_human_review": stored["requires_human_review"]}
        if context is not None:
            output["projection_pending"] = pending
        return output


def _verify_retrieved_database(conn, proof):
    """Re-read actual corpus/vector/context rows inside the persistence transaction."""
    from .embedding_store import verified_corpus_retrieval
    try:
        actual = verified_corpus_retrieval(conn, proof["corpus_hash"], proof["retrieval_hash"],
            allow_transaction=True, expected_input=proof)
        expected = proof["durable_proof"]
        if any(actual.get(key) != value for key, value in expected.items() if key != "verified_at"):
            raise ValueError()
    except Exception as error:
        # Avoid including database text, DSNs, or an exception's provider body.
        raise Llm1CompletionError("Durable retrieved corpus, vectors or context changed") from None
