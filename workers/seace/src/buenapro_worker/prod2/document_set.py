"""All loose official documents, with no selection by filename or document role.

ZIP/RAR are explicitly deferred. A missing or unreadable loose attachment blocks
the model call; an old single-document derivative is never a document-set cache.
The first sorted UUID is a storage anchor only, not a preferred requirements file.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
from uuid import UUID

from pypdf import PdfReader, PdfWriter

from buenapro_worker.local_document_conversion import prepare_pdf
from buenapro_worker.normalization.facets import canonical_hash
from .documents import METADATA_URL


DIRECT_DOCUMENTS_POLICY = "prod2_direct_documents_v1"
MAX_ANALYSIS_BYTES = 20 * 1024 * 1024
MAX_ANALYSIS_PAGES = 1000
SUPPORTED_DIRECT = {".pdf", ".doc", ".docx"}
DEFERRED_ARCHIVES = {".zip", ".rar"}


class DocumentSetError(ValueError):
    """No partial or ambiguous loose-document set may be sent for paid analysis."""


def _metadata(doc: dict) -> dict:
    code = str(UUID(doc["uuid"]))
    fields = {key: doc.get(key) for key in ("filename", "stage", "type", "published_at")}
    if any(not isinstance(value, str) for value in fields.values()):
        raise DocumentSetError("Official document metadata must be complete")
    return {"document_uuid": code, **fields, "source_url": METADATA_URL + "?id=" + code}


def document_inventory(documents: list[dict], *, inventory_complete: bool) -> tuple[list[dict], list[dict]]:
    """Partition by format only; names, dates and roles never discard a PDF/Word."""
    if inventory_complete is not True or not isinstance(documents, list):
        raise DocumentSetError("A complete official inventory is required")
    direct, excluded, seen = [], [], set()
    for doc in documents:
        metadata = _metadata(doc)
        code = metadata["document_uuid"]
        if code in seen:
            raise DocumentSetError("Duplicate official document UUID")
        seen.add(code)
        suffix = Path(metadata["filename"]).suffix.lower()
        if suffix in SUPPORTED_DIRECT:
            direct.append(metadata)
        elif suffix in DEFERRED_ARCHIVES:
            excluded.append({**metadata, "reason": "archive_deferred"})
        else:
            raise DocumentSetError("Unsupported loose attachment requires review")
    if not direct:
        raise DocumentSetError("No loose PDF or Word attachment is available")
    return sorted(direct, key=lambda row: row["document_uuid"]), sorted(excluded, key=lambda row: row["document_uuid"])


def prepare_direct_document_set(folder: Path, documents: list[dict], downloads: dict,
                                *, inventory_complete: bool) -> dict:
    from .batch_runner import private_write, read_private

    direct, excluded = document_inventory(documents, inventory_complete=inventory_complete)
    originals = {}
    for doc in direct:
        code = doc["document_uuid"]
        downloaded = downloads.get(code)
        if not downloaded:
            raise DocumentSetError("A loose attachment has no verified download")
        original = Path(downloaded["original"]).resolve()
        sha = downloaded["manifest"].get("sha256")
        if (not original.is_relative_to(folder.resolve()) or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sha)
                or not 0 < original.stat().st_size <= MAX_ANALYSIS_BYTES
                or hashlib.sha256(original.read_bytes()).hexdigest() != sha):
            raise DocumentSetError("Original attachment does not match its private download")
        doc["original_sha256"] = sha
        originals[code] = original
    set_hash = canonical_hash({"document_scope": DIRECT_DOCUMENTS_POLICY,
                               "document_set": direct, "excluded_documents": excluded})
    private = folder / ("direct-analysis-" + set_hash)
    private.mkdir(mode=0o700, exist_ok=True)
    marker, target = private / "manifest.private.json", private / "analysis.pdf"
    anchor = direct[0]
    common = {"document_scope": DIRECT_DOCUMENTS_POLICY, "document_set": direct,
              "excluded_documents": excluded, "document_set_hash": set_hash,
              "anchor_document_uuid": anchor["document_uuid"],
              "anchor_original_sha256": anchor["original_sha256"]}
    if marker.exists():
        pinned = read_private(marker)
        if (any(pinned.get(key) != value for key, value in common.items())
                or hashlib.sha256(target.read_bytes()).hexdigest() != pinned.get("analysis_sha256")):
            raise DocumentSetError("Pinned document set no longer matches its exact inventory/bytes")
        return {**pinned, "pdf_path": target}

    writer, manifest, rendered, pages = PdfWriter(), [], {}, 0
    for doc in direct:
        code, original = doc["document_uuid"], originals[doc["document_uuid"]]
        work = private / code
        work.mkdir(mode=0o700, exist_ok=True)
        copied = work / (code + Path(doc["filename"]).suffix.lower())
        content = original.read_bytes()
        if copied.exists() and copied.read_bytes() != content:
            raise DocumentSetError("Private conversion input has changed")
        if not copied.exists():
            with os.fdopen(os.open(copied, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(content)
        pdf_path = prepare_pdf(copied, copied.suffix.lstrip("."), max_bytes=MAX_ANALYSIS_BYTES)
        pdf = pdf_path.read_bytes()
        if len(pdf) > MAX_ANALYSIS_BYTES:
            raise DocumentSetError("Converted attachment exceeds analysis size policy")
        converted_sha = hashlib.sha256(pdf).hexdigest()
        if converted_sha in rendered:
            previous = rendered[converted_sha]
            manifest.append({**doc, "analysis_sha256": converted_sha,
                "start_page": previous["start_page"], "end_page": previous["end_page"],
                "duplicate_of": previous["document_uuid"]})
            continue
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted or not reader.pages:
            raise DocumentSetError("An attachment is encrypted or has no readable PDF pages")
        if pages + len(reader.pages) > MAX_ANALYSIS_PAGES:
            raise DocumentSetError("Loose document set exceeds analysis page policy")
        record = {**doc, "analysis_sha256": converted_sha,
                  "start_page": pages + 1, "end_page": pages + len(reader.pages)}
        for page in reader.pages:
            writer.add_page(page)
        writer.add_outline_item(doc["filename"], pages)
        pages += len(reader.pages)
        manifest.append(record)
        rendered[converted_sha] = record
    combined = io.BytesIO()
    writer.write(combined)
    pdf = combined.getvalue()
    if not 0 < len(pdf) <= MAX_ANALYSIS_BYTES:
        raise DocumentSetError("Loose document set exceeds analysis size policy")
    temporary = private / "analysis.pending.pdf"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
        out.write(pdf)
    os.replace(temporary, target)
    pinned = {**common, "analysis_sha256": hashlib.sha256(pdf).hexdigest(),
              "source_document_manifest": manifest, "page_count": pages,
              "analysis_bytes": len(pdf)}
    private_write(marker, pinned)
    return {**pinned, "pdf_path": target}
