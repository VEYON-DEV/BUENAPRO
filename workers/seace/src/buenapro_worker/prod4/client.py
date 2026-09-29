from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from buenapro_worker.settings import Settings


class Prod4Client:
    """Read-only client for the public current-opportunities API."""

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.Client(
            base_url=settings.prod4_base_url.rstrip("/") + "/",
            timeout=settings.seace_timeout_seconds,
            headers={"User-Agent": settings.seace_user_agent},
            follow_redirects=True,
        )

    def __enter__(self) -> Prod4Client:
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential_jitter(initial=1, max=12),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get(self, path: str) -> Any:
        response = self._client.get(path.lstrip("/"))
        response.raise_for_status()
        return response.json()

    def by_object(self, object_code: int) -> list[dict[str, Any]]:
        if object_code not in (62, 65):
            raise ValueError("Only goods and services are in scope")
        rows = self._get(f"codObjeto/{object_code}")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("PROD4 object listing changed shape")
        return rows

    def by_segment(self, segment: int) -> list[dict[str, Any]]:
        if segment not in (43, 81):
            raise ValueError("Only configured technology segments are in scope")
        rows = self._get(f"listaProcesosCubso/codigoSegmento/{segment}")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("PROD4 segment listing changed shape")
        return rows

    def detail(self, procedure_id: int) -> dict[str, Any]:
        payload = self._get(f"fichaProceso/idProceso/{procedure_id}")
        if not isinstance(payload, dict) or int(payload.get("nidExpediente") or -1) != procedure_id:
            raise TypeError(f"PROD4 detail identity mismatch: {procedure_id}")
        return payload
