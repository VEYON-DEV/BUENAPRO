"""Offline HTTP contract: permanent denials must not multiply source requests."""
from contextlib import contextmanager

import httpx
import pytest
from tenacity import wait_none

from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


@contextmanager
def source_client(handler):
    settings = Settings(database_url="postgresql://offline:offline@localhost/offline",
                        gemini_api_key="offline", _env_file=None)
    with SeaceClient(settings) as client:
        client._client.close()
        client._client = httpx.Client(base_url=settings.seace_base_url,
                                     transport=httpx.MockTransport(handler))
        yield client


def request(client, kind):
    method = client._get_json if kind == "json" else client._get_bytes
    return method.retry_with(wait=wait_none())(client, "/buscadorpublico/test")


@pytest.mark.parametrize("kind", ["json", "bytes"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422, 429, 501])
def test_permanent_status_makes_one_attempt(kind, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, request=req, json={"error": "denied"})

    with source_client(handler) as client, pytest.raises(httpx.HTTPStatusError) as raised:
        request(client, kind)
    assert raised.value.response.status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["json", "bytes"])
@pytest.mark.parametrize("status", [408, 425, 500, 502, 503, 504])
def test_transient_status_can_recover_within_bound(kind, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status if len(calls) == 1 else 200, json={"ok": True})

    with source_client(handler) as client:
        result = request(client, kind)
    assert result == ({"ok": True} if kind == "json" else b'{"ok":true}')
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["json", "bytes"])
def test_transient_retries_remain_bounded(kind):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503)

    with source_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        request(client, kind)
    assert len(calls) == 4


@pytest.mark.parametrize("kind", ["json", "bytes"])
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout,
                                  httpx.RemoteProtocolError])
def test_remote_transport_errors_can_recover(kind, error):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise error("offline remote failure", request=req)
        return httpx.Response(200, json={"ok": True})

    with source_client(handler) as client:
        request(client, kind)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["json", "bytes"])
@pytest.mark.parametrize("error", [httpx.LocalProtocolError, httpx.UnsupportedProtocol])
def test_local_configuration_errors_are_not_retried(kind, error):
    calls = []

    def handler(req):
        calls.append(req)
        raise error("offline local failure", request=req)

    with source_client(handler) as client, pytest.raises(error):
        request(client, kind)
    assert len(calls) == 1


def test_source_contract_retains_api_prefix_and_documented_filters():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    with source_client(handler) as client:
        client._get_json("/buscadorpublico/contrataciones/buscador", params={
            "anio": 2026, "lista_estado_contrato": 2, "lista_codigo_objeto": 1,
            "segmento": 43, "page": 1, "page_size": 100, "palabra_clave": "",
        })
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/s8uit-services/buscadorpublico/contrataciones/buscador"
    assert dict(calls[0].url.params) == {
        "anio": "2026", "lista_estado_contrato": "2", "lista_codigo_objeto": "1",
        "segmento": "43", "page": "1", "page_size": "100",
    }
