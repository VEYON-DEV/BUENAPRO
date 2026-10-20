"""Private runtime contracts; synthetic credentials and mocked SSH only."""
import json
import subprocess
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from buenapro_worker.prod2 import runtime


PG = "postgresql://synthetic-user:synthetic%40password@postgres:5432/synthetic?sslmode=prefer"
REDIS = "redis://synthetic-user:synthetic%40password@redis:6379/5?socket_timeout=20"


def test_urls_preserve_credentials_options_and_point_only_to_local_tunnel():
    pg = urlsplit(runtime.tunnel_url(PG, 15432))
    redis = urlsplit(runtime.redis_tunnel_url(REDIS, 16379))
    assert (pg.hostname, pg.port, pg.path, pg.query) == ("127.0.0.1", 15432, "/synthetic", "sslmode=prefer")
    assert pg.username == urlsplit(PG).username and pg.password == urlsplit(PG).password
    assert (redis.hostname, redis.port, redis.path, redis.query) == ("127.0.0.1", 16379, "/5", "socket_timeout=20")
    assert redis.username == urlsplit(REDIS).username and redis.password == urlsplit(REDIS).password
    assert runtime.redis_tunnel_url("redis://redis:6379", 16379) == "redis://127.0.0.1:16379"


@pytest.mark.parametrize("url", ["redis://external.invalid:6379/0", "redis://127.0.0.1:6379/0",
                              "redis://redis:6380/0", "http://redis:6379/0", "redis://redis:bad/0",
                              "redis://redis:6379/0#synthetic-fragment"])
def test_redis_tunnel_rejects_unobserved_hosts_ports_or_schemes_without_exposing_url(url):
    with pytest.raises(ValueError) as error:
        runtime.redis_tunnel_url(url, 16379)
    assert str(error.value) == "Expected the observed internal production Redis URL"


@pytest.mark.parametrize("networks", [
    {}, {"bridge": {"Aliases": ["other"], "IPAddress": "172.19.0.2"}},
    {"bridge": {"Aliases": ["redis"], "IPAddress": "8.8.8.8"}},
    {"bridge": {"Aliases": ["redis"], "IPAddress": "127.0.0.1"}},
    {"bridge": {"Aliases": ["redis"], "IPAddress": ""}},
    {"a": {"Aliases": ["redis"], "IPAddress": "172.19.0.2"},
     "b": {"Aliases": ["redis"], "IPAddress": "172.20.0.2"}},
])
def test_redis_bridge_requires_one_observed_private_service_address(monkeypatch, networks):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(networks)))
    with pytest.raises(RuntimeError, match="uniquely resolve"):
        runtime._redis_bridge(["ssh"], "synthetic-host", REDIS)


def test_malformed_remote_environment_is_a_redacted_error(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="synthetic-do-not-emit-password"))
    with pytest.raises(RuntimeError) as error:
        runtime._remote_environment(["ssh"], "synthetic-host", "docker-web-1")
    assert "synthetic-do-not-emit-password" not in str(error.value)


class FakeTunnel:
    def __init__(self, events, failed=False):
        self.events, self.failed = events, failed

    def poll(self):
        return 1 if self.failed else None

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))

    def kill(self):
        self.events.append("kill")


def runtime_fixture(monkeypatch, *, redis_configured=True, failed=False):
    events, commands, connects = [], [], []
    def inspect(command, **kwargs):
        commands.append(command)
        tail = command[-1]
        if ".NetworkSettings.Networks" in tail:
            value = {"bridge": {"Aliases": ["redis", "docker-redis-1"], "IPAddress": "172.19.0.2"}}
        elif "docker-web-1" in tail:
            value = ["DATABASE_URL=" + PG] + (["REDIS_URL=" + REDIS] if redis_configured else [])
        else:
            pytest.fail("Connection settings must not depend on retired worker containers")
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    def launch(command, **kwargs):
        commands.append(command)
        assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
        return FakeTunnel(events, failed=failed)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def connect(target, **kwargs):
        connects.append(target)
        return Connection()
    local = SimpleNamespace(model_copy=lambda *, update: SimpleNamespace(**update))
    monkeypatch.setattr(runtime.subprocess, "run", inspect)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "_free_ports", lambda count: [15432, 16379])
    monkeypatch.setattr(runtime.socket, "create_connection", connect)
    monkeypatch.setattr(runtime, "Settings", lambda: local)
    return events, commands, connects


def test_staging_settings_builds_two_private_forwards_and_closes_them(monkeypatch):
    events, commands, connects = runtime_fixture(monkeypatch)
    with runtime.production_staging_settings(ssh_host="synthetic-host", ssh_key="synthetic-key") as (settings, url):
        assert url == runtime.redis_tunnel_url(REDIS, 16379)
        assert settings.database_url == runtime.tunnel_url(PG, 15432)
        assert settings.prod4_enabled is settings.prod6_profile_poll_enabled is settings.schedule_refresh_enabled is False
        assert events == []
    launched = commands[-1]
    assert "ServerAliveInterval=20" in launched and "ServerAliveCountMax=3" in launched
    assert "127.0.0.1:15432:127.0.0.1:5432" in launched
    assert "127.0.0.1:16379:172.19.0.2:6379" in launched
    assert all("synthetic%40password" not in str(command) for command in commands)
    assert connects == [("127.0.0.1", 15432), ("127.0.0.1", 16379)]
    assert events == ["terminate", ("wait", 10)]
    assert sum("docker-web-1" in command[-1] for command in commands) == 1
    assert not any("docker-worker-" in str(command) for command in commands)


def test_missing_redis_configuration_stops_before_opening_a_tunnel(monkeypatch):
    events, commands, connects = runtime_fixture(monkeypatch, redis_configured=False)
    with pytest.raises(RuntimeError, match="Production Redis URL missing"):
        with runtime.production_staging_settings(ssh_host="synthetic-host", ssh_key="synthetic-key"):
            pytest.fail("Missing configuration must never start a reader")
    assert events == connects == []
    assert len(commands) == 1 and "docker-web-1" in commands[0][-1]


def test_failed_tunnel_is_closed_before_settings_are_yielded(monkeypatch):
    events, _, connects = runtime_fixture(monkeypatch, failed=True)
    with pytest.raises(RuntimeError, match="SSH tunnel failed"):
        with runtime.production_staging_settings(ssh_host="synthetic-host", ssh_key="synthetic-key"):
            pytest.fail("Failed tunnel must never yield runtime credentials")
    assert connects == [] and events == ["terminate", ("wait", 10)]


def test_caller_failure_also_closes_tunnels(monkeypatch):
    events, _, _ = runtime_fixture(monkeypatch)
    with pytest.raises(RuntimeError, match="synthetic caller failure"):
        with runtime.production_staging_settings(ssh_host="synthetic-host", ssh_key="synthetic-key"):
            raise RuntimeError("synthetic caller failure")
    assert events == ["terminate", ("wait", 10)]


def test_allocated_tunnel_ports_are_distinct_local_ephemeral_ports():
    ports = runtime._free_ports(2)
    assert len(set(ports)) == 2 and all(0 < port < 65536 for port in ports)


def test_existing_pg_only_runtime_keeps_its_contract_and_closes_private_tunnel(monkeypatch):
    events, commands, connects = runtime_fixture(monkeypatch, redis_configured=False)
    with runtime.production_settings(ssh_host="synthetic-host", ssh_key="synthetic-key") as settings:
        parsed = urlsplit(settings.database_url)
        assert parsed.hostname == "127.0.0.1" and parsed.password == urlsplit(PG).password
        assert settings.prod4_enabled is settings.prod6_profile_poll_enabled is settings.schedule_refresh_enabled is False
        assert connects == [("127.0.0.1", parsed.port)]
        assert events == []
    assert len(commands) == 2 and "127.0.0.1:" + str(parsed.port) + ":127.0.0.1:5432" in commands[-1]
    assert "ServerAliveInterval=20" in commands[-1] and "ServerAliveCountMax=3" in commands[-1]
    assert not any("NetworkSettings" in str(command) for command in commands)
    assert "docker-web-1" in commands[0][-1]
    assert not any("docker-worker-" in str(command) for command in commands)
    assert events == ["terminate", ("wait", 10)]
