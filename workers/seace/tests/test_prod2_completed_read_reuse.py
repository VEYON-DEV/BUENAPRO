"""Durable reuse after disposable Word/PDF removal; no services or real DB."""
import copy
import hashlib
import io
import json
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter

from buenapro_worker.prod2 import analysis, pdf_preparation as preparation
from buenapro_worker.prod2.completed_read_reuse import reuse_completed_read, CompletedReadReuseError
from buenapro_worker.prod2.extraction_policy import (
    schema_for_scope, schema_version_for_scope, QUALIFICATION_FOCUS_POLICY,
)
from buenapro_worker.prod2.input_policy import NATIVE_TEXT_OR_PDF_POLICY
from buenapro_worker.prod2.model_routing import default_model_routing
from buenapro_worker.normalization.facets import canonical_hash

CONV = 1047375
PROFILE = "00000000-0000-4000-8000-000000000099"
CODE = "00000000-0000-4000-8000-000000000001"


class Connection:
    def __init__(self, row, parent, unresolved=None):
        self.row, self.parent, self.queries = row, parent, []
        self.unresolved = unresolved

    def execute(self, sql, params):
        assert sql.lstrip().startswith("SELECT"), "reuse must remain read-only"
        self.queries.append((sql, params))
        if "status IN ('pending','running','uncertain')" in sql:
            assert params == (CONV, PROFILE)
            return SimpleNamespace(fetchone=lambda: copy.deepcopy(self.unresolved))
        if "FROM prod2_evaluations" in sql:
            conv, profile, digest, identity, selected, context = params
            b = self.row["result_json"] if self.row else {}
            matches = (self.row and self.row["id_convocatoria"] == conv
                       and str(self.row["profile_id"]) == profile
                       and (b.get("document_set_hash") == digest
                            or b.get("document_set_identity") == json.loads(identity)
                            or (b.get("document_set") == json.loads(selected)
                                and b.get("selection_context") == json.loads(context))))
            return SimpleNamespace(fetchall=lambda: [copy.deepcopy(self.row)] if matches else [])
        assert "FROM prod2_documents" in sql
        return SimpleNamespace(fetchone=lambda: copy.deepcopy(self.parent))


def settings():
    return SimpleNamespace(gemini_model="gemini-3.1-flash-lite", gemini_fallback_model="gemini-2.5-flash")


@pytest.fixture
def previous(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    source = old / (CODE + ".docx")
    source.write_bytes(b"immutable original Word bytes; conversion timestamp must not affect reuse")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    parent = {"document_uuid": CODE, "filename": "bases.docx", "type": "Bases Administrativas",
              "stage": "Convocatoria", "published_at": "06/10/2026 12:00",
              "source_url": "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + CODE,
              "original_sha256": sha}
    selected = [{**parent, "candidate_id": "loose:" + CODE, "source_kind": "loose"}]
    context = {"official_inventory": [parent], "candidates": copy.deepcopy(selected),
               "decisions": [{"candidate_id": selected[0]["candidate_id"], "decision": "selected"}],
               "thresholds": {"high": 70, "low": 40}, "selector_request_hash": "a" * 64,
               "selector_receipt_hash": "b" * 64, "ficha_description": "Licencias de software",
               "llm1_scope_policy": QUALIFICATION_FOCUS_POLICY,
               "llm1_input_policy": NATIVE_TEXT_OR_PDF_POLICY}
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    out = io.BytesIO()
    writer.write(out)

    def convert(path, extension, **kwargs):
        result = path.with_suffix(".pdf")
        result.write_bytes(out.getvalue())
        return result

    converter = MagicMock(side_effect=convert)
    monkeypatch.setattr(preparation, "prepare_pdf", converter)
    prepared = preparation.prepare_selected_document_set(old, selected,
        {selected[0]["candidate_id"]: source}, context, cache_folder=old / "preparation-cache")
    pdf = prepared["pdf_path"].read_bytes()
    dc = {key: prepared[key] for key in ("document_scope", "document_set", "document_set_hash",
          "document_set_identity", "selection_context")}
    dc["llm1_input"] = {"policy_version": NATIVE_TEXT_OR_PDF_POLICY, "route": "pdf",
        "analysis_sha256": prepared["analysis_sha256"], "page_count": prepared["page_count"],
        "text_sha256": None, "text_bytes": 0, "local_cache_key": "c" * 64,
        "diagnostic_sha256": "d" * 64, "source_manifest_hash": canonical_hash(prepared["source_document_manifest"])}
    inputs = analysis._document_input(settings(), pdf, "service", sha,
                                     prepared["source_document_manifest"], dc)
    request = analysis.extraction_request_hash(settings(), pdf, object_type="service",
        original_sha256=sha, manifest=prepared["source_document_manifest"], document_context=dc)
    raw = schema_for_scope("bases_service", context).model_validate({
        "requirements": {"provider": [{"tipo": "license", "descripcion": "Autorización del fabricante",
                                       "obligatorio": True, "evidencia": "Cláusula de autorización, página1"}]}
    }).model_dump()
    # Exercise the production writer contract rather than inventing bundle keys.
    # extract_document preserves the source manifest ONLY inside raw JSON.
    reader = SimpleNamespace(supports_selected_input_policies=True,
        prepared={**prepared, "llm1_input": dc["llm1_input"]},
        prompts={"bases_service": analysis.read_prompt(analysis.BASES_PROMPT_FILENAME)},
        extract=lambda *args, **kwargs: SimpleNamespace(raw_json=copy.deepcopy(raw),
            model=inputs["model"], prompt_version=inputs["prompt_version"],
            schema_version=schema_version_for_scope("bases_service", context),
            requires_human_review=True, input_tokens=100, output_tokens=30, cost_usd=0.001))
    bundle = analysis.extract_document(settings(), pdf, object_type="service", original_sha256=sha,
        manifest=prepared["source_document_manifest"], document_context=dc, extractor=reader)
    bundle.update(document_uuid=CODE, document_sha256=sha)
    assert bundle["cache_key"] == "prod2:llm1:" + request
    assert "source_document_manifest" not in bundle
    raw = bundle["raw_extraction_json"]
    row = {"evaluation_id": 7, "id_convocatoria": CONV, "kind": "gemini_extraction", "status": "done",
        "reserved_model": inputs["model"], "policy_version": "prod2_document_extraction_v1", "profile_id": PROFILE,
        "request_hash": request, "result_json": bundle, "extraction_id": 9, "is_current": True,
        "quality": "complete", "extraction_model": inputs["model"], "raw_extraction_json": copy.deepcopy(raw),
        "sha256_original": sha, "document_uuid": CODE, "is_listed": True}
    db_parent = {"is_listed": True, "source_url": parent["source_url"], "sha256_original": sha,
                 "bytes": source.stat().st_size}
    new = tmp_path / "new"
    new.mkdir()
    current = new / source.name
    current.write_bytes(source.read_bytes())
    shutil.rmtree(old)  # There is no old PDF to read, restore or hash.
    converter.reset_mock()
    return SimpleNamespace(folder=new, selected=selected, files={selected[0]["candidate_id"]: current},
        context=context, manifest={"max_source_mib": 128, "llm1_model_routing": default_model_routing()},
        row=row, parent=db_parent, converter=converter, prepared=prepared, pdf=pdf, dc=dc, sha=sha)


def reuse(f, conn=None, config=None, **kwargs):
    return reuse_completed_read(conn or Connection(f.row, f.parent), config or settings(),
        f.folder, CONV, PROFILE, kwargs.pop("object_type", "service"), f.selected, f.files,
        f.context, f.manifest, **kwargs)


def test_word_reuses_exact_paid_identity_after_all_old_derivatives_removed(previous):
    f = previous
    before = copy.deepcopy(f.row)
    result = reuse(f)
    assert result["evaluation_id"] == 7
    assert result["request_hash"] == f.row["request_hash"]
    assert result["bundle"] == f.row["result_json"] and result["bundle"] is not f.row["result_json"]
    assert result["bundle"]["requires_human_review"] is True
    assert result["page_count"] == 2 and result["routing"]["model"] == "gemini-3.1-flash-lite"
    assert result["analysis_bytes"] is None and result["transport_parts"] is None
    assert result["source_document_manifest"] == f.prepared["source_document_manifest"]
    assert "source_document_manifest" not in result["bundle"]
    assert f.row == before
    assert list(f.folder.iterdir()) == [next(iter(f.files.values()))]
    f.converter.assert_not_called()


@pytest.mark.parametrize("kind", ["scope", "input", "candidate", "conversion", "context"])
def test_different_preparation_identity_is_a_no_hit_without_conversion(previous, kind):
    f = previous
    if kind == "scope": f.context.pop("llm1_scope_policy")
    if kind == "input": f.context.pop("llm1_input_policy")
    if kind == "candidate": f.selected[0]["published_at"] = "07/10/2026 12:00"
    if kind == "conversion": f.manifest["word_conversion_policy"] = "word_footnotes_at_end_v1"
    if kind == "context": f.context["ficha_description"] = "Otra convocatoria"
    assert reuse(f) is None
    f.converter.assert_not_called()


def test_changed_actual_word_sha_is_no_hit_but_claimed_hash_mismatch_fails(previous):
    f = previous
    source = next(iter(f.files.values()))
    source.write_bytes(b"new original version")
    with pytest.raises(CompletedReadReuseError): reuse(f)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    f.selected[0]["original_sha256"] = sha
    f.context["official_inventory"][0]["original_sha256"] = sha
    f.context["candidates"][0]["original_sha256"] = sha
    assert reuse(f) is None
    f.converter.assert_not_called()


@pytest.mark.parametrize("field,value", [("is_current", False), ("is_listed", False),
    ("quality", "failed"), ("extraction_id", None), ("request_hash", "e" * 64)])
def test_same_preparation_but_incomplete_durable_evidence_blocks_repayment(previous, field, value):
    f = previous
    f.row[field] = value
    with pytest.raises(CompletedReadReuseError): reuse(f)
    f.converter.assert_not_called()


def test_incomplete_input_proof_blocks_repayment(previous):
    f = previous
    f.row["result_json"]["llm1_input"].pop("diagnostic_sha256")
    with pytest.raises(CompletedReadReuseError): reuse(f)


@pytest.mark.parametrize("missing", ["document_set_hash", "document_set_identity", "doc_class",
    "model", "prompt_hash", "prompt_version", "schema_version"])
def test_partial_paid_identity_is_detected_and_never_authorizes_repayment(previous, missing):
    f = previous
    f.row["result_json"].pop(missing)
    with pytest.raises(CompletedReadReuseError): reuse(f)


def test_source_manifest_is_required_in_raw_but_top_level_is_optional(previous):
    f = previous
    bundle = f.row["result_json"]
    before = copy.deepcopy(bundle)
    assert reuse(f)["bundle"] == before
    assert "source_document_manifest" not in bundle
    bundle["source_document_manifest"] = copy.deepcopy(bundle["raw_extraction_json"]["source_document_manifest"])
    assert reuse(f)["bundle"] == bundle
    bundle["source_document_manifest"][0]["end_page"] += 1
    with pytest.raises(CompletedReadReuseError): reuse(f)
    bundle.pop("source_document_manifest")
    bundle["raw_extraction_json"].pop("source_document_manifest")
    f.row["raw_extraction_json"] = copy.deepcopy(bundle["raw_extraction_json"])
    with pytest.raises(CompletedReadReuseError): reuse(f)


def test_current_model_or_effective_prompt_change_is_not_a_cache_hit(previous, monkeypatch):
    f = previous
    f.manifest.pop("llm1_model_routing")
    config = settings()
    config.gemini_model = "gemini-2.5-flash-lite"
    assert reuse(f, config=config) is None
    from buenapro_worker.extraction import gemini
    original = gemini.read_prompt
    monkeypatch.setattr(gemini, "read_prompt", lambda name: original(name) + "\nChanged current instruction")
    assert reuse(f) is None
    f.converter.assert_not_called()


def test_unexplained_full_request_mismatch_and_parent_revision_block(previous):
    f = previous
    config = settings()
    config.gemini_fallback_model = "gemini-2.5-pro"
    with pytest.raises(CompletedReadReuseError, match="automatic repayment blocked"):
        reuse(f, config=config)
    f.parent["sha256_original"] = "f" * 64
    with pytest.raises(CompletedReadReuseError, match="download facts"):
        reuse(f)


@pytest.mark.parametrize("object_type", ["service", "good"])
def test_legacy_request_bytes_exactly_match_shared_sha_factory(object_type):
    config, pdf = settings(), b"%PDF-legacy-identity"
    sha = hashlib.sha256(pdf).hexdigest()
    prompt = analysis.read_prompt(analysis.BASES_PROMPT_FILENAME)
    if object_type == "good": prompt += analysis.BASES_GOODS_INSTRUCTION
    prompt += analysis.PARTICIPATION_INSTRUCTION
    dc = "bases_good" if object_type == "good" else "bases_service"
    expected = {"original_sha": "a" * 64, "analysis_sha": sha, "manifest": [], "doc_class": dc,
        "prompt_version": analysis.prompt_version_for_doc_class(dc), "prompt_hash": canonical_hash(prompt),
        "model": config.gemini_model, "fallback_model": config.gemini_fallback_model}
    assert analysis._document_input(config, pdf, object_type, "A" * 64, None) == expected
    assert analysis.extraction_request_hash(config, pdf, object_type=object_type,
        original_sha256="A" * 64) == canonical_hash(expected)
    assert analysis.extraction_request_hash_from_verified_sha(config, sha, object_type=object_type,
        original_sha256="A" * 64) == canonical_hash(expected)


def test_selected_bytes_and_sha_request_identity_and_preparation_values_are_identical(previous):
    f = previous
    expected = {"document_scope": preparation.SELECTED_DOCUMENTS_POLICY,
        "document_set": f.selected, "selection_context": f.context,
        "max_source_bytes": 128 * 1024**2, "max_analysis_bytes": 45_000_000,
        "max_pages": 1000, "optimize_lossless": True}
    assert preparation.selected_preparation_identity(f.selected, f.context) == expected
    assert f.prepared["document_set_identity"] == expected
    assert f.prepared["document_set_hash"] == canonical_hash(expected)
    assert analysis.extraction_request_hash_from_verified_sha(settings(), f.prepared["analysis_sha256"],
        object_type="service", original_sha256=f.sha, manifest=f.prepared["source_document_manifest"],
        document_context=f.dc) == f.row["request_hash"]


def test_legacy_selected_receipt_reuses_without_adding_scope_or_input_policy(previous):
    f = previous
    f.context.pop("llm1_scope_policy")
    f.context.pop("llm1_input_policy")
    identity = preparation.selected_preparation_identity(f.selected, f.context)
    dc = {"document_scope": preparation.SELECTED_DOCUMENTS_POLICY, "document_set": f.selected,
          "document_set_hash": canonical_hash(identity), "document_set_identity": identity,
          "selection_context": f.context}
    bundle = f.row["result_json"]
    bundle.pop("llm1_input")
    raw = bundle["raw_extraction_json"]
    raw.pop("llm1_input")
    raw.pop("extraction_coverage")
    raw.update(copy.deepcopy(dc))
    bundle.update(copy.deepcopy(dc))
    inputs = analysis._document_input(settings(), f.pdf, "service", f.sha,
        f.prepared["source_document_manifest"], dc)
    request = canonical_hash(inputs)
    bundle.update(prompt_hash=inputs["prompt_hash"], prompt_version=inputs["prompt_version"],
                  schema_version=schema_version_for_scope("bases_service", f.context),
                  cache_key="prod2:llm1:" + request)
    f.row.update(request_hash=request, raw_extraction_json=copy.deepcopy(raw))
    before = copy.deepcopy(bundle)
    result = reuse(f)
    assert result["bundle"] == before
    assert "llm1_input" not in result["bundle"]
    assert "llm1_scope_policy" not in result["bundle"]["selection_context"]
    f.converter.assert_not_called()


def test_paid_text_input_is_reused_without_reading_deleted_text(previous):
    f = previous
    bundle = f.row["result_json"]
    proof = bundle["llm1_input"]
    proof.update(route="text", text_sha256="e" * 64, text_bytes=1000)
    bundle["raw_extraction_json"]["llm1_input"] = copy.deepcopy(proof)
    dc = {key: bundle[key] for key in ("document_scope", "document_set", "document_set_hash",
          "document_set_identity", "selection_context", "llm1_input")}
    inputs = analysis._document_input(settings(), f.pdf, "service", f.sha,
        f.prepared["source_document_manifest"], dc)
    request = canonical_hash(inputs)
    bundle.update(prompt_hash=inputs["prompt_hash"], cache_key="prod2:llm1:" + request)
    f.row.update(request_hash=request, raw_extraction_json=copy.deepcopy(bundle["raw_extraction_json"]))
    result = reuse(f)
    assert result["bundle"]["llm1_input"] == proof
    assert result["request_hash"] == request
    f.converter.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "running", "uncertain"])
@pytest.mark.parametrize("change", ["new_folder_no_done", "scope", "conversion", "model"])
def test_unresolved_prior_payment_blocks_every_new_read_path(previous, status, change):
    f = previous
    row = f.row
    config = settings()
    if change == "new_folder_no_done": row = None
    if change == "scope": f.context.pop("llm1_scope_policy")
    if change == "conversion": f.manifest["word_conversion_policy"] = "word_footnotes_at_end_v1"
    if change == "model":
        f.manifest.pop("llm1_model_routing")
        config.gemini_model = "gemini-2.5-flash-lite"
    conn = Connection(row, f.parent, unresolved={"id": 42, "status": status})
    with pytest.raises(CompletedReadReuseError, match="reservation requires reconciliation"):
        reuse(f, conn=conn, config=config)
    assert sum("status IN ('pending','running','uncertain')" in sql for sql, _ in conn.queries) == 1
    f.converter.assert_not_called()


def test_exact_done_reuse_is_allowed_without_new_payment_when_another_reservation_is_uncertain(previous):
    f = previous
    conn = Connection(f.row, f.parent, unresolved={"id": 42, "status": "uncertain"})
    result = reuse(f, conn=conn)
    assert result["evaluation_id"] == f.row["evaluation_id"]
    assert result["bundle"] == f.row["result_json"]
    assert not any("status IN ('pending','running','uncertain')" in sql for sql, _ in conn.queries)
    f.converter.assert_not_called()
