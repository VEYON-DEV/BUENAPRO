"""Explicit finite context recovery and equivalent per-call memoization. Offline."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from buenapro_worker.prod2 import embedding_retrieval as retrieval
from test_prod2_embedding_corpus_retrieval import build, query


def arguments(corpus):
    queries = [query("one", "subcontratacion"), query("two", "experiencia")]
    config = {"queries": queries, "semantic_seed_top_k_per_query": 1}
    vectors = [[1., float(index % 2)] for index, _ in enumerate(corpus["chunks"])]
    questions = {"one": [1., 0.], "two": [0., 1.]}
    return vectors, questions, config


def source(tmp_path, monkeypatch, *, other=False):
    texts = [
        "1. OBJETO\nSubcontratacion permitida segun numeral 2.\nExperiencia conforme anexo I.",
        "2. EXPERIENCIA\nExperiencia del postor de 30000 soles. Consultar pagina 3.",
        "ANEXO I\nExperiencia se acredita con contratos. Subcontratacion: autorizacion previa.",
        "Otra condicion de ejecucion sin referencia." if other else
        "ANEXO II\nExperiencia no requerida para otra prestacion. Ver anexo I.",
    ]
    tmp_path.mkdir(exist_ok=True)
    corpus, _ = build(tmp_path, monkeypatch, texts)
    return corpus


def test_memoization_preserves_full_result_and_hash_for_references_and_coverage(tmp_path, monkeypatch):
    corpus = source(tmp_path, monkeypatch)
    vectors, questions, config = arguments(corpus)
    original = deepcopy(corpus)
    ordinary = retrieval.retrieve_context(corpus, vectors, questions, config=config)
    memoized = retrieval.retrieve_context(corpus, vectors, questions, config=config, memoize_expansion=True)
    assert memoized == ordinary
    assert memoized["retrieval_hash"] == ordinary["retrieval_hash"]
    assert corpus == original
    assert memoized["references"]
    retrieval.validate_retrieval(memoized, corpus)


def test_default_has_no_memoization_or_new_config_key(tmp_path, monkeypatch):
    corpus = source(tmp_path, monkeypatch)
    vectors, questions, config = arguments(corpus)
    def forbidden(_):
        pytest.fail("Historical default unexpectedly allocated a memoization scope")
    monkeypatch.setattr(retrieval, "_ExpansionMemo", forbidden)
    result = retrieval.retrieve_context(corpus, vectors, questions, config=config)
    assert "context_expansion_policy" not in result["config"]
    assert result == retrieval.retrieve_context(corpus, vectors, questions,
        config=config, memoize_expansion=False)


def test_independent_concurrent_memoization_cannot_cross_corpus_boundaries(tmp_path, monkeypatch):
    first = source(tmp_path / "first", monkeypatch)
    second = source(tmp_path / "second", monkeypatch, other=True)
    corpora = [first, second, first, second]
    def run(corpus, enabled):
        vectors, questions, config = arguments(corpus)
        return retrieval.retrieve_context(corpus, vectors, questions,
            config=config, memoize_expansion=enabled)
    expected = [run(corpus, False) for corpus in corpora]
    with ThreadPoolExecutor(max_workers=2) as pool:
        actual = list(pool.map(lambda corpus: run(corpus, True), corpora))
    assert actual == expected
    assert actual[0]["corpus_hash"] != actual[1]["corpus_hash"]


def test_pure_expansion_caches_are_finite_and_original_functions_remain_pure():
    memo = retrieval._ExpansionMemo([])
    for number in range(33000):
        assert memo.normal(f"SECCION {number}") == retrieval._normal(f"SECCION {number}")
    assert memo.normal.cache_info().maxsize == 32768
    assert memo.normal.cache_info().currsize == 32768
    assert memo.page_refs.cache_info().maxsize == 8192
    assert memo.named_refs.cache_info().maxsize == 8192
    assert memo._heading.cache_info().maxsize == 65536


@pytest.mark.parametrize("value", [None, 1, 0, "true", []])
def test_memoization_requires_explicit_boolean(value):
    with pytest.raises(retrieval.RetrievalError, match="explicit boolean"):
        retrieval.retrieve_context({}, [], [], memoize_expansion=value)


def test_historical_default_and_100k_limit_are_unchanged():
    assert retrieval._config(None) == retrieval.DEFAULT_CONFIG
    assert "context_expansion_policy" not in retrieval._config(None)
    assert retrieval._config({"context_maximum_tokens": 100000})["context_maximum_tokens"] == 100000
    with pytest.raises(retrieval.RetrievalError, match="hard bounds"):
        retrieval._config({"context_maximum_tokens": 100001})


@pytest.mark.parametrize("value", [None, "", "expanded", True, 1])
def test_invalid_context_expansion_policy_is_rejected(value):
    with pytest.raises(retrieval.RetrievalError, match="expanded context policy"):
        retrieval._config({"context_expansion_policy": value})


def test_explicit_recovery_admits_at_most_350k_without_relaxing_structural_rules(tmp_path, monkeypatch):
    corpus = source(tmp_path, monkeypatch)
    vectors, questions, config = arguments(corpus)
    expanded = {**config, "context_maximum_tokens": 350000,
                "context_expansion_policy": retrieval.EXPANDED_CONTEXT_POLICY}
    result = retrieval.retrieve_context(corpus, vectors, questions, config=expanded, memoize_expansion=True)
    original = retrieval.retrieve_context(corpus, vectors, questions, config=config)
    assert result["context_text"] == original["context_text"]
    assert result["selected_blocks"] == original["selected_blocks"]
    assert result["citations"] == original["citations"]
    assert result["references"] == original["references"]
    assert result["unresolved_reference_ids"] == original["unresolved_reference_ids"]
    assert result["review_reasons"] == original["review_reasons"]
    assert result["config"]["context_maximum_tokens"] == 350000
    assert result["retrieval_hash"] != original["retrieval_hash"]
    retrieval.validate_retrieval(result, corpus)
    with pytest.raises(retrieval.RetrievalError, match="hard bounds"):
        retrieval._config({**expanded, "context_maximum_tokens": 350001})
    for field in ("expand_complete_sections", "include_complete_tables", "include_literal_matches"):
        with pytest.raises(retrieval.RetrievalError, match="cannot be disabled"):
            retrieval._config({**expanded, field: False})
