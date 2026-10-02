from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from buenapro_worker.settings import Settings


class Prod4Client:
    """Read-only client for the public current-opportunities API."""

    def __init__(self, settings: Settings) -> None:
        self._allowed_segments = frozenset(settings.prod4_segments)
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
        if segment not in self._allowed_segments:
            raise ValueError("Only configured relevance-rule segments are in scope")
        rows = self._get(f"listaProcesosCubso/codigoSegmento/{segment}")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("PROD4 segment listing changed shape")
        return rows

    def detail(self, procedure_id: int) -> dict[str, Any]:
        payload = self._get(f"fichaProceso/idProceso/{procedure_id}")
        if not isinstance(payload, dict) or int(payload.get("nidExpediente") or -1) != procedure_id:
            raise TypeError(f"PROD4 detail identity mismatch: {procedure_id}")
        return payload

    def download_document(self, code: UUID, *, max_bytes: int) -> bytes:
        """Download one official PDF conservatively; never persist the PDF itself."""
        if max_bytes < 5:
            raise ValueError("max_bytes must fit a PDF signature")
        url = "https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco"
        headers = {
            "Accept": "application/pdf,*/*",
            "Referer": "https://prod4.seace.gob.pe/openegocio/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
        }
        chunks: list[bytes] = []
        size = 0
        with self._client.stream("GET", url, params={"fileCode": str(code)}, headers=headers) as response:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise DocumentTooLargeError(f"Official PDF declares {length} bytes, limit {max_bytes}")
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise DocumentTooLargeError(f"Official PDF exceeded {max_bytes} bytes")
                chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF-"):
            raise ValueError("Official document is not a PDF")
        return content


class DocumentTooLargeError(ValueError):
    pass
