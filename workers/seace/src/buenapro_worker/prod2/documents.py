"""Official PROD2 document download; temporary Alfresco tickets never leave this module.

Pass a synchronous httpx.Client using normal TLS verification (verify=True).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

METADATA_URL = "https://alfprod.seace.gob.pe/alfresco/service/osce/downloadDoc"
CONTEXT_URL = "https://alfprod.seace.gob.pe/alfresco/"
_CALLBACK = "buenaproDocument"


class DocumentDownloadError(ValueError):
    """Sanitized error: never includes URLs, tickets, or server response bodies."""


def _official_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not host.endswith(".seace.gob.pe")
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise DocumentDownloadError("Document URL is outside official HTTPS hosts")
    return url


def _get(client: httpx.Client, url: str, *, params=None, max_bytes: int) -> tuple[bytes, str]:
    for _ in range(6):
        _official_url(url)
        with client.stream("GET", url, params=params, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise DocumentDownloadError("Document redirect lacks location")
                url = _official_url(urljoin(str(response.url), location))
                params = None
                continue
            if response.status_code != 200:
                raise DocumentDownloadError(f"Official document HTTP status {response.status_code}")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise DocumentDownloadError("Official document exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("content-type", "").split(";")[0]
    raise DocumentDownloadError("Too many document redirects")


def _metadata(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8").strip()
        if not text.startswith("{"):
            match = re.fullmatch(r"buenaproDocument\s*\((.*)\)\s*;?", text, re.S)
            if not match:
                raise ValueError()
            text = match.group(1)
        payload = json.loads(text)
        if not isinstance(payload, dict) or str(payload.get("result", "200")) != "200":
            raise ValueError()
        if not isinstance(payload.get("downloadUrl"), str) or not payload["downloadUrl"]:
            raise ValueError()
        return payload
    except (ValueError, UnicodeError):
        raise DocumentDownloadError("Invalid official document metadata") from None


def download_document(client: httpx.Client, uuid: str | UUID, destination: str | Path,
                      *, expected_format: str = "pdf", max_bytes: int = 50 * 1024 * 1024) -> dict:
    """Download one known UUID; return only stable identity and sanitized file facts.

    expected_format accepts pdf, zip (including DOCX), ole (DOC/XLS), rar, or 7z.
    Archives are saved as-is and never extracted.
    Does not discover documents, invoke models, or write metadata to a database.
    """
    try:
        code = str(UUID(str(uuid)))
    except ValueError:
        raise DocumentDownloadError("Invalid document UUID") from None
    if expected_format not in {"pdf", "zip", "ole", "rar", "7z"}:
        raise DocumentDownloadError("Unsupported expected document format")
    if max_bytes < 8:
        raise DocumentDownloadError("Invalid document size limit")
    try:
        raw, _ = _get(client, METADATA_URL, params={"id": code, "doc": _CALLBACK,
                      "guest": "false"}, max_bytes=1024 * 1024)
        reference = _metadata(raw)["downloadUrl"]
        # Alfresco returns /service/... relative to its application context,
        # not to the origin root. Absolute URLs still undergo the same validation.
        if reference.startswith("/service/"):
            url = CONTEXT_URL.rstrip("/") + reference
        else:
            url = urljoin(CONTEXT_URL, reference)
        content, content_type = _get(client, _official_url(url), max_bytes=max_bytes)
    except httpx.HTTPError:
        raise DocumentDownloadError("Official document transport failed") from None
    kind = next((kind for signature, kind in (
        (b"%PDF-", "pdf"), (b"PK\x03\x04", "zip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"), (b"Rar!\x1a\x07", "rar"),
        (b"7z\xbc\xaf\x27\x1c", "7z"),
    ) if content.startswith(signature)), None)
    if kind != expected_format:
        raise DocumentDownloadError("Official document signature does not match expected format")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwrites and symlinks; failed validation never creates a file.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
    manifest = {"document_uuid": code, "source_url": METADATA_URL + "?id=" + code,
                "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "content_type": content_type, "format": kind}
    if kind == "pdf":
        try:
            info = subprocess.run(["pdfinfo", str(target)], capture_output=True, text=True,
                                  timeout=15, check=True)
            pages = re.search(r"^Pages:\s+(\d+)", info.stdout, re.M)
            if pages:
                manifest["pages"] = int(pages.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return manifest
