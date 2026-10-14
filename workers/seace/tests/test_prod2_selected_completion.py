"""Selected LLM1 contracts: synthetic receipts, no HTTP, models or database."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.local_document_conversion import WORD_FOOTNOTES_AT_END_POLICY
from buenapro_worker.prod2 import analysis, document_selection, llm1_completion as subject
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.pdf_preparation import SELECTED_DOCUMENTS_POLICY, MAX_ANALYSIS_BYTES, MAX_SOURCE_BYTES
from buenapro_worker.prod2.selected_extractor import SELECTED_TRANSPORT_POLICY
from test_prod2_llm1_completion import (bundle, fixture, finish, FakeCursor, CODE, OTHER_CODE,
                                      SHA, MODEL, NOW, CONV, attach_publication_history)
from test_prod2_document_selection import response

REPORT_CODE = "afdf87fb-9e04-4fa7-9c34-5912db454f7f"
DESCRIPTION = "SYNTHETIC SOFTWARE"


def selected_bundle(*, archive=True, nested=False, published_at="06/10/2026 12:00",
                    policy_version=document_selection.DOCUMENT_SELECTION_POLICY, archive_format="zip"):
    def parent(code, name, sha=None):
        doc = {"document_uuid": code, "filename": name, "type": "Bases" if code == CODE else "Documento", "stage": "Convocatoria",
               "published_at": published_at, "source_url": METADATA_URL + "?id=" + code}
        return {**doc, **({"original_sha256": sha} if sha else {})}
    parent_name = "bases." + archive_format
    parents = [parent(CODE, parent_name if archive else "bases.pdf", SHA),
               parent(OTHER_CODE, "anexo.pdf", "e" * 64), parent(REPORT_CODE, "reporte.pdf")]
    if archive:
        candidates = [{**{k: v for k, v in parents[0].items() if k != "original_sha256"},
                       "candidate_id": "member-" + str(i), "source_kind": "archive_member",
                       "filename": name, "parent_filename": parent_name, "member_path": "docs/" + name,
                       "archive_sha256": SHA, "inventory_status": "candidate"}
                      for i, name in enumerate(("principal.pdf", "tecnico.docx"))]
        if archive_format == "7z":
            from buenapro_worker.prod2.archive_inventory import POLICY_7Z
            for candidate in candidates:
                candidate["archive_inventory_policy"] = POLICY_7Z
        selected = [{**doc, "original_sha256": str(i + 1) * 64, "member_sha256": str(i + 1) * 64}
                    for i, doc in enumerate(candidates)]
    else:
        candidates = [{**parents[0], "candidate_id": "loose-first", "source_kind": "loose"}]
        selected = deepcopy(candidates)
    candidates.extend([{**{k: v for k, v in p.items() if k != "original_sha256"},
                        "candidate_id": code, "source_kind": "loose"}
                       for p, code in zip(parents[1:], ("loose-annex", "loose-report"))])
    selected.append({**candidates[-2], "original_sha256": "e" * 64})
    if nested:
        candidates.append({**{k: v for k, v in candidates[0].items() if k != "original_sha256"},
                           "candidate_id": "nested", "filename": "extra.zip", "member_path": "docs/extra.zip",
                           "inventory_status": "pending_nested_archive"})
    rows = [("bases", .95, .05)] + ([("technical_annex", .95, .05)] if archive else [])
    rows.extend([("technical_annex", .95, .05),
                 ("administrative_record" if policy_version in {document_selection.DOCUMENT_SELECTION_POLICY_V2,
                                                               document_selection.DOCUMENT_SELECTION_POLICY_V3} else "award_record", .01, .01)])
    if policy_version in {document_selection.DOCUMENT_SELECTION_POLICY_V2, document_selection.DOCUMENT_SELECTION_POLICY_V3}:
        for candidate in candidates:
            candidate["preview_text"] = "SYNTHETIC SAMPLE OF DOCUMENT CONTENT"
        for included in selected:
            included["preview_text"] = "SYNTHETIC SAMPLE OF DOCUMENT CONTENT"
    raw_response = response(*rows)
    if policy_version == document_selection.DOCUMENT_SELECTION_POLICY_V3:
        for index, (_, needed, ambiguity) in enumerate(rows):
            stem = f"d{index:04d}"
            raw_response["answers"].pop(stem + "_needed")
            raw_response["answers"].pop(stem + "_ambiguity")
            probabilities = {"read": needed, "omit": 1 - needed - ambiguity, "inspect": ambiguity}
            choice = max(probabilities, key=probabilities.get)
            raw_response["answers"][stem + "_action"] = {"type": "choice", "choice": choice,
                "confidence": probabilities[choice], "probabilities": probabilities}
    digest = document_selection.document_selection_request_hash(DESCRIPTION, candidates, policy_version=policy_version)
    cache_key = document_selection.CACHE_PREFIX + digest
    cached = {"cache_key": cache_key, "request_hash": digest, "model": document_selection.JEV_DOCUMENT_MODEL,
              "policy_version": policy_version, "raw_response": raw_response}
    no_http = MagicMock()
    selection = document_selection.JevDocumentSelector("synthetic-key", client=no_http, policy_version=policy_version).select(
        DESCRIPTION, candidates, cache={cache_key: cached})
    no_http.post.assert_not_called()
    selection_context = {"official_inventory": parents, "selector_request_hash": digest,
        "selector_receipt_hash": canonical_hash(raw_response), "candidates": candidates,
        "decisions": selection["decisions"], "thresholds": selection["thresholds"]}
    if archive and archive_format == "7z":
        selection_context["archive_inventory_policy"] = POLICY_7Z
    identity = {"document_scope": SELECTED_DOCUMENTS_POLICY, "document_set": selected,
        "selection_context": selection_context, "max_source_bytes": MAX_SOURCE_BYTES,
        "max_analysis_bytes": MAX_ANALYSIS_BYTES, "max_pages": 1000, "optimize_lossless": False}
    context = {key: deepcopy(identity[key]) for key in ("document_scope", "document_set", "selection_context")}
    context.update(document_set_identity=identity, document_set_hash=canonical_hash(identity),
                   selected_transport_policy=SELECTED_TRANSPORT_POLICY)
    extraction = bundle()
    extraction.update(context, requires_human_review=True)
    extraction["raw_extraction_json"].update(deepcopy(context))
    manifests, page = [], 0
    for i, doc in enumerate(selected):
        end = page + (5 if i == 0 else 2)
        manifests.append({**doc, "analysis_sha256": str(i + 5) * 64,
                          "start_page": page + 1, "end_page": end, "page_offset": page})
        page = end
    extraction["raw_extraction_json"]["source_document_manifest"] = manifests
    return extraction, selection


def selected_fixture(monkeypatch, **kwargs):
    extraction, selection = selected_bundle(**kwargs)
    conn, extraction = fixture(monkeypatch, extraction)
    conn.discovery["description"] = DESCRIPTION
    parents = extraction["selection_context"]["official_inventory"]
    conn.detail = {"document_inventory_complete": True, "document_inventory_total": len(parents)}
    conn.inventory = [{"codigo_alfresco": p["document_uuid"], "is_listed": True, "name": p["filename"],
        "document_type": p["type"], "stage_name": p["stage"], "published_at": NOW,
        "published_at_text": p["published_at"], "source_url": p["source_url"]} for p in parents]
    conn.document_revisions = {p["document_uuid"]: [{"sha256_original": p["original_sha256"], "downloaded_at": NOW}]
                              for p in parents if "original_sha256" in p}
    conn.selector = {"request_hash": selection["request_hash"], "model": selection["model"],
        "policy_version": selection["policy_version"], "completed_at": NOW, "status": "done", "result_json": selection}
    class SelectedCursor(FakeCursor):
        def execute(self, statement, params=()):
            if "FROM prod2_document_selection_cache" in statement:
                assert conn.depth == 1
                self.response = conn.selector
            else:
                super().execute(statement, params)
    @contextmanager
    def cursor(**kwargs):
        yield SelectedCursor(conn)
    conn.cursor = cursor
    return conn, extraction


def sync(extraction, conn=None):
    identity = extraction["document_set_identity"]
    for key in ("document_scope", "document_set", "selection_context"):
        identity[key] = deepcopy(extraction[key])
    extraction["document_set_hash"] = canonical_hash(identity)
    extraction["raw_extraction_json"].update(deepcopy({key: extraction[key] for key in subject._SELECTED_FIELDS}))
    if conn:
        conn.reservation["result_json"] = deepcopy(extraction)
        conn.stored["raw_extraction_json"] = deepcopy(extraction["raw_extraction_json"])


def word_fixture(monkeypatch, *, original_placement="page"):
    conn, extraction = selected_fixture(monkeypatch)
    extraction["document_set_identity"]["word_conversion_policy"] = WORD_FOOTNOTES_AT_END_POLICY
    record = extraction["raw_extraction_json"]["source_document_manifest"][1]
    record["word_conversion"] = {
        "policy": WORD_FOOTNOTES_AT_END_POLICY, "original_placement": original_placement,
        "derived_placement": "document", "only_placement_attribute_changed": True,
        "footnote_count": 77, "native_fodt_sha256": "a" * 64,
        "derived_fodt_sha256": ("b" if original_placement == "page" else "a") * 64,
        "conversion_source_sha256": "c" * 64,
        "pdf_sha256": record["analysis_sha256"], "pdf_bytes": 1024,
    }
    sync(extraction, conn)
    return conn, extraction


@pytest.mark.parametrize("original_placement", ["page", "document"])
def test_completed_word_bundle_persists_exact_opt_in_provenance_without_changing_paid_reading(monkeypatch, original_placement):
    conn, extraction = word_fixture(monkeypatch, original_placement=original_placement)
    before, paid_before = deepcopy(extraction), deepcopy(conn.reservation)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and saved["requires_human_review"]
    assert len(conn.persist_calls) == len(conn.updates) == 1
    assert conn.events == ["begin", "commit"]
    assert extraction == before and conn.reservation == paid_before
    meta = json.loads(conn.updates[0][1][2])
    assert meta["document_set_identity"] == extraction["document_set_identity"]
    assert meta["source_document_manifest"] == extraction["raw_extraction_json"]["source_document_manifest"]
    word = meta["source_document_manifest"][1]
    # Template sanitization may produce a different local conversion source;
    # neither it nor the derived PDF replaces the immutable member/root SHA.
    assert word["word_conversion"]["conversion_source_sha256"] != word["original_sha256"]
    assert word["original_sha256"] == word["member_sha256"] == "2" * 64
    assert conn.persist_calls[0][2] == SHA


@pytest.mark.parametrize("policy", [None, "unknown_word_policy", True, {"policy": WORD_FOOTNOTES_AT_END_POLICY}])
def test_explicit_word_policy_must_be_the_exact_supported_value(monkeypatch, policy):
    conn, extraction = word_fixture(monkeypatch)
    extraction["document_set_identity"]["word_conversion_policy"] = policy
    sync(extraction, conn)
    with pytest.raises(subject.Llm1CompletionError, match="identity"):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates and conn.events == []


@pytest.mark.parametrize("mutation", ["legacy_receipt", "pdf_receipt", "missing_receipt", "missing_field", "extra_field",
    "extra_identity", "policy", "pdf_sha", "native_sha", "derived_sha", "source_sha", "changed_flag",
    "original_placement", "derived_placement", "boolean_notes", "negative_notes", "boolean_bytes",
    "empty_bytes", "oversized_bytes", "same_hash_after_change", "different_hash_without_change"])
def test_word_derivative_provenance_rejected_before_any_persistence(monkeypatch, mutation):
    conn, extraction = word_fixture(monkeypatch)
    manifest = extraction["raw_extraction_json"]["source_document_manifest"]
    receipt = manifest[1]["word_conversion"]
    if mutation == "legacy_receipt": extraction["document_set_identity"].pop("word_conversion_policy")
    elif mutation == "pdf_receipt": manifest[0]["word_conversion"] = deepcopy(receipt)
    elif mutation == "missing_receipt": manifest[1].pop("word_conversion")
    elif mutation == "missing_field": receipt.pop("conversion_source_sha256")
    elif mutation == "extra_field": receipt["unsafe_extra"] = True
    elif mutation == "extra_identity": extraction["document_set_identity"]["unsafe_extra"] = True
    elif mutation == "policy": receipt["policy"] = "unknown_word_policy"
    elif mutation == "pdf_sha": receipt["pdf_sha256"] = "f" * 64
    elif mutation == "native_sha": receipt["native_fodt_sha256"] = "invalid"
    elif mutation == "derived_sha": receipt["derived_fodt_sha256"] = "B" * 64
    elif mutation == "source_sha": receipt["conversion_source_sha256"] = None
    elif mutation == "changed_flag": receipt["only_placement_attribute_changed"] = 1
    elif mutation == "original_placement": receipt["original_placement"] = []
    elif mutation == "derived_placement": receipt["derived_placement"] = "page"
    elif mutation == "boolean_notes": receipt["footnote_count"] = True
    elif mutation == "negative_notes": receipt["footnote_count"] = -1
    elif mutation == "boolean_bytes": receipt["pdf_bytes"] = True
    elif mutation == "empty_bytes": receipt["pdf_bytes"] = 0
    elif mutation == "oversized_bytes": receipt["pdf_bytes"] = MAX_SOURCE_BYTES + 1
    elif mutation == "same_hash_after_change": receipt["derived_fodt_sha256"] = receipt["native_fodt_sha256"]
    elif mutation == "different_hash_without_change": receipt["original_placement"] = "document"
    sync(extraction, conn)
    before = deepcopy(conn.reservation)
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates and conn.events == []
    assert conn.reservation == before


def test_opt_in_identity_without_word_documents_does_not_allow_provenance_on_pdf(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch, archive=False)
    extraction["document_set_identity"]["word_conversion_policy"] = WORD_FOOTNOTES_AT_END_POLICY
    sync(extraction, conn)
    assert finish(conn, extraction)["projection_pending"]
    assert "word_conversion" not in json.loads(conn.updates[0][1][2])["source_document_manifest"][0]


def test_selected_members_sharing_parent_keep_original_receipt_and_project_pending(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch, nested=True)
    before = deepcopy(extraction)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and saved["requires_human_review"]
    assert saved["consorcio"]["status"] == saved["subcontratacion"]["status"] == "not_identified"
    assert extraction == before
    assert len(conn.persist_calls) == len(conn.updates) == 1
    assert conn.persist_calls[0][2] == SHA  # Container, not first member's SHA.
    meta = json.loads(conn.updates[0][1][2])
    assert meta["document_scope"] == SELECTED_DOCUMENTS_POLICY
    assert meta["coverage"] == "selected_documents_only" and meta["storage_anchor_only"]
    assert meta["selected_transport_policy"] == SELECTED_TRANSPORT_POLICY
    assert meta["selection_context"] == extraction["selection_context"]
    assert meta["source_document_manifest"] == extraction["raw_extraction_json"]["source_document_manifest"]
    assert meta["projection_policy_version"] == subject.SELECTED_PROJECTION_POLICY
    assert any(d["candidate_id"] == "nested" and d["decision"] == "pending" for d in meta["selection_context"]["decisions"])


def test_selected_receipt_reconciles_more_publications_than_unique_parent_resources(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    before = deepcopy(extraction)
    attach_publication_history(conn)
    assert conn.detail["document_inventory_total"] == len(conn.inventory) + 1
    assert finish(conn, extraction)["projection_pending"] is True
    assert extraction == before and len(conn.persist_calls) == 1
    assert conn.events == ["begin", "commit"]


def test_loose_only_selected_scope_also_requires_independent_review(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch, archive=False)
    assert finish(conn, extraction)["projection_pending"]


@pytest.mark.parametrize("policy", [document_selection.DOCUMENT_SELECTION_POLICY_V2, document_selection.DOCUMENT_SELECTION_POLICY_V3])
def test_versioned_paid_selection_reconstructs_exactly_at_completion_without_provider(monkeypatch, policy):
    conn, extraction = selected_fixture(monkeypatch, policy_version=policy)
    before = deepcopy(extraction)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and saved["requires_human_review"]
    assert conn.selector["policy_version"] == policy
    assert extraction == before and conn.events == ["begin", "commit"]


def test_unknown_selection_policy_rejected_before_completion(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    conn.selector["policy_version"] = "unknown_selector_policy"
    with pytest.raises(subject.Llm1CompletionError, match="durable selector receipt"):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


def test_stored_review_flag_cannot_disagree_with_paid_selected_receipt(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    conn.stored["requires_human_review"] = False
    with pytest.raises(subject.Llm1CompletionError, match="altered extraction"):
        finish(conn, extraction)
    assert not conn.updates and conn.events[-1] == "rollback"


@pytest.mark.parametrize("field", subject._SELECTED_FIELDS)
def test_every_context_field_is_bound_to_raw_and_paid_receipt(monkeypatch, field):
    conn, extraction = selected_fixture(monkeypatch)
    extraction.pop(field)
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


@pytest.mark.parametrize("mutation", ["hash", "member_root_sha", "root_missing_sha", "duplicate_id", "missing_decision",
    "wrong_selected_ids", "parent_metadata", "unselected_metadata", "member_path", "unselected_path_type", "nonfinite", "overlap", "offset", "manifest_sha",
    "citation", "transport", "false_review"])
def test_malformed_selected_provenance_never_projects(monkeypatch, mutation):
    conn, extraction = selected_fixture(monkeypatch)
    context, manifest = extraction["selection_context"], extraction["raw_extraction_json"]["source_document_manifest"]
    if mutation == "hash":
        extraction["document_set_hash"] = "f" * 64
    elif mutation == "member_root_sha":
        extraction["document_set"][0]["archive_sha256"] = "f" * 64
    elif mutation == "root_missing_sha":
        context["official_inventory"][0].pop("original_sha256")
    elif mutation == "duplicate_id":
        context["candidates"][1]["candidate_id"] = context["candidates"][0]["candidate_id"]
    elif mutation == "missing_decision":
        context["decisions"].pop()
    elif mutation == "wrong_selected_ids":
        context["decisions"][0]["decision"] = "pending"
    elif mutation == "parent_metadata":
        context["official_inventory"][0]["type"] = "Different official type"
    elif mutation == "unselected_metadata":
        context["candidates"][-1]["stage"] = "Different stage"
    elif mutation == "member_path":
        context["candidates"][0]["member_path"] = "../principal.pdf"
    elif mutation == "unselected_path_type":
        context["candidates"][-1].update(source_kind="archive_member", document_uuid=CODE,
            parent_filename="bases.zip", archive_sha256=SHA, member_path=1)
    elif mutation == "nonfinite":
        context["candidates"][-1]["extra"] = float("nan")
    elif mutation == "overlap":
        manifest[1].update(start_page=5, page_offset=4)
    elif mutation == "offset":
        manifest[0]["page_offset"] = 1
    elif mutation == "manifest_sha":
        manifest[0]["member_sha256"] = "f" * 64
    elif mutation == "citation":
        extraction["raw_extraction_json"]["participation"]["consorcio"]["page"] = 100
    elif mutation == "transport":
        extraction["selected_transport_policy"] = "unverified_transport"
    else:
        extraction["requires_human_review"] = False
    if mutation != "hash":
        sync(extraction, conn)
    with pytest.raises(subject.repository.Prod2PersistenceError):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


@pytest.mark.parametrize("mutation", ["missing", "uncertain", "model", "policy", "raw_hash", "question", "description", "decision"])
def test_selected_snapshot_requires_exact_completed_provider_cache_and_current_replan(monkeypatch, mutation):
    conn, extraction = selected_fixture(monkeypatch)
    if mutation == "missing":
        conn.selector = None
    elif mutation == "uncertain":
        conn.selector["status"] = "uncertain"
    elif mutation == "model":
        conn.selector["model"] = "jev-wrong"
    elif mutation == "policy":
        conn.selector["policy_version"] = "unknown_policy"
    elif mutation == "raw_hash":
        extraction["selection_context"]["selector_receipt_hash"] = "f" * 64
        sync(extraction, conn)
    elif mutation == "question":
        conn.selector["result_json"]["raw_response"]["answers"].pop("d0000_role")
        extraction["selection_context"]["selector_receipt_hash"] = canonical_hash(conn.selector["result_json"]["raw_response"])
        sync(extraction, conn)
    elif mutation == "description":
        conn.discovery["description"] = "DIFFERENT SOFTWARE"
    else:
        extraction["selection_context"]["decisions"][0]["reason"] = "invented reason"
        sync(extraction, conn)
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


def test_full_inventory_and_latest_nonanchor_root_revision_are_required(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    conn.document_revisions[OTHER_CODE].insert(0, {"sha256_original": "f" * 64, "downloaded_at": NOW + timedelta(seconds=1)})
    with pytest.raises(subject.Llm1CompletionError, match="downloaded revision"):
        finish(conn, extraction)
    assert not conn.persist_calls
    conn, extraction = selected_fixture(monkeypatch)
    conn.inventory.pop()
    with pytest.raises(subject.Llm1CompletionError, match="full current official inventory"):
        finish(conn, extraction)
    assert not conn.persist_calls


def test_original_selector_receipt_ids_can_be_different_from_current_snapshot(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    # Paid global cache originally belonged to another invocation. Ordinal raw
    # answers, not its stale business-ID decisions, define the present plan.
    conn.selector["result_json"]["decisions"] = [{"candidate_id": "old-id", "decision": "excluded"}]
    conn.selector["result_json"]["selected_ids"] = ["old-id"]
    assert finish(conn, extraction)["projection_pending"]


def test_paid_time_prevents_old_selected_scope_from_replacing_different_anchor(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    conn.current = {"evaluation_id": 99, "completed_at": NOW + timedelta(seconds=1), "codigo_alfresco": OTHER_CODE}
    with pytest.raises(subject.Llm1CompletionError, match="older paid extraction"):
        finish(conn, extraction)
    assert not conn.persist_calls


def test_selected_duplicate_uses_candidate_id_and_preserves_original_member_sha(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    records = extraction["raw_extraction_json"]["source_document_manifest"]
    records[1].update(analysis_sha256=records[0]["analysis_sha256"], start_page=1, end_page=5, page_offset=0,
                      duplicate_of=records[0]["candidate_id"])
    records[2].update(start_page=6, end_page=7, page_offset=5)
    sync(extraction, conn)
    assert finish(conn, extraction)["projection_pending"]
    assert records[0]["original_sha256"] != records[1]["original_sha256"]
    records[1]["duplicate_of"] = CODE  # UUID aliases multiple members; never adequate.
    sync(extraction, conn)
    with pytest.raises(subject.Llm1CompletionError, match="earlier identical candidate"):
        finish(conn, extraction)


def test_selected_reader_binds_transport_and_context_in_hash_and_always_reviews(monkeypatch):
    extraction, _ = selected_bundle()
    context = {key: extraction[key] for key in subject._SELECTED_FIELDS if key != "selected_transport_policy"}
    settings = SimpleNamespace(gemini_model=MODEL, gemini_fallback_model="gemini-2.5-flash")
    raw = {"requirements": {"provider": [{"tipo": "specific_experience", "descripcion": "SYNTHETIC SUPPORT", "obligatorio": True}]}}
    reader = MagicMock()
    reader.prompts = {"bases_service": "STATIC TEST PROMPT"}
    reader.extract.return_value = SimpleNamespace(raw_json=raw, model=MODEL, prompt_version="bases_extraction_v2",
        schema_version="bases_extraction_schema_v2", requires_human_review=False, input_tokens=0, output_tokens=0, cost_usd=0)
    args = {"object_type": "service", "original_sha256": SHA,
            "manifest": extraction["raw_extraction_json"]["source_document_manifest"], "document_context": context}
    digest = analysis.extraction_request_hash(settings, b"%PDF-synthetic", **args)
    result = analysis.extract_document(settings, b"%PDF-synthetic", extractor=reader, **args)
    assert result["cache_key"] == "prod2:llm1:" + digest
    assert result["requires_human_review"] and result["selected_transport_policy"] == SELECTED_TRANSPORT_POLICY
    assert result["raw_extraction_json"]["selected_transport_policy"] == SELECTED_TRANSPORT_POLICY
    assert raw == reader.extract.return_value.raw_json
    instruction = analysis._document_set_instruction(args["manifest"], context)
    assert "SOLO los documentos seleccionados" in instruction and "pending/excluded NO" in instruction
    assert "paginas GLOBALES" in instruction
    monkeypatch.setattr(analysis, "SELECTED_TRANSPORT_POLICY", "different_transport_policy")
    assert analysis.extraction_request_hash(settings, b"%PDF-synthetic", **args) != digest


def test_future_ficha_reading_changes_llm1_hash_but_not_selector_and_preserves_legacy_prompt():
    extraction, selection = selected_bundle(policy_version=document_selection.DOCUMENT_SELECTION_POLICY_V3)
    context = {key: deepcopy(extraction[key]) for key in subject._SELECTED_FIELDS if key != "selected_transport_policy"}
    pages = extraction["raw_extraction_json"]["source_document_manifest"]
    settings = SimpleNamespace(gemini_model=MODEL, gemini_fallback_model="gemini-2.5-flash")
    args = {"object_type": "service", "original_sha256": SHA, "manifest": pages, "document_context": context}
    legacy_prompt = analysis._document_set_instruction(pages, context)
    legacy_hash = analysis.extraction_request_hash(settings, b"%PDF-synthetic", **args)
    selector_hash = document_selection.document_selection_request_hash(DESCRIPTION,
        context["selection_context"]["candidates"], policy_version=selection["policy_version"])
    assert "ficha_description" not in context["selection_context"]
    context["selection_context"]["ficha_description"] = DESCRIPTION
    context["document_set_identity"]["selection_context"] = deepcopy(context["selection_context"])
    context["document_set_hash"] = canonical_hash(context["document_set_identity"])
    future_prompt = analysis._document_set_instruction(pages, context)
    future_hash = analysis.extraction_request_hash(settings, b"%PDF-synthetic", **args)
    assert future_hash != legacy_hash
    assert "SUBCONTRATACIÓN" in future_prompt and "NO APLICA aislado" in future_prompt
    assert "consultoria individual" in future_prompt and "clausula explicita aplicable" in future_prompt
    assert "not_identified, clause=null, page=null, conditions=[]" in future_prompt
    assert "summary.observaciones_clave" in future_prompt and "Compara ese objeto" in future_prompt
    assert document_selection.document_selection_request_hash(DESCRIPTION,
        context["selection_context"]["candidates"], policy_version=selection["policy_version"]) == selector_hash
    del context["selection_context"]["ficha_description"]
    context["document_set_identity"]["selection_context"] = deepcopy(context["selection_context"])
    context["document_set_hash"] = canonical_hash(context["document_set_identity"])
    assert analysis._document_set_instruction(pages, context) == legacy_prompt
    assert analysis.extraction_request_hash(settings, b"%PDF-synthetic", **args) == legacy_hash


def test_opt_in_ficha_description_is_sanitized_public_data_not_credentials():
    description = ("  Servicio SOFTWARE\nhttps://example.test?token=secret user@example.test "
        "apikey_private token=private 12345678 bcbc122b-c585-48fc-b345-07221a994975  ")
    sanitized = analysis.ficha_reading_description(description)
    assert sanitized.startswith("Servicio SOFTWARE ") and "\n" not in sanitized
    for private in ("example.test", "apikey_private", "token=private", "12345678", "bcbc122b"):
        assert private not in sanitized
    assert analysis.ficha_reading_description(sanitized) == sanitized


@pytest.mark.parametrize("value", [None, "", "DOUBLE  SPACE", "URL https://example.test", "x" * 20001])
def test_invalid_or_unsanitized_ficha_description_rejected_before_model(monkeypatch, value):
    conn, extraction = selected_fixture(monkeypatch)
    extraction["selection_context"]["ficha_description"] = value
    sync(extraction, conn)
    with pytest.raises(subject.Llm1CompletionError, match="ficha description"):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates and conn.events == []
    context = {key: extraction[key] for key in subject._SELECTED_FIELDS if key != "selected_transport_policy"}
    settings = SimpleNamespace(gemini_model=MODEL, gemini_fallback_model="gemini-2.5-flash")
    reader = MagicMock()
    with pytest.raises(ValueError):
        analysis.extract_document(settings, b"%PDF-synthetic", object_type="service", original_sha256=SHA,
            manifest=extraction["raw_extraction_json"]["source_document_manifest"], document_context=context,
            extractor=reader)
    reader.extract.assert_not_called()


@pytest.mark.parametrize("official_detail", [None, DESCRIPTION])
def test_future_ficha_description_matches_discovery_and_optional_current_detail(monkeypatch, official_detail):
    conn, extraction = selected_fixture(monkeypatch, policy_version=document_selection.DOCUMENT_SELECTION_POLICY_V3)
    extraction["selection_context"]["ficha_description"] = DESCRIPTION
    sync(extraction, conn)
    if official_detail is not None:
        conn.detail["raw_detail"] = {"description": official_detail}
    before = deepcopy(conn.selector)
    saved = finish(conn, extraction)
    assert saved["projection_pending"] and conn.events == ["begin", "commit"]
    assert conn.selector == before


@pytest.mark.parametrize("where", ["snapshot", "current_detail", "current_discovery"])
def test_ficha_scope_mismatch_never_projects_or_rewrites_paid_receipts(monkeypatch, where):
    conn, extraction = selected_fixture(monkeypatch, policy_version=document_selection.DOCUMENT_SELECTION_POLICY_V3)
    extraction["selection_context"]["ficha_description"] = "OTHER OBJECT" if where == "snapshot" else DESCRIPTION
    sync(extraction, conn)
    if where == "current_detail": conn.detail["raw_detail"] = {"description": "OTHER OBJECT"}
    if where == "current_discovery": conn.discovery["description"] = "OTHER OBJECT"
    before = deepcopy(conn.selector)
    with pytest.raises(subject.Llm1CompletionError, match="current official discovery/detail"):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates and conn.events == ["begin", "rollback"]
    assert conn.selector == before


def test_low_needed_forms_are_excluded_but_nested_archives_remain_pending_without_http():
    docs = [{"candidate_id": "form", "filename": "Formato.pdf"},
            {"candidate_id": "nested", "filename": "anexo.zip", "inventory_status": "pending_nested_archive"}]
    digest = document_selection.document_selection_request_hash(DESCRIPTION, docs)
    key = document_selection.CACHE_PREFIX + digest
    cached = {"cache_key": key, "request_hash": digest, "model": document_selection.JEV_DOCUMENT_MODEL,
              "policy_version": document_selection.DOCUMENT_SELECTION_POLICY, "raw_response": response(("form", .01, .01))}
    client = MagicMock()
    result = document_selection.JevDocumentSelector("synthetic-key", client=client).select(DESCRIPTION, docs, cache={key: cached})
    client.post.assert_not_called()
    assert result["excluded_ids"] == ["form"] and result["pending_ids"] == ["nested"]
    assert result["decisions"][1]["reason"] == "nested_archive_not_inspected"
    only_nested = document_selection.JevDocumentSelector("synthetic-key", client=client).select(DESCRIPTION, docs[1:])
    client.post.assert_not_called()
    assert only_nested["pending_ids"] == ["nested"] and only_nested["input_tokens"] == 0


def test_selected_sevenzip_parent_requires_new_policy_and_preserves_official_member_evidence(monkeypatch):
    from buenapro_worker.prod2.archive_inventory import POLICY_7Z
    conn, extraction = selected_fixture(monkeypatch, archive_format='7z',
                                       policy_version=document_selection.DOCUMENT_SELECTION_POLICY_V3)
    before = deepcopy(extraction)
    result = finish(conn, extraction)
    assert result['projection_pending'] and extraction == before
    stored = json.loads(conn.updates[0][1][2])
    assert stored['selection_context']['archive_inventory_policy'] == POLICY_7Z
    assert stored['selection_context']['official_inventory'][0]['filename'] == 'bases.7z'
    members = [d for d in stored['source_document_manifest'] if d['source_kind'] == 'archive_member']
    assert len(members) == 2 and all(d['parent_filename'] == 'bases.7z' for d in members)
    assert all(d['member_sha256'] == d['original_sha256'] for d in members)


@pytest.mark.parametrize('policy', [None, 'prod2_archive_inventory_v1', 'unknown'])
def test_sevenzip_completion_without_exact_policy_cannot_publish(monkeypatch, policy):
    conn, extraction = selected_fixture(monkeypatch, archive_format='7z',
                                       policy_version=document_selection.DOCUMENT_SELECTION_POLICY_V3)
    if policy is None:
        extraction['selection_context'].pop('archive_inventory_policy')
    else:
        extraction['selection_context']['archive_inventory_policy'] = policy
    sync(extraction, conn)
    with pytest.raises(subject.Llm1CompletionError):
        finish(conn, extraction)
    assert conn.persist_calls == [] and conn.updates == []
