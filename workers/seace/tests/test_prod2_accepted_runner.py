"""Boundary and paid-stage replay tests. No HTTP, production or model calls."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import accepted_runner as runner
from buenapro_worker.prod2.analysis import JEV_MODEL, JEV_POLICY_VERSION, jev_request_hash
from buenapro_worker.prod2.client import Prod2ProtocolError


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("accepted_script_test", ROOT / "scripts/run_prod2_accepted.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


@pytest.mark.parametrize("score,passes", [(0,False),(40,False),(59.99,False),(60,True),(100,True)])
def test_fixed_threshold(score, passes):
    assert runner.accepted({"affinity_index": score}) is passes


@pytest.mark.parametrize("score", [None,True,"60",float("nan"),float("inf"),-1,101])
def test_invalid_score_cannot_promote(score):
    with pytest.raises(ValueError):
        runner.accepted({"affinity_index":score})


def test_cached_rejected_jev_never_calls_provider(tmp_path):
    commercial = [{"descripcion":"Software", "lineas":["Licencias"]}]
    row = {"description":"software de impresion 3D", "convocation_id":77}
    request_hash = jev_request_hash(row["description"], commercial_profiles=commercial)
    result = {"affinity_index":53.25, "model":JEV_MODEL, "policy_version":JEV_POLICY_VERSION,
              "cache_key":"prod2:jev:"+request_hash}
    def forbidden(*a, **kw):
        pytest.fail("Cached/rejected result must not call a provider")
    repo = SimpleNamespace(claim_jev=lambda *a,**kw: {"state":"cached","result":result}, complete_jev=forbidden)
    actual_hash, value, hit = runner.paid_jev(None,tmp_path,row,commercial,"principal",
        SimpleNamespace(classify=forbidden),repo)
    assert actual_hash == request_hash and hit and not runner.accepted(value)
    assert not list(tmp_path.iterdir())


def test_uncertain_jev_never_automatically_repays(tmp_path):
    def forbidden(*a,**kw):
        pytest.fail("Uncertain claim must not invoke provider")
    repo = SimpleNamespace(claim_jev=lambda *a,**kw:{"state":"blocked"}, complete_jev=forbidden)
    with pytest.raises(RuntimeError,match="reconciliation"):
        runner.paid_jev(None,tmp_path,{"description":"Software", "convocation_id":77},
            [{"descripcion":"Software", "lineas":["Licencias"]}],"principal",SimpleNamespace(classify=forbidden),repo)


def test_jev_provider_failure_is_recorded_uncertain_without_retry(tmp_path):
    calls=[]
    conn=SimpleNamespace(execute=lambda statement,params:calls.append((statement,params)))
    repo=SimpleNamespace(claim_jev=lambda *a,**kw:{"state":"claimed","claim_token":"owner"})
    provider_calls=[]
    def fail(*a,**kw):
        provider_calls.append(True)
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        runner.paid_jev(conn,tmp_path,{"description":"Software", "convocation_id":77},
            [{"descripcion":"Software", "lineas":["Licencias"]}],"principal",SimpleNamespace(classify=fail),repo)
    assert len(provider_calls)==1
    assert len(calls)==1 and "status='uncertain'" in calls[0][0]
    assert len(list(tmp_path.glob('*.failure.private.json')))==1


def test_llm1_replay_bypasses_callback(monkeypatch,tmp_path):
    bundle={"cache_key":"prod2:llm1:"+"a"*64}
    monkeypatch.setattr(runner,"claim_evaluation",lambda *a,**kw:{"status":"cached","id":8,"result_json":bundle})
    def forbidden():
        pytest.fail("LLM1 replay must not pay again")
    evaluation_id,actual,hit=runner.paid_llm1(None,tmp_path,77,"a"*64,SimpleNamespace(gemini_model="gemini-test"),"principal",forbidden)
    assert evaluation_id==8 and actual==bundle and hit


def test_dry_run_has_no_io_to_source_database_redis_provider(monkeypatch,tmp_path,capsys):
    def forbidden(*a,**kw):
        pytest.fail("Dry run cannot touch external state")
    monkeypatch.setattr(script,"production_staging_settings",forbidden)
    monkeypatch.setattr(script,"discover",forbidden)
    monkeypatch.setattr(script,"JevDescriptionAnalyzer",forbidden)
    monkeypatch.setattr(script.sys,"argv",["run_prod2_accepted.py","--folder",str(tmp_path/"new"),"--dry-run"])
    assert script.main()==0
    assert '"jev_threshold": 60' in capsys.readouterr().out
    assert not (tmp_path/"new").exists()


@pytest.mark.parametrize("flags", [["--limit","21"],["--candidate-limit","31"],["--years","2026,2026"],["--keyword","not-configured"]])
def test_invalid_pilot_bounds_fail_before_runtime(tmp_path,flags):
    args=script.parser().parse_args(["--folder",str(tmp_path),*flags])
    with pytest.raises(ValueError):
        script.plan(args)


def test_user_authorized_twenty_accepted_keeps_thirty_candidate_bound(tmp_path):
    args = script.parser().parse_args(['--folder', str(tmp_path), '--limit', '20', '--candidate-limit', '30'])
    plan = script.plan(args)
    assert plan['limit'] == 20 and plan['candidate_limit'] == 30
    assert plan['jev_threshold'] == 60 and plan['terminal_stage'] == 'llm1'
    assert plan['document_scope'] == 'prod2_direct_documents_v1'


def test_retired_public_runner_cannot_start_old_load(monkeypatch,tmp_path,capsys):
    spec=importlib.util.spec_from_file_location("retired_public_cli",ROOT/"scripts/run_prod2_ingestion.py")
    legacy=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    def forbidden(*a,**kw):
        pytest.fail("Retired executor cannot access production")
    monkeypatch.setattr(legacy,"settings_context",forbidden)
    monkeypatch.setattr(legacy,"source_factory",forbidden)
    monkeypatch.setattr(legacy.sys,"argv",["run_prod2_ingestion.py","--folder",str(tmp_path/"old"),"--production"])
    assert legacy.main()==3
    assert '"status": "retired"' in capsys.readouterr().err
    assert not (tmp_path/"old").exists()


def detail_item(page=4):
    return {"row": {"convocation_id": 77, "procedure_id": 88, "description": "SOFTWARE EXACTO"},
        "query": {"keyword": "software", "year": 2026, "object_type": "service",
            "published_from": "2026-04-06", "published_to": "2026-10-06", "seace_version": "3"},
        "page_number": page}


def source_row(*, conv=77, procedure=88, description="SOFTWARE EXACTO"):
    return {"convocatoria_id": conv, "procedure_id": procedure, "description": description}


@pytest.fixture
def detail_source(monkeypatch):
    state = SimpleNamespace(pages={}, total_pages=6, calls=[], client_options=[], detail_calls=[],
                            protocol_error_page=None)
    class Http:
        def __init__(self, **kwargs):
            state.client_options.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Source:
        def __init__(self, client):
            self.rows, self.counter = [], ()
        def search(self, keyword, **kwargs):
            state.calls.append(("search", keyword, kwargs))
            self.search_snapshot = {"total_pages": state.total_pages}
            self.counter = (1, 1, state.total_pages, 1, state.total_pages)
            self.rows = state.pages.get(1, [source_row(conv=1001, procedure=2001)])
        def search_page(self, page):
            assert 1 <= page <= state.total_pages
            state.calls.append(("page", page))
            if page == state.protocol_error_page:
                raise Prod2ProtocolError("Synthetic native counter changed")
            self.counter = (page, page, state.total_pages, page, state.total_pages)
            self.rows = state.pages.get(page, [source_row(conv=1000+page, procedure=2000+page)])
        def detail(self, index, *, all_documents):
            assert all_documents is True
            state.detail_calls.append((self.counter[3], index, dict(self.rows[index])))
            return {"description": self.rows[index]["description"], "documents": [],
                    "http_status": 200, "redirect_statuses": [302]}
    monkeypatch.setattr(script, "seace_http_client", Http)
    monkeypatch.setattr(script, "Prod2HttpClient", Source)
    return state


@pytest.mark.parametrize("page", [1, 4])
def test_detail_original_page_match_keeps_exact_native_navigation_and_capture(tmp_path, detail_source, page):
    import json
    detail_source.pages[page] = [source_row(conv=999, procedure=999), source_row()]
    hooks = {"request": [lambda _: None], "response": [lambda _: None]}
    result = script.get_detail(detail_item(page), tmp_path, lambda _: None, hooks)
    assert detail_source.calls == [("search", "software", {"year": 2026, "object_type": "Servicio",
        "published_from": "06/04/2026", "published_to": "06/10/2026"})] + ([] if page == 1 else [("page", page)])
    assert detail_source.detail_calls == [(page, 1, source_row())]
    assert detail_source.client_options == [{"timeout": 60, "verify": True, "event_hooks": hooks}]
    assert set(result) == {"observed_at", "detail"}
    assert result["detail"] == {"description": "SOFTWARE EXACTO", "documents": []}
    assert json.loads((tmp_path / "detail.private.json").read_text()) == result


def test_detail_finds_shifted_identity_in_second_neighbor_without_discovering_other_rows(tmp_path, detail_source):
    detail_source.pages[6] = [source_row()]
    script.get_detail(detail_item(4), tmp_path, lambda _: None)
    assert [call[1] for call in detail_source.calls if call[0] == "page"] == [4, 3, 5, 2, 6]
    assert len([call for call in detail_source.calls if call[0] == "search"]) == 1
    assert detail_source.detail_calls == [(6, 0, source_row())]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["detail.private.json"]


@pytest.mark.parametrize("changed_page", [4, 3])
@pytest.mark.parametrize("changed_description", ["OTRO SOFTWARE", " SOFTWARE EXACTO "])
def test_detail_changed_description_requires_new_jev_before_any_detail_or_further_fallback(
        tmp_path, detail_source, changed_page, changed_description):
    detail_source.pages[changed_page] = [source_row(description=changed_description)]
    detail_source.pages[5] = [source_row()]
    with pytest.raises(RuntimeError, match="new Jev version is required"):
        script.get_detail(detail_item(4), tmp_path, lambda _: None)
    assert [call[1] for call in detail_source.calls if call[0] == "page"] == ([4] if changed_page == 4 else [4, 3])
    assert not detail_source.detail_calls and not (tmp_path / "detail.private.json").exists()


def test_detail_missing_exact_procedure_identity_stops_after_five_local_pages(tmp_path, detail_source):
    detail_source.pages = {n: [source_row(procedure=999)] for n in range(1, 7)}
    with pytest.raises(RuntimeError, match="bounded captured-page neighbors"):
        script.get_detail(detail_item(4), tmp_path, lambda _: None)
    assert [call[1] for call in detail_source.calls if call[0] == "page"] == [4, 3, 5, 2, 6]
    assert not detail_source.detail_calls and not (tmp_path / "detail.private.json").exists()


@pytest.mark.parametrize("captured,total,navigated", [
    (1, 2, [2]), (5, 5, [5, 4, 3]), (6, 5, [5, 4]), (100, 5, []), (1, 1, []),
])
def test_detail_neighbors_are_clipped_to_verified_current_page_bounds(
        tmp_path, detail_source, captured, total, navigated):
    detail_source.total_pages = total
    with pytest.raises(RuntimeError, match="bounded captured-page neighbors"):
        script.get_detail(detail_item(captured), tmp_path, lambda _: None)
    assert [call[1] for call in detail_source.calls if call[0] == "page"] == navigated
    assert not detail_source.detail_calls


def test_detail_pagination_protocol_failure_is_not_bypassed_or_retried(tmp_path, detail_source):
    detail_source.protocol_error_page = 3
    detail_source.pages[5] = [source_row()]
    with pytest.raises(Prod2ProtocolError, match="counter changed"):
        script.get_detail(detail_item(4), tmp_path, lambda _: None)
    assert [call[1] for call in detail_source.calls if call[0] == "page"] == [4, 3]
    assert not detail_source.detail_calls and not (tmp_path / "detail.private.json").exists()


def test_detail_cached_capture_replays_without_fresh_search(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Existing immutable detail must not query the source again")
    saved = {"observed_at": "2026-10-06T12:00:00+00:00", "detail": {"documents": []}}
    script.private_write(tmp_path / "detail.private.json", saved)
    monkeypatch.setattr(script, "seace_http_client", forbidden)
    assert script.get_detail(detail_item(), tmp_path, None) == saved


@pytest.mark.parametrize('fail_second', [False, True])
def test_enrichment_sends_all_loose_attachments_and_never_downloads_zip(monkeypatch, tmp_path, fail_second):
    import hashlib
    import io
    from pypdf import PdfReader
    from test_prod2_document_set import pdf_bytes
    from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY
    codes = [f'00000000-0000-4000-8000-{n:012d}' for n in (1, 2, 3)]
    documents = [{'uuid': code, 'filename': filename, 'type': 'Documento',
        'stage': 'Convocatoria', 'published_at': '06/10/2026 12:00'}
        for code, filename in zip(codes, ['sin nombre especifico.pdf', 'Acta Buena Pro.pdf', 'bases.zip'])]
    detail = {'documents': documents, 'document_inventory_complete': True, 'schedule': [], 'item_states': []}
    monkeypatch.setattr(script, 'get_detail', lambda *args: {'detail': detail, 'observed_at': '2026-10-06T17:00:00+00:00'})
    monkeypatch.setattr(script, 'persist_detail', lambda *args, **kwargs: {'detail_hash': 'a' * 64})
    monkeypatch.setattr(script, 'decide', lambda *args: {'state': 'unknown'})
    monkeypatch.setattr(script, 'save_participation_assessment', lambda *args, **kwargs: None)
    monkeypatch.setattr(script, 'record_document_download', lambda *args, **kwargs: None)
    class Http:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(script, 'seace_http_client', Http)
    downloaded, paid = [], []
    def download(http, code, target, **kwargs):
        assert code != codes[2], 'Deferred ZIP must not be downloaded or inspected'
        downloaded.append(code)
        if fail_second and code == codes[1]:
            raise TimeoutError('Synthetic transport failure')
        content = pdf_bytes('TECHNICAL DATA' if code == codes[0] else 'AWARD DATA')
        target.write_bytes(content)
        return {'document_uuid': code, 'sha256': hashlib.sha256(content).hexdigest(),
                'source_url': script.METADATA_URL + '?id=' + code}
    monkeypatch.setattr(script, 'download_document', download)
    monkeypatch.setattr(script, 'upload_preview', lambda *args, **kwargs: {'storage_key': 'synthetic/analysis.pdf'})
    def extract(settings, content, **kwargs):
        assert [page.extract_text() for page in PdfReader(io.BytesIO(content)).pages] == ['TECHNICAL DATA', 'AWARD DATA']
        assert len(kwargs['document_context']['document_set']) == 2
        assert kwargs['document_context']['excluded_documents'][0]['reason'] == 'archive_deferred'
        return {'facets': [], 'model': 'gemini-test', 'cost_usd': 0.0}
    monkeypatch.setattr(script, 'extract_document', extract)
    def pay(*args):
        paid.append(True)
        return 7, args[-1](), False
    monkeypatch.setattr(script, 'paid_llm1', pay)
    monkeypatch.setattr(script, 'persist_completed_llm1', lambda *args, **kwargs: {'requires_human_review': True})
    args = (None, SimpleNamespace(gemini_model='gemini-test', gemini_fallback_model='gemini-test-fallback'),
            tmp_path, {'row': {'convocation_id': 77}, 'query': {'object_type': 'service'}},
            {'profile_id': 'principal'}, lambda request: None)
    if fail_second:
        with pytest.raises(RuntimeError, match='no partial'):
            script.enrich(*args)
        assert paid == []
    else:
        result = script.enrich(*args)
        assert result['analyzed_document_uuids'] == codes[:2]
        assert result['deferred_archive_uuids'] == [codes[2]]
        assert result['analysis_page_count'] == 2
        assert result['document_scope'] == DIRECT_DOCUMENTS_POLICY
        assert result['requires_human_review'] is True
        assert paid == [True]
    assert downloaded == codes[:2]


@pytest.mark.parametrize('complete', [True, False])
def test_archive_only_enrichment_records_links_without_download_preview_or_model(monkeypatch, tmp_path, complete):
    codes = [f'00000000-0000-4000-8000-{n:012d}' for n in (1, 2)]
    docs = [{'uuid': code, 'filename': filename, 'type': 'Bases', 'stage': 'Convocatoria',
             'published_at': '06/10/2026 12:00'} for code, filename in zip(codes, ['bases.zip', 'TDR.rar'])]
    detail = {'documents': docs, 'document_inventory_complete': complete, 'schedule': [], 'item_states': []}
    monkeypatch.setattr(script, 'get_detail', lambda *args: {'detail': detail, 'observed_at': '2026-10-06T17:00:00+00:00'})
    monkeypatch.setattr(script, 'persist_detail', lambda *args, **kwargs: {'detail_hash': 'a' * 64})
    monkeypatch.setattr(script, 'decide', lambda *args: {'state': 'unknown'})
    monkeypatch.setattr(script, 'save_participation_assessment', lambda *args, **kwargs: None)
    def forbidden(*args, **kwargs):
        pytest.fail('Archive-only fichas cannot open/download/preview/analyze archives')
    for name in ['download_document', 'upload_preview', 'paid_llm1', 'prepare_direct_document_set']:
        monkeypatch.setattr(script, name, forbidden)
    if not complete:
        with pytest.raises(RuntimeError, match='incomplete official inventory'):
            script.enrich(None, None, tmp_path, {'row': {'convocation_id': 77}}, {}, None)
        return
    result = script.enrich(None, None, tmp_path, {'row': {'convocation_id': 77}}, {}, None)
    assert result['status'] == 'archives_deferred'
    assert result['analyzed_document_uuids'] == [] and result['downloads'] == 0
    assert result['deferred_archive_uuids'] == codes
    assert all(doc['source_url'].endswith(doc['uuid']) for doc in result['documents'])
