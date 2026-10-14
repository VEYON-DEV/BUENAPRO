"""Complete corpus/retrieval offline tests; no real provider or database calls."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil

import pytest
from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject,
                            NumberObject)

from buenapro_worker.prod2 import embedding_corpus as corpus_module
from buenapro_worker.prod2 import embedding_retrieval as retrieval_module


def pdf_bytes(texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 40 740 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def prepared(tmp_path, texts=None):
    texts = texts or ["Experiencia del postor acreditada 10000 soles"]
    data = pdf_bytes(["Fuente nativa completa" for _ in texts])
    path = tmp_path / "analysis.pdf"
    path.write_bytes(data)
    return {"pdf_path": path, "analysis_sha256": hashlib.sha256(data).hexdigest(),
        "analysis_bytes": len(data), "page_count": len(texts), "source_document_manifest": [{
            "candidate_id": "base-1", "document_uuid": "official-uuid-1",
            "original_sha256": "a" * 64, "analysis_sha256": "b" * 64,
            "filename": "bases.pdf", "start_page": 1, "end_page": len(texts), "page_offset": 0}]}


def native_row(text, page=1, **changes):
    row = {"global_page": page, "text": text, "tables": [], "width": 600, "height": 800,
        "images": [], "image_area_ratio": 0, "native_characters": len(text.replace(" ", "")),
        "native_unicode_corrupt": False, "vector_count": 0, "largest_vector_area_ratio": 0,
        "meaningful_annotations": 0, "unknown_object_types": []}
    row.update(changes)
    return row


def build(tmp_path, monkeypatch, texts, rows=None, config=None):
    source = prepared(tmp_path, texts)
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: rows or [
        native_row(text, page) for page, text in enumerate(texts, 1)])
    monkeypatch.setattr(corpus_module, "_versions", lambda *_: {"test": "offline"})
    return corpus_module.build_corpus(source, tmp_path / "private", config=config), source


def query(identifier="subcontracting", anchor="subcontratacion"):
    return {"id": identifier, "fields": ["participation.subcontratacion"],
            "query": "Condiciones y excepciones de la subcontratacion", "literal_anchors": [anchor]}


def retrieve(corpus, *, queries=None, vectors=None, config=None):
    queries = queries or [query()]
    vectors = vectors or [[1., 0.] for _ in corpus["chunks"]]
    effective = {"queries": queries, "semantic_seed_top_k_per_query": 1, **(config or {})}
    return retrieval_module.retrieve_context(corpus, vectors,
        {item["id"]: [1., 0.] for item in queries}, config=effective)


def test_real_native_pdf_complete_cache_and_source_aliases(tmp_path, monkeypatch):
    source = prepared(tmp_path, ["a", "b"])
    source["source_document_manifest"].append({**source["source_document_manifest"][0],
        "candidate_id": "duplicate-base", "document_uuid": "duplicate-official-uuid",
        "duplicate_of": "base-1"})
    result = corpus_module.build_corpus(source, tmp_path / "private")
    assert result["coverage_complete"] and not result["cache_hit"]
    assert result["pdf_sha256_before"] == result["pdf_sha256_after"] == source["analysis_sha256"]
    assert len(result["pages"]) == 2 and result["pages"][0]["method"] == "native_layout"
    assert {location["candidate_id"] for location in result["blocks"][0]["locations"]} == {
        "base-1", "duplicate-base"}
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: pytest.fail("cache re-extracted PDF"))
    cached = corpus_module.build_corpus(source, tmp_path / "private")
    assert cached["cache_hit"] and cached["corpus_hash"] == result["corpus_hash"]


def test_word_converted_pages_are_explicit_pdf_derivative_pages(tmp_path, monkeypatch):
    source = prepared(tmp_path, ["a", "b"])
    source["source_document_manifest"] = [
        {**source["source_document_manifest"][0], "end_page": 1},
        {**source["source_document_manifest"][0], "candidate_id": "word.docx",
         "document_uuid": "word-official", "filename": "requisitos.docx",
         "start_page": 2, "end_page": 2, "page_offset": 1}]
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: [native_row("bases", 1), native_row("personal", 2)])
    result = corpus_module.build_corpus(source, tmp_path / "private")
    location = result["blocks"][1]["locations"][0]
    assert location["source_page"] == 1 and result["blocks"][1]["global_page"] == 2
    assert location["location_kind"] == "prepared_pdf_page"
    assert location["candidate_id"] == "word.docx"


def test_pdf_changed_during_extraction_is_rejected_without_corpus_cache(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    def mutate(*_):
        source["pdf_path"].write_bytes(source["pdf_path"].read_bytes() + b"changed")
        return [native_row("experiencia")]
    monkeypatch.setattr(corpus_module, "_extract_native", mutate)
    with pytest.raises(corpus_module.CorpusError, match="changed during"):
        corpus_module.build_corpus(source, tmp_path / "private")
    assert not list((tmp_path / "private").rglob("corpus.private.json"))


def test_cache_tamper_and_chunks_tamper_cannot_be_reused(tmp_path, monkeypatch):
    result, source = build(tmp_path, monkeypatch, ["Experiencia requerida: 30000 soles."])
    marker, = (tmp_path / "private").rglob("corpus.private.json")
    pinned = json.loads(marker.read_text())
    pinned["blocks"][0]["text"] = "Prohibida experiencia"
    marker.write_text(json.dumps(pinned))
    with pytest.raises(corpus_module.CorpusError, match="content"):
        corpus_module.build_corpus(source, tmp_path / "private")
    mutated = deepcopy(result)
    mutated["chunks"][0]["text"] = "changed"
    mutated["corpus_hash"] = corpus_module.canonical_hash({k: v for k, v in mutated.items()
                                                         if k not in {"corpus_hash", "cache_hit"}})
    with pytest.raises(corpus_module.CorpusError, match="chunks"):
        corpus_module.validate_corpus(mutated)


def test_rehashed_false_source_page_cannot_become_trusted_provenance(tmp_path, monkeypatch):
    result, _ = build(tmp_path, monkeypatch, ["Experiencia requerida."])
    result.pop("cache_hit")
    result["blocks"][0]["locations"][0]["source_page"] = 999
    result["blocks"][0]["id"] = corpus_module.canonical_hash({
        key: value for key, value in result["blocks"][0].items() if key != "id"})
    result["chunks"] = corpus_module._make_chunks(result["blocks"], result["config"])
    result["corpus_hash"] = corpus_module.canonical_hash({
        key: value for key, value in result.items() if key != "corpus_hash"})
    with pytest.raises(corpus_module.CorpusError, match="source identity"):
        corpus_module.validate_corpus(result)


def test_unicode_fragments_are_exact_and_overlap_without_dropping_characters():
    text = ("Educación técnica Perú 中文 🚀 e\u0301 — S/ 200,000.00; " * 250)
    pieces = corpus_module._split_tokens(text, 1000, 128)
    covered = set()
    for piece, start, end in pieces:
        assert piece == text[start:end] and "\ufffd" not in piece
        assert corpus_module.token_count(piece) <= 1000
        covered.update(range(start, end))
    assert covered == set(range(len(text)))
    assert pieces[0][2] > pieces[1][1]


def test_chunk_packs_section_paragraphs_and_binds_complete_block_ranges(tmp_path, monkeypatch):
    result, _ = build(tmp_path, monkeypatch, ["Texto de requisito contractual.\n\nTexto de acreditación específica."])
    assert len(result["blocks"]) == 2 and len(result["chunks"]) == 1
    assert result["chunks"][0]["block_ids"] == [block["id"] for block in result["blocks"]]
    assert all(row["end_char"] > row["start_char"] for row in result["chunks"][0]["block_ranges"])


@pytest.mark.parametrize("config", [{"chunk_target_tokens": 9000}, {"overlap_tokens": 1000},
    {"minimum_ocr_confidence": 30}, {"relevant_image_area_ratio": .5}, {"ocr_textual_scan_policy": "unsafe"}])
def test_configuration_never_relaxes_chunk_or_ocr_bounds(config):
    with pytest.raises(corpus_module.CorpusError):
        corpus_module._config(config)


def good_ocr(**changes):
    value = {"text": "El proveedor acredita experiencia específica mediante contratos similares. " * 10,
        "word_count": 80, "mean_confidence": 94., "confident_word_ratio": .96,
        "column_gap_rows": 0, "acceptable": True, "reasons": [], "cleaned_render_bytes": 2345}
    value.update(changes)
    return value


def scan_row(page=1, image_ratio=.95, text=""):
    return native_row(text, page, native_characters=sum(not char.isspace() for char in text), images=[
        {"bbox": [0, 0, 600, 800], "area_ratio": image_ratio}], image_area_ratio=image_ratio)


def test_confident_textual_scan_uses_local_ocr_not_embeddings_of_image(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr())
    result, _ = build(tmp_path, monkeypatch, [""], rows=[scan_row()])
    assert result["coverage_complete"] and result["ocr_page_count"] == 1
    assert result["pages"][0]["method"] == "full_page_ocr"
    assert result["visual_warnings"] and result["human_review_recommended"]
    assert "acredita experiencia" in result["chunks"][0]["text"]
    assert result["cleaned_render_bytes"] == 2345


@pytest.mark.parametrize("ocr_changes", [
    {"text": "", "word_count": 0, "acceptable": False, "reasons": ["ocr_empty_on_visible_page"]},
    {"acceptable": False, "reasons": ["ocr_low_confidence"], "confident_word_ratio": .5},
    {"column_gap_rows": 8}, {"word_count": 5},
])
def test_ocr_empty_low_confidence_sparse_or_tables_remain_review(tmp_path, monkeypatch, ocr_changes):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr(**ocr_changes))
    result, _ = build(tmp_path, monkeypatch, [""], rows=[scan_row()])
    assert not result["coverage_complete"] and result["unresolved_pages"] == [1]
    assert "pdf_fallback" not in json.dumps(result)


def test_mixed_small_visual_region_keeps_native_and_ocr_but_review(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr())
    row = scan_row(image_ratio=.2, text="Título nativo y requisito de contratación")
    result, _ = build(tmp_path, monkeypatch, [row["text"]], rows=[row])
    assert not result["coverage_complete"] and result["pages"][0]["method"] == "native_plus_full_page_ocr"
    assert "Título nativo" in "\n".join(block["text"] for block in result["blocks"])
    assert "acredita experiencia" in "\n".join(block["text"] for block in result["blocks"])


def test_true_empty_page_is_complete_but_unread_visual_page_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: pytest.fail("blank OCR"))
    result, _ = build(tmp_path, monkeypatch, [""], rows=[native_row("", native_characters=0)])
    assert result["coverage_complete"] and result["pages"][0]["confirmed_empty"]


def test_ocr_page_budget_and_native_parse_timeout_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr())
    result, _ = build(tmp_path, monkeypatch, ["", ""], rows=[scan_row(1), scan_row(2)], config={"max_ocr_pages": 1})
    assert result["unresolved_pages"] == [2]
    assert "ocr_page_budget_exhausted" in result["pages"][1]["reasons"]
    source = prepared(tmp_path)
    def fail(*_):
        raise TimeoutError("synthetic local timeout")
    monkeypatch.setattr(corpus_module, "_extract_native", fail)
    result = corpus_module.build_corpus(source, tmp_path / "different")
    assert result["native_extraction_error_type"] == "TimeoutError"
    assert result["unresolved_pages"] == [1] and not result["chunks"]


def test_low_semantic_similarity_literal_clause_and_adjacent_exception_are_kept(tmp_path, monkeypatch):
    texts = ["1. INTRODUCCION\nObjeto del contrato.",
             "2. PARTICIPACION\nSe permite subcontratación hasta 30%.",
             "3. EXCEPCION\nSalvo actividades de seguridad: estas NO pueden subcontratarse.",
             "4. GENERALIDADES\nPublicidad del proceso."]
    corpus, _ = build(tmp_path, monkeypatch, texts)
    vectors = [[1., 0.]] + [[-1., 0.] for _ in corpus["chunks"][1:]]
    result = retrieve(corpus, vectors=vectors)
    assert "Se permite subcontratación hasta 30%" in result["context_text"]
    assert "NO pueden subcontratarse" in result["context_text"]
    assert result["coverage_by_field"]["subcontracting"]["literal_evidence_identified"]
    assert not result["scores_are_compliance_probabilities"]
    assert {citation["source_page"] for citation in result["citations"]} >= {2, 3}
    retrieval_module.validate_retrieval(result, corpus)


def test_continued_tables_include_all_rows_after_embedding_slice(tmp_path, monkeypatch):
    header = ["Personal", "Experiencia requerida"]
    rows = []
    for page in range(1, 5):
        rows.append(native_row(f"{page}. SECCION {page}\nTexto general.", page, tables=[{
            "ordinal": 0, "bbox": [40, 80, 500, 500], "rows": [header,
                [f"Especialista {page}", f"{page + 2} años"]]}]))
    corpus, _ = build(tmp_path, monkeypatch, [row["text"] for row in rows], rows=rows)
    # Literal only seeds first-page row; table header is not used as the anchor.
    result = retrieve(corpus, queries=[query("personnel", "Especialista 1")])
    assert "Especialista 4 | 6 años" in result["context_text"]
    tables = [block for block in result["selected_blocks"] if block["table_id"]]
    assert len(tables) == 4


def test_references_follow_real_source_page_and_annex_section(tmp_path, monkeypatch):
    texts = ["1. BASES\nLa subcontratación se regula en la página 4 y el Anexo 2.",
        "2. GENERAL\nTexto neutral.", "3. ANEXO\nAnexo 2\nExcepción aplicable al servicio.",
        "4. CLAUSULA\nEl porcentaje permitido es 20% con autorización previa."]
    corpus, _ = build(tmp_path, monkeypatch, texts)
    result = retrieve(corpus)
    assert "20% con autorización previa" in result["context_text"]
    assert "Excepción aplicable" in result["context_text"]
    assert not result["unresolved_reference_ids"]


def test_missing_referenced_annex_and_absent_literal_are_unknown_not_prohibition(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, ["Objeto: contrato. Ver Anexo 99."])
    result = retrieve(corpus)
    assert result["needs_review"] and "referenced_evidence_unresolved" in result["review_reasons"]
    assert result["coverage_by_field"]["subcontracting"]["absence_semantics"] == "not_identified_not_permission_or_prohibition"
    assert not result["unverified_retrieval_projects_canonical_participation"]


def test_budget_overflow_never_slices_requirement_or_calls_full_pdf(tmp_path, monkeypatch):
    text = "1. REQUISITO\nLa subcontratacion conserva todos los requisitos y excepciones. " + "experiencia acreditada " * 500
    corpus, _ = build(tmp_path, monkeypatch, [text])
    result = retrieve(corpus, config={"context_target_tokens": 100, "context_maximum_tokens": 200})
    assert result["needs_review"] and result["context_tokens"] > 200
    assert text in result["context_text"] and len(result["context_text"]) > len(text)
    assert "expanded_context_exceeds_budget_without_truncation" in result["review_reasons"]


def test_context_token_reduction_measured_with_source_and_metadata(tmp_path, monkeypatch):
    texts = [f"{page}. SECCION {page}\n" + ("Texto neutral sin relación con requerimientos. " * 130)
             for page in range(1, 31)]
    texts[5] = "6. PARTICIPACION\nSubcontratación máxima 30% con aprobación previa."
    texts[6] = "7. EXCEPCIONES\nExcepto el componente de seguridad, indelegable."
    corpus, _ = build(tmp_path, monkeypatch, texts)
    vectors = [[1., 0.] if 6 in chunk["global_pages"] else [-1., 0.] for chunk in corpus["chunks"]]
    result = retrieve(corpus, vectors=vectors)
    assert "indelegable" in result["context_text"]
    assert result["source_tokens"] == corpus["source_tokens"]
    assert result["context_tokens"] < result["source_tokens"] * .25
    assert result["context_tokens"] == corpus_module.token_count(result["context_text"])


@pytest.mark.parametrize("vectors", [[[0., 0.]], [[float("nan"), 1.]], [[True, 1.]], [[1., 2., 3.]]])
def test_invalid_or_incompatible_vectors_fail_closed(tmp_path, monkeypatch, vectors):
    corpus, _ = build(tmp_path, monkeypatch, ["Requisito general."])
    with pytest.raises(retrieval_module.RetrievalError):
        retrieve(corpus, vectors=vectors)


def test_vector_id_mismatch_and_context_tamper_are_rejected(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, ["Se permite subcontratacion hasta 30%."])
    with pytest.raises(retrieval_module.RetrievalError, match="identities"):
        retrieval_module.retrieve_context(corpus, {"incorrect": [1., 0.]},
            {"subcontracting": [1., 0.]}, config={"queries": [query()]})
    result = retrieve(corpus)
    result["context_text"] = "Se prohíbe subcontratar."
    with pytest.raises(retrieval_module.RetrievalError, match="identity"):
        retrieval_module.validate_retrieval(result, corpus)


def test_rehashed_false_citation_is_still_rejected(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, ["Se permite subcontratacion hasta 30%."])
    result = retrieve(corpus)
    result["citations"][0]["source_page"] = 999
    result["retrieval_hash"] = corpus_module.canonical_hash({
        key: value for key, value in result.items() if key != "retrieval_hash"})
    with pytest.raises(retrieval_module.RetrievalError, match="citations"):
        retrieval_module.validate_retrieval(result, corpus)


def test_tsv_native_ocr_confidence_and_table_gaps_are_measured():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    content = header + "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t95\tPersonal\n" + \
        "5\t1\t1\t1\t1\t2\t200\t10\t40\t20\t20\tExperiencia\n"
    result = corpus_module._parse_tsv(content.encode(), corpus_module.DEFAULT_CONFIG)
    assert result["column_gap_rows"] == 1 and "\t" in result["text"]
    assert result["confident_word_ratio"] == .5


def test_real_local_ocr_scanned_text_cleans_its_render(tmp_path):
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        pytest.skip("Local OCR tools not installed")
    from PIL import Image, ImageDraw, ImageFont
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if not font_path.is_file():
        pytest.skip("Fixture font not installed")
    image = Image.new("RGB", (1400, 1800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 38)
    for line in range(10):
        draw.text((70, 100 + line * 120), "El postor acredita experiencia mediante contratos similares", font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=150)
    data = output.getvalue()
    source = prepared(tmp_path)
    source["pdf_path"].write_bytes(data)
    source.update(analysis_sha256=hashlib.sha256(data).hexdigest(), analysis_bytes=len(data))
    result = corpus_module.build_corpus(source, tmp_path / "private")
    assert result["coverage_complete"] and result["ocr_page_count"] == 1
    assert "contratos similares" in result["blocks"][0]["text"]
    assert not list((tmp_path / "private").rglob("page.png"))
    assert result["cleaned_render_bytes"] > 0


V2 = {"ocr_textual_scan_policy": "prod2_confident_text_layout_review_v2"}


@pytest.mark.parametrize("image_ratio,words,gaps", [(.0983, 4, 0), (.37, 80, 0), (.61, 80, 8), (.95, 80, 12)])
def test_v2_mixed_logo_or_table_text_indexes_without_claiming_visual_certainty(tmp_path, monkeypatch,
        image_ratio, words, gaps):
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr(word_count=words, column_gap_rows=gaps))
    row = scan_row(image_ratio=image_ratio, text="Texto nativo completo del alcance.")
    result, _ = build(tmp_path, monkeypatch, [row["text"]], rows=[row], config=V2)
    assert result["coverage_complete"] and result["text_coverage_complete"]
    assert result["claims_visual_complete"] is False
    assert result["human_review_recommended"] and result["pages"][0]["visual_warnings"]
    assert result["pages"][0]["claims_visual_complete"] is False
    assert result["pages"][0]["ocr_cache_file_sha256"]
    retrieved = retrieve(result)
    assert "ADVERTENCIAS DE COBERTURA" in retrieved["context_text"]
    assert "require_human_review_of_layout_relations" in retrieved["context_text"]
    assert retrieved["claims_visual_complete"] is False
    retrieval_module.validate_retrieval(retrieved, result)


@pytest.mark.parametrize("changes", [{"text": "", "word_count": 0}, {"mean_confidence": 60},
                                    {"confident_word_ratio": .84}, {"text": "Texto \ufffd corrupto"}])
def test_v2_never_uses_empty_low_quality_or_corrupt_ocr_as_complete(tmp_path, monkeypatch, changes):
    # Even a mistaken acceptable=True field cannot override real quality fields.
    monkeypatch.setattr(corpus_module, "_ocr_page", lambda *_: good_ocr(**changes))
    result, _ = build(tmp_path, monkeypatch, [""], rows=[scan_row(image_ratio=.5)], config=V2)
    assert not result["text_coverage_complete"] and result["unresolved_pages"] == [1]
    assert "ocr_text_quality_unresolved" in result["pages"][0]["reasons"]
    assert len(list((tmp_path / "private").rglob("ocr-page-0001.private.json"))) == 1


def test_v2_stopped_after_ocr_preserves_page_then_resume_reuses_it(tmp_path, monkeypatch):
    source = prepared(tmp_path, ["", ""])
    monkeypatch.setattr(corpus_module, "_versions", lambda *_: {"offline": True})
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: [scan_row(1, .4), scan_row(2, .6)])
    calls = []
    def ocr(_, page, *args):
        calls.append(page)
        return good_ocr()
    monkeypatch.setattr(corpus_module, "_ocr_page", ocr)
    with pytest.raises(corpus_module.CorpusStopped):
        corpus_module.build_corpus(source, tmp_path / "private", config=V2,
            stop_requested=lambda: bool(calls))
    assert calls == [1] and not list((tmp_path / "private").rglob("corpus.private.json"))
    assert len(list((tmp_path / "private").rglob("ocr-page-0001.private.json"))) == 1
    result = corpus_module.build_corpus(source, tmp_path / "private", config=V2)
    assert calls == [1, 2] and result["coverage_complete"]
    assert len(list((tmp_path / "private").rglob("ocr-page-*.private.json"))) == 2


def test_v2_cached_bad_ocr_is_reused_but_stays_unresolved(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    monkeypatch.setattr(corpus_module, "_versions", lambda *_: {"offline": True})
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: [scan_row(image_ratio=.4)])
    calls = []
    def ocr(*args):
        calls.append(True)
        return good_ocr(text="", word_count=0, acceptable=False, reasons=["ocr_empty_on_visible_page"])
    monkeypatch.setattr(corpus_module, "_ocr_page", ocr)
    with pytest.raises(corpus_module.CorpusStopped):
        corpus_module.build_corpus(source, tmp_path / "private", config=V2,
            stop_requested=lambda: bool(calls))
    result = corpus_module.build_corpus(source, tmp_path / "private", config=V2)
    assert calls == [True] and not result["coverage_complete"]


def test_v2_ocr_cache_tampering_is_rejected_instead_of_rereading(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    monkeypatch.setattr(corpus_module, "_versions", lambda *_: {"offline": True})
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: [scan_row(image_ratio=.4)])
    calls = []
    def ocr(*args):
        calls.append(True)
        return good_ocr()
    monkeypatch.setattr(corpus_module, "_ocr_page", ocr)
    with pytest.raises(corpus_module.CorpusStopped):
        corpus_module.build_corpus(source, tmp_path / "private", config=V2,
            stop_requested=lambda: bool(calls))
    path, = (tmp_path / "private").rglob("ocr-page-0001.private.json")
    cache = json.loads(path.read_text())
    cache["result"]["text"] = "Subcontratacion prohibida"
    path.write_text(json.dumps(cache))
    with pytest.raises(corpus_module.CorpusError, match="Cached page OCR"):
        corpus_module.build_corpus(source, tmp_path / "private", config=V2)
    assert calls == [True]


def test_v2_source_revision_cannot_reuse_an_earlier_page_cache(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    monkeypatch.setattr(corpus_module, "_versions", lambda *_: {"offline": True})
    monkeypatch.setattr(corpus_module, "_extract_native", lambda *_: [scan_row(image_ratio=.4)])
    calls = []
    def ocr(*args):
        calls.append(True)
        return good_ocr()
    monkeypatch.setattr(corpus_module, "_ocr_page", ocr)
    first = corpus_module.build_corpus(source, tmp_path / "private", config=V2)
    data = source["pdf_path"].read_bytes() + b"\n%new-source-version"
    source["pdf_path"].write_bytes(data)
    source.update(analysis_sha256=hashlib.sha256(data).hexdigest(), analysis_bytes=len(data))
    second = corpus_module.build_corpus(source, tmp_path / "private", config=V2)
    assert len(calls) == 2 and first["corpus_hash"] != second["corpus_hash"]
