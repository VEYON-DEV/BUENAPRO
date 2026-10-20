"""Offline same-owner recovery guards; no provider, production or database I/O."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/recover_din_known_commercial_review.py"
SPEC = importlib.util.spec_from_file_location("known_commercial_review_under_test", SCRIPT)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Provider/source HTTP is forbidden in this offline recovery")
    monkeypatch.setattr(httpx.Client, "request", denied)
    monkeypatch.setattr(httpx.AsyncClient, "request", denied)


@pytest.fixture(scope="module")
def actual():
    if not (recovery.RUN_FOLDER / str(recovery.CONVOCATION)).exists():
        pytest.skip("Exact private paid fixture absent; synthetic guards still run")
    return recovery.local_material(recovery.RUN_FOLDER, recovery.RESPONSE_SHA)


def synthetic():
    return {"record": {"procedure_id": 123, "description": "Supervisión de mantenimiento eléctrico",
        "object_type": "service", "detail_hash": "a" * 64, "closes_at": "2026-11-10T23:59:00-05:00"},
        "claim": {"id": recovery.EVALUATION_ID, "evaluation_key": "b" * 64,
                  "claim_token": "00000000-0000-4000-8000-000000000001"},
        "bundle": {"model": "gemini-2.5-flash-lite", "input_tokens": 67331, "output_tokens": 22859,
                   "cost_usd": .0158767, "document_uuid": "00000000-0000-4000-8000-000000000002",
                   "document_sha256": "c" * 64, "requires_human_review": True},
        "proof": {"requires_human_review": True}, "source_url": "https://example.invalid/unused",
        "evidence": []}


class Reply:
    def __init__(self, value):
        self.value = value
    def fetchone(self):
        return self.value
    def fetchall(self):
        return self.value


class FakePG:
    def __init__(self, material):
        record, claim = material["record"], material["claim"]
        self.source = {"id_procedimiento": record["procedure_id"], "description": record["description"],
            "row_hash": "d" * 64, "object_type": record["object_type"],
            "opportunity_id": "00000000-0000-4000-8000-000000000003", "detail_hash": record["detail_hash"],
            "proposals_closes_at": datetime.fromisoformat(record["closes_at"])}
        self.profile = {"id": recovery.SOURCE_PROFILE_ID, "tenant_id": "tenant-original",
                        "profile_hash": "profile-original", "is_active": True}
        self.evaluation = {"id": recovery.EVALUATION_ID, "id_convocatoria": recovery.CONVOCATION,
            "kind": "gemini_extraction", "request_hash": recovery.REQUEST_HASH,
            "evaluation_key": claim["evaluation_key"], "model": material["bundle"]["model"],
            "policy_version": "prod2_document_extraction_v1", "profile_id": recovery.SOURCE_PROFILE_ID,
            "claim_token": claim["claim_token"], "status": "uncertain", "result_json": None,
            "input_tokens": None, "output_tokens": None, "cost_usd": None}
        self.extractions, self.events, self.completed = [], [], []
        self.locked, self.depth = True, 0

    @contextmanager
    def transaction(self):
        before = deepcopy((self.evaluation, self.extractions, self.completed))
        self.depth += 1
        self.events.append("begin")
        try:
            yield
        except Exception:
            self.evaluation, self.extractions, self.completed = before
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.depth -= 1

    def execute(self, statement, params=()):
        assert self.depth == 1
        self.events.append(statement)
        if statement.startswith("SET TRANSACTION"):
            return Reply(None)
        assert statement.startswith("SELECT"), "Only explicit completion functions may write"
        if "pg_try_advisory_xact_lock" in statement:
            return Reply({"locked": self.locked})
        if "FROM prod2_discoveries" in statement:
            return Reply(self.source)
        if "FROM company_profiles" in statement:
            return Reply(self.profile)
        if "FROM prod2_evaluations" in statement:
            return Reply([self.evaluation])
        if "FROM prod2_document_extractions" in statement:
            return Reply(self.extractions)
        raise AssertionError("Unexpected query in isolated recovery contract")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    material = synthetic()
    case = tmp_path / str(recovery.CONVOCATION)
    case.mkdir()
    monkeypatch.setattr(recovery, "require_folder", lambda path: Path(path).absolute())
    monkeypatch.setattr(recovery, "local_material", lambda *args: deepcopy(material))
    monkeypatch.setattr(recovery, "runtime_bindings", lambda: {"code.py": "a" * 64})
    monkeypatch.setattr(recovery, "require_committed", lambda *args: None)
    conn = FakePG(material)
    plan = recovery.prepare(conn, tmp_path, recovery.RESPONSE_SHA)
    conn.events.clear()
    def complete(conn, identity, token, result, **usage):
        assert conn.depth == 1 and identity == recovery.EVALUATION_ID
        assert token == conn.evaluation["claim_token"]
        assert usage == {key: material["bundle"][key] for key in ("input_tokens", "output_tokens", "cost_usd")}
        conn.completed.append((identity, token, deepcopy(result)))
        conn.evaluation.update(status="done", result_json=deepcopy(result), **usage)
    def persist(conn, **kwargs):
        assert conn.depth == 1 and conn.evaluation["status"] == "done"
        assert kwargs["extraction"]["evaluation_id"] == recovery.EVALUATION_ID
        assert kwargs["convocation_id"] == recovery.CONVOCATION
        conn.extractions.append({"id": 99, "evaluation_id": recovery.EVALUATION_ID})
        return {"extraction_id": 99, "requires_human_review": True, "projection_pending": True,
                "canonical_participation_preserved": True}
    monkeypatch.setattr(recovery, "complete_evaluation", complete)
    monkeypatch.setattr(recovery, "persist_completed_llm1", persist)
    return tmp_path, material, conn, plan


def test_real_paid_body_reuses_normalizer_exact_request_facets_and_all_quotes(actual):
    bundle = actual["bundle"]
    assert bundle["cache_key"] == "prod2:llm1:" + recovery.REQUEST_HASH
    assert bundle["model"] == "gemini-2.5-flash-lite"
    assert len(bundle["facets"]) == 48
    assert bundle["requires_human_review"]
    assert len(bundle["raw_extraction_json"]["retrieval_evidence_audit"]["errors"]) == 47
    assert len(bundle["raw_extraction_json"]["evidence_citations"]) == 96
    assert bundle["raw_extraction_json"]["commercial_classification"]["primary_area"] == "sin_clasificar"
    assert bundle["raw_extraction_json"]["commercial_classification"]["support_status"] == "needs_review"
    assert bundle["known_commercial_review_projection"]["changed_paths"] == ["commercial_classification"]


def test_offline_decoder_never_modifies_original_paid_receipts(actual):
    before = {row["path"]: recovery.file_sha(row["path"]) for row in actual["evidence"]}
    again = recovery.local_material(recovery.RUN_FOLDER, recovery.RESPONSE_SHA)
    assert again["bundle"] == actual["bundle"]
    assert before == {path: recovery.file_sha(path) for path in before}


def test_execute_requires_explicit_go_before_any_work():
    with pytest.raises(recovery.RecoveryError, match="--go"):
        recovery.execute(None, "/unused", "unused", recovery.RESPONSE_SHA)


def test_same_original_owner_completion_and_persistence_share_one_transaction(isolated):
    folder, material, conn, plan = isolated
    result = recovery.execute(conn, folder, recovery.file_sha(folder / str(recovery.CONVOCATION) / recovery.PLAN_NAME), recovery.RESPONSE_SHA, go=True)
    assert conn.events[-1] == "commit" and conn.depth == 0
    assert len(conn.completed) == 1 and conn.completed[0][0] == recovery.EVALUATION_ID
    assert result["known_original_cost_usd"] == .0158767
    assert result["new_inference_cost_usd"] == 0 and result["new_provider_calls"] == 0
    assert result["saved"]["projection_pending"] and result["old_stop_preserved"]
    assert not result["semantic_qa_complete"]
    assert (folder / str(recovery.CONVOCATION) / recovery.COMPLETE_NAME).is_file()


def test_persistence_failure_rolls_back_evaluation_completion_together(isolated, monkeypatch):
    folder, _, conn, plan = isolated
    def fail(*args, **kwargs):
        assert conn.evaluation["status"] == "done"
        raise ValueError("Synthetic canonical persistence failure")
    monkeypatch.setattr(recovery, "persist_completed_llm1", fail)
    with pytest.raises(ValueError, match="Synthetic"):
        recovery.execute(conn, folder, recovery.file_sha(folder / str(recovery.CONVOCATION) / recovery.PLAN_NAME), recovery.RESPONSE_SHA, go=True)
    assert conn.evaluation["status"] == "uncertain" and not conn.completed and not conn.extractions
    assert conn.events[-1] == "rollback"
    assert not (folder / str(recovery.CONVOCATION) / recovery.COMPLETE_NAME).exists()


@pytest.mark.parametrize("field,value", [("claim_token", "other-owner"), ("request_hash", "f" * 64),
    ("profile_id", "foreign-profile"), ("status", "done"), ("input_tokens", 1)])
def test_changed_evaluation_identity_or_completed_usage_blocks_before_writes(isolated, field, value):
    folder, _, conn, plan = isolated
    conn.evaluation[field] = value
    with pytest.raises(recovery.RecoveryError):
        recovery.execute(conn, folder, recovery.file_sha(folder / str(recovery.CONVOCATION) / recovery.PLAN_NAME), recovery.RESPONSE_SHA, go=True)
    assert not conn.completed and not conn.extractions


@pytest.mark.parametrize("changed", ["profile", "source", "existing", "coordinator"])
def test_current_profile_source_reading_and_owner_are_rechecked(isolated, changed):
    folder, _, conn, plan = isolated
    if changed == "profile":
        conn.profile["profile_hash"] = "changed"
    elif changed == "source":
        conn.source["row_hash"] = "changed"
    elif changed == "existing":
        conn.extractions.append({"id": 2, "evaluation_id": 123})
    else:
        conn.locked = False
    with pytest.raises(recovery.RecoveryError):
        recovery.execute(conn, folder, recovery.file_sha(folder / str(recovery.CONVOCATION) / recovery.PLAN_NAME), recovery.RESPONSE_SHA, go=True)
    assert not conn.completed


def test_changed_derived_body_blocks_before_pg_transaction(isolated):
    folder, _, conn, plan = isolated
    path = folder / str(recovery.CONVOCATION) / recovery.DERIVED_NAME
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        recovery.execute(conn, folder, recovery.file_sha(folder / str(recovery.CONVOCATION) / recovery.PLAN_NAME), recovery.RESPONSE_SHA, go=True)
    assert not conn.events and not conn.completed


def test_cli_rejects_missing_go_before_opening_production(monkeypatch):
    monkeypatch.setattr(recovery, "database", lambda *args: pytest.fail("Database must not open"))
    with pytest.raises(recovery.RecoveryError, match="--go"):
        recovery.main(["execute", "--folder", str(recovery.RUN_FOLDER),
                       "--expected-response-sha256", recovery.RESPONSE_SHA])


def test_explicit_wrong_raw_sha_cannot_reach_database(monkeypatch):
    monkeypatch.setattr(recovery, "database", lambda *args: pytest.fail("Database must not open"))
    with pytest.raises(recovery.RecoveryError, match="response SHA"):
        recovery.main(["prepare", "--folder", str(recovery.RUN_FOLDER),
                       "--expected-response-sha256", "0" * 64])


def test_unknown_runtime_must_be_committed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "code.py"
    path.write_text("local bytes")
    monkeypatch.setattr(recovery, "ROOT", root)
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k:
                        type("Result", (), {"returncode": 0, "stdout": b"committed bytes"})())
    with pytest.raises(recovery.RecoveryError, match="Commit"):
        recovery.require_committed({"code.py": hashlib.sha256(path.read_bytes()).hexdigest()})
