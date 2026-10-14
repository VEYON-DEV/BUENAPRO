"""Offline transactional completion contracts; no models or PostgreSQL access."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from pydantic import ValidationError
import pytest

from buenapro_worker.prod2 import llm1_completion as subject


CONV = 1046958
CODE = "efdf87fb-9e04-4fa7-9c34-5912db454f7f"
OTHER_CODE = "bfdf87fb-9e04-4fa7-9c34-5912db454f7f"
OPPORTUNITY = "bcbc122b-c585-48fc-b345-07221a994975"
SHA = "a" * 64
REQUEST = "b" * 64
MODEL = "gemini-3.1-flash-lite"
NOW = datetime(2026, 10, 6, 17, tzinfo=timezone.utc)
SOURCE = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml"


def bundle():
    return {"cache_key": "prod2:llm1:" + REQUEST, "original_sha256": SHA,
            "analysis_sha256": "c" * 64, "doc_class": "bases_service", "model": MODEL,
            "prompt_version": "bases_extraction_v2", "prompt_hash": "d" * 64,
            "schema_version": "bases_extraction_schema_v2", "requires_human_review": False,
            "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.001,
            "evaluation_id": 7, "allowed_models": [MODEL, "gemini-2.5-flash"],
            "document_uuid": CODE, "document_sha256": SHA,
            "raw_extraction_json": {"participation": {
                "subcontratacion": {"status": "conditional", "clause": "Requiere autorización previa.",
                                    "page": 4, "conditions": ["Autorización previa"]},
                "consorcio": {"status": "prohibited", "clause": "No se permite consorcio.",
                              "page": 5, "conditions": []}}}}


class FakeConnection:
    def __init__(self, extraction):
        self.extraction = extraction
        self.depth, self.events, self.updates, self.persist_calls = 0, [], [], []
        self.discovery = {"opportunity_id": OPPORTUNITY}
        self.canonical = {"participation_terms_json": {}}
        self.reservation = {"id": 7, "id_convocatoria": CONV, "kind": "gemini_extraction",
                            "status": "done", "model": MODEL, "request_hash": REQUEST,
                            "policy_version": "prod2_document_extraction_v1", "completed_at": NOW,
                            "result_json": deepcopy(extraction)}
        self.candidate = {"codigo_alfresco": CODE, "is_listed": True, "published_at": NOW,
                          "published_at_text": "06/10/2026 12:00"}
        self.revisions = [{"sha256_original": SHA, "downloaded_at": NOW}]
        self.inventory, self.detail, self.document_revisions, self.publications = [], None, {}, []
        self.current, self.prior = None, None
        self.stored = {"id_convocatoria": CONV, "opportunity_id": OPPORTUNITY, "codigo_alfresco": CODE,
                       "sha256_original": SHA, "is_current": True, "quality": "auto",
                       "raw_extraction_json": deepcopy(extraction["raw_extraction_json"]),
                       "requires_human_review": extraction["requires_human_review"], "created_at": NOW}

    @contextmanager
    def transaction(self):
        self.depth += 1
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.depth -= 1

    @contextmanager
    def cursor(self, **kwargs):
        yield FakeCursor(self)

    def execute(self, statement, params):
        assert self.depth == 1
        self.updates.append((statement, params))


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.response = conn, None

    def execute(self, statement, params=()):
        assert self.conn.depth == 1
        if "FROM prod2_discoveries" in statement:
            self.response = self.conn.discovery
        elif "FROM opportunities" in statement:
            self.response = self.conn.canonical
        elif "FROM prod2_evaluations WHERE" in statement:
            self.response = self.conn.reservation
        elif "FROM prod2_details" in statement:
            self.response = self.conn.detail
        elif "FROM prod2_document_events" in statement:
            self.response = self.conn.publications
        elif "FROM prod2_documents WHERE" in statement and "AND is_listed" in statement:
            self.response = self.conn.inventory
        elif "FROM prod2_documents WHERE" in statement:
            self.response = self.conn.candidate
        elif "FROM prod2_document_downloads" in statement:
            self.response = self.conn.document_revisions.get(params[1], self.conn.revisions)
        elif "FROM prod2_document_extractions ex" in statement and "WHERE ex.is_current" in statement:
            raise AssertionError("Unexpected query contract")
        elif "FROM prod2_document_extractions ex" in statement and "AND ex.is_current" in statement:
            self.response = self.conn.current
        elif "FROM prod2_document_extractions ex" in statement and "WHERE ex.id=%s" in statement:
            self.response = self.conn.prior
        elif "FROM prod2_document_extractions WHERE id=%s" in statement:
            self.response = self.conn.stored
        else:
            raise AssertionError("Unexpected query contract")

    def fetchone(self):
        return self.response

    def fetchall(self):
        return self.response


def fixture(monkeypatch, extraction=None):
    extraction = extraction or bundle()
    conn = FakeConnection(extraction)
    def persist(conn_arg, conv, code, sha, *, evaluation_id, result, allowed_models):
        assert conn_arg is conn and conn.depth == 1
        conn.persist_calls.append((conv, code, sha, evaluation_id, result, allowed_models))
        return {"extraction_id": 42, "opportunity_id": OPPORTUNITY, "cached": conn.current is not None}
    monkeypatch.setattr(subject.repository, "persist_extraction", persist)
    return conn, extraction


def finish(conn, extraction, **overrides):
    return subject.persist_completed_llm1(conn, **{
        "convocation_id": CONV, "document_uuid": CODE, "extraction": extraction,
        "document_sha256": SHA, "source_url": SOURCE, **overrides})


def test_completed_llm1_projects_normalized_terms_with_document_and_paid_provenance(monkeypatch):
    conn, extraction = fixture(monkeypatch)
    before = deepcopy(extraction)
    result = finish(conn, extraction)
    assert extraction == before
    assert result == {"extraction_id": 42, "subcontratacion": before["raw_extraction_json"]["participation"]["subcontratacion"],
                      "consorcio": before["raw_extraction_json"]["participation"]["consorcio"], "requires_human_review": False}
    assert conn.events == ["begin", "commit"]
    assert len(conn.persist_calls) == 1
    assert conn.persist_calls[0][0:4] == (CONV, CODE, SHA, 7)
    assert conn.persist_calls[0][5] == (MODEL, "gemini-2.5-flash")
    assert len(conn.updates) == 1
    sql, params = conn.updates[0]
    assert "UPDATE opportunities" in sql
    assert params[0:2] == ("prohibited", "conditional")
    assert params[3] == OPPORTUNITY
    provenance = json.loads(params[2])
    assert provenance["source"] == "seace_prod2"
    assert provenance["extraction_id"] == 42
    assert provenance["document_sha256"] == SHA
    assert provenance["document_uuid"] == CODE
    assert provenance["source_url"] == SOURCE
    assert provenance["request_hash"] == REQUEST
    assert provenance["projection_policy_version"] == subject.PROJECTION_POLICY


def test_missing_participation_is_not_permission_and_preserves_review_flag(monkeypatch):
    extraction = bundle()
    extraction.update(raw_extraction_json={"contract": {"objeto": "SOFTWARE"}}, requires_human_review=True)
    conn, extraction = fixture(monkeypatch, extraction)
    result = finish(conn, extraction)
    assert result["consorcio"]["status"] == "not_identified"
    assert result["subcontratacion"]["status"] == "not_identified"
    assert result["requires_human_review"] is True


def test_unsupported_permission_claim_without_clause_is_normalized_not_identified(monkeypatch):
    extraction = bundle()
    extraction["raw_extraction_json"]["participation"]["subcontratacion"] = {"status": "permitted"}
    conn, extraction = fixture(monkeypatch, extraction)
    assert finish(conn, extraction)["subcontratacion"]["status"] == "not_identified"


@pytest.mark.parametrize("mutation", ["missing_evaluation", "missing_allowlist", "foreign_sha", "foreign_uuid", "bad_cache", "altered_raw", "wrong_prompt", "not_done"])
def test_unbound_or_modified_receipt_never_projects_canonical_terms(monkeypatch, mutation):
    conn, extraction = fixture(monkeypatch)
    if mutation == "missing_evaluation":
        extraction.pop("evaluation_id")
    elif mutation == "missing_allowlist":
        extraction.pop("allowed_models")
    elif mutation == "foreign_sha":
        extraction["original_sha256"] = "f" * 64
    elif mutation == "foreign_uuid":
        extraction["document_uuid"] = OTHER_CODE
    elif mutation == "bad_cache":
        extraction["cache_key"] = "prod2:llm1:" + "e" * 64
    elif mutation == "altered_raw":
        extraction["raw_extraction_json"]["participation"]["subcontratacion"]["status"] = "permitted"
    elif mutation == "wrong_prompt":
        extraction["prompt_version"] = "foreign-prompt"
    else:
        conn.reservation["status"] = "uncertain"
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction)
    assert conn.persist_calls == []
    assert conn.updates == []


@pytest.mark.parametrize("field,value", [("page", True), ("page", "4"), ("page", 0), ("status", "allowed")])
def test_corrupted_typed_participation_is_rejected_before_database_access(monkeypatch, field, value):
    extraction = bundle()
    extraction["raw_extraction_json"]["participation"]["subcontratacion"][field] = value
    conn, extraction = fixture(monkeypatch, extraction)
    with pytest.raises(ValidationError):
        finish(conn, extraction)
    assert conn.events == []


@pytest.mark.parametrize("mutation", ["unlisted", "older_sha", "ambiguous_sha", "historic_extraction", "failed_quality", "changed_stored_raw"])
def test_stale_or_uncertain_revision_never_projects_and_rolls_back(monkeypatch, mutation):
    conn, extraction = fixture(monkeypatch)
    if mutation == "unlisted":
        conn.candidate["is_listed"] = False
    elif mutation == "older_sha":
        conn.revisions[0]["sha256_original"] = "f" * 64
    elif mutation == "ambiguous_sha":
        conn.revisions.append({"sha256_original": "f" * 64, "downloaded_at": NOW})
    elif mutation == "historic_extraction":
        conn.stored["is_current"] = False
    elif mutation == "failed_quality":
        conn.stored["quality"] = "failed"
    else:
        conn.stored["raw_extraction_json"]["participation"]["subcontratacion"]["clause"] = "Changed stored evidence"
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert conn.updates == []
    assert conn.events == ["begin", "rollback"]


def current_document(*, code=OTHER_CODE, published=NOW + timedelta(days=1), completed=NOW + timedelta(days=1)):
    return {"id": 43, "evaluation_id": 8, "codigo_alfresco": code, "sha256_original": SHA,
            "created_at": NOW, "id_convocatoria": CONV, "published_at": published,
            "published_at_text": "07/10/2026", "completed_at": completed}


@pytest.mark.parametrize("where", ["current", "canonical"])
def test_older_document_or_paid_cache_cannot_replace_newer_participation(monkeypatch, where):
    conn, extraction = fixture(monkeypatch)
    if where == "current":
        conn.current = current_document()
    else:
        conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 43, "document_sha256": SHA}
        conn.prior = current_document()
    with pytest.raises(subject.Llm1CompletionError, match="chronology"):
        finish(conn, extraction)
    assert conn.persist_calls == []
    assert conn.updates == []


def test_older_prompt_result_for_same_document_does_not_replace_current(monkeypatch):
    conn, extraction = fixture(monkeypatch)
    conn.current = current_document(code=CODE)
    with pytest.raises(subject.Llm1CompletionError, match="older paid extraction"):
        finish(conn, extraction)
    assert conn.persist_calls == []


def test_idempotent_current_cached_extraction_can_repair_canonical_projection(monkeypatch):
    conn, extraction = fixture(monkeypatch)
    conn.current = {**current_document(code=CODE), "id": 42, "evaluation_id": 7}
    conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 42, "document_sha256": SHA}
    conn.prior = conn.current
    assert finish(conn, extraction)["extraction_id"] == 42
    assert len(conn.updates) == 1


def test_canonical_other_source_revision_is_preserved_until_reconciled(monkeypatch):
    conn, extraction = fixture(monkeypatch)
    conn.canonical["participation_terms_json"] = {"source": "seace_prod4", "extraction_id": 100, "document_sha256": "f" * 64}
    with pytest.raises(subject.Llm1CompletionError, match="another source"):
        finish(conn, extraction)
    assert conn.updates == []
    assert conn.persist_calls == []


@pytest.mark.parametrize("url", [SOURCE + "?alf_ticket=opaque", SOURCE + ";jsessionid=opaque", "https://outside.example/ficha"])
def test_source_reference_never_stores_tickets_or_foreign_urls(monkeypatch, url):
    conn, extraction = fixture(monkeypatch)
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction, source_url=url)
    assert conn.events == []


def test_official_public_document_link_is_bound_to_its_exact_uuid_and_preserved(monkeypatch):
    conn, extraction = fixture(monkeypatch)
    url = "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE
    assert finish(conn, extraction, source_url=url)["extraction_id"] == 42
    assert json.loads(conn.updates[0][1][2])["source_url"] == url


@pytest.mark.parametrize("url", [
    "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + OTHER_CODE,
    "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE + "&alf_ticket=opaque",
    "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE + "&id=" + CODE,
    "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=",
    "http://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE,
    "https://alfprod.seace.gob.pe/alfresco/service/other?id=" + CODE,
    "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE + "#fragment",
    "https://user@alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE,
])
def test_document_link_rejects_wrong_uuid_tickets_redirect_routes_and_ambiguous_params(monkeypatch, url):
    conn, extraction = fixture(monkeypatch)
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction, source_url=url)
    assert conn.events == []


SET_CODE = "ffdf87fb-9e04-4fa7-9c34-5912db454f7f"
ARCHIVE_CODE = "afdf87fb-9e04-4fa7-9c34-5912db454f7f"


def set_bundle(*, multiple=True, archive=False):
    extraction = bundle()
    def document(code, name):
        return {"document_uuid": code, "filename": name, "stage": "Convocatoria", "type": "Documento",
                "published_at": "06/10/2026 12:00",
                "source_url": "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + code}
    direct = [{**document(CODE, "primero.pdf"), "original_sha256": SHA}]
    if multiple:
        direct.append({**document(SET_CODE, "Acta.docx"), "original_sha256": "e" * 64})
    excluded = [{**document(ARCHIVE_CODE, "Bases.zip"), "reason": "archive_deferred"}] if archive else []
    context = {"document_scope": subject.DIRECT_DOCUMENTS_POLICY,
               "document_set": direct, "excluded_documents": excluded}
    context["document_set_hash"] = subject.canonical_hash(context)
    extraction.update(context, requires_human_review=multiple or archive)
    extraction["raw_extraction_json"].update(deepcopy(context))
    extraction["raw_extraction_json"]["source_document_manifest"] = [
        {**doc, "analysis_sha256": str(index + 1) * 64,
         "start_page": 1 if index == 0 else 6, "end_page": 5 if index == 0 else 7}
        for index, doc in enumerate(direct)]
    return extraction


def set_fixture(monkeypatch, extraction=None, **kwargs):
    conn, extraction = fixture(monkeypatch, extraction or set_bundle(**kwargs))
    docs = extraction["document_set"] + extraction["excluded_documents"]
    conn.inventory = [{"codigo_alfresco": doc["document_uuid"], "is_listed": True,
                       "name": doc["filename"], "stage_name": doc["stage"], "document_type": doc["type"],
                       "published_at": NOW, "published_at_text": doc["published_at"], "source_url": doc["source_url"]}
                      for doc in docs]
    conn.detail = {"document_inventory_complete": True, "document_inventory_total": len(docs)}
    conn.document_revisions = {doc["document_uuid"]: [{"sha256_original": doc["original_sha256"], "downloaded_at": NOW}]
                               for doc in extraction["document_set"]}
    return conn, extraction


def sync_context(extraction):
    context = {field: extraction[field] for field in subject._SET_FIELDS[:-1]}
    extraction["document_set_hash"] = subject.canonical_hash(context)
    extraction["raw_extraction_json"].update(deepcopy({**context, "document_set_hash": extraction["document_set_hash"]}))


def test_direct_set_preserves_paid_reading_and_projects_pending_authority_with_all_provenance(monkeypatch):
    conn, extraction = set_fixture(monkeypatch, archive=True)
    before = deepcopy(extraction)
    result = finish(conn, extraction)
    assert extraction == before
    assert conn.persist_calls[0][4].raw_json == before["raw_extraction_json"]
    assert result["projection_pending"] is True
    assert result["requires_human_review"] is True
    assert result["subcontratacion"] == {"status": "not_identified", "clause": None, "page": None, "conditions": []}
    assert result["consorcio"]["status"] == "not_identified"
    metadata = json.loads(conn.updates[0][1][2])
    for field in (*subject._SET_FIELDS, "source_document_manifest"):
        assert metadata[field] == before["raw_extraction_json"][field]
    assert metadata["projection_pending"] is True
    assert metadata["storage_anchor_only"] is True
    assert metadata["coverage"] == "loose_documents_only_archives_deferred"
    assert metadata["analysis_page_count"] == 7
    assert metadata["projection_policy_version"] == subject.SET_PROJECTION_POLICY
    assert conn.events == ["begin", "commit"]


def test_one_direct_document_without_review_can_project_typed_terms(monkeypatch):
    conn, extraction = set_fixture(monkeypatch, multiple=False)
    result = finish(conn, extraction)
    assert result["projection_pending"] is False
    assert result["subcontratacion"]["status"] == "conditional"
    assert json.loads(conn.updates[0][1][2])["coverage"] == "loose_documents_only"


def attach_publication_history(conn):
    events = [{"uuid": row["codigo_alfresco"], "filename": row["name"], "stage": row["stage_name"],
               "type": row["document_type"], "published_at_text": row["published_at_text"],
               "published_at": subject.repository._official_time(row["published_at_text"])}
              for row in conn.inventory]
    events.append({**events[0], "stage": "Integración de las Bases", "type": "Bases Integradas",
                   "published_at_text": "06/10/2026 12:15", "published_at": "2026-10-06T12:15:00-05:00"})
    conn.detail["raw_detail"] = {"document_events": events}
    conn.detail["document_inventory_total"] = len(events)
    conn.publications = [{"codigo_alfresco": event["uuid"], "filename": event["filename"],
                          "event_hash": subject.event_hash(event), "seace_version": "3", "is_listed": True,
                          "stage_name": event["stage"], "document_type": event["type"],
                          "published_at_text": event["published_at_text"],
                          "published_at": datetime.fromisoformat(event["published_at"]) if event["published_at"] else None}
                         for event in events]
    return events


def test_publication_rows_sharing_one_resource_reconcile_without_changing_paid_hashes(monkeypatch):
    conn, extraction = set_fixture(monkeypatch, multiple=False)
    before = deepcopy(extraction)
    attach_publication_history(conn)
    assert len(conn.inventory) == 1 and conn.detail["document_inventory_total"] == 2
    assert finish(conn, extraction)["projection_pending"] is False
    assert extraction == before and len(conn.persist_calls) == 1
    assert conn.events == ["begin", "commit"]


def test_legacy_capture_without_event_array_keeps_original_unique_inventory_contract(monkeypatch):
    conn, extraction = set_fixture(monkeypatch)
    conn.detail["raw_detail"] = {"documents": [{"uuid": row["codigo_alfresco"]} for row in conn.inventory]}
    assert finish(conn, extraction)["projection_pending"] is True
    assert conn.publications == []  # Legacy receipts do not require the additive array.


@pytest.mark.parametrize("mutation", ["total", "missing_raw", "orphan", "duplicate", "filename", "representative",
    "missing_persisted", "persisted_hash", "persisted_version", "persisted_date", "persisted_type", "unlisted", "raw_date", "raw_fields"])
def test_incomplete_or_contradictory_publication_inventory_never_promotes_paid_reading(monkeypatch, mutation):
    conn, extraction = set_fixture(monkeypatch, multiple=False)
    events = attach_publication_history(conn)
    if mutation == "total":
        conn.detail["document_inventory_total"] = 1
    elif mutation == "missing_raw":
        events.pop()
        conn.detail["document_inventory_total"] = len(events)
    elif mutation == "orphan":
        events[-1]["uuid"] = OTHER_CODE
    elif mutation == "duplicate":
        events[-1] = deepcopy(events[0])
    elif mutation == "filename":
        events[-1]["filename"] = "otro.pdf"
    elif mutation == "representative":
        events.reverse()
    elif mutation == "missing_persisted":
        conn.publications.pop()
    elif mutation == "persisted_hash":
        conn.publications[-1]["event_hash"] = "f" * 64
    elif mutation == "persisted_version":
        conn.publications[-1]["seace_version"] = "4"
    elif mutation == "persisted_date":
        conn.publications[-1]["published_at"] += timedelta(minutes=1)
    elif mutation == "persisted_type":
        conn.publications[-1]["document_type"] = "Otro documento"
    elif mutation == "unlisted":
        conn.publications[-1]["is_listed"] = False
    elif mutation == "raw_date":
        events[-1]["published_at"] = None
    else:
        events[-1].pop("filename")
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []
    assert conn.events == ["begin", "rollback"]


def test_single_direct_document_with_review_never_projects_a_permission(monkeypatch):
    extraction = set_bundle(multiple=False)
    extraction["requires_human_review"] = True
    conn, extraction = set_fixture(monkeypatch, extraction)
    assert finish(conn, extraction)["subcontratacion"]["status"] == "not_identified"
    assert conn.stored["raw_extraction_json"]["participation"]["subcontratacion"]["status"] == "conditional"


@pytest.mark.parametrize("mutation", ["raw_context", "hash", "unsorted", "duplicate_uuid", "anchor", "excluded_pdf",
                                    "missing_scope", "scope", "unreviewed", "manifest_missing", "manifest_sha",
                                    "range_gap", "range_overlap", "range_bool", "citation", "duplicate_bad"])
def test_invalid_set_envelope_or_page_attribution_is_rejected_before_database(monkeypatch, mutation):
    extraction = set_bundle(archive=True)
    raw = extraction["raw_extraction_json"]
    if mutation == "raw_context":
        raw["document_set"][0]["filename"] = "altered.pdf"
    elif mutation == "hash":
        extraction["document_set_hash"] = raw["document_set_hash"] = "0" * 64
    elif mutation == "unsorted":
        extraction["document_set"].reverse()
        sync_context(extraction)
    elif mutation == "duplicate_uuid":
        extraction["document_set"][1]["document_uuid"] = CODE
        sync_context(extraction)
    elif mutation == "anchor":
        extraction["document_set"][0]["original_sha256"] = "f" * 64
        sync_context(extraction)
    elif mutation == "excluded_pdf":
        extraction["excluded_documents"][0]["filename"] = "tdr.pdf"
        sync_context(extraction)
    elif mutation == "missing_scope":
        extraction.pop("document_scope")
    elif mutation == "scope":
        extraction["document_scope"] = raw["document_scope"] = "expediente_completo"
    elif mutation == "unreviewed":
        extraction["requires_human_review"] = False
    elif mutation == "manifest_missing":
        raw["source_document_manifest"].pop()
    elif mutation == "manifest_sha":
        raw["source_document_manifest"][1]["original_sha256"] = "f" * 64
    elif mutation == "range_gap":
        raw["source_document_manifest"][1]["start_page"] = 7
    elif mutation == "range_overlap":
        raw["source_document_manifest"][1]["start_page"] = 5
    elif mutation == "range_bool":
        raw["source_document_manifest"][0]["start_page"] = True
    elif mutation == "citation":
        raw["participation"]["subcontratacion"]["page"] = 8
    else:
        raw["source_document_manifest"][1]["duplicate_of"] = CODE
    conn, extraction = fixture(monkeypatch, extraction)
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert conn.events == [] and conn.persist_calls == [] and conn.updates == []


def test_identical_derivative_uuids_share_pages_but_keep_both_original_revisions(monkeypatch):
    extraction = set_bundle()
    first, duplicate = extraction["raw_extraction_json"]["source_document_manifest"]
    duplicate.update(analysis_sha256=first["analysis_sha256"], start_page=1, end_page=5, duplicate_of=CODE)
    conn, extraction = set_fixture(monkeypatch, extraction)
    assert finish(conn, extraction)["projection_pending"] is True
    assert json.loads(conn.updates[0][1][2])["analysis_page_count"] == 5


@pytest.mark.parametrize("mutation", ["incomplete", "missing_doc", "extra_doc", "total", "metadata",
                                    "removed_archive", "source_url", "nonanchor_sha", "tied_revision", "receipt_context"])
def test_current_full_inventory_metadata_and_every_downloaded_revision_must_match_paid_set(monkeypatch, mutation):
    conn, extraction = set_fixture(monkeypatch, archive=True)
    if mutation == "incomplete":
        conn.detail["document_inventory_complete"] = False
    elif mutation in {"missing_doc", "removed_archive"}:
        conn.inventory.pop(1 if mutation == "missing_doc" else 2)
    elif mutation == "extra_doc":
        conn.inventory.append({**conn.inventory[0], "codigo_alfresco": OTHER_CODE})
    elif mutation == "total":
        conn.detail["document_inventory_total"] = 4
    elif mutation == "metadata":
        conn.inventory[1]["published_at_text"] = "07/10/2026 12:00"
    elif mutation == "source_url":
        conn.inventory[1]["source_url"] = SOURCE
    elif mutation == "nonanchor_sha":
        conn.document_revisions[SET_CODE][0]["sha256_original"] = "f" * 64
    elif mutation == "tied_revision":
        conn.document_revisions[SET_CODE].append({"sha256_original": "f" * 64, "downloaded_at": NOW})
    else:
        conn.reservation["result_json"]["document_scope"] = "old-single-document"
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []
    assert conn.events == ["begin", "rollback"]


@pytest.mark.parametrize("where", ["current", "canonical"])
def test_new_set_anchor_can_change_without_claiming_document_chronology_authority(monkeypatch, where):
    conn, extraction = set_fixture(monkeypatch)
    prior = current_document(published=NOW + timedelta(days=1), completed=NOW - timedelta(seconds=1))
    if where == "current":
        conn.current = prior
    else:
        conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 43}
        conn.prior = prior
    assert finish(conn, extraction)["projection_pending"] is True


@pytest.mark.parametrize("where", ["current", "canonical"])
def test_older_or_equal_paid_set_cannot_replace_current_or_canonical_reading(monkeypatch, where):
    conn, extraction = set_fixture(monkeypatch)
    prior = current_document(published=NOW - timedelta(days=1), completed=NOW)
    if where == "current":
        conn.current = prior
    else:
        conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 43}
        conn.prior = prior
    with pytest.raises(subject.Llm1CompletionError, match="older paid"):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []


def test_cached_current_document_set_repairs_projection_without_replacing_raw_or_paying(monkeypatch):
    conn, extraction = set_fixture(monkeypatch)
    conn.current = {**current_document(code=CODE), "id": 42, "evaluation_id": 7}
    conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 42}
    conn.prior = conn.current
    assert finish(conn, extraction)["projection_pending"] is True
    assert len(conn.updates) == 1 and len(conn.persist_calls) == 1


def test_storage_anchor_sha_cannot_reconcile_another_sources_participation(monkeypatch):
    conn, extraction = set_fixture(monkeypatch)
    conn.canonical["participation_terms_json"] = {"source": "seace_prod4", "document_sha256": SHA}
    with pytest.raises(subject.Llm1CompletionError, match="another source"):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []


def test_canonical_participation_from_another_convocation_cannot_be_replaced_by_newer_set(monkeypatch):
    conn, extraction = set_fixture(monkeypatch)
    conn.canonical["participation_terms_json"] = {"source": "seace_prod2", "extraction_id": 43}
    conn.prior = {**current_document(completed=NOW - timedelta(days=1)), "id_convocatoria": CONV + 1}
    with pytest.raises(subject.Llm1CompletionError, match="different convocation"):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []
