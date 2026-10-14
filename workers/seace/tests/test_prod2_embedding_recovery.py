"""Offline scope, immutable adoption and source/provider exclusion contracts."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("embedding_recovery_runner", ROOT / "scripts/recover_prod2_embeddings.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)

CODE = "00000000-0000-4000-8000-000000000001"


def fixture(tmp_path, subset=()):
    original = tmp_path / "original"
    original.mkdir()
    config = json.loads((ROOT / "docs/sources/prod2-embedding-reading.runtime.v2.json").read_text())
    manifest = {"run_id": "synthetic-pilot", "pipeline": "prod2_selected_llm1_v1", "terminal_stage": "llm1",
        "max_accepted": 10, "jev_threshold": 60, "llm1_input_policy": "prod2_embedding_retrieved_text_v1",
        "document_storage_policy": "prod2_official_links_temporary_v1", "llm1_scope_policy": "prod2_qualification_focus_v1",
        "llm1_visual_review_policy": "prod2_ocr_text_layout_review_v2",
        "selector_policy_version": "prod2_jev_document_selection_v3", "max_source_mib": 128,
        "embedding_reading": {"config": config, "config_sha256": subject.config_hash(config)}}
    subject.private_write(original / "manifest.private.json", manifest)
    records = []
    for conv in range(1, 11):
        private = original / str(conv)
        private.mkdir()
        outcome = {"convocation_id": conv, "accepted": True, "status": "needs_review", "affinity_index": 70,
            "description": "SOFTWARE", "error_type": "EmbeddingPreparationReview"}
        subject.private_write(private / "outcome.private.json", outcome)
        records.append(outcome)
        subject.private_write(private / "detail.private.json", {"observed_at": "2026-10-08T00:00:00+00:00",
            "detail": {"convocatoria_id": conv, "description": "SOFTWARE", "document_inventory_complete": True,
                "documents": [{"uuid": CODE, "filename": "Bases.pdf"}], "schedule": []}})
        content = b"%PDF-1.7 offline original never sent to a model"
        (private / (CODE + ".pdf")).write_bytes(content)
        subject.private_write(private / (CODE + ".download.private.json"), {
            "document_uuid": CODE, "format": "pdf", "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "source_url": subject.METADATA_URL + "?id=" + CODE})
        subject.private_write(private / "selection.private.json", {"policy_version": manifest["selector_policy_version"],
            "request_hash": "1" * 64, "selected_ids": ["loose:" + CODE]})
    subject.private_write(original / "report-1.private.json", {"run_id": manifest["run_id"], "accepted": 10,
        "llm1_complete": 0, "records": records, "costs": {"usage_unknown_attempts": 0, "llm1_calls": 0}})
    corrected = deepcopy(config)
    corrected["configuration_revision"] = "synthetic_explicit_corrected_configuration"
    config_path = tmp_path / "corrected.json"
    config_path.write_text(json.dumps(corrected))
    values = ["--original-folder", str(original), "--folder", str(tmp_path / "recovery"),
        "--embedding-config", str(config_path), "--prepare-local"]
    for conv in subset:
        values.extend(["--convocation-id", str(conv)])
    args = subject.parser().parse_args(values)
    return args, subject.plan(args), records


def test_default_scope_exactly_ten_and_subset_does_not_expand(tmp_path):
    args, manifest, _ = fixture(tmp_path, (2, 1))
    assert manifest["accepted_scope_ids"] == list(range(1, 11))
    assert manifest["recovery_ids"] == [1, 2]
    assert manifest["no_discovery"] and manifest["no_redis"] and manifest["no_downloads"]
    assert manifest["max_jev_calls"] == manifest["max_source_requests"] == 0
    assert manifest["run_id"] == "synthetic-pilot" and manifest["recovery_run_id"] == "recovery"
    args.convocation_id = [11]
    with pytest.raises(ValueError, match="subset"):
        subject.plan(args)


def test_duplicate_ids_fail(tmp_path):
    args, *_ = fixture(tmp_path)
    args.convocation_id = [1, 1]
    with pytest.raises(ValueError, match="subset"):
        subject.plan(args)


@pytest.mark.parametrize("change", ["score", "kind", "record", "unknown", "reader"])
def test_historical_or_uncertain_attempts_do_not_enter_recovery(tmp_path, monkeypatch, change):
    args, _, records = fixture(tmp_path)
    original = args.original_folder
    if change in {"unknown", "reader"}:
        summary = subject.audit_summary(original)
        summary["usage_unknown_attempts" if change == "unknown" else "llm1_calls"] = 1
        monkeypatch.setattr(subject, "audit_summary", lambda _: summary)
    else:
        value = subject.read_private(original / "1/outcome.private.json")
        value[{"score": "affinity_index", "kind": "error_type", "record": "description"}[change]] = {
            "score": 59, "kind": "ServerError", "record": "CHANGED"}[change]
        (original / "1/outcome.private.json").write_text(json.dumps(value))
        if change != "record":
            report = subject.read_private(original / "report-1.private.json")
            report["records"][0] = value
            (original / "report-1.private.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        subject.plan(args)
    assert not args.folder.exists()


def test_local_adoption_preserves_source_and_has_no_external_actions(tmp_path, monkeypatch):
    args, manifest, _ = fixture(tmp_path, (1, 2))
    before = {str(p.relative_to(args.original_folder)): subject.sha(p) for p in args.original_folder.rglob("*") if p.is_file()}
    monkeypatch.setattr(subject, "production_settings", lambda **_: pytest.fail("Local preparation opened production"))
    monkeypatch.setattr(subject, "member_proof", lambda *_: pytest.fail("Local preparation queried PostgreSQL"))
    receipt = subject.prepare_local(manifest)
    assert receipt["files_count"] == 6
    assert all(receipt[k] == 0 for k in ("source_calls", "provider_calls", "postgres_calls", "redis_calls"))
    assert subject.verify_adoption(manifest) == receipt
    assert subject.prepare_local(manifest) == receipt
    assert not (args.folder / "3").exists()
    after = {str(p.relative_to(args.original_folder)): subject.sha(p) for p in args.original_folder.rglob("*") if p.is_file()}
    assert before == after
    assert not (args.folder / "1/outcome.private.json").exists()
    assert not (args.folder / "1/selection.private.json").exists()


@pytest.mark.parametrize("where", ["source", "target"])
def test_changed_original_or_keeper_blocks_execution_before_database(tmp_path, where):
    args, manifest, _ = fixture(tmp_path, (1,))
    subject.prepare_local(manifest)
    parent = args.original_folder if where == "source" else args.folder
    (parent / "1" / (CODE + ".pdf")).write_bytes(b"changed original or keeper")
    with pytest.raises(ValueError, match="changed"):
        subject.verify_adoption(manifest)


def test_preview_adoption_records_serialization_change_without_forging_source_hash(tmp_path, monkeypatch):
    args, manifest, _ = fixture(tmp_path, (1,))
    relative = Path("text-previews/source/document-preview-cache/preview.private.json")
    source = args.original_folder / "1" / relative
    source.parent.mkdir(parents=True)
    value = {"preview_text": "Software requirements", "local_only": True}
    source.write_text(json.dumps(value, indent=2))
    def validated_preview_copy(roots, private):
        subject.private_write(private / relative, value)
    monkeypatch.setattr(subject.recovery, "cached_previews", validated_preview_copy)
    receipt = subject.prepare_local(manifest)
    fact = next(f for f in receipt["files"] if f.get("adoption_mode") == "validated_preview_json")
    assert fact["sha256"] == subject.sha(source)
    assert fact["target_sha256"] != fact["sha256"]
    assert fact["canonical_hash"] == subject.hash_request(value)
    assert subject.verify_adoption(manifest) == receipt
    (args.folder / "1" / relative).write_text(json.dumps({**value, "local_only": False}))
    with pytest.raises(ValueError, match="changed"):
        subject.verify_adoption(manifest)


def test_missing_download_receipt_never_falls_back_to_http(tmp_path):
    args, manifest, _ = fixture(tmp_path, (1,))
    (args.original_folder / "1" / (CODE + ".download.private.json")).unlink()
    with pytest.raises(FileNotFoundError):
        subject.prepare_local(manifest)
    with pytest.raises(RuntimeError, match="forbids"):
        subject.NoSourcePacer()(object())


def test_symlinks_and_nested_folders_rejected(tmp_path):
    args, *_ = fixture(tmp_path)
    link = tmp_path / "symlink"
    link.symlink_to(args.original_folder, target_is_directory=True)
    args.original_folder = link
    with pytest.raises(ValueError, match="symlink"):
        subject.plan(args)
    args.original_folder = tmp_path / "original"
    args.folder = args.original_folder / "nested"
    with pytest.raises(ValueError, match="separate"):
        subject.plan(args)


def test_frozen_config_and_scope_cannot_change_after_prepare(tmp_path):
    args, manifest, _ = fixture(tmp_path, (1,))
    subject.prepare_local(manifest)
    assert subject.plan(args) == manifest
    args.convocation_id = [2]
    with pytest.raises(ValueError, match="Frozen recovery"):
        subject.plan(args)


def test_existing_terminal_recovery_outcome_is_not_retried(tmp_path, monkeypatch):
    args, manifest, _ = fixture(tmp_path, (1,))
    subject.prepare_local(manifest)
    outcome = {"convocation_id": 1, "status": "needs_review", "recovery_manifest_hash": subject.hash_request(manifest)}
    subject.private_write(args.folder / "1/outcome.private.json", outcome)
    monkeypatch.setattr(subject.recovery, "prepare_member", lambda *_: pytest.fail("A terminal attempt was replayed"))
    assert subject.recover_member(None, {"convocation_id": 1}, manifest, None, "unused") == outcome


def test_successful_outcome_is_published_before_verified_cleanup(tmp_path, monkeypatch):
    args, manifest, _ = fixture(tmp_path, (1,))
    subject.prepare_local(manifest)
    monkeypatch.setattr(subject.recovery, "prepare_member", lambda *_: {"row": {"convocation_id": 1}})
    monkeypatch.setattr(subject.selected, "enrich", lambda *_: {"status": "llm1_complete", "llm1_cost_usd": 0.001})
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    monkeypatch.setattr(subject.psycopg, "connect", lambda *_, **__: Connection())
    cleanups = []
    def cleanup(conn, private, record, frozen):
        assert subject.read_private(private / "outcome.private.json") == record
        assert record["status"] == "llm1_complete"
        cleanups.append(private)
    monkeypatch.setattr(subject.selected, "cleanup_finished_case", cleanup)
    subject.STOP.clear()
    proof = {"convocation_id": 1, "affinity_index": 70}
    result = subject.recover_member(SimpleNamespace(database_url="fake-not-opened"), proof, manifest, None, "unused")
    assert result["status"] == "llm1_complete" and cleanups == [args.folder / "1"]
    # Repeated execute returns the paid outcome and never repeats cleanup/model.
    assert subject.recover_member(None, proof, manifest, None, "unused") == result


def test_dry_run_reads_scope_only_and_cannot_connect(tmp_path, monkeypatch, capsys):
    args, _, _ = fixture(tmp_path, (1, 2))
    args.prepare_local, args.dry_run = False, True
    monkeypatch.setattr(subject.parser(), "parse_args", lambda: args)
    monkeypatch.setattr(subject, "parser", lambda: type("Parser", (), {"parse_args": lambda _: args})())
    monkeypatch.setattr(subject, "production_settings", lambda **_: pytest.fail("Dry-run opened production"))
    assert subject.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope_ids"] == list(range(1, 11)) and result["execution_ids"] == [1, 2]
    assert result["provider_calls"] == result["postgres_calls"] == result["redis_calls"] == 0
    assert not args.folder.exists()
