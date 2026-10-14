"""Explicit local PDF page anchors do not change default previews or Jev gates."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import run_prod2_selected as selected
import recover_prod2_selected as recovery
from buenapro_worker.prod2 import document_previews as subject
from test_prod2_document_previews import pdf_bytes
from test_prod2_selected_recovery import fixture as recovery_fixture


def source(tmp_path):
    labels = ["Administrative context and publication record " * 8] * 57
    labels += ["Especificaciones tecnicas de software LICENSE_REQUIREMENTS " * 8,
               "Plazo de entrega condiciones pago DELIVERY_REQUIREMENTS " * 8,
               "Obligaciones proveedor THIRD_PAGE_REQUIREMENTS " * 8]
    labels += ["Other administrative context " * 8] * 2
    path = tmp_path / "source.pdf"
    path.write_bytes(pdf_bytes(labels))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def options(sha):
    return {"anchor_source_sha256": sha, "anchor_pages": [58, 59, 60]}


def manifest(sha):
    return {"document_previews": True, "selector_policy_version": "prod2_jev_document_selection_v3",
        "selector_high": 80, "selector_low": 20, "document_preview_anchors": {
            "policy_version": subject.ANCHORED_PREVIEW_POLICY, "pages_by_source_sha256": {sha: [58, 59, 60]}}}


def test_native_anchors_have_new_identity_and_default_cache_is_unchanged(tmp_path, monkeypatch):
    path, sha = source(tmp_path)
    before = path.read_bytes()
    work = tmp_path / "work"
    old = subject.document_preview(path, work, max_ocr_pages=0)
    legacy_file = next((work / "document-preview-cache").glob("*.private.json"))
    legacy_bytes = legacy_file.read_bytes()
    result = subject.document_preview(path, work, max_ocr_pages=0, **options(sha))
    assert old["preview_policy"] == subject.PREVIEW_POLICY and "anchor_pages" not in old
    assert "LICENSE_REQUIREMENTS" not in old["preview_text"]
    assert result["preview_policy"] == subject.ANCHORED_PREVIEW_POLICY
    assert result["source_sha256"] == result["anchor_source_sha256"] == sha
    assert result["anchor_pages"] == result["preview_pages"] == [58, 59, 60]
    assert all(word in result["preview_text"] for word in ("LICENSE_REQUIREMENTS", "DELIVERY_REQUIREMENTS", "THIRD_PAGE_REQUIREMENTS"))
    assert all(f"Pagina global {n}" in result["preview_text"] for n in (58, 59, 60))
    assert result["content_hash"] != old["content_hash"] and path.read_bytes() == before
    assert legacy_file.read_bytes() == legacy_bytes
    assert len(list((work / "document-preview-cache").glob("*.private.json"))) == 2
    monkeypatch.setattr(subject, "_extract_process", lambda *a: pytest.fail("Replay cannot extract again"))
    assert subject.document_preview(path, work, max_ocr_pages=0, **options(sha)) == result
    assert subject.document_preview(path, work, max_ocr_pages=0) == old


@pytest.mark.parametrize("anchors", [[], [True], [0], [-1], [1001], [1, 2, 3, 4], [2, 1], [1, 1], "58,59,60"])
def test_anchor_bounds_fail_before_creating_cache(tmp_path, anchors):
    path, sha = source(tmp_path)
    with pytest.raises(subject.DocumentPreviewError, match="Explicit preview anchors"):
        subject.document_preview(path, tmp_path / "work", anchor_source_sha256=sha, anchor_pages=anchors)
    assert not (tmp_path / "work").exists()


def test_wrong_source_sha_and_word_anchors_fail_before_extraction(tmp_path, monkeypatch):
    path, sha = source(tmp_path)
    monkeypatch.setattr(subject, "_extract_process", lambda *a: pytest.fail("Mismatched SHA cannot extract"))
    with pytest.raises(subject.DocumentPreviewError, match="actual original PDF SHA"):
        subject.document_preview(path, tmp_path / "work", **options("a" * 64))
    word = tmp_path / "source.doc"
    word.write_bytes(b"Word source")
    with pytest.raises(subject.DocumentPreviewError, match="actual original PDF SHA"):
        subject.document_preview(word, tmp_path / "work", anchor_source_sha256=hashlib.sha256(word.read_bytes()).hexdigest(), anchor_pages=[1])
    assert not (tmp_path / "work").exists()


def test_out_of_range_anchor_cannot_trigger_ocr_or_publish_cache(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(pdf_bytes(["Page one text " * 12]))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(subject, "_extract_process", subject._extract)
    monkeypatch.setattr(subject, "_ocr", lambda *a: pytest.fail("Out of range cannot OCR"))
    with pytest.raises(subject.DocumentPreviewError, match="outside the original PDF"):
        subject.document_preview(path, tmp_path / "work", anchor_source_sha256=sha, anchor_pages=[2])
    assert not list((tmp_path / "work/document-preview-cache").glob("*.private.json"))


def test_scanned_pdf_ocr_uses_only_explicit_global_pages(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(6): writer.add_blank_page(width=612, height=792)
    path = tmp_path / "scanned.pdf"
    with path.open("wb") as output: writer.write(output)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    seen = []
    def ocr(path, scratch, pages):
        seen.append(pages)
        return [(number, "Especificaciones tecnicas de software " * 8) for number in pages]
    monkeypatch.setattr(subject, "_extract_process", subject._extract)
    monkeypatch.setattr(subject, "_ocr", ocr)
    result = subject.document_preview(path, tmp_path / "work", anchor_source_sha256=sha, anchor_pages=[4, 5, 6])
    assert seen == [[4, 5, 6]] and result["preview_pages"] == [4, 5, 6]
    assert result["method"] == "OCR" and result["sample_only"] is True


def test_anchor_cache_cannot_claim_pages_beyond_its_document(tmp_path):
    path, sha = source(tmp_path)
    result = subject.document_preview(path, tmp_path / "work", max_ocr_pages=0, **options(sha))
    bad = {**result, "page_count": 10}
    bad["content_hash"] = subject.canonical_hash({k: v for k, v in bad.items() if k != "content_hash"})
    with pytest.raises(subject.DocumentPreviewError, match="anchor cache"):
        subject._validate(bad, sha, 3500, 0, **options(sha))


@pytest.mark.parametrize("specs", [["bad"], ["a" * 64 + ":0"], ["a" * 64 + ":58,59,60,61"],
    ["a" * 64 + ":60,58"], ["a" * 64 + ":58,58"], ["a" * 64 + ":1001"],
    ["a" * 64 + ":58", "A" * 64 + ":59"], ["a" * 64 + ":1"] * 11])
def test_cli_anchor_specs_are_bounded_and_canonical(specs):
    with pytest.raises(subject.DocumentPreviewError): subject.parse_preview_anchor_specs(specs)


def test_selected_and_recovery_cli_freeze_policy_mapping_and_keep_gates(tmp_path):
    sha = "a" * 64
    args = selected.parser().parse_args(["--folder", str(tmp_path / "selected"), "--document-previews",
        "--preview-anchor", sha.upper() + ":58,59,60"])
    value = selected.plan(args)
    assert value["document_preview_anchors"] == manifest(sha)["document_preview_anchors"]
    assert value["selector_high"] == 80 and value["selector_low"] == 20
    old = selected.plan(selected.parser().parse_args(["--folder", str(tmp_path / "legacy")]))
    assert "document_preview_anchors" not in old
    args.document_previews = False
    with pytest.raises(ValueError, match="previews and selector V3"): selected.plan(args)
    recovery_args, _, _, _, original_outcome = recovery_fixture(tmp_path)
    before = original_outcome.read_bytes()
    recovery_args.preview_anchor = [sha + ":58,59,60"]
    recovered = recovery.plan(recovery_args)
    assert recovered["document_preview_anchors"] == value["document_preview_anchors"]
    assert recovered["selector_high"] == 80 and recovered["selector_low"] == 20
    recovery.private_write(recovery_args.folder / "manifest.private.json", recovered)
    assert recovery.plan(recovery_args) == recovered
    recovery_args.preview_anchor = []
    with pytest.raises(ValueError, match="Pinned recovery inputs changed"): recovery.plan(recovery_args)
    assert original_outcome.read_bytes() == before


@pytest.mark.parametrize("value", [None, {}, {"policy_version": "changed", "pages_by_source_sha256": {"a" * 64: [58]}},
    {"policy_version": subject.ANCHORED_PREVIEW_POLICY, "pages_by_source_sha256": {"a" * 64: [True]}}])
def test_invalid_manifest_anchor_policy_fails_before_enrichment_effects(value):
    with pytest.raises(subject.DocumentPreviewError):
        selected.enrich(None, None, None, None, {"document_preview_anchors": value}, None, None, None)


def candidate_preview_context(tmp_path, monkeypatch, *, unrelated=False, mutate_second=False):
    path, sha = source(tmp_path)
    second = tmp_path / "second.pdf"
    second.write_bytes(path.read_bytes())
    if mutate_second: second.write_bytes(pdf_bytes(["Different bytes " * 10]))
    codes = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    candidates = [{"document_uuid": code, "candidate_id": "loose:" + code, "source_kind": "loose", "filename": "source.pdf"} for code in codes]
    parents = [{"document_uuid": code} for code in codes]
    paths = dict(zip(codes, [path, second]))
    monkeypatch.setattr(selected, "download", lambda conn, folder, conv, parent, *a: (paths[parent["document_uuid"]], {"sha256": sha}))
    seen = []
    def preview(path, folder, **kwargs):
        seen.append(kwargs)
        return {"source_sha256": sha, "preview_text": "Technical specifications", "content_hash": "b" * 64}
    monkeypatch.setattr(subject, "document_preview", preview)
    configured = manifest("c" * 64 if unrelated else sha)
    return sha, candidates, parents, configured, seen


def test_same_sha_candidates_receive_identical_anchors_without_identity_changes(tmp_path, monkeypatch):
    sha, candidates, parents, configured, seen = candidate_preview_context(tmp_path, monkeypatch)
    ids = [row["candidate_id"] for row in candidates]
    selected.preview_candidates(None, tmp_path / "capture", 123, candidates, parents, {}, {}, configured, None)
    assert len(seen) == 1 and seen[0]["anchor_source_sha256"] == sha and seen[0]["anchor_pages"] == [58, 59, 60]
    assert [row["candidate_id"] for row in candidates] == ids
    values = selected.read_private(tmp_path / "capture/document-previews.private.json")
    assert values[ids[0]] == values[ids[1]]


def test_archive_and_loose_copy_share_anchored_sample_and_keep_provenance(tmp_path, monkeypatch):
    sha, candidates, parents, configured, seen = candidate_preview_context(tmp_path, monkeypatch)
    archive = candidates[0]
    archive.update(source_kind="archive_member", candidate_id="archive:" + "d" * 64,
                   inventory_status="candidate", member_path="docs/source.pdf")
    monkeypatch.setattr(selected, "extract_members", lambda *a, **kw: [{
        "candidate_id": archive["candidate_id"], "original_sha256": sha,
        "path": str(tmp_path / "source.pdf")}])
    ids = [row["candidate_id"] for row in candidates]
    archives = {archive["document_uuid"]: (tmp_path / "source.zip", {})}
    selected.preview_candidates(None, tmp_path / "capture", 123, candidates, parents, archives, {}, configured, None)
    assert len(seen) == 1 and seen[0]["anchor_pages"] == [58, 59, 60]
    assert [row["candidate_id"] for row in candidates] == ids
    assert candidates[0]["member_path"] == "docs/source.pdf" and candidates[0]["member_sha256"] == sha
    assert candidates[0]["source_kind"] == "archive_member" and candidates[1]["source_kind"] == "loose"
    values = selected.read_private(tmp_path / "capture/document-previews.private.json")
    assert values[ids[0]] == values[ids[1]]


def test_unmatched_sha_keeps_default_previews_for_each_candidate(tmp_path, monkeypatch):
    _, candidates, parents, configured, seen = candidate_preview_context(tmp_path, monkeypatch, unrelated=True)
    selected.preview_candidates(None, tmp_path / "capture", 123, candidates, parents, {}, {}, configured, None)
    assert len(seen) == 2 and all("anchor_pages" not in item for item in seen)


def test_memoized_anchors_still_verify_every_candidates_actual_bytes(tmp_path, monkeypatch):
    _, candidates, parents, configured, seen = candidate_preview_context(tmp_path, monkeypatch, mutate_second=True)
    with pytest.raises(ValueError, match="actual candidate bytes"):
        selected.preview_candidates(None, tmp_path / "capture", 123, candidates, parents, {}, {}, configured, None)
    assert len(seen) == 1
    assert not (tmp_path / "capture/document-previews.private.json").exists()


def test_recovery_copies_verified_anchored_cache_with_new_identity(tmp_path):
    path, sha = source(tmp_path)
    root, private = tmp_path / "cache", tmp_path / "destination"
    result = subject.document_preview(path, root / "text-previews" / ("b" * 64), max_ocr_pages=0, **options(sha))
    assert recovery.cached_previews([root], private) == 1
    original = next((root / "text-previews").rglob("*.private.json"))
    assert recovery.read_private(private / original.relative_to(root)) == result
    changed = {**result, "anchor_pages": [57, 58, 59], "preview_pages": [57, 58, 59]}
    changed["content_hash"] = subject.canonical_hash({k: v for k, v in changed.items() if k != "content_hash"})
    original.write_text(json.dumps(changed))
    with pytest.raises(subject.DocumentPreviewError, match="filename differs"):
        recovery.cached_previews([root], private)
