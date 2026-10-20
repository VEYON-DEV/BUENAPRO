"""Offline proof that discovery cannot dispatch without limits and ownership."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import threading

import httpx
import pytest

from buenapro_worker.prod2 import discovery_transport as transport


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def gate(**changes):
    clock = changes.pop("clock", Clock())
    values = {"stop": threading.Event(), "interval_seconds": 1,
              "max_requests": 100, "max_seconds": 300,
              "clock": clock, "sleep": clock.sleep}
    return transport.SourceGate(**(values | changes)), clock


def request(source_gate, method="GET"):
    source_gate.hooks()["request"][0](httpx.Request(method, "https://prod2.seace.gob.pe/test"))


def test_local_database_url_needs_no_model_keys_and_does_not_parse_other_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env.local").write_text(
        'GEMINI_API_KEY="unterminated ignored value\n'
        "export DATABASE_URL='postgresql://reader:private@localhost/buenapro'\n"
        "OPENAI_API_KEY=unused\nTYPESAFE_API_KEY=unused\n")
    with transport.database_url(project_root=tmp_path) as value:
        assert value == "postgresql://reader:private@localhost/buenapro"


def test_environment_database_url_precedes_private_file_and_initializes_no_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://reader:private@localhost/buenapro")
    monkeypatch.setattr(transport.Path, "open", lambda *a, **k: pytest.fail("file not needed"))
    with transport.database_url(project_root=tmp_path) as value:
        assert value.startswith("postgresql://reader:")


@pytest.mark.parametrize("value", ["", "redis://private@localhost/db", "postgresql://private@localhost/db#fragment",
                                  "postgresql://private@localhost:0/db", "postgresql://invalid\nsecret"])
def test_bad_database_configuration_is_sanitized(tmp_path, monkeypatch, value):
    monkeypatch.setenv("DATABASE_URL", value)
    with pytest.raises(ValueError, match="^database_url_configuration_invalid$") as error:
        with transport.database_url(project_root=tmp_path):
            pytest.fail("invalid URL yielded")
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_missing_or_duplicate_local_configuration_fails_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="^database_url_missing$"):
        with transport.database_url(project_root=tmp_path):
            pass
    (tmp_path / ".env.local").write_text("DATABASE_URL=postgresql://a@localhost/db\n"
                                          "DATABASE_URL=postgresql://b@localhost/db\n")
    with pytest.raises(ValueError, match="^database_url_configuration_invalid$"):
        with transport.database_url(project_root=tmp_path):
            pass


def test_database_url_does_not_interpolate_private_variables(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "never-load-this")
    (tmp_path / ".env.local").write_text("DATABASE_URL='postgresql://reader:${GEMINI_API_KEY}@localhost/db'\n")
    with transport.database_url(project_root=tmp_path) as value:
        assert "never-load-this" not in value and "${GEMINI_API_KEY}" in value


def test_ssh_reads_database_url_only_and_closes_tunnel_after_consumer_error(tmp_path, monkeypatch):
    events = []

    def capture(command, **options):
        assert "docker exec docker-web-1 node" in command[-1]
        assert "process.env.DATABASE_URL" in command[-1]
        assert "Config.Env" not in command[-1]
        assert "REDIS" not in command[-1] and "GEMINI" not in command[-1]
        assert options == {"capture_output": True, "text": True, "timeout": 30}
        events.append("capture")
        return SimpleNamespace(returncode=0, stdout="postgresql://reader:private@postgres/db?sslmode=disable")

    def popen(command, **options):
        assert "127.0.0.1:45678:127.0.0.1:5432" in command
        assert all(options[name] == transport.subprocess.DEVNULL
                   for name in ["stdin", "stdout", "stderr"])
        events.append("tunnel")
        return "private-tunnel"

    monkeypatch.setattr(transport.subprocess, "run", capture)
    monkeypatch.setattr(transport.subprocess, "Popen", popen)
    monkeypatch.setattr(transport, "_free_ports", lambda number: [45678])
    monkeypatch.setattr(transport, "_wait_tunnel", lambda tunnel, ports: events.append("ready"))
    monkeypatch.setattr(transport, "_stop_tunnel", lambda tunnel: events.append("closed"))
    with pytest.raises(RuntimeError, match="consumer error"):
        with transport.database_url(project_root=tmp_path, ssh_host="ubuntu@example.test", ssh_key=tmp_path / "key") as value:
            assert value == "postgresql://reader:private@127.0.0.1:45678/db?sslmode=disable"
            raise RuntimeError("consumer error")
    assert events == ["capture", "tunnel", "ready", "closed"]


@pytest.mark.parametrize("url", ["postgresql://private@unrelated.example/db", "postgresql://private@postgres:6543/db",
                                "postgresql://private@postgres/db?host=unrelated.example"])
def test_bad_remote_configuration_never_opens_tunnel_or_exports_url(tmp_path, monkeypatch, url):
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=url))
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **k: pytest.fail("no tunnel"))
    with pytest.raises(ValueError, match="^database_remote_configuration_unavailable$"):
        with transport.database_url(project_root=tmp_path, ssh_host="ubuntu@example.test", ssh_key="key"):
            pass


def test_tunnel_readiness_error_is_sanitized_and_tunnel_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="postgresql://reader:private@postgres/db"))
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **k: "tunnel")
    monkeypatch.setattr(transport, "_free_ports", lambda number: [45678])
    monkeypatch.setattr(transport, "_wait_tunnel", lambda *a: (_ for _ in ()).throw(RuntimeError("private diagnostic")))
    closed = []
    monkeypatch.setattr(transport, "_stop_tunnel", closed.append)
    with pytest.raises(ValueError, match="^database_tunnel_unavailable$"):
        with transport.database_url(project_root=tmp_path, ssh_host="ubuntu@example.test", ssh_key="key"):
            pass
    assert closed == ["tunnel"]


@pytest.mark.parametrize("overrides", [{"interval_seconds": .9}, {"interval_seconds": float("nan")},
    {"interval_seconds": True}, {"max_requests": 0}, {"max_requests": 1.0},
    {"max_requests": True}, {"max_seconds": 0}, {"max_seconds": float("inf")}])
def test_invalid_limits_fail_before_clients(overrides):
    with pytest.raises(ValueError, match="invalid_source_limits"):
        gate(**overrides)


@pytest.mark.parametrize("workers", [1, 4, 10])
def test_one_global_pacer_and_owner_check_for_every_dispatch(workers):
    clock, timestamps = Clock(), []
    source_gate, _ = gate(clock=clock, check_owner=lambda: timestamps.append(clock()))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda _: request(source_gate), range(20)))
    assert timestamps == list(range(20))
    assert source_gate.metrics()["requests"] == 20
    assert source_gate.metrics()["methods"] == {"GET": 20}


@pytest.mark.parametrize("status", [403, 429])
def test_denial_stops_dispatch_without_retry_or_response_data_in_metrics(status):
    source_gate, _ = gate()
    request(source_gate)
    source_gate.hooks()["response"][0](httpx.Response(status, headers={"Set-Cookie": "private"},
        json={"message": "private"}, request=httpx.Request("GET", "https://prod2.seace.gob.pe/private")))
    with pytest.raises(ValueError, match=f"^source_{status}$"):
        request(source_gate)
    metrics = source_gate.metrics()
    assert metrics["requests"] == metrics["responses"] == 1
    assert metrics["status_codes"] == {str(status): 1}
    assert source_gate.stop.is_set()
    assert "private" not in json.dumps(metrics) and "https" not in json.dumps(metrics)


def test_stop_arriving_during_pacer_prevents_owner_check_and_dispatch():
    clock, stop, ownership = Clock(), threading.Event(), []

    def sleep(seconds):
        clock.sleep(seconds)
        stop.set()

    source_gate, _ = gate(clock=clock, stop=stop, sleep=sleep, check_owner=lambda: ownership.append(True))
    request(source_gate)
    with pytest.raises(ValueError, match="source_stop_requested"):
        request(source_gate)
    assert source_gate.metrics()["requests"] == 1 and ownership == [True]


@pytest.mark.parametrize("status", [403, 429])
def test_denial_publishes_stop_even_when_another_request_holds_pacing_lock(status):
    entered, release, owner_checks = threading.Event(), threading.Event(), []
    clock = Clock()

    def sleep(seconds):
        entered.set()
        assert release.wait(timeout=1)
        clock.sleep(seconds)

    source_gate, _ = gate(clock=clock, sleep=sleep,
                          check_owner=lambda: owner_checks.append(True))
    request(source_gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiter = pool.submit(request, source_gate)
        assert entered.wait(timeout=1)
        denied = pool.submit(source_gate.hooks()["response"][0], httpx.Response(status))
        try:
            assert source_gate.stop.wait(timeout=1)
        finally:
            release.set()
        with pytest.raises(ValueError):
            waiter.result(timeout=1)
        denied.result(timeout=1)
    assert owner_checks == [True] and source_gate.metrics()["requests"] == 1


def test_owner_loss_stops_and_sanitizes_failure_before_first_request():
    source_gate, _ = gate(check_owner=lambda: (_ for _ in ()).throw(RuntimeError("postgresql://private")))
    with pytest.raises(ValueError, match="^coordinator_connection_lost$") as error:
        request(source_gate)
    assert error.value.__suppress_context__
    assert source_gate.stop.is_set() and source_gate.metrics()["requests"] == 0
    assert "private" not in str(error.value)


def test_stop_arriving_in_owner_guard_also_prevents_dispatch():
    stop = threading.Event()
    source_gate, _ = gate(stop=stop, check_owner=stop.set)
    with pytest.raises(ValueError, match="source_stop_requested"):
        request(source_gate)
    assert source_gate.metrics()["requests"] == 0


def test_request_budget_is_global_and_never_overshoots():
    source_gate, _ = gate(max_requests=3)

    def try_request(_):
        try:
            request(source_gate)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(try_request, range(20))) == 3
    assert source_gate.metrics()["stop_reason"] == "source_request_budget_exhausted"


def test_time_budget_can_expire_while_waiting_without_oversleep_or_new_dispatch():
    source_gate, clock = gate(interval_seconds=3, max_seconds=2)
    request(source_gate)
    with pytest.raises(ValueError, match="^source_time_budget_exhausted$"):
        request(source_gate)
    assert clock() == 2 and source_gate.metrics()["requests"] == 1


def test_elapsed_response_sets_stop_and_keeps_sanitized_response_count():
    source_gate, clock = gate(max_seconds=2)
    request(source_gate)
    clock.now = 3
    source_gate.hooks()["response"][0](httpx.Response(200))
    assert source_gate.metrics()["responses"] == 1
    assert source_gate.metrics()["stop_reason"] == "source_time_budget_exhausted"
    with pytest.raises(ValueError, match="source_time_budget_exhausted"):
        request(source_gate)


def test_stopped_gate_does_not_create_another_source_session(monkeypatch):
    source_gate, _ = gate()
    source_gate.stop.set()
    monkeypatch.setattr(transport.httpx, "Client", lambda **kwargs: pytest.fail("no new session"))
    with pytest.raises(ValueError, match="source_stop_requested"):
        with source_gate.source_factory():
            pytest.fail("stopped gate yielded")


def test_independent_source_sessions_close_and_share_only_the_gate(monkeypatch):
    original_client, clients, seen_cookies = httpx.Client, [], []
    source_gate, _ = gate()

    def handler(req):
        seen_cookies.append(req.headers.get("Cookie"))
        return httpx.Response(200, headers={"Set-Cookie": "JSESSIONID=private; Path=/"})

    def factory(**kwargs):
        assert kwargs["verify"] is True and kwargs["trust_env"] is False
        assert kwargs["timeout"] == 60 and kwargs["follow_redirects"] is False
        client = original_client(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(transport.httpx, "Client", factory)
    with source_gate.source_factory() as first:
        first.client.get("https://prod2.seace.gob.pe/test")
        first.client.get("https://prod2.seace.gob.pe/test")
    with source_gate.source_factory() as second:
        second.client.get("https://prod2.seace.gob.pe/test")
    assert first is not second and first.client is not second.client
    assert seen_cookies == [None, "JSESSIONID=private", None]
    assert all(client.is_closed for client in clients)
    assert source_gate.metrics()["requests"] == source_gate.metrics()["responses"] == 3
