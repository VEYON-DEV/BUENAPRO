from copy import deepcopy
import json

import pytest

from buenapro_worker.prod2 import embedding_corpus as corpus
from buenapro_worker.prod2.embedding_ocr_recovery import adopt_page_ocr, contains_raw_tsv_rows


def origin(tmp_path, *, text="Experiencia del postor y personal clave.", acceptable=True):
    old = corpus._config({"ocr_textual_scan_policy": "prod2_confident_text_layout_review_v2"})
    new = {**old, "ocr_textual_scan_policy": "prod2_confident_text_layout_review_v3"}
    source = {"page_count": 1, "analysis_sha256": "a" * 64}
    versions = {"tesseract": "fixture-v1"}
    key = {"version": "prod2_local_page_ocr_cache_v1", "global_page": 1,
           "source_binding": source, "config": old, "versions": versions}
    result = {"text": text, "acceptable": acceptable, "reasons": [] if acceptable else ["ocr_low_confidence"],
              "mean_confidence": 93, "confident_word_ratio": .98}
    pinned = {"identity": key, "identity_hash": corpus.canonical_hash(key), "result": result}
    pinned["cache_hash"] = corpus.canonical_hash(pinned)
    folder = tmp_path / "source"
    folder.mkdir()
    path = folder / "ocr-page-0001.private.json"
    path.write_text(json.dumps(pinned))
    args = {"source_binding": source, "config": new, "versions": versions, "number": 1}
    return folder, path, pinned, args


def test_exact_good_ocr_is_adopted_with_immutable_provenance(tmp_path):
    folder, path, pinned, args = origin(tmp_path)
    before = path.read_bytes()
    result, proof = adopt_page_ocr([folder], **args)
    assert result == pinned["result"]
    assert proof["source_cache_hash"] == pinned["cache_hash"]
    assert proof["original_ocr_policy"].endswith("v2")
    assert path.read_bytes() == before


@pytest.mark.parametrize("difference", ["source", "versions", "ocr_options"])
def test_different_source_or_ocr_execution_cannot_reuse_text(tmp_path, difference):
    folder, _, _, args = origin(tmp_path)
    if difference == "source":
        args["source_binding"] = {**args["source_binding"], "analysis_sha256": "b" * 64}
    elif difference == "versions":
        args["versions"] = {"tesseract": "changed"}
    else:
        args["config"] = {**args["config"], "ocr_dpi": 300}
    assert adopt_page_ocr([folder], **args) is None


@pytest.mark.parametrize("acceptable,text", [(False, "Lectura incierta"),
    (True, 'Una comilla "\n5\t1\t1\t1\t1\t4\t50\t70\t100\t20\t96.1\tExperiencia')])
def test_known_bad_and_false_pass_tsv_text_are_not_adopted(tmp_path, acceptable, text):
    folder, _, _, args = origin(tmp_path, text=text, acceptable=acceptable)
    assert adopt_page_ocr([folder], **args) is None


def test_prior_receipt_corruption_is_not_treated_as_free_missing_cache(tmp_path):
    folder, path, pinned, args = origin(tmp_path)
    pinned["result"]["text"] = "Alterado"
    path.write_text(json.dumps(pinned))
    with pytest.raises(corpus.CorpusError, match="integrity"):
        adopt_page_ocr([folder], **args)


def test_symlink_origin_and_invalid_policy_are_rejected(tmp_path):
    folder, _, _, args = origin(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError, match="literal"):
        adopt_page_ocr([alias], **args)
    args["config"] = {**args["config"], "ocr_textual_scan_policy": "prod2_confident_text_layout_review_v2"}
    with pytest.raises(corpus.CorpusError, match="explicit V3"):
        adopt_page_ocr([folder], **args)


def test_real_tsv_row_shape_is_distinct_from_normal_tabular_text():
    assert contains_raw_tsv_rows('Nombre\n5\t1\t2\t3\t4\t5\t6\t7\t8\t9\t87.0\tDatos')
    assert not contains_raw_tsv_rows("Experiencia\tMonto\nServicio\t100000")


def test_tesseract_raw_quotes_never_merge_tsv_rows_in_v3():
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    data = (header + '5\t1\t1\t1\t1\t1\t10\t20\t10\t10\t92\t"\n'
            + '5\t1\t1\t1\t1\t2\t25\t20\t60\t10\t97\tExperiencia\n'
            + '5\t1\t1\t1\t1\t3\t90\t20\t30\t10\t98\tpostor\n').encode()
    v3 = corpus._config({'ocr_textual_scan_policy': 'prod2_confident_text_layout_review_v3'})
    result = corpus._parse_tsv(data, v3)
    assert result['word_count'] == 3
    assert 'Experiencia postor' in result['text']
    assert not contains_raw_tsv_rows(result['text'])
    legacy = corpus._parse_tsv(data, corpus._config(None))
    assert legacy['word_count'] == 1  # Historical behavior stays versioned.


def test_retries_only_bad_pages_and_stops_after_orientation_recovery(monkeypatch, tmp_path):
    calls = []
    def once(pdf, number, private, config, *, psm='3'):
        calls.append((config['ocr_dpi'], psm))
        good = psm == '1'
        return {'text': 'Experiencia profesional', 'word_count': 2,
                'mean_confidence': 95 if good else 50, 'confident_word_ratio': .99 if good else .4,
                'acceptable': good, 'reasons': [] if good else ['ocr_low_confidence'],
                'cleaned_render_bytes': 100}
    monkeypatch.setattr(corpus, '_ocr_page_once', once)
    config = corpus._config({'ocr_textual_scan_policy': 'prod2_confident_text_layout_review_v3'})
    result = corpus._ocr_page(tmp_path/'pdf', 1, tmp_path, config)
    assert calls == [(180, '3'), (300, '3'), (300, '1')]
    assert result['acceptable'] and result['cleaned_render_bytes'] == 300
    assert len(result['ocr_attempts']) == 3


def test_local_retry_never_turns_subthreshold_results_into_success(monkeypatch, tmp_path):
    calls = []
    def once(pdf, number, private, config, *, psm='3'):
        calls.append(psm)
        return {'text': 'Requisito', 'word_count': 1, 'mean_confidence': 90,
                'confident_word_ratio': .849, 'acceptable': False,
                'reasons': ['ocr_low_confidence'], 'cleaned_render_bytes': 10}
    monkeypatch.setattr(corpus, '_ocr_page_once', once)
    result = corpus._ocr_page(tmp_path/'pdf', 1, tmp_path,
        corpus._config({'ocr_textual_scan_policy': 'prod2_confident_text_layout_review_v3'}))
    assert calls == ['3', '3', '1', '6']
    assert not result['acceptable'] and not corpus._strong_ocr_text(result, corpus.DEFAULT_CONFIG)
