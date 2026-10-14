"""Offline contracts for bounded OCR with explicitly partial page coverage."""
from copy import deepcopy
import hashlib
import json

import pytest

from buenapro_worker.prod2 import embedding_corpus as corpus
from buenapro_worker.prod2 import embedding_retrieval as retrieval
from buenapro_worker.prod2.embedding_ocr_recovery import adopt_page_ocr
from test_prod2_embedding_corpus_retrieval import native_row, prepared, scan_row


V3 = "prod2_confident_text_layout_review_v3"
V5 = "prod2_skip_failed_ocr_pages_v5"
PARTIAL = {"ocr_textual_scan_policy": V3, "page_failure_policy": V5}


def ocr_result(page=1, **changes):
    text = (f"Pagina{page} experiencia del postor personal clave requisitos tecnicos. " * 10)
    result = {"text": text, "word_count": 90, "mean_confidence": 96.,
              "confident_word_ratio": .98, "column_gap_rows": 0,
              "acceptable": True, "reasons": [], "cleaned_render_bytes": 100}
    result.update(changes)
    return result


def configure_native(monkeypatch, rows):
    monkeypatch.setattr(corpus, "_extract_native", lambda *_: deepcopy(rows))
    monkeypatch.setattr(corpus, "_versions", lambda *_: {"test": "offline"})


def build_partial(tmp_path, monkeypatch, rows):
    source = prepared(tmp_path, ["source" for _ in rows])
    configure_native(monkeypatch, rows)
    result = corpus.build_corpus(source, tmp_path / "partial-private", config=PARTIAL)
    return result, source


def assert_bound_partial(result, source, *, unresolved):
    assert result["unresolved_pages"] == unresolved
    assert result["coverage_complete"] is (not unresolved)
    assert result["text_coverage_complete"] is (not unresolved)
    assert not result["claims_visual_complete"]
    assert not result["claims_exhaustive_legal_read"]
    assert result["pdf_sha256_before"] == result["pdf_sha256_after"] == source["analysis_sha256"]
    assert hashlib.sha256(source["pdf_path"].read_bytes()).hexdigest() == source["analysis_sha256"]
    assert corpus.validate_corpus(result) is result
    assert result["corpus_hash"] == corpus.canonical_hash({
        key: value for key, value in result.items() if key not in {"corpus_hash", "cache_hit"}})


@pytest.mark.parametrize("configuration", [
    {"page_failure_policy": V5},
    {"ocr_textual_scan_policy": "prod2_confident_text_layout_review_v2", "page_failure_policy": V5},
    {"ocr_textual_scan_policy": V3, "page_failure_policy": "skip_everything"},
    {"ocr_textual_scan_policy": V3, "page_failure_policy": None},
    {"ocr_textual_scan_policy": V3, "page_failure_policy": True},
])
def test_partial_policy_requires_its_explicit_v3_configuration(configuration):
    with pytest.raises(corpus.CorpusError):
        corpus._config(configuration)


def test_absent_optional_policy_preserves_the_old_exact_configuration():
    strict = corpus._config({"ocr_textual_scan_policy": V3})
    assert "page_failure_policy" not in strict
    assert strict == {**corpus.DEFAULT_CONFIG, "ocr_textual_scan_policy": V3}
    assert corpus._config(PARTIAL)["page_failure_policy"] == V5


@pytest.mark.parametrize("configuration", [
    {"minimum_ocr_confidence": 64}, {"minimum_ocr_confident_word_ratio": .849},
])
def test_partial_policy_does_not_relax_ocr_thresholds(configuration):
    with pytest.raises(corpus.CorpusError):
        corpus._config({**PARTIAL, **configuration})


@pytest.mark.parametrize("bad", [
    {"acceptable": False, "reasons": ["ocr_low_confidence"], "confident_word_ratio": .4},
    {"acceptable": True, "reasons": [], "mean_confidence": 64.9},
    {"acceptable": True, "reasons": [], "confident_word_ratio": .849},
])
def test_good_bad_good_pages_continue_and_exclude_failed_text(tmp_path, monkeypatch, bad):
    calls = []
    def once(pdf, number, private, config, *, psm="3"):
        calls.append((number, config["ocr_dpi"], psm))
        return ocr_result(number, **bad) if number == 2 else ocr_result(number)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    result, source = build_partial(tmp_path, monkeypatch, [scan_row(page) for page in (1, 2, 3)])
    assert calls == [(1, 180, "3"), (2, 180, "3"), (3, 180, "3")]
    assert [page["global_page"] for page in result["pages"]] == [1, 2, 3]
    assert [page["acceptable"] for page in result["pages"]] == [True, False, True]
    assert {block["global_page"] for block in result["blocks"]} == {1, 3}
    assert all("Pagina2" not in item["text"] for item in result["blocks"] + result["chunks"])
    assert any("Pagina1" in block["text"] for block in result["blocks"])
    assert any("Pagina3" in block["text"] for block in result["blocks"])
    assert_bound_partial(result, source, unresolved=[2])


@pytest.mark.parametrize("error", [TimeoutError, OSError, ValueError, corpus.CorpusError])
def test_local_page_failure_is_recorded_then_next_page_is_processed(tmp_path, monkeypatch, error):
    calls = []
    def once(pdf, number, private, config, *, psm="3"):
        calls.append(number)
        if number == 2:
            raise error("Private source details must not become diagnostics")
        return ocr_result(number)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    result, source = build_partial(tmp_path, monkeypatch, [scan_row(page) for page in (1, 2, 3)])
    assert calls == [1, 2, 3]
    assert "local_ocr_" + error.__name__.lower() in result["pages"][1]["reasons"]
    assert "Private source details" not in json.dumps(result)
    assert {block["global_page"] for block in result["blocks"]} == {1, 3}
    assert_bound_partial(result, source, unresolved=[2])


def test_all_failed_pages_cannot_create_usable_embedding_chunks(tmp_path, monkeypatch):
    calls = []
    def once(pdf, number, private, config, *, psm="3"):
        calls.append(number)
        return ocr_result(number, acceptable=False, reasons=["ocr_low_confidence"],
                          mean_confidence=40., confident_word_ratio=.3)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    result, source = build_partial(tmp_path, monkeypatch, [scan_row(1), scan_row(2)])
    assert calls == [1, 2]
    assert result["blocks"] == result["chunks"] == []
    assert result["source_tokens"] == result["text_bytes"] == 0
    assert_bound_partial(result, source, unresolved=[1, 2])


def test_failed_scan_does_not_prevent_usable_native_text(tmp_path, monkeypatch):
    calls = []
    def once(pdf, number, private, config, *, psm="3"):
        calls.append(number)
        raise TimeoutError("Slow OCR")
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    rows = [native_row("Requisitos del proveedor disponibles como texto.", 1),
            scan_row(2), native_row("Se permite la participacion en consorcio.", 3)]
    result, source = build_partial(tmp_path, monkeypatch, rows)
    assert calls == [2]
    assert {block["global_page"] for block in result["blocks"]} == {1, 3}
    assert_bound_partial(result, source, unresolved=[2])


def test_source_identity_error_remains_fatal_in_partial_mode(tmp_path, monkeypatch):
    def cached(*args, **kwargs):
        raise corpus.CorpusError("Source identity no longer matches")
    monkeypatch.setattr(corpus, "_cached_ocr_page", cached)
    with pytest.raises(corpus.CorpusError, match="Source identity"):
        build_partial(tmp_path, monkeypatch, [scan_row(1)])
    assert not list((tmp_path / "partial-private").rglob("corpus.private.json"))


def test_pdf_mutation_after_page_ocr_is_fatal_not_a_skipped_page(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    configure_native(monkeypatch, [scan_row(1)])
    def once(pdf, number, private, config, *, psm="3"):
        pdf.write_bytes(pdf.read_bytes() + b"changed")
        return ocr_result(number)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    with pytest.raises(corpus.CorpusError, match="changed"):
        corpus.build_corpus(source, tmp_path / "partial-private", config=PARTIAL)
    assert not list((tmp_path / "partial-private").rglob("corpus.private.json"))


def test_strict_v3_still_retries_bad_pages_and_keeps_failed_coverage(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    configure_native(monkeypatch, [scan_row(1)])
    calls = []
    def once(pdf, number, private, config, *, psm="3"):
        calls.append((config["ocr_dpi"], psm))
        return ocr_result(number, acceptable=False, reasons=["ocr_low_confidence"],
                          mean_confidence=40., confident_word_ratio=.3)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    result = corpus.build_corpus(source, tmp_path / "strict-private",
                                 config={"ocr_textual_scan_policy": V3})
    assert calls == [(180, "3"), (300, "3"), (300, "1"), (300, "6")]
    assert "page_failure_policy" not in result["config"]
    assert result["unresolved_pages"] == [1] and not result["coverage_complete"]
    assert result["blocks"]  # Existing strict corpus retains diagnostics/evidence unchanged.
    assert corpus.validate_corpus(result) is result


def prior_good_page(tmp_path, monkeypatch):
    source = prepared(tmp_path)
    configure_native(monkeypatch, [scan_row(1)])
    monkeypatch.setattr(corpus, "_ocr_page_once", lambda *args, **kwargs: ocr_result())
    result = corpus.build_corpus(source, tmp_path / "strict-private",
                                 config={"ocr_textual_scan_policy": V3})
    page_file, = (tmp_path / "strict-private").rglob("ocr-page-0001.private.json")
    args = {"source_binding": result["identity"]["source_binding"],
            "config": corpus._config(PARTIAL), "versions": result["identity"]["versions"], "number": 1}
    return source, page_file, args


def test_healthy_prior_v3_cache_is_reused_without_mutating_its_receipt(tmp_path, monkeypatch):
    source, page_file, args = prior_good_page(tmp_path, monkeypatch)
    before = page_file.read_bytes()
    monkeypatch.setattr(corpus, "_ocr_page_once", lambda *args, **kwargs:
                        pytest.fail("Healthy exact prior OCR should not run again"))
    result = corpus.build_corpus(source, tmp_path / "partial-private", config=PARTIAL,
                                 ocr_cache_sources=[page_file.parent])
    assert page_file.read_bytes() == before
    assert result["pages"][0]["ocr"]["ocr_adoption_proof"]["source_receipt_sha256"] == hashlib.sha256(before).hexdigest()
    assert_bound_partial(result, source, unresolved=[])


@pytest.mark.parametrize("difference", ["dpi", "dimension", "language", "threshold", "timeout", "versions", "source"])
def test_cache_adoption_ignores_attempt_policy_but_not_execution_identity(tmp_path, monkeypatch, difference):
    _, page_file, args = prior_good_page(tmp_path, monkeypatch)
    before = page_file.read_bytes()
    if difference == "versions":
        args["versions"] = {"test": "changed"}
    elif difference == "source":
        args["source_binding"] = {**args["source_binding"], "analysis_sha256": "c" * 64}
    else:
        field, value = {"dpi": ("ocr_dpi", 300), "dimension": ("ocr_max_dimension", 3000),
                        "language": ("ocr_languages", "spa"),
                        "threshold": ("minimum_ocr_confidence", 70.),
                        "timeout": ("ocr_timeout_seconds", 30)}[difference]
        args["config"] = {**args["config"], field: value}
    assert adopt_page_ocr([page_file.parent], **args) is None
    assert page_file.read_bytes() == before


def test_corrupt_prior_page_is_not_silently_skipped_or_reused(tmp_path, monkeypatch):
    _, page_file, args = prior_good_page(tmp_path, monkeypatch)
    pinned = json.loads(page_file.read_bytes())
    pinned["result"]["text"] = "Tampered content"
    page_file.write_text(json.dumps(pinned))
    with pytest.raises(corpus.CorpusError, match="integrity"):
        adopt_page_ocr([page_file.parent], **args)


def test_invalid_prior_receipt_json_is_fatal_not_a_page_quality_failure(tmp_path, monkeypatch):
    _, page_file, args = prior_good_page(tmp_path, monkeypatch)
    page_file.write_bytes(b'{"interrupted"')
    with pytest.raises(corpus.CorpusError, match="JSON integrity"):
        adopt_page_ocr([page_file.parent], **args)


def test_invalid_current_receipt_json_is_fatal_in_partial_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "_ocr_page_once", lambda *args, **kwargs: ocr_result(1))
    result, source = build_partial(tmp_path, monkeypatch, [scan_row(1)])
    # This fixture never invokes a real OCR binary.
    private, = (tmp_path / "partial-private").glob("embedding-corpus-*")
    page_file = private / "ocr-page-0001.private.json"
    page_file.write_bytes(b'{"interrupted"')
    with pytest.raises(corpus.CorpusError, match="JSON integrity"):
        corpus._cached_ocr_page(source["pdf_path"], 1, private,
                               result["config"], result["identity"])


def test_partial_mode_preserves_stop_requests_instead_of_omitting_them(tmp_path, monkeypatch):
    def stop(*args, **kwargs):
        raise corpus.CorpusStopped("Stopped")
    monkeypatch.setattr(corpus, "_ocr_page_once", stop)
    with pytest.raises(corpus.CorpusStopped):
        build_partial(tmp_path, monkeypatch, [scan_row(1)])


@pytest.mark.parametrize("tamper", [None, "coverage", "capture", "context"])
def test_actual_partial_retrieval_uses_only_good_pages_with_verified_gap_marker(tmp_path, monkeypatch, tamper):
    def once(pdf, number, private, config, *, psm="3"):
        return ocr_result(number, acceptable=number != 2,
            reasons=["ocr_low_confidence"] if number == 2 else [],
            confident_word_ratio=.4 if number == 2 else .98)
    monkeypatch.setattr(corpus, "_ocr_page_once", once)
    value, _ = build_partial(tmp_path, monkeypatch, [scan_row(p) for p in (1, 2, 3)])
    result = retrieval.retrieve_context(value, [[1., 0.] for _ in value["chunks"]],
        [[1., 0.] for _ in retrieval.DEFAULT_CONFIG["queries"]])
    assert {b["global_page"] for b in result["selected_blocks"]} == {1, 3}
    assert "Pagina2" not in result["context_text"]
    assert "COBERTURA PARCIAL DE LA FUENTE" in result["context_text"]
    assert '"omitted_global_page_ranges":[[2,2]]' in result["context_text"]
    assert result["source_capture"]["captured_pages"] == [1, 3]
    assert result["indexed_complete"] is False and result["needs_review"] is True
    assert retrieval.validate_retrieval(result, value) is result
    if tamper is None:
        return
    if tamper == "coverage":
        result["indexed_complete"] = True
    elif tamper == "capture":
        result["source_capture"]["captured_pages"] = [1, 2, 3]
    else:
        result["context_text"] = result["context_text"].replace("COBERTURA PARCIAL", "COBERTURA COMPLETA")
        result["context_sha256"] = corpus.text_hash(result["context_text"])
        result["context_bytes"] = len(result["context_text"].encode())
        result["context_tokens"] = corpus.token_count(result["context_text"])
    result["retrieval_hash"] = corpus.canonical_hash({k: v for k, v in result.items() if k != "retrieval_hash"})
    with pytest.raises(retrieval.RetrievalError):
        retrieval.validate_retrieval(result, value)
