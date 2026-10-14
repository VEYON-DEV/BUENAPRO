"""Exact derived-cap repair tests; no network, model or real database access."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import repair_prod2_penalty_caps as subject


@pytest.fixture
def evidence(tmp_path):
    folder = tmp_path / "source"
    raw = {"penalties": [{"cap_pct": 10, "description": "Disponibilidad 99%, tope máximo de penalidades10%."}]}
    summary = {**subject.audit.derive_summary(raw), subject.CAP: 99}
    bundle = {"cache_key": "prod2:llm1:" + "a" * 64, "raw_extraction_json": raw, "summary_json": deepcopy(summary),
        "document_set_hash": "b" * 64, "analysis_sha256": "c" * 64, "model": "gemini-3.1-flash-lite",
        "document_uuid": "bc4da6f4-41be-4c60-9421-c14e6b5c95d3", "original_sha256": "d" * 64}
    row = {"id": 41, "id_convocatoria": 100, "evaluation_id": 121, "is_current": True,
        "raw_extraction_json": deepcopy(raw), "summary_json": deepcopy(summary), "requires_human_review": True,
        "model": bundle["model"], "codigo_alfresco": bundle["document_uuid"], "sha256_original": bundle["original_sha256"]}
    evaluation = {"id": 121, "id_convocatoria": 100, "kind": "gemini_extraction", "status": "done",
        "result_json": deepcopy(bundle), "request_hash": "a" * 64, "model": bundle["model"]}
    prior = {"current_extraction_ids": {str(conv): 30 + conv for conv in range(1, 11)},
        "raw_sha256": "synthetic frozen previous ten"}
    local = {"ids": list(range(100, 190)), "prior_ids": list(range(1, 11)), "complete": [100], "blocked": [101],
        "outcomes": {100: {"status": "llm1_complete", "accepted": True, "extraction_id": 41,
            "document_set_hash": bundle["document_set_hash"], "llm1_model": bundle["model"]}},
        "manifest": {"run_id": "synthetic-pending90"}, "baseline": {"prior_snapshot": deepcopy(prior)},
        "prepared": {100: {"analysis_sha256": bundle["analysis_sha256"]}}, "bundles": {100: deepcopy(bundle)},
        "source_folder": folder, "outcome_folders": {100: folder}, "recovery_roots": [], "retry_roots": []}
    subject.audit.private_write(folder / "100" / ("gemini_extraction-" + "a" * 64 + ".claim.private.json"), {"id": 121})
    value = {"rows": [row], "evaluations": [evaluation], "facets": [{"id": 601, "details_json": {"cap_pct": 10}}],
        "participation": [{"consortium_status": "not_identified", "subcontracting_status": "not_identified"}],
        "prior": deepcopy(prior), "receipts_sha256": subject.receipt_fingerprint(local)}
    args = SimpleNamespace(folder=folder, prior_folder=tmp_path / "prior", output=tmp_path / "audit.private.json",
        execute=False, allow_incomplete=False)
    return SimpleNamespace(local=local, value=value, args=args)


def test_only_cap_changes_and_cutoff_comes_from_frozen_previous_ids(evidence):
    frozen = deepcopy(evidence.value)
    changes, cutoff = subject.plan_changes(evidence.local, evidence.value)
    assert cutoff == 40 and len(changes) == 1
    assert changes[0]["before"] == 99 and changes[0]["after"] == 10
    assert evidence.value == frozen
    result = subject.report(evidence.local, evidence.value, changes, cutoff, executed=False)
    assert result["db_writes"] == result["updated_rows"] == 0 and result["planned_changes"] == 1
    assert "row" not in result["changes"][0] and "raw_extraction_json" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("id", 40), ("is_current", False), ("evaluation_id", 999), ("requires_human_review", False)])
def test_cannot_repair_old_unowned_or_unreviewed_extraction(evidence, field, value):
    evidence.value["rows"][0][field] = value
    with pytest.raises(subject.audit.PendingVerificationError):
        subject.plan_changes(evidence.local, evidence.value)


@pytest.mark.parametrize("where", ["raw", "paid_raw", "paid_request", "local_paid", "paid_status", "claim"])
def test_raw_paid_receipts_and_evaluation_id_are_exact(evidence, where):
    if where == "raw": evidence.value["rows"][0]["raw_extraction_json"] = {}
    elif where == "paid_raw": evidence.value["evaluations"][0]["result_json"]["raw_extraction_json"] = {}
    elif where == "paid_request": evidence.value["evaluations"][0]["request_hash"] = "f" * 64
    elif where == "local_paid": evidence.local["bundles"][100]["summary_json"][subject.CAP] = 80
    elif where == "paid_status": evidence.value["evaluations"][0]["status"] = "uncertain"
    else:
        path = next(evidence.args.folder.rglob("*.claim.private.json"))
        path.unlink()
        subject.audit.private_write(path, {"id": 999})
    with pytest.raises(subject.audit.PendingVerificationError):
        subject.plan_changes(evidence.local, evidence.value)


@pytest.mark.parametrize("where", ["canonical", "paid"])
def test_repair_refuses_other_derivation_changes(evidence, where):
    target = evidence.value["rows"][0]["summary_json"] if where == "canonical" else evidence.value["evaluations"][0]["result_json"]["summary_json"]
    target["modalidad"] = "unrelated changed value"
    if where == "paid": evidence.local["bundles"][100] = deepcopy(evidence.value["evaluations"][0]["result_json"])
    with pytest.raises(subject.audit.PendingVerificationError, match="repair_other_derived_summary_difference"):
        subject.plan_changes(evidence.local, evidence.value)


def test_replay_of_repaired_cap_is_noop_preserving_original_paid_summary(evidence):
    evidence.value["rows"][0]["summary_json"][subject.CAP] = 10
    assert subject.plan_changes(evidence.local, evidence.value)[0] == []
    assert evidence.value["evaluations"][0]["result_json"]["summary_json"][subject.CAP] == 99


@pytest.mark.parametrize("field", ["rows", "evaluations", "facets", "participation", "prior", "receipts_sha256"])
def test_protected_fingerprints_detect_every_immutable_surface(evidence, field):
    before = subject.protected_fingerprints(evidence.value)
    if field == "rows": evidence.value[field][0]["raw_extraction_json"] = {}
    elif field in {"evaluations", "facets", "participation"}: evidence.value[field][0]["unexpected_change"] = True
    elif field == "prior": evidence.value[field]["raw_sha256"] = "changed"
    else: evidence.value[field] = "changed"
    assert before != subject.protected_fingerprints(evidence.value)


def test_cap_is_the_only_fingerprint_exclusion(evidence):
    before = subject.protected_fingerprints(evidence.value)
    evidence.value["rows"][0]["summary_json"][subject.CAP] = 10
    assert subject.protected_fingerprints(evidence.value) == before
    evidence.value["rows"][0]["summary_json"]["modalidad"] = "changed"
    assert subject.protected_fingerprints(evidence.value) != before


class Connection:
    def __init__(self, evidence):
        self.evidence, self.queries, self.coordinator, self.running, self.cas_ok = evidence, [], True, 0, True
        self.change_facets, self.locked_snapshots, self.committed = False, [], False
    def __enter__(self):
        self.saved = deepcopy(self.evidence.value)
        return self
    def __exit__(self, kind, *args):
        self.committed = kind is None
        if kind is not None: self.evidence.value = self.saved
    def execute(self, query, params=None):
        self.queries.append((query, params))
        rows = self.evidence.value
        if query.startswith("SET "): result = None
        elif "pg_try_advisory_xact_lock" in query: result = {"locked": self.coordinator}
        elif "AS n" in query: result = {"n": self.running}
        elif "pg_locks" in query: result = {"active": False}
        elif query.startswith("UPDATE"):
            assert "SET summary_json=jsonb_set" in query
            assert params[1:4] == (41, 100, 121)
            assert json.loads(params[4]) == rows["rows"][0]["summary_json"]
            assert json.loads(params[5]) == rows["rows"][0]["raw_extraction_json"]
            rows["rows"][0]["summary_json"][subject.CAP] = json.loads(params[0])
            if self.change_facets: rows["facets"][0]["unexpected_change"] = True
            result = {"id": 41} if self.cas_ok else None
        elif query.startswith("SELECT * FROM prod2_document_extractions"):
            self.locked_snapshots.append(" FOR UPDATE" in query)
            result = deepcopy(rows["rows"])
        elif query.startswith("SELECT * FROM prod2_evaluations"): result = deepcopy(rows["evaluations"])
        elif query.startswith("SELECT * FROM prod2_requirement_facets"): result = deepcopy(rows["facets"])
        elif "JOIN opportunities" in query: result = deepcopy(rows["participation"])
        else: pytest.fail("Unexpected query: " + query)
        return SimpleNamespace(fetchone=lambda: result, fetchall=lambda: result)


@pytest.fixture
def runtime(evidence, monkeypatch):
    connection = Connection(evidence)
    @contextmanager
    def local_runtime(args):
        yield SimpleNamespace(database_url="synthetic database"), None
    monkeypatch.setattr(subject.audit, "local_run", lambda args: evidence.local)
    monkeypatch.setattr(subject.audit.selected, "runtime", local_runtime)
    monkeypatch.setattr(subject.audit.psycopg, "connect", lambda *a, **k: connection)
    monkeypatch.setattr(subject.audit, "prior_snapshot", lambda *a: deepcopy(evidence.value["prior"]))
    return connection


def test_default_dryrun_uses_readonly_no_lock_no_update(evidence, runtime, capsys):
    before = deepcopy(evidence.value)
    result = subject.run(evidence.args)
    assert result["dry_run"] and result["planned_changes"] == 1 and result["db_writes"] == 0
    assert runtime.queries[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert not any(query.startswith("UPDATE") or "pg_try_advisory" in query for query, _ in runtime.queries)
    assert runtime.locked_snapshots == [False] and evidence.value == before
    assert "changes" not in json.loads(capsys.readouterr().out)


def test_execute_locks_then_updates_one_key_and_verifies_preserved_evidence(evidence, runtime):
    evidence.args.execute = True
    before = subject.protected_fingerprints(evidence.value)
    result = subject.run(evidence.args)
    assert runtime.committed and result["updated_rows"] == result["db_writes"] == 1
    assert evidence.value["rows"][0]["summary_json"][subject.CAP] == 10
    assert subject.protected_fingerprints(evidence.value) == before
    assert runtime.locked_snapshots == [True, False]
    assert len([q for q, _ in runtime.queries if q.startswith("UPDATE")]) == 1
    assert result["protected_fingerprints"] == result["after_protected_fingerprints"]
    assert evidence.args.output.exists() and evidence.args.output.with_name(evidence.args.output.stem + ".intent.private.json").exists()


@pytest.mark.parametrize("failure", ["coordinator", "running", "cas", "facets", "old10"])
def test_execute_fails_closed_and_rolls_back_without_success_receipt(evidence, runtime, failure):
    evidence.args.execute = True
    if failure == "coordinator": runtime.coordinator = False
    elif failure == "running": runtime.running = 1
    elif failure == "cas": runtime.cas_ok = False
    elif failure == "facets": runtime.change_facets = True
    else: evidence.value["prior"]["raw_sha256"] = "changed prior data"
    with pytest.raises(subject.audit.PendingVerificationError): subject.run(evidence.args)
    assert not runtime.committed and not evidence.args.output.exists()
    assert evidence.value["rows"][0]["summary_json"][subject.CAP] == 99


def test_output_cannot_mutate_source_evidence(evidence, runtime):
    evidence.args.output = evidence.args.folder / "audit.private.json"
    with pytest.raises(subject.audit.PendingVerificationError, match="repair_output_inside_frozen_evidence"):
        subject.run(evidence.args)
    assert runtime.queries == []
