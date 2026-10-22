"""Offline egress contracts: proxy only for SEACE, fail closed, no real network."""
import socket

import httpx
import pytest
from tenacity import wait_none

from buenapro_worker.notifications import channels
from buenapro_worker.seace import egress
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings

PROXY = "http://10.161.181.75:8888"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("SEACE_EGRESS_PROXY", "SEACE_EGRESS_REQUIRED", "TELEGRAM_BOT_TOKEN", "OPS_TELEGRAM_CHAT_ID",
                 "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def proxied(monkeypatch):
    """SEACE_EGRESS_PROXY set, with the proxy transport replaced by a recording mock."""
    monkeypatch.setenv("SEACE_EGRESS_PROXY", PROXY)
    seen = []

    def transport(proxy, verify):
        assert proxy == PROXY
        return httpx.MockTransport(lambda request: seen.append(str(request.url)) or httpx.Response(200, text="proxy"))

    monkeypatch.setattr(egress, "_proxy_transport", transport)
    return seen


def direct(**kwargs):
    return httpx.MockTransport(lambda request: httpx.Response(200, text="direct"))


@pytest.mark.parametrize("url", ["https://prod2.seace.gob.pe/x", "https://alfprod.seace.gob.pe/x",
                                 "https://prod6.seace.gob.pe:443/x", "https://prod1.seace.gob.pe/x",
                                 "https://seace.gob.pe/"])
def test_seace_hosts_use_the_proxy_transport(proxied, url):
    with egress.seace_http_client(transport=direct()) as client:
        assert client.get(url).text == "proxy"
    assert [httpx.URL(seen).host for seen in proxied] == [httpx.URL(url).host]


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://api.telegram.org/bot",
                                 "https://evilseace.gob.pe/", "https://seace.gob.pe.evil.com/"])
def test_other_hosts_never_use_the_proxy(proxied, url):
    with egress.seace_http_client(transport=direct()) as client:
        assert client.get(url).text == "direct"
    assert proxied == []


def test_without_proxy_there_are_no_mounts_and_environment_proxies_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.invalid:3128")
    with egress.seace_http_client(timeout=7, follow_redirects=False) as client:
        assert client._mounts == {} and client.trust_env is False
        assert client.timeout.read == 7 and client.follow_redirects is False


def test_trust_env_is_always_false_even_if_requested(proxied):
    with egress.seace_http_client(trust_env=True) as client:
        assert client.trust_env is False


def test_caller_options_and_real_proxy_transport_are_preserved(monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", PROXY)
    seen = []
    original = egress._proxy_transport
    monkeypatch.setattr(egress, "_proxy_transport", lambda proxy, verify: seen.append(verify) or original(proxy, verify))
    hooks = {"request": [lambda request: None]}
    with egress.seace_http_client(base_url="https://prod6.seace.gob.pe/v1", verify=False, event_hooks=hooks,
                                  headers={"User-Agent": "test"}, follow_redirects=True) as client:
        assert seen == [False, False]
        assert len(client._mounts) == 2 and client.follow_redirects is True
        assert client.event_hooks["request"] == hooks["request"]
        assert client.headers["User-Agent"] == "test"
        assert str(client.base_url) == "https://prod6.seace.gob.pe/v1/"


@pytest.mark.parametrize("required", ["1", "true", "TRUE"])
def test_required_without_proxy_fails_closed(monkeypatch, required):
    monkeypatch.setenv("SEACE_EGRESS_REQUIRED", required)
    with pytest.raises(egress.EgressNotConfigured):
        egress.seace_http_client()
    with pytest.raises(egress.EgressUnavailable):
        egress.check_egress()


def test_required_false_or_empty_does_not_force_the_proxy(monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_REQUIRED", "false")
    monkeypatch.setenv("SEACE_EGRESS_PROXY", "  ")
    with egress.seace_http_client() as client:
        assert client._mounts == {}


@pytest.mark.parametrize("value", ["10.0.0.1:8888", "socks5://10.0.0.1:1080", "http://"])
def test_invalid_proxy_url_is_rejected(monkeypatch, value):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", value)
    with pytest.raises(egress.EgressNotConfigured):
        egress.seace_http_client()


def test_check_egress_is_a_no_op_without_proxy():
    egress.check_egress()


def test_check_egress_connects_only_to_the_proxy_port(monkeypatch):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        monkeypatch.setenv("SEACE_EGRESS_PROXY", f"http://127.0.0.1:{server.getsockname()[1]}")
        egress.check_egress(timeout=2)
        connection, _ = server.accept()
        with connection:
            assert connection.recv(1) == b""  # Connected and closed, nothing sent.


def test_check_egress_fails_on_a_closed_port(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("SEACE_EGRESS_PROXY", f"http://127.0.0.1:{port}")
    with pytest.raises(egress.EgressUnavailable, match="unreachable"):
        egress.check_egress(timeout=2)


def request(url="https://prod2.seace.gob.pe/x"):
    return httpx.Request("GET", url)


def test_proxy_errors_are_egress_failures_and_source_denials_are_not(monkeypatch):
    assert egress.is_egress_failure(httpx.ProxyError("403 Forbidden"))
    denied = httpx.HTTPStatusError("denied", request=request(), response=httpx.Response(403, request=request()))
    assert not egress.is_egress_failure(denied)
    assert not egress.is_egress_failure(httpx.ReadTimeout("slow", request=request()))
    assert not egress.is_egress_failure(ValueError("x"))


def test_connect_failures_count_only_for_seace_hosts_through_the_proxy(monkeypatch):
    seace, other = httpx.ConnectError("x", request=request()), httpx.ConnectError("x", request=request("https://api.openai.com/"))
    assert not egress.is_egress_failure(seace)  # Direct: a plain network error.
    monkeypatch.setenv("SEACE_EGRESS_PROXY", PROXY)
    assert egress.is_egress_failure(seace)
    assert egress.is_egress_failure(httpx.ConnectTimeout("x", request=request("https://alfprod.seace.gob.pe/")))
    assert not egress.is_egress_failure(other)
    assert not egress.is_egress_failure(httpx.ConnectError("no request attached"))


def test_real_proxy_refusing_connect_is_a_proxy_error_not_a_status(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("SEACE_EGRESS_PROXY", f"http://127.0.0.1:{port}")
    with egress.seace_http_client(timeout=2) as client, pytest.raises(httpx.HTTPError) as raised:
        client.get("https://prod2.seace.gob.pe/")
    assert egress.is_egress_failure(raised.value)


def test_seace_client_does_not_retry_egress_failures(monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", PROXY)
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ProxyError("503 Service Unavailable", request=req)

    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    with SeaceClient(settings) as client:
        client._client.close()
        client._client = httpx.Client(base_url=settings.seace_base_url, transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.ProxyError):
            client._get_json.retry_with(wait=wait_none())(client, "/buscadorpublico/test")
    assert len(calls) == 1


def test_seace_client_builds_through_the_egress_factory(monkeypatch, proxied):
    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    with SeaceClient(settings) as client:
        assert client._client.trust_env is False and len(client._client._mounts) == 2


def test_notify_ops_needs_token_and_chat(monkeypatch):
    sent = []
    monkeypatch.setattr(channels, "send_telegram", lambda *args, **kwargs: sent.append((args, kwargs)))
    assert egress.notify_ops("hello") is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert egress.notify_ops("hello") is False
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setenv("OPS_TELEGRAM_CHAT_ID", "-100")
    assert egress.notify_ops("hello") is False
    assert sent == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert egress.notify_ops("hello") is True
    assert sent == [((None, "-100", "hello"), {"bot_token": "token"})]


def test_notify_ops_never_raises_nor_logs_the_token(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("OPS_TELEGRAM_CHAT_ID", "-100")

    def fail(*args, **kwargs):
        raise httpx.ConnectError("https://api.telegram.org/botsecret-token/sendMessage")

    monkeypatch.setattr(channels, "send_telegram", fail)
    with caplog.at_level("WARNING"):
        assert egress.notify_ops("hello") is False
    assert "secret-token" not in caplog.text and "ConnectError" in caplog.text


def test_notify_ops_does_not_use_the_seace_proxy(monkeypatch):
    monkeypatch.setenv("SEACE_EGRESS_PROXY", PROXY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OPS_TELEGRAM_CHAT_ID", "-100")
    seen = []

    def post(url, **kwargs):
        seen.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(channels.httpx, "post", post)
    monkeypatch.setattr(egress, "_proxy_transport", lambda *a: pytest.fail("proxy transport built"))
    assert egress.notify_ops("hello") is True
    assert seen == ["https://api.telegram.org/bottoken/sendMessage"]
