"""Offline orchestration safety; no DB, provider, SSH or token downloads."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("dinaut_three_dot_runner", ROOT / "scripts/run_dinaut_three_dot_matches.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sample_case():
    return {"ready": True, "source_key": "seace_prod2", "source_id": 19,
        "opportunity_id": "opportunity", "fit_level": 3, "code": "EXAMPLE",
        "description": "Licencias software", "buyer_name": "Entidad",
        "extraction_id": 27, "document_sha256": "a" * 64, "is_current": True,
        "quality": "auto", "requires_human_review": True,
        "raw_extraction_json": {"read": True}, "summary_json": {"object": "Software"},
        "facets": [{"extraction_id": 27, "is_current": True, "facet": "specific_experience",
            "label": "Experiencia software", "required": True, "details_json": {}}]}


@pytest.fixture
def frozen_folder(tmp_path, monkeypatch):
    def build(cases=None):
        receipt = {"profile_id": runner.PROFILE_ID, "tenant_id": runner.TENANT_ID,
                   "cutoff_utc": "2026-10-15T20:00:00+00:00"}
        frozen = {"receipt": dict(receipt), "profile": {"razon_social": "DIN"},
                  "business_lines": [], "cases": cases or [sample_case()]}
        path = tmp_path / "inputs.private.json"
        path.write_text(json.dumps(frozen))
        receipt["inputs_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (tmp_path / "preflight.private.json").write_text(json.dumps(receipt))
        return tmp_path
    monkeypatch.setattr(runner, "build_profile_cards", lambda _p: {"company": {}, "coverage": {"document_count": 1}})
    monkeypatch.setattr(runner.tiktoken, "get_encoding", lambda _name: SimpleNamespace(encode=lambda text: list(text)))
    monkeypatch.setattr(runner, "build_jev_payload", lambda *a, **k: {"state": {}, "questions": {}})
    monkeypatch.setattr(runner, "token_budget", lambda *a, **k: {"offline": True})
    monkeypatch.setattr(runner, "production_settings", lambda *a, **k: pytest.fail("No SSH in preparation"))
    monkeypatch.setattr(runner, "dispatch_once", lambda *a, **k: pytest.fail("No provider in preparation"))
    monkeypatch.setattr(runner.psycopg, "connect", lambda *a, **k: pytest.fail("No PG in preparation"))
    return build


def test_default_main_prepares_without_network_or_db(frozen_folder, monkeypatch, capsys):
    prepared = frozen_folder()
    isolated_root = prepared / "isolated-repo"
    folder = isolated_root / "tmp/dinaut-three-dot-20261015"
    folder.mkdir(parents=True)
    for name in ("inputs.private.json", "preflight.private.json"):
        (prepared / name).replace(folder / name)
    monkeypatch.setattr(runner, "ROOT", isolated_root)
    monkeypatch.setattr(sys, "argv", ["runner", "--folder", str(folder)])
    runner.main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "prepared" and output["inference_calls"] == 0
    assert (folder / "execution-manifest.private.json").exists()
    assert not (folder / "coordinator.lock").exists()


def test_copied_paid_folder_is_rejected_before_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--folder", str(tmp_path)])
    monkeypatch.setattr(runner, "prepare", lambda *_: pytest.fail("Copied folder must fail before reading inputs"))
    with pytest.raises(ValueError, match="canonical DIN three-dot folder"):
        runner.main()


@pytest.mark.parametrize("change", [
    {"fit_level": 2},
    {"existing_match_id": 999},
    {"prior_match_reservations": [{"status": "running"}]},
    {"prior_match_reservations": [{"status": "uncertain"}]},
    {"prior_worker_jobs": [{"status": "claimed"}]},
])
def test_prepare_rejects_unauthorized_or_unresolved_case_before_dispatch(frozen_folder, change):
    with pytest.raises((ValueError, runner.ProviderBlocked, runner.PublicationConflict)):
        runner.prepare(frozen_folder([{**sample_case(), **change}]))


def test_exact_existing_replacement_allowlist_and_no_other_source(frozen_folder):
    row = {**sample_case(), "existing_match_id": 129}
    assert len(runner.prepare(frozen_folder([row]))[2]) == 1
    assert runner.ALLOWED_REPLACEMENTS == frozenset({("seace_prod2", 129), ("seace_prod2", 130), ("seace_prod6", 911)})


@pytest.mark.parametrize("change", [{"extraction_id": None}, {"raw_extraction_json": {}}, {"facets": []}])
def test_prepare_revalidates_ready_flag_against_actual_documentary_inputs(frozen_folder, change):
    # A copied/incorrect boolean must not be the only guard before paid dispatch.
    with pytest.raises((ValueError, runner.ProviderBlocked, runner.PublicationConflict)):
        runner.prepare(frozen_folder([{**sample_case(), **change}]))


def test_frozen_input_hash_is_checked_before_preparation(frozen_folder):
    folder = frozen_folder()
    with (folder / "inputs.private.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.prepare(folder)


def test_already_published_case_reuses_exact_result_without_dispatch(tmp_path, monkeypatch):
    case = sample_case()
    item = {"case": case, "pipeline_request_hash": "pipeline-a"}
    result = {"match_id": 99, "score": 72, "source_key": case["source_key"], "source_id": case["source_id"]}
    case_folder = tmp_path / f"{case['source_key']}-{case['source_id']}"
    case_folder.mkdir()
    (case_folder / "published.private.json").write_text(json.dumps(result))
    monkeypatch.setattr(runner, "current_match", lambda *_: {"previous_match": {
        "id": 99, "breakdown_json": {"meta": {"request_hash": "pipeline-a"}}}})
    monkeypatch.setattr(runner, "dispatch_once", lambda *a, **k: pytest.fail("Published case must not pay again"))
    monkeypatch.setattr(runner, "predispatch", lambda *a, **k: pytest.fail("Published case must not start again"))
    assert runner.run_case(None, None, {}, {}, item, tmp_path, "not-used")["reused"] is True


def test_published_pointer_drift_blocks_without_repeating_inference(tmp_path, monkeypatch):
    case = sample_case(); item = {"case": case, "pipeline_request_hash": "pipeline-a"}
    case_folder = tmp_path / f"{case['source_key']}-{case['source_id']}"
    case_folder.mkdir()
    (case_folder / "published.private.json").write_text(json.dumps({"match_id": 99}))
    monkeypatch.setattr(runner, "current_match", lambda *_: {"previous_match": {
        "id": 99, "breakdown_json": {"meta": {"request_hash": "other"}}}})
    monkeypatch.setattr(runner, "dispatch_once", lambda *a, **k: pytest.fail("No repeat inference"))
    with pytest.raises(runner.PublicationConflict):
        runner.run_case(None, None, {}, {}, item, tmp_path, "not-used")


def test_compose_preserves_scoring_and_states_when_gemma_only_explains():
    case = sample_case()
    scoring = {"score": 71, "requisitos": [{"source_index": 1, "requisito": "Licencias",
        "categoria": "experiencia", "estado": "requiere_revision", "critico": False, "evidence_refs": ["D2"]}],
        "coverage": {"partial_score": False}, "administrative_checklist": [], "source_review_required": True}
    original = deepcopy(scoring)
    recommendations = {"resumen": "Falta acreditar la experiencia específica.", "requisitos": [{
        "source_index": 1, "evidencia": "D1 corresponde a licencias.", "brecha": "Conformidad pendiente.",
        "accion": "Adjuntar conformidad del contrato.", "evidence_refs": ["D1"]}],
        "acciones_recomendadas": []}
    cards = {"reference_map": {"D1": {"document_id": "uuid-1"}, "D2": {"document_id": "uuid-2"}}}
    item = {"pipeline_request_hash": "pipeline", "binding": {"profile": "profile-sha"}}
    usage = {"input_tokens": 100, "output_tokens": 40, "cost_usd": "0.000001", "providers": {}}
    breakdown, missing = runner.compose(case, {}, scoring, recommendations, cards, item, usage)
    assert scoring == original
    assert breakdown["capacity_scoring"]["score"] == 71
    assert breakdown["requisitos"][0]["estado"] == "requiere_revision"
    assert breakdown["requisitos"][0]["evidence_refs"] == ["uuid-2", "uuid-1"]
    assert missing[0]["estado"] == "requiere_revision"
    assert breakdown["meta"]["score_validated"] is False
    assert breakdown["meta"]["source_review_required"] is True
    assert breakdown["meta"]["administrative_score_impact"] is False


@pytest.mark.parametrize("key,value", [("score", 100), ("estado", "cumple"), ("veredicto", "verde")])
def test_gemma_cannot_inject_a_score_or_requirement_status(key, value):
    request = {"state": {"requirements": [{"source_index": 1}], "profile": {"documents": [{"ref": "D1"}]}}}
    body = {"resumen": "Explicación", "requisitos": [{"source_index": 1, "evidencia": "D1",
        "brecha": "Pendiente", "accion": "Verificar", "evidence_refs": ["D1"]}], "acciones_recomendadas": []}
    if key == "score":
        body[key] = value
    else:
        body["requisitos"][0][key] = value
    with pytest.raises(ValueError):
        runner.validate_recommendations(body, request)


class PreflightConnection:
    def __init__(self, profile=None, lines=None):
        self.profile = profile or {"razon_social": "DIN"}
        self.lines = lines or []
        self.query = ""

    def execute(self, query, _args=()):
        self.query = query
        assert query.lstrip().upper().startswith("SELECT")
        return self

    def fetchone(self):
        return self.profile

    def fetchall(self):
        return self.lines


@pytest.mark.parametrize("job_status", ["pending", "claimed", "failed", "dead"])
def test_live_worker_job_requires_reconciliation_even_if_frozen_list_was_empty(tmp_path, monkeypatch, job_status):
    row = sample_case(); conn = PreflightConnection()
    monkeypatch.setattr(runner, "source_reader", lambda *_: {**row, "prior_worker_jobs": [{"status": job_status}]})
    monkeypatch.setattr(runner, "current_match", lambda *_: {"previous_match": None, "previous_provenance": None})
    with pytest.raises(runner.PublicationConflict, match="worker"):
        runner.predispatch(conn, {"profile": conn.profile, "business_lines": []}, row, tmp_path)
    assert not (tmp_path / "baseline-match.private.json").exists()


def test_same_existing_match_id_with_changed_metadata_is_not_silently_overwritten(tmp_path, monkeypatch):
    row = {**sample_case(), "existing_match_id": 129, "existing_match_meta": {"request_hash": "frozen"}}
    conn = PreflightConnection()
    monkeypatch.setattr(runner, "source_reader", lambda *_: deepcopy(row))
    monkeypatch.setattr(runner, "current_match", lambda *_: {"previous_match": {
        "id": 129, "breakdown_json": {"meta": {"request_hash": "newer"}}}, "previous_provenance": None})
    with pytest.raises(runner.PublicationConflict, match="analysis changed"):
        runner.predispatch(conn, {"profile": conn.profile, "business_lines": []}, row, tmp_path)


@pytest.mark.parametrize("status", ["running", "uncertain"])
def test_another_live_pg_reservation_blocks_before_journal_or_model(tmp_path, monkeypatch, status):
    row = sample_case(); conn = PreflightConnection()
    monkeypatch.setattr(runner, "source_reader", lambda *_: {**row, "prior_match_reservations": [{"id": 88, "status": status}]})
    monkeypatch.setattr(runner, "current_match", lambda *_: pytest.fail("Other unresolved claim must block early"))
    with pytest.raises(runner.PublicationConflict, match="unresolved provider"):
        runner.predispatch(conn, {"profile": conn.profile, "business_lines": []}, row, tmp_path)


def test_durable_stage_ledger_without_local_response_blocks_cross_folder_repayment(tmp_path, monkeypatch):
    class LedgerConnection:
        def execute(self, query, _args=()):
            assert query.lstrip().upper().startswith("SELECT")
            return self
        def fetchone(self):
            return {"id": 88, "status": "started"}
    monkeypatch.setattr(runner, "dispatch_once", lambda *a, **k: pytest.fail("No repeat request from another folder"))
    with pytest.raises(runner.ProviderBlocked, match="durable provider attempt"):
        runner.reserved_dispatch(LedgerConnection(), sample_case(), tmp_path, "jev", {"state": {}},
            url="not-used", api_key="not-used", header_name="not-used")


@pytest.mark.parametrize('mutate',[
    {'claim_token':'someone-else'}, {'status':'uncertain'},
    {'status':'running','lease_live':False}, {'request_hash':'different-input'},
])
def test_old_pg_claim_never_authorizes_a_fresh_dispatch(tmp_path,mutate):
    case=sample_case()
    row={'id_convocatoria':case['source_id'],'profile_id':runner.PROFILE_ID,
        'kind':'gemini_match','model':runner.MODEL,'policy_version':runner.POLICY_VERSION,
        'request_hash':'expected','claim_token':'owner','status':'running','lease_live':True,**mutate}
    class Connection:
        def execute(self,*_):return self
        def fetchone(self):return row
    with pytest.raises(runner.ProviderBlocked):
        runner.verify_owned_claim(Connection(),{'id':4,'claim_token':'owner'},case,
                                   {'pipeline_request_hash':'expected'},tmp_path)


def test_professional_refs_keep_profile_provenance_without_inventing_document_ids():
    case=sample_case()
    scoring={'score':71,'requisitos':[{'source_index':1,'requisito':'Especialista',
        'categoria':'perfiles','estado':'requiere_revision','critico':True,'evidence_refs':[]}],
        'coverage':{'partial_score':False},'administrative_checklist':[],'source_review_required':True}
    recommendations={'resumen':'Falta acreditar experiencia del especialista.','requisitos':[{
        'source_index':1,'evidencia':'Perfil P1 en la ficha.','brecha':'Falta experiencia específica.',
        'accion':'Aportar certificados.','evidence_refs':['P1']}],'acciones_recomendadas':[]}
    cards={'reference_map':{},'professionals':[{'ref':'P1','source_path':'team_json/0'}]}
    item={'pipeline_request_hash':'pipeline','binding':{'profile':'profile-sha'}}
    usage={'input_tokens':100,'output_tokens':40,'cost_usd':'0.000001','providers':{}}
    result,_=runner.compose(case,{},scoring,recommendations,cards,item,usage)
    row=result['requisitos'][0]
    assert row['evidence_refs']==[]
    assert row['profile_evidence_refs']==[{'ref':'P1','source_path':'team_json/0',
        'profile_snapshot_sha256':'profile-sha','documentary_accreditation':False}]
    assert result['capacity_scoring']['score']==71
    recommendations['requisitos'][0]['evidence_refs']=['P99']
    with pytest.raises(ValueError):runner.compose(case,{},scoring,recommendations,cards,item,usage)
