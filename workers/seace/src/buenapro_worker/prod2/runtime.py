"""Laptop PROD2 runtime with a private temporary tunnel to the production database."""
from __future__ import annotations

from contextlib import contextmanager
import json
import ipaddress
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from buenapro_worker.settings import Settings


def _remote_environment(ssh: list[str], ssh_host: str, container: str) -> dict[str, str]:
    try:
        result = subprocess.run(ssh + [ssh_host, "docker inspect --format '{{json .Config.Env}}' " + container],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Could not read production connection configuration")
        values = json.loads(result.stdout)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError()
        return dict(value.split("=", 1) for value in values if "=" in value)
    except (subprocess.SubprocessError, ValueError, TypeError):
        raise RuntimeError("Invalid production connection configuration") from None


def redis_tunnel_url(original: str, port: int) -> str:
    """Preserve Redis credentials/database/options exclusively in memory."""
    try:
        parsed = urlsplit(original)
        if (parsed.scheme not in {"redis", "rediss"} or parsed.port not in (None, 6379)
                or parsed.hostname not in {"redis", "docker-redis-1"} or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise ValueError("Expected the observed internal production Redis URL") from None
    credentials = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    return urlunsplit((parsed.scheme, f"{credentials}127.0.0.1:{port}", parsed.path, parsed.query, ""))


def _redis_bridge(ssh: list[str], ssh_host: str, redis_url: str) -> str:
    # Production Redis is not published on the host. Resolve only the inspected
    # redis service's private Docker address; never trust an arbitrary URL host.
    redis_tunnel_url(redis_url, 6379)
    hostname = urlsplit(redis_url).hostname
    try:
        result = subprocess.run(ssh + [ssh_host,
            "docker inspect --format '{{json .NetworkSettings.Networks}}' docker-redis-1"],
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError()
        networks = json.loads(result.stdout)
        if not isinstance(networks, dict):
            raise ValueError()
        candidates = set()
        for network in networks.values():
            if not isinstance(network, dict):
                raise ValueError()
            aliases = network.get("Aliases") or []
            if not isinstance(aliases, list):
                raise ValueError()
            if hostname != "docker-redis-1" and hostname not in aliases:
                continue
            address = ipaddress.ip_address(network.get("IPAddress", ""))
            if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified:
                raise ValueError()
            candidates.add(str(address))
        if len(candidates) != 1:
            raise ValueError()
        return candidates.pop()
    except (subprocess.SubprocessError, ValueError, TypeError):
        raise RuntimeError("Could not uniquely resolve the private production Redis service") from None


def _free_ports(count: int) -> list[int]:
    sockets = []
    try:
        for _ in range(count):
            candidate = socket.socket()
            sockets.append(candidate)
            candidate.bind(("127.0.0.1", 0))
        return [candidate.getsockname()[1] for candidate in sockets]
    finally:
        for candidate in sockets:
            candidate.close()


def _wait_tunnel(tunnel, ports: list[int]):
    for _ in range(50):
        if tunnel.poll() is not None:
            raise RuntimeError("Production SSH tunnel failed")
        try:
            for port in ports:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Production SSH tunnel did not become ready")


def _stop_tunnel(tunnel):
    tunnel.terminate()
    try:
        tunnel.wait(timeout=10)
    except subprocess.TimeoutExpired:
        tunnel.kill()
        tunnel.wait()


def tunnel_url(original: str, port: int) -> str:
    parsed = urlsplit(original)
    if parsed.scheme not in {"postgres", "postgresql"} or "@" not in parsed.netloc:
        raise ValueError("Expected a PostgreSQL URL")
    credentials = parsed.netloc.rsplit("@", 1)[0]
    return urlunsplit((parsed.scheme, f"{credentials}@127.0.0.1:{port}", parsed.path, parsed.query, ""))


@contextmanager
def production_settings(*, ssh_host: str, ssh_key: str | Path):
    """Read credentials only into memory; never start retired workers or model jobs."""
    ssh = ["ssh", "-i", str(Path(ssh_key).resolve()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"]
    # Connection settings belong to the active application, not retired queue
    # containers. Their removal must not disable either reader's PG transport.
    remote = _remote_environment(ssh, ssh_host, "docker-web-1")
    if not remote.get("DATABASE_URL"):
        raise RuntimeError("Production database URL missing")
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-L",
                                    f"127.0.0.1:{port}:127.0.0.1:5432", ssh_host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if tunnel.poll() is not None:
                raise RuntimeError("Production SSH tunnel failed")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Production SSH tunnel did not become ready")
        local = Settings()
        # Keys stay in their existing ignored local configuration; no secret export.
        yield local.model_copy(update={"database_url": tunnel_url(remote["DATABASE_URL"], port),
                                       "prod4_enabled": False, "prod6_profile_poll_enabled": False,
                                       "schedule_refresh_enabled": False})
    finally:
        _stop_tunnel(tunnel)


@contextmanager
def production_staging_settings(*, ssh_host: str, ssh_key: str | Path):
    """Yield isolated PG settings and Redis URL through private SSH forwards.

    Docker bridge lookup is read-only. Redis is never published, credentials
    never leave memory, and legacy worker/scheduler flags remain disabled.
    """
    ssh = ["ssh", "-i", str(Path(ssh_key).resolve()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"]
    remote = _remote_environment(ssh, ssh_host, "docker-web-1")
    if not remote.get("DATABASE_URL"):
        raise RuntimeError("Production database URL missing")
    redis_url = remote.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("Production Redis URL missing")
    address = _redis_bridge(ssh, ssh_host, redis_url)
    pg_port, redis_port = _free_ports(2)
    tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes",
        "-L", f"127.0.0.1:{pg_port}:127.0.0.1:5432",
        "-L", f"127.0.0.1:{redis_port}:{address}:6379", ssh_host],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_tunnel(tunnel, [pg_port, redis_port])
        local = Settings()
        yield (local.model_copy(update={"database_url": tunnel_url(remote["DATABASE_URL"], pg_port),
                                       "prod4_enabled": False, "prod6_profile_poll_enabled": False,
                                       "schedule_refresh_enabled": False}), redis_tunnel_url(redis_url, redis_port))
    finally:
        _stop_tunnel(tunnel)
