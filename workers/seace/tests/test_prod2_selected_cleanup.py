"""Private cloud evidence must precede every scoped binary deletion."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("prod2_selected_cleanup", ROOT / "scripts/cleanup_prod2_selected.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(subject, "ROOT", root)
    workspace = root / "tmp"
    workspace.mkdir()
    final, original, cache = [workspace / name for name in ("final", "original", "cache")]
    for folder in (final, original, cache):
        folder.mkdir()
    ids = list(range(1000, 1010))
    manifest = {"run_id": "original", "terminal_stage": "llm1", "recovery_folder": str(final),
                "recovery_ids": ids, "original_folder": str(original), "cache_folder": str(cache)}
    subject.private_write(final / "manifest.private.json", manifest)
    subject.private_write(original / "manifest.private.json", {"run_id": "original"})
    records = []
    for conv in ids:
        (final / str(conv)).mkdir()
        (original / str(conv)).mkdir()
        proof = {"run_id": "original", "convocation_id": conv}
        sha = hashlib.sha256(str(conv).encode()).hexdigest()
        outcome = {"convocation_id": conv, "status": "llm1_complete", "accepted": True,
                   "document_set_hash": sha, "accepted_proof_hash": subject.hash_request(proof),
                   "recovery_manifest_hash": subject.hash_request(manifest)}
        subject.private_write(final / str(conv) / "accepted-proof.private.json", proof)
        subject.private_write(final / str(conv) / "outcome.private.json", outcome)
        (final / str(conv) / "selected.pdf").write_bytes(b"%PDF-synthetic-shared-content")
        (original / str(conv) / "original.zip").write_bytes(b"PK\x03\x04synthetic-shared-archive")
        subject.private_write(original / str(conv) / "source.private.json", {"id": conv})
        pin = cache / ("selected-analysis-" + sha)
        pin.mkdir()
        (pin / "analysis.pdf").write_bytes(b"%PDF-synthetic-shared-content")
        subject.private_write(pin / "manifest.private.json", {"document_set_hash": sha})
        records.append(outcome)
    subject.private_write(final / "report-1.private.json", {"run_id": "original", "llm1_complete": 10, "records": records})
    proof = {"verified_ids": ids, "llm1_complete": 10, "pg_verified": True, "api_verified": True,
             "r2_verified": True, "remote_cleanup_pending": 0, "usage_unknown_attempts": 0,
             "active_payments": 0, "process_running": False}
    verification = workspace / "verification.private.json"
    subject.private_write(verification, proof)
    return final, verification, workspace / "archive", original, cache, ids


def inventory(evidence):
    final, verification, archive, *_ = evidence
    return subject.inventory(final, verification, archive)


def overwrite(path, value):
    path.write_text(json.dumps(value))


@pytest.mark.parametrize("field,value", [("llm1_complete", 9), ("pg_verified", False), ("api_verified", False),
    ("r2_verified", False), ("remote_cleanup_pending", 1), ("usage_unknown_attempts", 1),
    ("active_payments", 1), ("process_running", True), ("verified_ids", list(range(1000, 1009))),
    ("active_payments", False)])
def test_verification_gate_blocks_incomplete_or_active_runs(evidence, field, value):
    _, verification, *_ = evidence
    proof = subject.read_private(verification)
    proof[field] = value
    overwrite(verification, proof)
    with pytest.raises(subject.CleanupError):
        inventory(evidence)


def test_exact_final_report_outcomes_and_membership_are_required(evidence):
    final, _, _, _, _, ids = evidence
    outcome = final / str(ids[0]) / "outcome.private.json"
    data = subject.read_private(outcome)
    data["accepted_proof_hash"] = "f" * 64
    overwrite(outcome, data)
    with pytest.raises(subject.CleanupError, match="outcome"):
        inventory(evidence)


def test_subset_inventory_never_reads_other_cases_keyword_cache_env_or_credentials(evidence):
    final, _, _, original, cache, ids = evidence
    unrelated = original / "999999"
    unrelated.mkdir()
    (unrelated / "keep.pdf").write_bytes(b"other-case")
    extra = cache / ("selected-analysis-" + "f" * 64)
    extra.mkdir()
    (extra / "keep.pdf").write_bytes(b"other-pin")
    keyword = subject.ROOT / "tmp" / "prod2-keyword-cache"
    keyword.mkdir()
    (keyword / "keep.zip").write_bytes(b"keyword-proof")
    case = final / str(ids[0])
    (case / ".env.local").write_text("DO-NOT-ARCHIVE")
    credentials = case / "credentials"
    credentials.mkdir()
    (credentials / "ssh.key").write_bytes(b"DO-NOT-ARCHIVE")
    value = inventory(evidence)
    paths = [row["logical_path"] for row in value["files"]]
    assert not any("999999" in p or "prod2-keyword-cache" in p or ".env" in p or "credentials" in p for p in paths)
    assert not any("selected-analysis-" + "f" * 64 in p for p in paths)
    assert value["binary_files"] == 30


def test_symlinked_evidence_blocks_all_cleanup(evidence):
    final, _, _, _, _, ids = evidence
    source = final / str(ids[0]) / "selected.pdf"
    (final / str(ids[1]) / "unsafe.pdf").symlink_to(source)
    with pytest.raises(subject.CleanupError, match="symlink"):
        inventory(evidence)


def test_paths_outside_repository_tmp_and_nested_archive_are_rejected(evidence, tmp_path):
    final, verification, _, *_ = evidence
    with pytest.raises(subject.CleanupError, match="inside"):
        subject.inventory(final, verification, tmp_path / "external")
    with pytest.raises(subject.CleanupError, match="separate"):
        subject.inventory(final, verification, final / "archive")
    with pytest.raises(subject.CleanupError, match="separate"):
        subject.inventory(final, verification, evidence[4] / "archive")


def test_missing_response_or_unknown_attempt_blocks_cleanup(evidence):
    final, _, _, _, _, ids = evidence
    case = final / str(ids[0])
    subject.private_write(case / "selector-test.claim.private.json", {"claim_token": "owned"})
    held = case.relative_to(subject.ROOT).as_posix()
    value = inventory(evidence)
    assert value["retained_unreconciled_case_paths"] == [held]
    assert value["retained_unreconciled_binary_files"] == 1
    subject.private_write(case / "selector-test.response.private.json", {"usage": {"input_tokens": 1}})
    subject.private_write(case / "jev-attempt-test.started.private.json", {"attempt_id": "jev-attempt-test"})
    assert inventory(evidence)["retained_unreconciled_case_paths"] == [held]
    subject.private_write(case / "jev-attempt-test.outcome.private.json", {"usage_known": False})
    assert inventory(evidence)["retained_unreconciled_case_paths"] == [held]


def test_gemini_remote_unknown_cleanup_blocks_local_cleanup(evidence):
    final, _, _, _, _, ids = evidence
    case = final / str(ids[0])
    subject.private_write(case / "gemini-selected-test.started.private.json", {"attempt_id": "test"})
    subject.private_write(case / "gemini-selected-test.outcome.private.json",
        {"provider_usage_known": True, "remote_cleanup_pending": 0, "remote_cleanup_unknown": True})
    assert inventory(evidence)["retained_unreconciled_case_paths"] == [case.relative_to(subject.ROOT).as_posix()]


def test_unscoped_uncertain_payment_still_blocks_entire_cleanup(evidence):
    final = evidence[0]
    subject.private_write(final / "selector-unscoped.claim.private.json", {"claim_token": "owned"})
    with pytest.raises(subject.CleanupError, match="reconciled"):
        inventory(evidence)


def test_unreconciled_prior_case_retains_all_its_binaries_and_no_other_case(evidence, monkeypatch):
    final, verification, archive, original, cache, ids = evidence
    case = original / str(ids[0])
    subject.private_write(case / "selector-test.claim.private.json", {"claim_token": "owned"})
    # LimitReached plus absent provider attempts does not prove no billable call.
    subject.private_write(case / "outcome.private.json", {"status": "needs_review", "error_type": "LimitReached"})
    (case / "other.pdf").write_bytes(b"%PDF-preserve-uncertain-context")
    value = inventory(evidence)
    assert value["retained_unreconciled_case_paths"] == [case.relative_to(subject.ROOT).as_posix()]
    assert value["retained_unreconciled_binary_files"] == 2
    assert all(not row["delete"] for row in value["files"] if row.get("case_path") == case.relative_to(subject.ROOT).as_posix())
    cloud = Cloud()
    monkeypatch.setattr(subject, "r2_client", lambda: (cloud, "private-test-bucket"))
    result = subject.execute_inventory(value, archive)
    assert result["deleted_files"] == 29
    assert (case / "original.zip").exists() and (case / "other.pdf").exists()
    assert (case / "selector-test.claim.private.json").exists()
    assert not (final / str(ids[0]) / "selected.pdf").exists()
    assert not (original / str(ids[1]) / "original.zip").exists()
    assert result["retained_unreconciled_case_paths"] == value["retained_unreconciled_case_paths"]


def test_archive_deduplicates_identical_content_and_retains_logical_path_mapping(evidence):
    value = inventory(evidence)
    archive, facts = subject.build_archive(value, evidence[2])
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        stored = json.load(tar.extractfile("manifest.private.json"))
    assert stored == value and len(names) == value["unique_blobs"] + 1
    shared = hashlib.sha256(b"%PDF-synthetic-shared-content").hexdigest()
    assert names.count("blobs/" + shared) == 1
    assert sum(row["sha256"] == shared for row in value["files"]) == 20
    assert subject.build_archive(value, evidence[2]) == (archive, facts)


def test_existing_corrupt_archive_is_never_uploaded_or_authorizes_deletion(evidence):
    value = inventory(evidence)
    archive, _ = subject.build_archive(value, evidence[2])
    archive.write_bytes(b"corrupt")
    with pytest.raises(subject.CleanupError, match="validation"):
        subject.build_archive(value, evidence[2])
    assert (evidence[0] / "1000/selected.pdf").exists()


def test_mutation_after_inventory_blocks_before_archive_upload(evidence):
    value = inventory(evidence)
    (evidence[0] / "1000/selected.pdf").write_bytes(b"changed")
    with pytest.raises(subject.CleanupError, match="changed"):
        subject.build_archive(value, evidence[2])
    assert (evidence[0] / "1001/selected.pdf").exists()


class Cloud:
    def __init__(self, *, corrupt_get=False, bad_head=False):
        self.body = None
        self.metadata = {}
        self.corrupt_get, self.bad_head = corrupt_get, bad_head
        self.closed = False

    def head_object(self, **kwargs):
        if self.body is None:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {"ContentLength": len(self.body) + int(self.bad_head), "Metadata": self.metadata}

    def put_object(self, **kwargs):
        self.body = kwargs["Body"].read()
        self.metadata = kwargs["Metadata"]

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.body + b"bad" if self.corrupt_get else self.body)}

    def close(self):
        self.closed = True


@pytest.mark.parametrize("failure", ["head", "get"])
def test_cloud_verify_failure_never_deletes_any_binary(evidence, monkeypatch, failure):
    value = inventory(evidence)
    cloud = Cloud(corrupt_get=failure == "get", bad_head=failure == "head")
    monkeypatch.setattr(subject, "r2_client", lambda: (cloud, "private-test-bucket"))
    with pytest.raises(subject.CleanupError):
        subject.execute_inventory(value, evidence[2])
    assert all((subject.ROOT / row["logical_path"]).exists() for row in value["files"])
    assert cloud.closed


def test_cloud_success_before_deletion_retains_json_pins_archive_and_other_cases(evidence, monkeypatch):
    value = inventory(evidence)
    cloud = Cloud()
    monkeypatch.setattr(subject, "r2_client", lambda: (cloud, "private-test-bucket"))
    unrelated = evidence[3] / "999999"
    unrelated.mkdir()
    (unrelated / "keep.pdf").write_bytes(b"preserve")
    result = subject.execute_inventory(value, evidence[2])
    assert result["deleted_files"] == value["binary_files"]
    assert result["archive"]["head_verified"] and result["archive"]["get_sha_verified"]
    for row in value["files"]:
        assert (subject.ROOT / row["logical_path"]).exists() is not row["binary"]
    assert (unrelated / "keep.pdf").exists()
    assert Path(result["local_archive_path"]).exists()
    assert list(evidence[2].glob("cloud-backup-*.private.json"))
    assert list(evidence[2].glob("cleanup-summary-*.private.json"))
    assert cloud.closed


def test_mutation_during_cloud_backup_blocks_entire_delete_set(evidence, monkeypatch):
    value = inventory(evidence)
    cloud = Cloud()
    monkeypatch.setattr(subject, "r2_client", lambda: (cloud, "private-test-bucket"))
    actual = subject.cloud_backup
    def mutate(*args):
        result = actual(*args)
        (evidence[0] / "1000/selected.pdf").write_bytes(b"changed after archive")
        return result
    monkeypatch.setattr(subject, "cloud_backup", mutate)
    with pytest.raises(subject.CleanupError, match="changed"):
        subject.execute_inventory(value, evidence[2])
    assert all((subject.ROOT / row["logical_path"]).exists() for row in value["files"])


def test_two_gib_byte_limit_is_enforced_before_cloud(evidence, monkeypatch):
    monkeypatch.setattr(subject, "MAX_BYTES", 8)
    with pytest.raises(subject.CleanupError, match="GiB"):
        inventory(evidence)


def test_default_dry_run_never_creates_archive_or_calls_cloud(evidence, monkeypatch, capsys):
    final, verification, archive, *_ = evidence
    monkeypatch.setattr(subject.sys, "argv", ["cleanup", "--run-folder", str(final),
        "--verification-file", str(verification), "--archive-folder", str(archive)])
    cloud = MagicMock(side_effect=AssertionError("Dry run has no cloud calls"))
    monkeypatch.setattr(subject, "r2_client", cloud)
    assert subject.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["cloud_calls"] == result["deleted_files"] == 0
    assert not archive.exists()
    assert (final / "1000/selected.pdf").exists()
    cloud.assert_not_called()
