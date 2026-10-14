"""Selected source provenance, lossless transport and exact replay; no services."""
import base64
import hashlib
import io
import json
from pathlib import Path
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

from buenapro_worker.prod2 import pdf_preparation as subject


def pdf_bytes(count=1, *, label="REQUIREMENT", payload=0):
    writer = PdfWriter()
    randomizer = random.Random(label)
    for number in range(count):
        page = writer.add_blank_page(width=612 + number, height=792 + number)
        page[NameObject("/OriginalPage")] = TextStringObject(f"{label}-{number + 1}")
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 740 Td ({label} {number + 1}) Tj ET\n".encode()
            + b"%" + base64.b64encode(randomizer.randbytes(payload)) + b"\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def candidate(folder, number, *, count=1, label="REQUIREMENT", payload=0,
              source_kind="loose", suffix=".pdf"):
    code = f"00000000-0000-4000-8000-{number:012d}"
    content = pdf_bytes(count, label=label, payload=payload)
    filename = f"selected-{number}{suffix}"
    row = {"candidate_id": f"{source_kind}:{code}:{filename}", "document_uuid": code,
        "source_kind": source_kind, "filename": filename, "original_sha256": hashlib.sha256(content).hexdigest(),
        "type": "Documento", "stage": "Convocatoria", "published_at": "06/10/2026 12:00",
        "source_url": "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc?id=" + code}
    if source_kind == "archive_member":
        row.update(parent_filename="parent.zip", member_path="documents/" + filename,
            archive_sha256="a" * 64, member_sha256=row["original_sha256"])
    path = folder / (code + suffix)
    path.write_bytes(content)
    return row, path


@pytest.fixture(autouse=True)
def local_pdf_only(monkeypatch):
    # These tests exercise pypdf only. Real DOC conversion has its own local smoke.
    def prepare(path, extension, **kwargs):
        assert extension in {"pdf", "doc", "docx"}
        return path
    monkeypatch.setattr(subject, "prepare_pdf", prepare)


def build(folder, pairs, *, context=None, **kwargs):
    return subject.prepare_selected_document_set(folder, [row for row, _ in pairs],
        {row["candidate_id"]: path for row, path in pairs},
        context or {"official_inventory": [{"uuid": row["document_uuid"]} for row, _ in pairs]}, **kwargs)


def test_loose_and_archive_sources_keep_selection_order_global_ranges_and_hashes(tmp_path):
    loose = candidate(tmp_path, 1, count=2, label="LOOSE")
    archive = candidate(tmp_path, 2, count=3, label="ARCHIVE", source_kind="archive_member")
    result = build(tmp_path, [archive, loose])
    reader = PdfReader(result["pdf_path"])
    assert [page["/OriginalPage"] for page in reader.pages] == [
        "ARCHIVE-1", "ARCHIVE-2", "ARCHIVE-3", "LOOSE-1", "LOOSE-2"]
    assert result["page_count"] == 5
    assert result["document_scope"] == "prod2_selected_documents_v1"
    assert result["document_set_hash"] == result["selection_hash"]
    assert subject.canonical_hash(result["document_set_identity"]) == result["selection_hash"]
    assert result["document_set_identity"]["selection_context"] == result["selection_context"]
    assert [(row["start_page"], row["end_page"], row["page_offset"])
        for row in result["source_document_manifest"]] == [(1, 3, 0), (4, 5, 3)]
    first = result["source_document_manifest"][0]
    assert first["document_uuid"] == archive[0]["document_uuid"]
    assert first["member_path"] == archive[0]["member_path"]
    assert first["archive_sha256"] == "a" * 64
    assert first["member_sha256"] == first["original_sha256"]
    assert result["analysis_sha256"] == hashlib.sha256(result["pdf_path"].read_bytes()).hexdigest()
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["pdf_path"].read_bytes() == result["pdf_path"].read_bytes()
    assert [loose[1].read_bytes(), archive[1].read_bytes()] == [
        pdf_bytes(2, label="LOOSE"), pdf_bytes(3, label="ARCHIVE")]


def test_exact_duplicate_pdf_dedup_keeps_both_parent_and_member_provenance(tmp_path):
    first = candidate(tmp_path, 1, count=2)
    duplicate = candidate(tmp_path, 2, count=2, source_kind="archive_member")
    result = build(tmp_path, [first, duplicate])
    assert result["page_count"] == 2 and len(result["document_set"]) == 2
    left, right = result["source_document_manifest"]
    assert right["duplicate_of"] == left["candidate_id"]
    assert (right["start_page"], right["end_page"]) == (1, 2)
    assert right["document_uuid"] != left["document_uuid"]
    assert right["source_kind"] == "archive_member"


def test_lossless_optimization_reduces_stream_bytes_without_removing_text_or_geometry(tmp_path):
    pair = candidate(tmp_path, 1, count=3, payload=5000)
    plain = build(tmp_path, [pair], optimize_lossless=False)
    optimized = build(tmp_path, [pair])
    assert optimized["optimization_applied"]
    assert optimized["analysis_bytes"] < plain["analysis_bytes"]
    before, after = PdfReader(plain["pdf_path"]), PdfReader(optimized["pdf_path"])
    assert len(before.pages) == len(after.pages) == 3
    for original, derivative in zip(before.pages, after.pages):
        assert original.get_contents().get_data() == derivative.get_contents().get_data()
        assert original.extract_text() == derivative.extract_text()
        assert original.mediabox == derivative.mediabox


def test_lossless_optimization_never_accepts_larger_output(monkeypatch):
    original = pdf_bytes()
    monkeypatch.setattr(subject, "_serialize", lambda writer: original + b"% larger\n")
    assert subject._lossless_optimization(original) is original


def test_source_and_transport_limits_are_separate_and_every_page_is_partitioned(tmp_path):
    pair = candidate(tmp_path, 1, count=7, payload=2400)
    result = build(tmp_path, [pair], max_source_bytes=100_000,
                   max_analysis_bytes=8000, optimize_lossless=False)
    assert pair[1].stat().st_size > 8000
    assert result["pdf_path"].stat().st_size > 8000
    assert len(result["chunks"]) > 1 and result["page_count"] == 7
    expected, source = 1, PdfReader(result["pdf_path"])
    for chunk in result["chunks"]:
        content = chunk["pdf_path"].read_bytes()
        assert len(content) <= 8000 and chunk["bytes"] == len(content)
        assert chunk["sha256"] == hashlib.sha256(content).hexdigest()
        assert chunk["start_page"] == expected and chunk["page_offset"] == expected - 1
        pages = PdfReader(io.BytesIO(content)).pages
        assert len(pages) == chunk["page_count"]
        for index, page in enumerate(pages, expected - 1):
            original = source.pages[index]
            assert page["/OriginalPage"] == original["/OriginalPage"]
            assert page.mediabox == original.mediabox
            assert page.get_contents().get_data() == original.get_contents().get_data()
        expected = chunk["end_page"] + 1
    assert expected == 8


def test_single_oversized_page_blocks_complete_result_not_partial_pages(tmp_path):
    pair = candidate(tmp_path, 1, count=2, payload=5000)
    with pytest.raises(subject.PdfPreparationError, match="lossless transport"):
        build(tmp_path, [pair], max_source_bytes=100_000, max_analysis_bytes=1000, optimize_lossless=False)
    assert not list(tmp_path.glob("selected-analysis-*/manifest.private.json"))


def test_both_word_formats_use_source_limit_and_uuid_isolation(monkeypatch, tmp_path):
    pairs = [candidate(tmp_path, 1, label="DOC", suffix=".doc"),
             candidate(tmp_path, 2, label="DOCX", suffix=".docx")]
    calls = []
    def convert(path, extension, *, max_bytes):
        assert max_bytes == 100_000
        assert path.parent.parent.name in {row["document_uuid"] for row, _ in pairs}
        assert extension in {"doc", "docx"}
        calls.append(extension)
        target = path.parent / "converted.pdf"
        target.write_bytes(path.read_bytes())
        return target
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    result = build(tmp_path, pairs, max_source_bytes=100_000, max_analysis_bytes=8000)
    assert calls == ["doc", "docx"] and result["page_count"] == 2


def test_explicit_word_policy_has_distinct_identity_and_durable_provenance_without_changing_legacy(monkeypatch, tmp_path):
    pair = candidate(tmp_path, 1, suffix=".docx")
    original = pair[1].read_bytes()
    calls = []
    def convert(path, extension, *, max_bytes, **kwargs):
        calls.append(kwargs)
        profile = path.parent / ("profile-explicit" if kwargs else "profile-legacy")
        converted = profile / "converted"
        converted.mkdir(parents=True)
        result = converted / "source.pdf"
        result.write_bytes(original)
        if kwargs:
            assert kwargs == {"word_conversion_policy": subject.WORD_FOOTNOTES_AT_END_POLICY}
            (profile / "word-conversion.private.json").write_text(json.dumps({
                "policy": subject.WORD_FOOTNOTES_AT_END_POLICY, "original_placement": "page",
                "derived_placement": "document", "only_placement_attribute_changed": True,
                "footnote_count": 3, "native_fodt_sha256": "a" * 64, "derived_fodt_sha256": "b" * 64,
                "conversion_source_sha256": hashlib.sha256(original).hexdigest(),
                "pdf_sha256": hashlib.sha256(original).hexdigest(), "pdf_bytes": len(original)}))
        return result
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    legacy = build(tmp_path, [pair])
    explicit = build(tmp_path, [pair], word_conversion_policy=subject.WORD_FOOTNOTES_AT_END_POLICY)
    replay = build(tmp_path, [pair], word_conversion_policy=subject.WORD_FOOTNOTES_AT_END_POLICY)
    assert calls == [{}, {"word_conversion_policy": subject.WORD_FOOTNOTES_AT_END_POLICY}]
    assert "word_conversion_policy" not in legacy["document_set_identity"]
    assert "word_conversion" not in legacy["source_document_manifest"][0]
    assert explicit["document_set_identity"]["word_conversion_policy"] == subject.WORD_FOOTNOTES_AT_END_POLICY
    assert explicit["selection_hash"] != legacy["selection_hash"]
    assert explicit["selection_hash"] == replay["selection_hash"]
    assert explicit["analysis_sha256"] == legacy["analysis_sha256"]
    assert explicit["source_document_manifest"][0]["word_conversion"]["footnote_count"] == 3
    assert explicit["source_document_manifest"] == replay["source_document_manifest"]
    assert pair[1].read_bytes() == original
    old_identity = {key: value for key, value in explicit["document_set_identity"].items() if key != "word_conversion_policy"}
    assert subject.canonical_hash(old_identity) == legacy["selection_hash"]


def test_explicit_word_policy_requires_matching_conversion_provenance(monkeypatch, tmp_path):
    pair = candidate(tmp_path, 1, suffix=".doc")
    def convert(path, extension, **kwargs):
        profile = path.parent / "profile"
        converted = profile / "converted"
        converted.mkdir(parents=True)
        result = converted / "source.pdf"
        result.write_bytes(path.read_bytes())
        (profile / "word-conversion.private.json").write_text(json.dumps({
            "policy": subject.WORD_FOOTNOTES_AT_END_POLICY, "pdf_sha256": "0" * 64}))
        return result
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    with pytest.raises(subject.PdfPreparationError, match="provenance does not match"):
        build(tmp_path, [pair], word_conversion_policy=subject.WORD_FOOTNOTES_AT_END_POLICY)
    assert not list(tmp_path.glob("selected-analysis-*/manifest.private.json"))


def test_unknown_word_policy_fails_before_source_or_converter(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("An unknown policy must not touch conversion")
    monkeypatch.setattr(subject, "prepare_pdf", forbidden)
    with pytest.raises(subject.PdfPreparationError, match="Unsupported explicit Word conversion policy"):
        subject.prepare_selected_document_set(tmp_path / "missing", [], {}, {}, word_conversion_policy="guess")


def test_changed_original_during_conversion_never_pins_analysis(monkeypatch, tmp_path):
    pair = candidate(tmp_path, 1)
    def mutate(path, extension, **kwargs):
        pair[1].write_bytes(b"changed during conversion")
        return path
    monkeypatch.setattr(subject, "prepare_pdf", mutate)
    with pytest.raises(subject.PdfPreparationError, match="source hash changed"):
        build(tmp_path, [pair])
    assert not list(tmp_path.glob("selected-analysis-*/manifest.private.json"))


def test_conversion_cannot_modify_private_original_copy(monkeypatch, tmp_path):
    pair = candidate(tmp_path, 1)
    def mutate(path, extension, **kwargs):
        path.write_bytes(pdf_bytes(label="CHANGED"))
        return path
    monkeypatch.setattr(subject, "prepare_pdf", mutate)
    with pytest.raises(subject.PdfPreparationError, match="source hash changed"):
        build(tmp_path, [pair])


def test_exact_replay_uses_verified_derivatives_without_reconverting(monkeypatch, tmp_path):
    pair = candidate(tmp_path, 1, count=2)
    result = build(tmp_path, [pair])
    def forbidden(*args, **kwargs):
        pytest.fail("Pinned selection replay must not reconvert")
    monkeypatch.setattr(subject, "prepare_pdf", forbidden)
    replay = build(tmp_path, [pair])
    assert replay["analysis_sha256"] == result["analysis_sha256"]
    assert replay["pdf_path"] == result["pdf_path"]
    result["chunks"][0]["pdf_path"].write_bytes(b"tampered")
    with pytest.raises(subject.PdfPreparationError, match="Pinned preparation"):
        build(tmp_path, [pair])


def test_shared_cache_reuses_word_derivative_across_runs_and_source_revision_invalidates(monkeypatch, tmp_path):
    one, two, cache = tmp_path / "run-one", tmp_path / "run-two", tmp_path / "shared-cache"
    one.mkdir()
    two.mkdir()
    first, second = candidate(one, 1, suffix=".doc"), candidate(two, 1, suffix=".doc")
    calls = []
    def convert(path, extension, **kwargs):
        calls.append(extension)
        converted = path.parent / "converted.pdf"
        converted.write_bytes(pdf_bytes(label=f"CONVERSION {len(calls)}"))
        return converted
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    result = build(one, [first], cache_folder=cache)
    replay = build(two, [second], cache_folder=cache)
    assert calls == ["doc"]
    assert replay["analysis_sha256"] == result["analysis_sha256"]
    assert replay["pdf_path"] == result["pdf_path"]
    assert cache not in replay["document_set_identity"].values()
    assert cache.stat().st_mode & 0o777 == 0o700
    assert (result["pdf_path"].parent / ".preparation.lock").stat().st_mode & 0o777 == 0o600
    changed = pdf_bytes(label="ORIGINAL REVISION")
    second[1].write_bytes(changed)
    second[0]["original_sha256"] = hashlib.sha256(changed).hexdigest()
    revision = build(two, [second], cache_folder=cache)
    assert calls == ["doc", "doc"]
    assert revision["selection_hash"] != result["selection_hash"]
    assert revision["analysis_sha256"] != result["analysis_sha256"]


def test_shared_cache_serializes_simultaneous_word_converters(monkeypatch, tmp_path):
    folders = [tmp_path / "run-one", tmp_path / "run-two"]
    for folder in folders:
        folder.mkdir()
    pairs = [candidate(folder, 1, suffix=".docx") for folder in folders]
    calls, guard = [], threading.Lock()
    def convert(path, extension, **kwargs):
        with guard:
            calls.append(extension)
        converted = path.parent / "converted.pdf"
        converted.write_bytes(pdf_bytes(label="ONE CONVERSION"))
        return converted
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda index: build(folders[index], [pairs[index]],
            cache_folder=tmp_path / "shared-cache"), [0, 1]))
    assert calls == ["docx"]
    assert results[0]["analysis_sha256"] == results[1]["analysis_sha256"]


def test_cache_symlinks_and_tampered_shared_pdf_are_not_reconverted(monkeypatch, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    pair = candidate(run, 1)
    cache = tmp_path / "cache"
    link = tmp_path / "cache-link"
    cache.mkdir()
    link.symlink_to(cache, target_is_directory=True)
    with pytest.raises(subject.PdfPreparationError, match="cache folder.*symlink"):
        build(run, [pair], cache_folder=link)
    result = build(run, [pair], cache_folder=cache)
    result["pdf_path"].write_bytes(b"tampered global")
    def forbidden(*args, **kwargs):
        pytest.fail("A tampered shared derivative requires reconciliation, not conversion/payment")
    monkeypatch.setattr(subject, "prepare_pdf", forbidden)
    with pytest.raises(subject.PdfPreparationError, match="Pinned preparation"):
        build(run, [pair], cache_folder=cache)


def test_selection_context_order_and_archive_revision_freeze_distinct_identity(tmp_path):
    one = candidate(tmp_path, 1, label="ONE")
    two = candidate(tmp_path, 2, label="TWO", source_kind="archive_member")
    base = build(tmp_path, [one, two], context={"official_inventory": [], "excluded": ["previous"]})
    changed_context = build(tmp_path, [one, two], context={"official_inventory": [], "excluded": []})
    reversed_order = build(tmp_path, [two, one], context={"official_inventory": [], "excluded": ["previous"]})
    two[0]["archive_sha256"] = "b" * 64
    revised_parent = build(tmp_path, [one, two], context={"official_inventory": [], "excluded": ["previous"]})
    assert len({result["document_set_hash"] for result in [base, changed_context, reversed_order, revised_parent]}) == 4


def test_nonfinite_context_duplicate_ids_and_malformed_pin_are_rejected(tmp_path):
    pair = candidate(tmp_path, 1)
    with pytest.raises(subject.PdfPreparationError, match="finite JSON"):
        build(tmp_path, [pair], context={"version": float("nan")})
    with pytest.raises(subject.PdfPreparationError, match="unique"):
        build(tmp_path, [pair, pair])
    result = build(tmp_path, [pair])
    marker = result["pdf_path"].parent / "manifest.private.json"
    marker.write_text("[]")
    with pytest.raises(subject.PdfPreparationError, match="Pinned selection"):
        build(tmp_path, [pair])


@pytest.mark.parametrize("change", [
    {"source_kind": "zip"}, {"filename": "../member.pdf"}, {"original_sha256": "bad"},
    {"member_sha256": "b" * 64}, {"member_path": "../selected-1.pdf"},
    {"archive_sha256": "bad"}, {"parent_filename": "parent.pdf"},
])
def test_invalid_provenance_or_unselected_format_blocks_before_conversion(monkeypatch, tmp_path, change):
    pair = candidate(tmp_path, 1, source_kind="archive_member")
    pair[0].update(change)
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid candidate must be rejected before conversion")
    monkeypatch.setattr(subject, "prepare_pdf", forbidden)
    with pytest.raises(subject.PdfPreparationError):
        build(tmp_path, [pair])


def test_missing_hash_mismatch_external_path_and_source_byte_limits(tmp_path):
    pair = candidate(tmp_path, 1, payload=200)
    with pytest.raises(subject.PdfPreparationError, match="original byte policy"):
        build(tmp_path, [pair], max_source_bytes=100)
    row, path = pair
    with pytest.raises(subject.PdfPreparationError, match="no local verified file"):
        subject.prepare_selected_document_set(tmp_path, [row], {}, {})
    outside = tmp_path.parent / "outside-selected.pdf"
    outside.write_bytes(path.read_bytes())
    try:
        with pytest.raises(subject.PdfPreparationError, match="inside"):
            build(tmp_path, [(row, outside)])
    finally:
        outside.unlink()
    path.write_bytes(b"different")
    with pytest.raises(subject.PdfPreparationError, match="hash changed"):
        build(tmp_path, [pair])


def test_aggregate_unique_original_limit_allows_exact_duplicates_only(tmp_path):
    first = candidate(tmp_path, 1, payload=200)
    second = candidate(tmp_path, 2, label="OTHER", payload=200)
    maximum = max(first[1].stat().st_size, second[1].stat().st_size)
    with pytest.raises(subject.PdfPreparationError, match="aggregate"):
        build(tmp_path, [first, second], max_source_bytes=maximum)
    duplicate = candidate(tmp_path, 3, payload=200)
    assert build(tmp_path, [first, duplicate], max_source_bytes=maximum)["page_count"] == 1


def test_encrypted_invalid_and_total_page_excess_block_all_documents(tmp_path):
    pair = candidate(tmp_path, 1)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("")
    output = io.BytesIO()
    writer.write(output)
    pair[1].write_bytes(output.getvalue())
    pair[0]["original_sha256"] = hashlib.sha256(output.getvalue()).hexdigest()
    with pytest.raises(subject.PdfPreparationError, match="Encrypted"):
        build(tmp_path, [pair])
    invalid = b"%PDF-invalid"
    pair[1].write_bytes(invalid)
    pair[0]["original_sha256"] = hashlib.sha256(invalid).hexdigest()
    with pytest.raises(subject.PdfPreparationError, match="parsed"):
        build(tmp_path, [pair])
    many = candidate(tmp_path, 2, count=600)
    extra = candidate(tmp_path, 3, count=401, label="EXTRA")
    with pytest.raises(subject.PdfPreparationError, match="total page policy"):
        build(tmp_path, [many, extra])


@pytest.mark.parametrize("options", [{"max_source_bytes": 0}, {"max_analysis_bytes": 0},
    {"max_analysis_bytes": 50_000_000}, {"optimize_lossless": "yes"}])
def test_invalid_resource_or_provider_limits(tmp_path, options):
    with pytest.raises(subject.PdfPreparationError, match="limits"):
        build(tmp_path, [candidate(tmp_path, 1)], **options)
