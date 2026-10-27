"""A SEACE egress failure is never read as a SEACE denial; no network, DB or Redis."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import threading

import httpx
import pytest

from buenapro_worker.prod2 import empty_scope_probe, keyword_chain, keyword_sweep
from buenapro_worker.prod2.client import Prod2EgressUnavailable, Prod2HttpClient, Prod2ProtocolError, SEARCH_URL
from buenapro_worker.prod2.documents import DocumentDownloadError, DocumentEgressUnavailable, download_document
from buenapro_worker.seace.egress import EgressUnavailable

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prod2_selected_egress", ROOT / "scripts/run_prod2_selected.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

QUERY = {"keyword": "SOFTWARE", "object_type": "service", "year": 2026,
         "published_from": "2026-10-01", "published_to": "2026-10-05", "seace_version": "3"}
CODE = "00000000-0000-4000-8000-000000000001"


def prod2_client(handler):
    return Prod2HttpClient(httpx.Client(transport=httpx.MockTransport(handler)))


def proxy_failure(request):
    raise httpx.ProxyError("502 Bad Gateway", request=request)


def test_proxy_failure_is_egress_unavailable_not_a_generic_protocol_error():
    with pytest.raises(Prod2EgressUnavailable) as raised:
        prod2_client(proxy_failure)._request("GET", SEARCH_URL)
    assert isinstance(raised.value, EgressUnavailable) and isinstance(raised.value, Prod2ProtocolError)
    assert str(raised.value) == "PROD2 SEACE egress unavailable"


def test_other_transport_failures_keep_the_generic_protocol_error():
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(Prod2ProtocolError, match="PROD2 HTTP transport failed") as raised:
        prod2_client(refused)._request("GET", SEARCH_URL)
    assert not isinstance(raised.value, EgressUnavailable)


@pytest.mark.parametrize("status", [403, 429])
def test_source_denial_stays_a_status_error(status):
    with pytest.raises(Prod2ProtocolError, match=f"PROD2 HTTP status {status}") as raised:
        prod2_client(lambda request: httpx.Response(status))._request("GET", SEARCH_URL)
    assert not isinstance(raised.value, EgressUnavailable)


def test_document_download_reports_egress_separately_from_status():
    with httpx.Client(transport=httpx.MockTransport(proxy_failure)) as client:
        with pytest.raises(DocumentEgressUnavailable) as raised:
            download_document(client, CODE, Path("unused.pdf"))
    assert isinstance(raised.value, DocumentDownloadError) and isinstance(raised.value, EgressUnavailable)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
        with pytest.raises(DocumentDownloadError, match="HTTP status 403") as denied:
            download_document(client, CODE, Path("unused.pdf"))
    assert not isinstance(denied.value, EgressUnavailable)


def failing_source(error):
    class Source:
        def search(self, *args, **kwargs):
            raise error

    @contextmanager
    def factory():
        yield Source()

    return factory


def test_sweep_records_egress_unavailable_without_blocking_the_source(tmp_path):
    report = keyword_sweep.sweep_query(QUERY, source_factory=failing_source(Prod2EgressUnavailable("x")),
                                       emit=lambda item: True, folder=tmp_path)
    assert report["errors"][0]["code"] == "egress_unavailable"
    assert report["status"] == "partial" and not report["coverage_complete"]
    assert report["http_searches"] == 1


def test_empty_scope_probe_marks_egress_failure_as_failed_not_blocked(tmp_path):
    proof = empty_scope_probe.probe_empty_scope(
        {**QUERY, "keyword": ""}, source_factory=failing_source(Prod2EgressUnavailable("x")), folder=tmp_path)
    assert proof["status"] == "failed" and proof["errors"][0]["code"] == "egress_unavailable"


class Guard:
    def __init__(self):
        self.statuses = []

    def stop_access(self, status):
        self.statuses.append(status)


@pytest.fixture
def stop_state(monkeypatch):
    guard = Guard()
    monkeypatch.setattr(subject, "STOP", threading.Event())
    monkeypatch.setattr(subject, "HANDOFF_GUARD", guard)
    return guard


def test_proxy_failure_does_not_trigger_source_access_stop(stop_state):
    hooks = subject.source_hooks(lambda request: None)
    with httpx.Client(transport=httpx.MockTransport(proxy_failure), event_hooks=hooks) as client:
        with pytest.raises(httpx.ProxyError):
            client.get("https://prod2.seace.gob.pe/x")
    assert stop_state.statuses == [] and not subject.STOP.is_set()


def test_source_403_still_stops_and_marks_access(stop_state):
    hooks = subject.source_hooks(lambda request: None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)), event_hooks=hooks) as client:
        client.get("https://prod2.seace.gob.pe/x")
    assert stop_state.statuses == [403] and subject.STOP.is_set()


def test_egress_failure_is_found_in_the_error_chain():
    try:
        try:
            raise Prod2EgressUnavailable("x")
        except Prod2EgressUnavailable:
            raise RuntimeError("wrapped") from None
    except RuntimeError as error:
        assert subject.egress_failure(error)
    assert not subject.egress_failure(Prod2ProtocolError("PROD2 HTTP status 403"))
    assert subject.EGRESS_EXIT_CODE == 75


# --- Shared SEACE gate: the child's request hook takes the shared turn and shares its 403s ---

def gated_client(handler, order=None):
    """A Prod2HttpClient wired like the child: shared hooks over a mock transport."""
    pacer = (lambda request: order.append("local")) if order is not None else (lambda request: None)
    return Prod2HttpClient(httpx.Client(transport=httpx.MockTransport(handler),
                                        event_hooks=subject.source_hooks(pacer)))


def shared_block(gate, status=403, source="prod6"):
    gate.write_block(json.dumps({"status": status, "source": source, "at": "2026-10-26T15:00:00.000Z"}), 60_000)


@pytest.mark.parametrize("status", [403, 429])
def test_shared_block_is_handled_as_a_received_denial_and_nothing_is_sent(fake_gate, stop_state, status):
    shared_block(fake_gate, status)
    sent = []
    client = gated_client(lambda request: sent.append(request) or httpx.Response(200))
    with pytest.raises(Prod2ProtocolError, match=f"PROD2 HTTP status {status}") as raised:
        client._request("GET", SEARCH_URL)
    assert sent == [] and not isinstance(raised.value, EgressUnavailable)
    assert subject.STOP.is_set() and stop_state.statuses == [status]
    with pytest.raises(subject.LimitReached):  # Later requests stop at the local STOP, as after a 403.
        client._request("GET", SEARCH_URL)


def test_shared_block_classifies_the_sweep_and_the_scope_probe_as_blocked(fake_gate, stop_state, tmp_path):
    shared_block(fake_gate)
    sent = []

    @contextmanager
    def factory():
        yield gated_client(lambda request: sent.append(request) or httpx.Response(200))

    report = keyword_sweep.sweep_query(QUERY, source_factory=factory, emit=lambda item: True, folder=tmp_path / "sweep")
    assert report["errors"][0]["code"] == "source_403" and report["status"] == "blocked"
    assert keyword_chain._error_reason([report]) == "source_access_blocked"
    subject.STOP.clear()
    proof = empty_scope_probe.probe_empty_scope({**QUERY, "keyword": ""}, source_factory=factory, folder=tmp_path / "probe")
    assert proof["status"] == "blocked" and proof["errors"][0]["code"] == "source_403"
    assert sent == []


def test_shared_block_stops_without_a_guard_too(fake_gate, monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    monkeypatch.setattr(subject, "HANDOFF_GUARD", None)
    shared_block(fake_gate)
    with pytest.raises(Prod2ProtocolError, match="status 403"):
        gated_client(lambda request: httpx.Response(200))._request("GET", SEARCH_URL)
    assert subject.STOP.is_set()


def test_shared_turn_comes_after_the_local_pacer_and_spaces_requests(fake_gate, stop_state):
    order = []
    client = gated_client(lambda request: order.append("sent") or httpx.Response(200), order)
    client._request("GET", SEARCH_URL)
    client._request("GET", SEARCH_URL)
    assert order == ["local", "sent", "local", "sent"]
    assert fake_gate.now_ms == 1_000_000 + 6000  # The second request waited the shared interval.
    assert fake_gate.next_slot == 1_000_000 + 12000
    assert not subject.STOP.is_set() and stop_state.statuses == []


@pytest.mark.parametrize("status", [403, 429])
def test_a_received_denial_is_shared_as_prod2(fake_gate, stop_state, status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)),
                      event_hooks=subject.source_hooks(lambda request: None)) as client:
        client.get("https://prod2.seace.gob.pe/x")
    payload = json.loads(fake_gate.read_block()[0])
    assert (payload["status"], payload["source"]) == (status, "prod2")
    assert stop_state.statuses == [status] and subject.STOP.is_set()


def test_other_statuses_are_not_shared(fake_gate, stop_state):
    for status in (200, 404, 500, 502):
        with httpx.Client(transport=httpx.MockTransport(lambda request, status=status: httpx.Response(status)),
                          event_hooks=subject.source_hooks(lambda request: None)) as client:
            client.get("https://prod2.seace.gob.pe/x")
    assert fake_gate.block is None and not subject.STOP.is_set()


def test_a_gate_outage_is_an_egress_failure_and_never_a_source_block(fake_gate, stop_state, tmp_path):
    fake_gate.fail = ConnectionError("redis://:hunter2@gate.invalid:6379/0")
    sent = []

    @contextmanager
    def factory():
        yield gated_client(lambda request: sent.append(request) or httpx.Response(200))

    with pytest.raises(Prod2EgressUnavailable) as raised:
        gated_client(lambda request: sent.append(request) or httpx.Response(200))._request("GET", SEARCH_URL)
    assert isinstance(raised.value, EgressUnavailable) and subject.egress_failure(raised.value)
    assert "hunter2" not in str(raised.value) and sent == []
    assert stop_state.statuses == [] and not subject.STOP.is_set()
    report = keyword_sweep.sweep_query(QUERY, source_factory=factory, emit=lambda item: True, folder=tmp_path)
    assert report["errors"][0]["code"] == "egress_unavailable" and sent == []


def test_a_denial_is_still_handled_when_sharing_it_fails(fake_gate, stop_state):
    def refuse(value, ttl_ms):
        raise TimeoutError("down")

    fake_gate.write_block = refuse
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)),
                      event_hooks=subject.source_hooks(lambda request: None)) as client:
        client.get("https://prod2.seace.gob.pe/x")  # The response hook must not raise.
    assert stop_state.statuses == [403] and subject.STOP.is_set()


def test_with_the_gate_off_the_hooks_never_touch_redis(stop_state, monkeypatch):
    monkeypatch.setattr(subject.shared_gate, "_BACKEND", object())
    hooks = subject.source_hooks(lambda request: None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)), event_hooks=hooks) as client:
        client.get("https://prod2.seace.gob.pe/x")
    assert stop_state.statuses == [403]
