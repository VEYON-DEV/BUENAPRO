"""No services: cleanup ownership, durable completion, paid replay and isolation."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import temporary_storage as subject


UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def sha(content):
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def case(tmp_path):
    workspace = tmp_path / "repo" / "tmp"
    folder = workspace / "run" / "1046958"
    folder.mkdir(parents=True)
    original = b"%PDF-own-original"
    source = folder / (UUID + ".pdf")
    source.write_bytes(original)
    write_json(folder / (UUID + ".download.private.json"), {"document_uuid": UUID,
        "sha256": sha(original), "bytes": len(original)})
    identity = {"fixture": "owned-preparation"}
    digest = canonical_hash(identity)
    private = folder / "preparation-cache" / ("selected-analysis-" + digest)
    private.mkdir(parents=True)
    pdf, part = b"%PDF-owned-analysis", b"%PDF-owned-part"
    (private / "analysis.pdf").write_bytes(pdf)
    (private / "part-0001.pdf").write_bytes(part)
    work = private / UUID / sha(("loose:" + UUID).encode())
    work.mkdir(parents=True)
    (work / "source.pdf").write_bytes(original)
    write_json(private / "manifest.private.json", {"document_set_hash": digest,
        "document_set_identity": identity, "analysis_sha256": sha(pdf), "analysis_bytes": len(pdf),
        "chunks": [{"file_name": "part-0001.pdf", "sha256": sha(part), "bytes": len(part)}],
        "document_set": [{"document_uuid": UUID, "candidate_id": "loose:" + UUID,
                           "filename": "source.pdf", "original_sha256": sha(original)}]})
    outcome = {"status": "llm1_complete", "accepted": True, "convocation_id": 1046958,
               "document_set_hash": digest, "extraction_id": 107}
    write_json(folder / "outcome.private.json", outcome)
    write_json(folder / "gemini_extraction-paid.claim.private.json", {"id": 321})
    write_json(folder / "gemini_extraction-paid.response.private.json", {"cache_key": "paid"})
    write_json(folder / "gemini-selected-paid.started.private.json", {"attempt_id": "paid"})
    write_json(folder / "gemini-selected-paid.outcome.private.json", {
        "provider_usage_known": True, "remote_cleanup_pending": 0, "remote_cleanup_unknown": False})
    proof = {"pg_verified": True, "status": "llm1_complete", "convocation_id": 1046958,
             "document_set_hash": digest, "extraction_id": 107, "active_payments": 0,
             "usage_unknown_attempts": 0, "remote_cleanup_pending": 0, "remote_cleanup_unknown": False}
    return folder, workspace, proof, private, work


def clean(case, **options):
    folder, workspace, proof, *_ = case
    return subject.cleanup_completed_case(folder, verification=proof, reference_roots=(workspace,), **options)


def json_snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*.json")}


def test_completed_case_cleans_exact_owned_docs_preserves_paid_receipts_and_replays(case):
    folder, _, _, private, _ = case
    frozen = json_snapshot(folder)
    originals = [p for p in folder.rglob("*") if p.suffix == ".pdf"]
    expected = sum(p.stat().st_size for p in originals)
    result = clean(case)
    assert result["deleted_files"] == 4
    assert result["deleted_bytes"] == expected
    assert result["cumulative_deleted_bytes"] == expected
    assert all(not p.exists() for p in originals)
    assert all((folder / path).read_bytes() == data for path, data in frozen.items())
    assert (private / "manifest.private.json").exists()
    again = clean(case)
    assert again["deleted_bytes"] == 0
    assert again["deleted_files"] == 0
    assert again["already_absent_files"] == 4
    assert again["cumulative_deleted_bytes"] == expected


@pytest.mark.parametrize("field,value", [("pg_verified", False), ("pg_verified", 1),
    ("convocation_id", 1046959), ("convocation_id", True), ("status", "needs_review"),
    ("document_set_hash", "f" * 64), ("active_payments", 1), ("active_payments", False),
    ("usage_unknown_attempts", 1), ("remote_cleanup_pending", 1),
    ("remote_cleanup_unknown", True), ("remote_cleanup_unknown", 0), ("extraction_id", 108)])
def test_wrong_or_unreconciled_pg_proof_never_deletes(case, field, value):
    folder, _, proof, *_ = case
    proof[field] = value
    before = {p: p.read_bytes() for p in folder.rglob("*.pdf")}
    with pytest.raises(subject.TemporaryCleanupError):
        clean(case)
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("mutation", ["missing_outcome", "pending_outcome", "unaccepted", "wrong_case"])
def test_durable_final_outcome_required_before_local_cleanup(case, mutation):
    folder, *_ = case
    target = folder / "outcome.private.json"
    if mutation == "missing_outcome":
        target.unlink()
    else:
        value = json.loads(target.read_text())
        value.update({"pending_outcome": {"status": "needs_review"}, "unaccepted": {"accepted": False},
                      "wrong_case": {"convocation_id": 1046959}}[mutation])
        write_json(target, value)
    with pytest.raises(subject.TemporaryCleanupError):
        clean(case)
    assert len(list(folder.rglob("*.pdf"))) == 4


@pytest.mark.parametrize("mutation", ["missing_response", "missing_attempt_outcome", "unknown_billing",
                                      "remote_pending", "remote_unknown", "missing_jev_outcome"])
def test_local_uncertain_paid_evidence_retains_all_inputs(case, mutation):
    folder, *_ = case
    if mutation == "missing_response":
        (folder / "gemini_extraction-paid.response.private.json").unlink()
    elif mutation == "missing_attempt_outcome":
        (folder / "gemini-selected-paid.outcome.private.json").unlink()
    elif mutation == "missing_jev_outcome":
        write_json(folder / "jev-attempt-pending.started.private.json", {"attempt_id": "pending"})
    else:
        target = folder / "gemini-selected-paid.outcome.private.json"
        value = json.loads(target.read_text())
        value.update({"unknown_billing": {"provider_usage_known": False},
                      "remote_pending": {"remote_cleanup_pending": 1},
                      "remote_unknown": {"remote_cleanup_unknown": True}}[mutation])
        write_json(target, value)
    result = clean(case)
    assert result["status"] == "retained_unreconciled_payment"
    assert result["deleted_files"] == 0
    assert len(list(folder.rglob("*.pdf"))) == 4


def test_dry_run_creates_no_inventory_and_removes_nothing(case):
    folder, *_ = case
    result = clean(case, execute=False)
    assert result["planned_files"] == 4
    assert result["deleted_bytes"] == 0
    assert not (folder / "temporary-cleanup-plan.private.json").exists()
    assert not (folder / "temporary-cleanup.private.json").exists()
    assert len(list(folder.rglob("*.pdf"))) == 4


@pytest.mark.parametrize("location", ["source", "prepared", "nested_directory"])
def test_symlinked_case_artifact_or_directory_blocks_entire_cleanup(case, tmp_path, location):
    folder, _, _, private, work = case
    outside = tmp_path / "user-file.pdf"
    outside.write_bytes(b"keep-user-data")
    if location == "source":
        path = folder / (UUID + ".pdf")
        path.unlink()
        path.symlink_to(outside)
    elif location == "prepared":
        path = private / "analysis.pdf"
        path.unlink()
        path.symlink_to(outside)
    else:
        (work / "untrusted-directory").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(subject.TemporaryCleanupError, match="symlink"):
        clean(case)
    assert outside.read_bytes() == b"keep-user-data"
    assert (private / "part-0001.pdf").exists()


def test_hardlinked_document_is_retained(case, tmp_path):
    folder, *_ = case
    original = folder / (UUID + ".pdf")
    outside = tmp_path / "shared.pdf"
    os.link(original, outside)
    result = clean(case)
    assert result["deleted_files"] == 3
    assert original.exists() and outside.exists()
    assert {"path": original.name, "reason": "hardlinked_file"} in result["retained"]


def test_unknown_document_and_shared_global_pin_never_enter_deletion_scope(case):
    folder, workspace, _, _, _ = case
    unknown = folder / "uploaded-by-user.pdf"
    unknown.write_bytes(b"%PDF-do-not-delete")
    global_pin = workspace / "model-cache" / "selected-analysis-global" / "analysis.pdf"
    global_pin.parent.mkdir(parents=True)
    global_pin.write_bytes(b"%PDF-shared-cache")
    result = clean(case)
    assert unknown.exists() and global_pin.exists()
    assert result["deleted_files"] == 4
    assert result["status"] == "complete_with_retained"
    assert {"path": unknown.name, "reason": "ownership_unproven"} in result["retained"]


@pytest.mark.parametrize("reference", ["absolute_file", "absolute_dir", "relative_file", "case_dir"])
def test_external_pending_or_compact_recovery_reference_protects_inputs(case, reference):
    folder, workspace, _, private, _ = case
    sibling = workspace / "pending" / "1050000"
    sibling.mkdir(parents=True)
    target = private / "analysis.pdf"
    value = {"absolute_file": str(target), "absolute_dir": str(private),
             "relative_file": "tmp/" + target.relative_to(workspace).as_posix(), "case_dir": str(folder)}[reference]
    write_json(sibling / "manifest.private.json", {"compact_source": {"path": value}})
    result = clean(case)
    assert target.exists()
    assert any(row["reason"] == "external_reference" for row in result["retained"])
    if reference == "case_dir":
        assert result["deleted_files"] == 0


@pytest.mark.parametrize("target_kind", ["own_file", "own_directory", "unrelated_file", "unrelated_directory"])
def test_external_symlink_is_a_reference_without_becoming_a_walkable_namespace(case, tmp_path, target_kind):
    folder, workspace, _, private, _ = case
    if target_kind == "own_file":
        target = private / "analysis.pdf"
    elif target_kind == "own_directory":
        target = private
    elif target_kind == "unrelated_file":
        target = tmp_path / "foreign-dependency.bin"
        target.write_bytes(b"keep")
    else:
        target = tmp_path / "foreign-dependency"
        target.mkdir()
        write_json(target / "package.json", {"name": "dependency"})
    (workspace / "foreign-link").symlink_to(target, target_is_directory=target.is_dir())
    result = clean(case)
    assert result["status"] == ("complete_with_retained" if target_kind.startswith("own_") else "complete")
    if target_kind.startswith("own_"):
        assert (private / "analysis.pdf").exists()
    else:
        assert result["deleted_files"] == 4


@pytest.mark.parametrize("scope", ["absent", "invalid_json", "outside", "oversized"])
def test_unverified_reference_universe_conserves_every_input(case, tmp_path, monkeypatch, scope):
    folder, workspace, proof, *_ = case
    roots = (workspace,)
    if scope == "absent":
        roots = ()
    elif scope == "invalid_json":
        (workspace / "broken.json").write_bytes(b"{bad}")
    elif scope == "outside":
        roots = (tmp_path, workspace / "run")
        (tmp_path / "broken.json").write_bytes(b"{bad}")
    else:
        monkeypatch.setattr(subject, "_MAX_REFERENCE_BYTES", 1)
        write_json(workspace / "external.json", {"scope": "too large"})
    result = subject.cleanup_completed_case(folder, verification=proof, reference_roots=roots)
    assert result["status"] == "retained_reference_scope_unverified"
    assert len(list(folder.rglob("*.pdf"))) == 4


def test_stale_original_hash_blocks_all_deletions_before_plan_publication(case):
    folder, *_ = case
    (folder / (UUID + ".pdf")).write_bytes(b"different-original")
    with pytest.raises(subject.TemporaryCleanupError, match="pinned"):
        clean(case)
    assert len(list(folder.rglob("*.pdf"))) == 4
    assert not (folder / "temporary-cleanup-plan.private.json").exists()


def test_frozen_cleanup_plan_never_expands_to_new_artifacts(case):
    folder, *_ = case
    clean(case)
    # Creating another official-looking receipt later cannot extend a completed
    # cleanup plan into a second source version without a new case/outcome.
    new_code = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    content = b"%PDF-new-version"
    (folder / (new_code + ".pdf")).write_bytes(content)
    write_json(folder / (new_code + ".download.private.json"), {"document_uuid": new_code,
        "sha256": sha(content), "bytes": len(content)})
    result = clean(case)
    assert result["deleted_files"] == 0
    assert (folder / (new_code + ".pdf")).exists()


def test_mutated_plan_input_after_first_pass_is_not_deleted(case):
    folder, *_ = case
    clean(case)
    (folder / (UUID + ".pdf")).write_bytes(b"%PDF-unrelated-new-input")
    with pytest.raises(subject.TemporaryCleanupError, match="immutable"):
        clean(case)
    assert (folder / (UUID + ".pdf")).exists()


def test_extracted_archive_member_exact_sidecar_is_cleaned_and_preserved(case):
    folder, *_ = case
    archive_sha, member_id = "b" * 64, "c" * 64
    directory = folder / (UUID + ".members") / UUID / archive_sha
    directory.mkdir(parents=True)
    content = b"%PDF-archive-member"
    member = directory / (member_id + ".pdf")
    member.write_bytes(content)
    receipt = directory / (member_id + ".json")
    write_json(receipt, {"parent_uuid": UUID, "parent_sha256": archive_sha,
        "candidate_id": "archive:" + member_id, "format": "pdf", "size": len(content),
        "original_sha256": sha(content)})
    result = clean(case)
    assert result["deleted_files"] == 5
    assert not member.exists() and receipt.exists()


def test_native_text_and_word_conversion_artifacts_are_cleaned_without_touching_json(case):
    folder, _, _, private, work = case
    text = b"full marked source text"
    text_dir = folder / ("local-text-" + "a" * 64)
    text_dir.mkdir()
    (text_dir / "complete.private.txt").write_bytes(text)
    write_json(text_dir / "manifest.private.json", {"cache_key": "a" * 64,
        "source_binding": {"analysis_sha256": sha((private / "analysis.pdf").read_bytes())},
        "text_sha256": sha(text), "text_bytes": len(text)})
    converted = work / "libreoffice-profile-owned" / "converted"
    converted.mkdir(parents=True)
    (converted / "source.pdf").write_bytes(b"%PDF-conversion")
    (converted / "source.fodt").write_bytes(b"native word xml")
    write_json(converted.parent / "word-conversion.private.json", {"provenance": "preserve"})
    result = clean(case)
    assert result["deleted_files"] == 7
    assert not (text_dir / "complete.private.txt").exists()
    assert not (converted / "source.fodt").exists()
    assert (text_dir / "manifest.private.json").exists()
    assert (converted.parent / "word-conversion.private.json").exists()


def test_unbound_native_text_is_retained(case):
    folder, *_ = case
    directory = folder / ("local-text-" + "a" * 64)
    directory.mkdir()
    (directory / "complete.private.txt").write_text("not bound to the current input")
    write_json(directory / "manifest.private.json", {"cache_key": "a" * 64,
        "source_binding": {"analysis_sha256": "f" * 64}})
    result = clean(case)
    assert (directory / "complete.private.txt").exists()
    assert any(row["reason"] == "unbound_local_text" for row in result["retained"])


def test_crash_mid_cleanup_replays_only_original_targets_without_new_input(case, monkeypatch):
    folder, *_ = case
    actual_remove = subject._remove
    calls = 0
    def interrupted(case_dir, entry):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interrupted cleanup")
        actual_remove(case_dir, entry)
    monkeypatch.setattr(subject, "_remove", interrupted)
    with pytest.raises(OSError, match="synthetic"):
        clean(case)
    assert (folder / "temporary-cleanup-plan.private.json").exists()
    assert len(list(folder.rglob("*.pdf"))) == 3
    monkeypatch.setattr(subject, "_remove", actual_remove)
    result = clean(case)
    assert result["deleted_files"] == 3
    assert result["already_absent_files"] == 1
    assert not list(folder.rglob("*.pdf"))


def test_tampered_outcome_after_cleanup_refuses_replay(case):
    folder, *_ = case
    clean(case)
    path = folder / "outcome.private.json"
    value = json.loads(path.read_text())
    value["description"] = "modified-final-outcome"
    write_json(path, value)
    with pytest.raises(subject.TemporaryCleanupError, match="differs"):
        clean(case)


@pytest.mark.parametrize("final_state", ["resolved", "wrong_attempt", "still_pending", "still_unknown"])
def test_final_remote_cleanup_receipt_supersedes_generation_outcome_safely(case, final_state):
    folder, *_ = case
    write_json(folder / "gemini-selected-paid.outcome.private.json", {
        "attempt_id": "gemini-selected-paid", "provider_usage_known": True,
        "remote_cleanup_pending": 1, "remote_cleanup_unknown": False})
    cleanup = {"attempt_id": "gemini-selected-paid", "remote_cleanup_pending": 0,
               "remote_cleanup_unknown": False}
    cleanup.update({"resolved": {}, "wrong_attempt": {"attempt_id": "gemini-selected-other"},
                    "still_pending": {"remote_cleanup_pending": 1},
                    "still_unknown": {"remote_cleanup_unknown": True}}[final_state])
    write_json(folder / "gemini-selected-paid.cleanup.private.json", cleanup)
    result = clean(case)
    assert result["deleted_files"] == (4 if final_state == "resolved" else 0)
    if final_state != "resolved":
        assert result["status"] == "retained_unreconciled_payment"


@pytest.mark.parametrize("path", ["outcome.private.json", "../../user.pdf", "/tmp/user.pdf"])
def test_modified_plan_cannot_delete_receipts_or_escape_case(case, path):
    folder, *_ = case
    clean(case)
    marker = folder / "temporary-cleanup-plan.private.json"
    value = json.loads(marker.read_text())
    data = (folder / "outcome.private.json").read_bytes()
    value["files"] = [{"path": path, "sha256": sha(data), "bytes": len(data)}]
    write_json(marker, value)
    with pytest.raises(subject.TemporaryCleanupError, match="receipts or unsafe"):
        clean(case)
    assert (folder / "outcome.private.json").read_bytes() == data


def test_real_local_document_lifecycle_cleanup_keeps_replay_provenance(tmp_path):
    """Real ZIP inventory/member cache, preview subprocess, pypdf and local text.

    No paid client or database is constructed. Files are created only in pytest's
    own temporary directory, and cleanup must recognize real production receipts.
    """
    if shutil.which("pdftotext") is None:
        pytest.skip("Local PDF text tool unavailable")
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from buenapro_worker.prod2.archive_inventory import list_archive, extract_members
    from buenapro_worker.prod2.document_previews import document_preview
    from buenapro_worker.prod2.pdf_preparation import prepare_selected_document_set
    from buenapro_worker.prod2.local_text import prepare_local_text
    from buenapro_worker.prod2.documents import METADATA_URL

    workspace = tmp_path / "tmp"
    folder = workspace / "run" / "1046958"
    folder.mkdir(parents=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 40 700 Td (Requisitos de software y experiencia del postor en desarrollo y soporte informatico.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    pdf = output.getvalue()
    original = folder / (UUID + ".zip")
    with zipfile.ZipFile(original, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("terms/bases.pdf", pdf)
        archive.writestr("other/annex.txt", "Fill in your form here")
    archive_sha = sha(original.read_bytes())
    write_json(folder / (UUID + ".download.private.json"), {"document_uuid": UUID,
        "sha256": archive_sha, "bytes": original.stat().st_size, "format": "zip"})
    inventory = list_archive(original, UUID, max_source_bytes=1024**2,
        max_expanded_bytes=1024**2, max_member_bytes=1024**2, max_members=10)
    write_json(folder / (UUID + ".archive.private.json"), inventory)
    candidate = next(row for row in inventory["members"] if row["format"] == "pdf")
    member = extract_members(original, inventory, candidate_ids=[candidate["candidate_id"]],
        destination=folder / (UUID + ".members"))[0]
    member_path = Path(member["path"])
    preview = document_preview(member_path, folder / "text-previews" / "member", max_ocr_pages=0)
    assert preview["source_sha256"] == sha(pdf)
    selected = {"document_uuid": UUID, "candidate_id": member["candidate_id"], "source_kind": "archive_member",
        "filename": "bases.pdf", "parent_filename": "documents.zip", "member_path": "terms/bases.pdf",
        "member_index": member["member_index"], "original_sha256": sha(pdf), "member_sha256": sha(pdf),
        "archive_sha256": archive_sha, "type": "Bases", "stage": "Convocatoria",
        "published_at": "2026-10-07 00:00", "source_url": METADATA_URL + "?id=" + UUID}
    prepared = prepare_selected_document_set(folder, [selected], {selected["candidate_id"]: member_path},
        {"fixture": "local lifecycle"}, max_source_bytes=1024**2, cache_folder=folder / "preparation-cache")
    text = prepare_local_text(prepared, folder)
    assert text["coverage_complete"] is True
    assert text["text_path"] is not None
    outcome = {"accepted": True, "status": "llm1_complete", "convocation_id": 1046958,
               "document_set_hash": prepared["document_set_hash"]}
    write_json(folder / "outcome.private.json", outcome)
    proof = {"pg_verified": True, **outcome, "active_payments": 0, "usage_unknown_attempts": 0,
             "remote_cleanup_pending": 0, "remote_cleanup_unknown": False}
    frozen = json_snapshot(folder)
    targets = [original, member_path, prepared["pdf_path"], prepared["chunks"][0]["pdf_path"], text["text_path"]]
    result = subject.cleanup_completed_case(folder, verification=proof, reference_roots=(workspace,))
    assert result["deleted_files"] == 6  # Original/member/source copy/analysis/part/text.
    assert all(not path.exists() for path in targets)
    assert all((folder / path).read_bytes() == data for path, data in frozen.items())
    assert subject.cleanup_completed_case(folder, verification=proof,
        reference_roots=(workspace,))["deleted_bytes"] == 0


def test_completed_sevenzip_source_cleans_only_verified_uuid_owned_bytes(case):
    folder, *_ = case
    code = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
    content = b'7z\xbc\xaf\x27\x1cowned-original'
    source = folder / (code + '.7z')
    source.write_bytes(content)
    write_json(folder / (code + '.download.private.json'), {
        'document_uuid': code, 'format': '7z', 'sha256': sha(content), 'bytes': len(content)})
    other = folder / 'unbound.7z'
    other.write_bytes(b'keep-unbound')
    result = clean(case)
    assert not source.exists() and other.read_bytes() == b'keep-unbound'
    assert (folder / (code + '.download.private.json')).exists()
    assert any(row['path'] == 'unbound.7z' for row in result['retained'])
    assert result['deleted_files'] == 5
    assert clean(case)['deleted_files'] == 0


def test_uncertain_payment_preserves_sevenzip_inputs(case):
    folder, *_ = case
    code = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
    source = folder / (code + '.7z')
    content = b'7z\xbc\xaf\x27\x1ckeep-pending'
    source.write_bytes(content)
    write_json(folder / (code + '.download.private.json'), {
        'document_uuid': code, 'format': '7z', 'sha256': sha(content), 'bytes': len(content)})
    (folder / 'gemini_extraction-paid.response.private.json').unlink()
    result = clean(case)
    assert result['status'] == 'retained_unreconciled_payment' and result['deleted_files'] == 0
    assert source.read_bytes() == content
