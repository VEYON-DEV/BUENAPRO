from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from buenapro_worker.seace import pacing
from buenapro_worker.seace.egress import is_egress_failure, seace_http_client
from buenapro_worker.seace.schemas import SearchResponse
from buenapro_worker.settings import Settings


def _temporary_source_failure(error: BaseException) -> bool:
    """Retry remote/transient failures, never an access denial (403/429), egress outage,
    open breaker or local config error."""
    if is_egress_failure(error):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 425, 500, 502, 503, 504}
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError,
                              httpx.RemoteProtocolError))


class SeaceClient:
    def __init__(self, settings: Settings, *, pacer: pacing.SourcePacer | None = None) -> None:
        # Every request, retries included, waits for the process-wide pacer.
        self._pacer = pacer or pacing.PACER
        self._client = seace_http_client(
            base_url=settings.seace_base_url,
            timeout=settings.seace_timeout_seconds,
            headers={"User-Agent": settings.seace_user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SeaceClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_temporary_source_failure),
        wait=wait_exponential_jitter(initial=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._send(
            path,
            params={key: value for key, value in (params or {}).items() if value not in (None, "")},
        ).json()

    @retry(
        retry=retry_if_exception(_temporary_source_failure),
        wait=wait_exponential_jitter(initial=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get_bytes(self, path: str) -> bytes:
        return self._send(path).content

    def _send(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        self._pacer.acquire()
        response = self._client.get(path, params=params)
        if response.status_code in pacing.BLOCK_STATUSES:
            # 403/429 is a block of our IP: stop every PROD6 request, not just this one.
            self._pacer.trip(response.status_code)
        response.raise_for_status()
        return response

    def search_contracts(
        self,
        *,
        anio: int,
        estado: int | str = 2,
        objeto: int | str | None = None,
        segmento: int | str | None = None,
        palabra_clave: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> SearchResponse:
        payload = self._get_json(
            "/buscadorpublico/contrataciones/buscador",
            params={
                "anio": anio,
                "lista_estado_contrato": estado,
                "lista_codigo_objeto": objeto,
                "segmento": segmento,
                "palabra_clave": palabra_clave,
                "orden": 2,
                "page": page,
                "page_size": page_size,
            },
        )
        return SearchResponse.model_validate(payload)

    def contract_detail(self, id_contrato: int) -> dict[str, Any]:
        return self._get_json(
            "/buscadorpublico/contrataciones/listar-completo",
            params={"id_contrato": id_contrato},
        )

    def list_files(self, id_contrato: int, category: int = 1) -> list[dict[str, Any]]:
        payload = self._get_json(
            f"/archivo/archivos-publico/listar-archivos-contrato/{id_contrato}/{category}"
        )
        if not isinstance(payload, list):
            raise TypeError("SEACE files endpoint returned a non-list payload")
        return payload

    def download_file(self, id_contrato_archivo: int) -> bytes:
        return self._get_bytes(
            f"/archivo/archivos-publico/descargar-archivo-contrato/{id_contrato_archivo}"
        )

    def list_cubso_segments(self, anio: int) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/buscadorpublico/contrataciones/listar-segmentos-cubso",
            params={"anio": anio},
        )
        if not isinstance(payload, list):
            raise TypeError("SEACE CUBSO segment endpoint returned a non-list payload")
        return payload
