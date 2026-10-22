"""Source-only discovery transport: bounded HTTP and one private database URL.

No settings model, paid provider, Redis client or migration is initialized here.
All HTTP sessions share one gate, but never share JSF cookies or ViewState.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from io import StringIO
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from dotenv import dotenv_values

from ..seace.egress import seace_http_client
from .client import Prod2HttpClient
from .runtime import _free_ports, _stop_tunnel, _wait_tunnel, tunnel_url


def _validated_url(value):
    try:
        if not isinstance(value, str) or not value or any(char.isspace() for char in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
                or parsed.fragment or parsed.port == 0):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError("database_url_configuration_invalid") from None


def _local_url(project_root):
    value = os.environ.get("DATABASE_URL")
    if value is not None:
        return _validated_url(value)
    # Only parse DATABASE_URL lines; do not load unrelated private model keys
    # or interpolate them into this source-only process's configuration.
    path = Path(project_root) / ".env.local"
    matches = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if re.match(r"^\s*(?:export\s+)?DATABASE_URL\s*=", line):
                    matches.append(line)
    except OSError:
        raise ValueError("database_url_missing") from None
    if not matches:
        raise ValueError("database_url_missing")
    if len(matches) != 1:
        raise ValueError("database_url_configuration_invalid")
    value = dotenv_values(stream=StringIO(matches[0]), interpolate=False).get("DATABASE_URL")
    return _validated_url(value)


@contextmanager
def database_url(*, project_root, ssh_host=None, ssh_key=None):
    """Yield only the PostgreSQL URL, keeping all credentials in memory.

    Remote mode asks the running web application to emit DATABASE_URL alone.
    It never inspects the container's full environment or initializes Settings.
    """
    if ssh_host is None:
        yield _local_url(project_root)
        return
    if (not isinstance(ssh_host, str)
            or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.:-]+", ssh_host)
            or ssh_host.startswith("-") or ssh_key is None):
        raise ValueError("database_ssh_configuration_invalid")
    ssh = ["ssh", "-i", str(Path(ssh_key).expanduser().resolve()),
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"]
    command = "docker exec docker-web-1 node -e " + shlex.quote(
        'process.stdout.write(process.env.DATABASE_URL || "")')
    try:
        captured = subprocess.run(ssh + [ssh_host, command], capture_output=True,
                                  text=True, timeout=30)
        if captured.returncode:
            raise ValueError()
        original = _validated_url(captured.stdout.strip())
        parsed = urlsplit(original)
        if (parsed.hostname not in {"postgres", "docker-postgres-1", "localhost", "127.0.0.1"}
                or parsed.port not in (None, 5432)
                or any(key.lower() in {"host", "hostaddr", "port", "service"}
                       for key, _ in parse_qsl(parsed.query))):
            raise ValueError()
        port = _free_ports(1)[0]
        private_url = tunnel_url(original, port)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise ValueError("database_remote_configuration_unavailable") from None
    try:
        tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-L",
            f"127.0.0.1:{port}:127.0.0.1:5432", ssh_host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        raise ValueError("database_tunnel_unavailable") from None
    try:
        try:
            _wait_tunnel(tunnel, [port])
        except Exception:
            raise ValueError("database_tunnel_unavailable") from None
        yield private_url
    finally:
        _stop_tunnel(tunnel)


class SourceGate:
    """One coordinator's shared HTTP pacer, budgets and fail-closed owner guard."""

    def __init__(self, stop, interval_seconds, max_requests, max_seconds,
                 check_owner=lambda: None, clock=time.monotonic, sleep=time.sleep):
        if (type(interval_seconds) not in (int, float)
                or not math.isfinite(interval_seconds) or interval_seconds < 1
                or type(max_requests) is not int or max_requests < 1
                or type(max_seconds) not in (int, float)
                or not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ValueError("invalid_source_limits")
        if not all(callable(value) for value in (check_owner, clock, sleep)):
            raise ValueError("invalid_source_callbacks")
        self.stop, self.interval_seconds = stop, interval_seconds
        self.max_requests, self.max_seconds = max_requests, max_seconds
        self.check_owner, self.clock, self.sleep = check_owner, clock, sleep
        self._lock = threading.RLock()
        self._started, self._last = clock(), None
        self._requests, self._responses = 0, 0
        self._methods, self._statuses = Counter(), Counter()
        self._stop_reason = None

    def _fail(self, reason):
        self._stop_reason = self._stop_reason or reason
        self.stop.set()
        raise ValueError(reason) from None

    def _check(self):
        if self.stop.is_set():
            raise ValueError(self._stop_reason or "source_stop_requested") from None
        if self.clock() - self._started >= self.max_seconds:
            self._fail("source_time_budget_exhausted")
        if self._requests >= self.max_requests:
            self._fail("source_request_budget_exhausted")

    def _request(self, request):
        with self._lock:
            self._check()
            if self._last is not None:
                delay = self.interval_seconds - (self.clock() - self._last)
                if delay > 0:
                    self.sleep(min(delay, max(0, self.max_seconds -
                                              (self.clock() - self._started))))
            self._check()
            try:
                self.check_owner()
            except Exception:
                self._fail("coordinator_connection_lost")
            self._check()
            self._last = self.clock()
            self._requests += 1
            self._methods[request.method] += 1

    def _response(self, response):
        denied = response.status_code in {403, 429}
        expired = self.clock() - self._started >= self.max_seconds
        # A request may currently hold the pacing lock while waiting. Publish
        # STOP immediately, so that waiter cannot send before we count this
        # response under the lock.
        if denied or expired:
            self.stop.set()
        with self._lock:
            self._responses += 1
            self._statuses[str(response.status_code)] += 1
            if denied:
                self._stop_reason = f"source_{response.status_code}"
            elif expired:
                self._stop_reason = self._stop_reason or "source_time_budget_exhausted"

    def hooks(self):
        return {"request": [self._request], "response": [self._response]}

    @contextmanager
    def source_factory(self):
        with self._lock:
            self._check()
        # No shared session, cookie export, redirects, retries or proxy override;
        # the only proxy allowed is the configured SEACE egress (trust_env stays off).
        with seace_http_client(timeout=60, verify=True, follow_redirects=False,
                               event_hooks=self.hooks()) as client:
            yield Prod2HttpClient(client)

    def metrics(self):
        """Return counters only: never request URLs, headers, bodies or cookies."""
        with self._lock:
            return {"requests": self._requests, "responses": self._responses,
                    "methods": dict(self._methods), "status_codes": dict(self._statuses),
                    "elapsed_seconds": round(max(0, self.clock() - self._started), 3),
                    "stop_reason": self._stop_reason}
