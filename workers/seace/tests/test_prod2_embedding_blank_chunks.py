"""Offline regression contracts for whitespace-only embedding fragments.

Inputs are synthetic. The real corpus builder, tokenizer, hashes, page bindings,
cache replay and persistence validator run; PDF extraction is the only stub.
No provider, database, source service or ingestion corpus is touched.
"""
from copy import deepcopy
import hashlib
import io

from pypdf import PdfWriter
import pytest

from buenapro_worker.prod2 import embedding_corpus as corpus
from buenapro_worker.prod2 import embedding_store as store


def _prepared(tmp_path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    pdf = tmp_path / "synthetic-analysis.pdf"
    pdf.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "pdf_path": pdf, "analysis_sha256": digest,
        "analysis_bytes": len(data), "page_count": pages,
        "source_document_manifest": [{
            "candidate_id": "synthetic-source", "document_uuid": "synthetic-official",
            "original_sha256": digest, "analysis_sha256": digest,
            "filename": "synthetic-bases.pdf", "start_page": 1,
            "end_page": pages, "page_offset": 0,
        }],
    }


def _native(text, *, page=1, tables=()):
    return {
        "global_page": page, "text": text, "tables": list(tables),
        "width": 600, "height": 800, "images": [], "image_area_ratio": 0,
        "native_characters": sum(not char.isspace() for char in text),
        "native_unicode_corrupt": False, "vector_count": 0,
        "largest_vector_area_ratio": 0, "meaningful_annotations": 0,
        "unknown_object_types": [],
    }


def _table(rows):
    return {"ordinal": 0, "bbox": [40, 100, 400, 200], "rows": rows}


def _build(tmp_path, monkeypatch, rows, *, config=None):
    source = _prepared(tmp_path, len(rows))
    monkeypatch.setattr(corpus, "_extract_native", lambda *_: deepcopy(rows))
    monkeypatch.setattr(corpus, "_versions", lambda *_: {"test": "synthetic-offline"})
    result = corpus.build_corpus(source, tmp_path / "private", config=config)
    return result, source


def _block(text, *, page=1, kind="paragraph"):
    locations = [{
        "source_page": page, "document_uuid": "synthetic-official",
        "candidate_id": "synthetic-source", "original_sha256": "a" * 64,
        "analysis_sha256": "b" * 64,
    }]
    return corpus._block(
        text, page, locations, "synthetic-section", kind=kind,
        table="c" * 64 if kind == "table" else None,
    )


@pytest.mark.parametrize("cells", [["", ""], [""], [" \t", "\t"], ["\u00a0"]])
def test_real_builder_preserves_empty_table_but_never_embeds_it(tmp_path, monkeypatch, cells):
    rows = [[cell] for cell in cells]
    result, source = _build(tmp_path, monkeypatch, [
        _native("Experiencia tecnica del postor.", tables=[_table(rows)]),
    ])
    table, = [block for block in result["blocks"] if block["kind"] == "table"]
    assert table["text"] == "\n".join(cells)
    assert table["table_rows"] == rows
    assert table["table_bbox"] == [40, 100, 400, 200]
    assert table["locations"][0]["document_uuid"] == "synthetic-official"
    assert table["source_page"] == table["global_page"] == 1
    assert table["source_sha256"] == source["analysis_sha256"]
    assert all(chunk["text"].strip() for chunk in result["chunks"])
    assert all(table["id"] not in chunk["block_ids"] for chunk in result["chunks"])
    assert result["pages"][0]["table_count"] == 1
    assert result["coverage_complete"] is True
    assert result["source_tokens"] == sum(corpus.token_count(b["text"]) for b in result["blocks"])
    assert corpus.validate_corpus(result) is result
    assert store._corpus_chunks(result) is result["chunks"]


def test_real_builder_skips_whitespace_tail_after_token_fragmentation(tmp_path, monkeypatch):
    useful = "hello" * 128
    text = useful + "\n"
    fragments = corpus._split_tokens(text, 128, 0)
    assert fragments == [(useful, 0, len(useful)), ("\n", len(useful), len(text))]
    result, _ = _build(tmp_path, monkeypatch, [
        _native("Requisitos tecnicos.", tables=[_table([[useful], [""]])]),
    ], config={"chunk_target_tokens": 128, "overlap_tokens": 0})
    table, = [block for block in result["blocks"] if block["kind"] == "table"]
    assert table["text"] == text
    chunk, = [c for c in result["chunks"] if table["id"] in c["block_ids"]]
    assert chunk["text"] == useful
    assert chunk["ordinal"] == 0
    assert chunk["block_ranges"] == [{
        "block_id": table["id"], "start_char": 0, "end_char": len(useful),
    }]
    assert store._corpus_chunks(result) is result["chunks"]


def test_interior_whitespace_fragments_do_not_renumber_useful_ordinals_or_drop_text():
    text = "hello" * 128 + "\n" * 4096 + "end"
    block = _block(text, kind="table")
    original = deepcopy(block)
    config = corpus._config({"chunk_target_tokens": 128, "overlap_tokens": 0})
    fragments = corpus._split_tokens(text, 128, 0)
    assert [bool(piece.strip()) for piece, _, _ in fragments] == [True, False, True]
    chunks = corpus._make_chunks([block], config)
    assert [c["ordinal"] for c in chunks] == [0, 2]
    assert [c["group_ordinal"] for c in chunks] == [0, 0]
    assert block == original
    covered = set()
    for chunk in chunks:
        location, = chunk["block_ranges"]
        first, last = location["start_char"], location["end_char"]
        assert chunk["text"] == text[first:last]
        assert chunk["block_ids"] == [block["id"]]
        assert chunk["global_pages"] == [1]
        assert chunk["table_id"] == block["table_id"]
        assert chunk["text_sha256"] == hashlib.sha256(chunk["text"].encode()).hexdigest()
        covered.update(range(first, last))
    assert {i for i, char in enumerate(text) if not char.isspace()} <= covered


@pytest.mark.parametrize("kind", ["paragraph", "table"])
@pytest.mark.parametrize("text", ["", "\n", "\t \u00a0"])
def test_blank_block_stays_intact_without_producing_embedding_inputs(kind, text):
    block = _block(text, kind=kind)
    original = deepcopy(block)
    assert corpus._make_chunks([block], corpus._config(None)) == []
    assert block == original


def test_useful_chunk_text_provenance_and_preexisting_ids_survive_blank_middle_group():
    blocks = [
        _block("  Experiencia tecnica del postor.\t", page=1),
        _block("\n", page=2, kind="table"),
        _block("\tPersonal tecnico requerido.  ", page=3),
    ]
    original = deepcopy(blocks)
    chunks = corpus._make_chunks(blocks, corpus._config(None))
    assert blocks == original
    assert [c["text"] for c in chunks] == [blocks[0]["text"], blocks[2]["text"]]
    assert [c["group_ordinal"] for c in chunks] == [0, 2]
    assert [c["ordinal"] for c in chunks] == [0, 0]
    # Pinned identities from the same useful chunks before the whitespace guard.
    assert [c["chunk_id"] for c in chunks] == [
        "ee96d6eadfba4f1a46a2fb6ffed6c8e1ef4c615f0b59b7b9f582ba27cdcd077d",
        "0ad3c1825d8a053e8d7bcca790f73ce9f872582dfe2c854250a615f0e42a04d5",
    ]
    for chunk, block in zip(chunks, [blocks[0], blocks[2]], strict=True):
        assert chunk["block_ids"] == [block["id"]]
        assert chunk["global_pages"] == [block["global_page"]]
        assert chunk["block_ranges"] == [{
            "block_id": block["id"], "start_char": 0, "end_char": len(block["text"]),
        }]
        assert chunk["text_sha256"] == block["text_sha256"]


def test_blank_only_corpus_remains_ineligible_for_paid_embeddings(tmp_path, monkeypatch):
    result, _ = _build(tmp_path, monkeypatch, [_native("", tables=[_table([[""], [""]])])])
    assert result["blocks"] and result["chunks"] == []
    assert corpus.validate_corpus(result) is result
    with pytest.raises(store.EmbeddingPersistenceError, match="Corpus chunks required"):
        store._corpus_chunks(result)


def test_persisted_blank_chunk_is_still_rejected_instead_of_silently_rewritten():
    blank = {"chunk_id": "a" * 64, "text": "\n", "text_sha256": corpus.text_hash("\n")}
    value = {"corpus_hash": "b" * 64, "chunks": [blank]}
    original = deepcopy(value)
    with pytest.raises(store.EmbeddingPersistenceError, match="Corpus exact text missing"):
        store._corpus_chunks(value)
    assert value == original


def test_delimiter_only_table_is_not_removed_by_a_semantic_heuristic(tmp_path, monkeypatch):
    result, _ = _build(tmp_path, monkeypatch, [
        _native("Requisitos tecnicos.", tables=[_table([["", ""]])]),
    ])
    table, = [b for b in result["blocks"] if b["kind"] == "table"]
    assert table["text"] == " | "
    chunk, = [c for c in result["chunks"] if table["id"] in c["block_ids"]]
    assert chunk["text"] == table["text"]


def test_healthy_corpus_replay_keeps_paid_input_ids_hash_and_receipt_bytes(tmp_path, monkeypatch):
    result, source = _build(tmp_path, monkeypatch, [
        _native("Experiencia tecnica del postor.", tables=[_table([["Perfil", "Requisito"]])]),
        _native("Personal tecnico requerido.", page=2),
    ])
    marker, = (tmp_path / "private").rglob("corpus.private.json")
    before = marker.read_bytes()
    source_before = source["pdf_path"].read_bytes()
    monkeypatch.setattr(corpus, "_extract_native", lambda *_: pytest.fail("Healthy replay re-extracted source"))
    cached = corpus.build_corpus(source, tmp_path / "private")
    assert cached["cache_hit"] is True
    assert cached["corpus_hash"] == result["corpus_hash"]
    assert cached["chunks"] == result["chunks"]
    assert cached["blocks"] == result["blocks"]
    assert marker.read_bytes() == before
    assert source["pdf_path"].read_bytes() == source_before
    assert store._corpus_chunks(cached) is cached["chunks"]
