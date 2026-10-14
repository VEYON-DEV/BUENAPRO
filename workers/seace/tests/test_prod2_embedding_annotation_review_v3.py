"""Opt-in V3 annotation classification; local fixtures, never paid/API calls."""
import hashlib
import io
import json
from copy import deepcopy

import pytest
from pdfminer.psparser import LIT
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, DecodedStreamObject,
    DictionaryObject, NameObject, NumberObject, TextStringObject)

from buenapro_worker.prod2 import embedding_corpus as corpus


V3 = {"ocr_textual_scan_policy": "prod2_confident_text_layout_review_v3"}
V2 = {"ocr_textual_scan_policy": "prod2_confident_text_layout_review_v2"}


def annotation(subtype, **values):
    return {"data": {"Subtype": LIT(subtype), **values}}


def fixture_pdf(tmp_path, annotations):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 40 740 Td (Experiencia del postor: 10000 soles. Consorcio permitido.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    entries = []
    for fields in annotations:
        value = DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Rect"): ArrayObject([NumberObject(40), NumberObject(600),
                                            NumberObject(300), NumberObject(650)])})
        value.update(fields)
        entries.append(writer._add_object(value))
    page[NameObject("/Annots")] = ArrayObject(entries)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    path = tmp_path / "annotated.pdf"
    path.write_bytes(data)
    return {"pdf_path": path, "analysis_sha256": hashlib.sha256(data).hexdigest(),
        "analysis_bytes": len(data), "page_count": 1, "source_document_manifest": [{
            "candidate_id": "base-1", "document_uuid": "official-uuid-1",
            "original_sha256": "a" * 64, "analysis_sha256": "b" * 64,
            "filename": "bases.pdf", "start_page": 1, "end_page": 1, "page_offset": 0}]}


@pytest.mark.parametrize("value", [LIT("Link"), "Link", "/Link", b"/Link"])
def test_literal_names_are_normalized_without_str_literal_quote_bug(value):
    assert corpus._annotation_name(value) == "Link"


@pytest.mark.parametrize("contents", [None, b"", b"   ", "\t \n", b"\xfe\xff\x00 "])
def test_plain_navigation_link_has_no_substantive_text(contents):
    result = corpus._classify_annotations([annotation("Link", Contents=contents,
        A={"S": LIT("URI"), "URI": b"https://official.example/bases"})])
    assert result["unresolved_count"] == 0
    assert result["visual_warnings"] == []
    assert result["annotations"][0]["classification"] == "navigation_link_without_content"


@pytest.mark.parametrize("values", [
    {"Contents": b"Subcontratacion prohibida"},
    {"AP": {"N": {"appearance": "substantive"}}},
    {"Contents": b"\xfe\xff\x00N\x00o"},
])
def test_link_with_substantive_annotation_content_is_still_unresolved(values):
    result = corpus._classify_annotations([annotation("Link", **values)])
    assert result["unresolved_count"] == 1
    assert "annotation_content_or_appearance_not_captured" in result["visual_warnings"]


@pytest.mark.parametrize("field_type,values", [
    ("Tx", {"V": b"Experiencia minima tres anios"}),
    ("Ch", {"V": [b"Consorcio permitido"]}),
    ("Btn", {"AS": LIT("Off")}),
    ("Tx", {"DV": b"Monto: 500000"}),
    ("Tx", {"AP": {"N": {"value": "rendered"}}}),
    (None, {"V": b"Contratacion"}),
])
def test_form_widget_values_and_visual_states_are_not_dropped(field_type, values):
    if field_type:
        values["FT"] = LIT(field_type)
    result = corpus._classify_annotations([annotation("Widget", **values)])
    assert result["unresolved_count"] == 1
    assert result["annotations"][0]["classification"] == "form_widget_data_not_captured"
    # Source/field values must not appear in compact telemetry diagnostics.
    assert "Experiencia minima" not in json.dumps(result)


def test_parent_field_type_and_value_are_resolved():
    result = corpus._classify_annotations([annotation("Widget", Parent={
        "Parent": {"FT": LIT("Tx"), "V": b"Requisito relevante"}})])
    assert result["annotations"][0]["field_type"] == "Tx"
    assert result["annotations"][0]["has_value"] is True
    assert result["unresolved_count"] == 1


def test_parent_signature_type_is_reviewable_not_authentication():
    result = corpus._classify_annotations([annotation("Widget", Parent={
        "FT": LIT("Sig"), "V": {"Contents": b"cryptographic bytes"}},
        AP={"N": {"appearance": "signature"}})])
    assert result["unresolved_count"] == 0
    assert result["visual_warnings"] == ["signature_widget_visual_content_not_verified"]
    assert result["annotations"][0]["classification"] == "signature_widget_visual_review"
    assert "authority_verified" not in result


def test_signature_widget_with_explicit_comment_is_not_ignored():
    result = corpus._classify_annotations([annotation("Widget", FT=LIT("Sig"),
        V={"Contents": b"cryptographic bytes"}, Contents=b"Obligacion adicional")])
    assert result["unresolved_count"] == 1


@pytest.mark.parametrize("subtype", ["Text", "FreeText", "Highlight", "Stamp", "Unknown"])
def test_substantive_or_unknown_annotations_stay_unresolved(subtype):
    result = corpus._classify_annotations([annotation(subtype, Contents=b"Requisito adicional")])
    assert result["unresolved_count"] == 1


def test_malformed_annotation_fails_closed():
    with pytest.raises(corpus.CorpusError, match="dictionary is invalid"):
        corpus._classify_annotations([{"data": []}])


def test_inheritance_over_bound_fails_closed():
    parent = {"FT": LIT("Sig")}
    for _ in range(9):
        parent = {"Parent": parent}
    with pytest.raises(corpus.CorpusError, match="inheritance exceeds"):
        corpus._classify_annotations([annotation("Widget", **parent)])


def test_real_pdf_v3_navigation_and_signature_no_longer_block_text(tmp_path, monkeypatch):
    source = fixture_pdf(tmp_path, [
        {NameObject("/Subtype"): NameObject("/Link"), NameObject("/A"):
            DictionaryObject({NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("https://official.example/bases")})},
        {NameObject("/Subtype"): NameObject("/Widget"), NameObject("/FT"): NameObject("/Sig"),
            NameObject("/V"): DictionaryObject({NameObject("/Contents"): ByteStringObject(b"signature bytes")})},
    ])
    monkeypatch.setattr(corpus, "_versions", lambda *_: {"test": "offline"})
    legacy = corpus._native_pages(source["pdf_path"])
    assert legacy == corpus._native_pages(source["pdf_path"], V2["ocr_textual_scan_policy"])
    assert legacy[0]["meaningful_annotations"] == 2
    assert "annotation_review" not in legacy[0]
    result = corpus.build_corpus(source, tmp_path / "v3", config=V3)
    assert result["coverage_complete"] is True
    assert result["text_capture_policy"] == V3["ocr_textual_scan_policy"]
    assert result["claims_visual_complete"] is False
    assert result["authority_verified"] is False
    assert result["claims_exhaustive_legal_read"] is False
    assert result["human_review_recommended"] is True
    page = result["pages"][0]
    assert "signature_widget_visual_content_not_verified" in page["visual_warnings"]
    assert page["annotation_review"]["unresolved_count"] == 0
    assert result["blocks"] and "10000" in result["blocks"][0]["text"]
    assert result["ocr_page_count"] == 0
    assert corpus.validate_corpus(result) is result
    assert corpus.build_corpus(source, tmp_path / "v3", config=V3)["cache_hit"] is True
    v2 = corpus.build_corpus(source, tmp_path / "v2", config=V2)
    assert v2["unresolved_pages"] == [1]
    assert v2["pages"][0]["reasons"] == ["unrepresented_page_objects"]
    assert "annotation_review" not in v2["pages"][0]
    assert v2["corpus_hash"] != result["corpus_hash"]


def test_real_pdf_form_value_not_in_native_text_stays_blocked(tmp_path, monkeypatch):
    source = fixture_pdf(tmp_path, [{NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"), NameObject("/V"): TextStringObject("Experiencia: 999999 soles") }])
    monkeypatch.setattr(corpus, "_versions", lambda *_: {"test": "offline"})
    result = corpus.build_corpus(source, tmp_path / "v3", config=V3)
    assert result["unresolved_pages"] == [1]
    assert "unrepresented_page_objects" in result["pages"][0]["reasons"]
    assert result["pages"][0]["annotation_review"]["unresolved_count"] == 1
    assert "999999" not in " ".join(block["text"] for block in result["blocks"])
    assert result["claims_visual_complete"] is False


def test_v3_policy_has_no_default_configuration_changes():
    assert corpus.DEFAULT_CONFIG["ocr_textual_scan_policy"] == "prod2_confident_textual_scan_v1"
    assert corpus._config(None) == corpus.DEFAULT_CONFIG
    assert corpus._config(V3)["ocr_textual_scan_policy"] == V3["ocr_textual_scan_policy"]


def test_annotation_diagnostic_counts_cannot_claim_false_coverage(tmp_path, monkeypatch):
    source = fixture_pdf(tmp_path, [{NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"), NameObject("/V"): TextStringObject("Requisito relevante")}])
    monkeypatch.setattr(corpus, "_versions", lambda *_: {"test": "offline"})
    value = corpus.build_corpus(source, tmp_path / "v3", config=V3)
    tampered = deepcopy(value)
    tampered["pages"][0]["annotation_review"]["unresolved_count"] = 0
    tampered["corpus_hash"] = corpus.canonical_hash({k: v for k, v in tampered.items()
                                                    if k not in {"corpus_hash", "cache_hit"}})
    with pytest.raises(corpus.CorpusError, match="annotation coverage contradicts"):
        corpus.validate_corpus(tampered)
