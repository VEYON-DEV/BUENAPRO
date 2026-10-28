"""v6 reading context: the bases' "Sección Específica", trimmed by relevance. Offline."""
import pytest

from buenapro_worker.prod2 import embedding_retrieval as retrieval
from buenapro_worker.prod2.embedding_corpus import token_count
from test_prod2_embedding_corpus_retrieval import build, query

V6 = retrieval.SPECIFIC_SECTION_POLICY
BASES = [
    "INDICE\nSECCIÓN GENERAL ....... 2\nSECCIÓN ESPECÍFICA ....... 3\nANEXOS ....... 5",
    "SECCIÓN GENERAL\n\nDisposiciones comunes a todos los procedimientos de seleccion.\n\n"
    "Regla general de la ley sobre subcontratacion para cualquier procedimiento.",
    "SECCIÓN ESPECÍFICA\n\nCONDICIONES ESPECIALES DEL PROCEDIMIENTO DE SELECCION\n\n"
    "Requisitos de calificacion: experiencia del postor por 30000 soles en servicios similares.",
    "CAPÍTULO III\n\nREQUERIMIENTO\n\nTerminos de referencia del soporte de la plataforma.\n\n"
    "Subcontratacion no permitida en este procedimiento.",
    "ANEXOS\n\nAnexo 1 Declaracion jurada de datos del postor.\n\nAnexo 2 Pacto de integridad.",
]


def run(corpus, **config):
    queries = [query("subcontracting", "subcontratacion"), query("experience", "experiencia")]
    vectors = [[1., 0.] for _ in corpus["chunks"]]
    effective = {"queries": queries, "semantic_seed_top_k_per_query": 1, **config}
    return retrieval.retrieve_context(corpus, vectors, {"subcontracting": [1., 0.], "experience": [0., 1.]},
                                      config=effective)


def pages_of(result):
    return sorted({block["global_page"] for block in result["selected_blocks"]})


def test_v6_reads_the_specific_section_until_the_annexes(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, BASES)
    result = run(corpus, context_selection_policy=V6)
    assert pages_of(result) == [3, 4]  # Neither the index, the general section nor the annexes.
    assert "Subcontratacion no permitida" in result["context_text"]
    assert "Regla general de la ley" not in result["context_text"]
    assert result["context_selection"]["policy_version"] == V6
    assert result["context_selection"]["specific_section_block_count"] == len(result["selected_blocks"])
    assert result["context_selection"]["trimmed_block_ids"] == []
    assert "context_trimmed_by_relevance" not in result["reasons"]
    retrieval.validate_retrieval(result, corpus)


def test_v6_trims_the_least_related_blocks_instead_of_sending_the_case_to_review(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, BASES)
    whole = run(corpus, context_selection_policy=V6)
    budget = whole["context_tokens"] - 5
    trimmed = run(corpus, context_selection_policy=V6, context_target_tokens=budget, context_maximum_tokens=budget)
    assert trimmed["context_tokens"] <= budget and trimmed["context_selection"]["trimmed_block_ids"]
    assert "context_trimmed_by_relevance" in trimmed["reasons"]
    assert "expanded_context_exceeds_budget_without_truncation" not in trimmed["reasons"]
    # Literal evidence of a qualification query ranks first and stays.
    assert "Subcontratacion no permitida" in trimmed["context_text"]
    kept = [block["block_id"] for block in trimmed["selected_blocks"]]
    assert kept == [block["block_id"] for block in whole["selected_blocks"] if block["block_id"] in kept]
    assert trimmed == run(corpus, context_selection_policy=V6, context_target_tokens=budget,
                          context_maximum_tokens=budget)  # Deterministic.
    retrieval.validate_retrieval(trimmed, corpus)


def test_a_document_without_the_section_keeps_its_retrieval_and_is_trimmed_too(tmp_path, monkeypatch):
    texts = [page.replace("SECCIÓN ESPECÍFICA\n\n", "PARTE DOS\n\n") for page in BASES[1:]]
    corpus, _ = build(tmp_path, monkeypatch, texts)
    v6 = run(corpus, context_selection_policy=V6)
    v5 = run(corpus)
    assert v6["context_selection"]["specific_section_candidates"] == []
    assert pages_of(v6) == pages_of(v5)
    small = v5["context_tokens"] - 5
    trimmed = run(corpus, context_selection_policy=V6, context_target_tokens=small, context_maximum_tokens=small)
    assert trimmed["context_tokens"] <= small and trimmed["needs_review"] is True


def test_an_index_line_is_never_taken_as_the_section(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, [BASES[0], BASES[1]])
    result = run(corpus, context_selection_policy=V6)
    assert result["context_selection"]["specific_section_candidates"] == []


def test_without_the_policy_nothing_changes(tmp_path, monkeypatch):
    corpus, _ = build(tmp_path, monkeypatch, BASES)
    result = run(corpus)
    assert "context_selection" not in result and "context_selection_policy" not in result["config"]
    small = result["context_tokens"] - 5
    over = run(corpus, context_target_tokens=small, context_maximum_tokens=small)
    assert "expanded_context_exceeds_budget_without_truncation" in over["reasons"]
    assert over["context_tokens"] == result["context_tokens"]  # v5 never cuts.


@pytest.mark.parametrize("value", ["", "prod2_specific_section_ranked_v2", None])
def test_an_unknown_selection_policy_is_rejected(tmp_path, monkeypatch, value):
    corpus, _ = build(tmp_path, monkeypatch, BASES)
    with pytest.raises(retrieval.RetrievalError):
        run(corpus, context_selection_policy=value)


def test_token_budget_helper_keeps_document_order():
    blocks = [{"id": str(n), "text": "palabra " * 50, "global_page": n} for n in range(6)]
    render = lambda chosen: " ".join(block["text"] for block in chosen)
    score = {"0": 0.9, "1": 0.1, "2": 0.8, "3": 0.2, "4": 0.7, "5": 0.3}
    kept, dropped = retrieval._trim_to_budget(blocks, token_count(render(blocks[:3])) + 5, render, score)
    assert [block["id"] for block in kept] == ["0", "2", "4"] and sorted(dropped) == ["1", "3", "5"]
