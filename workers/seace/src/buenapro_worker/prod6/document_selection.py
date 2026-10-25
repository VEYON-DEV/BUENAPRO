"""Which listed file of a PROD6 contract carries the requirement to read.

SEACE lists a contract's files in two categories. Category 1 is the requirement (TDR/EETT),
normally a PDF. Some entities attach it as a RAR/ZIP or a Word file, or publish the PDF as a
category-2 attachment ("Documentos que acreditan el cumplimiento de TDR o EETT"). Only a PDF
can be read, so:

* a readable primary PDF is always the document (unchanged behaviour);
* with no primary PDF, one attachment PDF whose name says it IS the requirement is read;
* anything else is an explicit manual review with a stable reason, never a guess.

Archives are not opened: ``prod2.archive_inventory`` exists, but it inventories a frozen
parent UUID/SHA and returns members bound to that inventory, while a PROD6 reading, its
payment receipts and ``contract_documents.sha256_original`` are bound to the SHA of the file
SEACE lists. Reading a member would need its own document row, identity and cleanup policy;
that is a design of its own and is not done halfway here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from buenapro_worker.documents.pdf import classify_requirement_file, is_archive_name

REVIEW_ARCHIVE = "primary_document_is_archive"
REVIEW_NOT_PDF = "primary_document_not_pdf"
REVIEW_NO_PRIMARY = "no_primary_document"
REVIEW_AMBIGUOUS = "ambiguous_secondary_documents"
REVIEW_MESSAGES = {
    REVIEW_ARCHIVE: "El requerimiento está en un archivo comprimido (RAR/ZIP/7z) y no hay otro PDF que lo sea: revisar a mano.",
    REVIEW_NOT_PDF: "El requerimiento no está en PDF (por ejemplo Word) y no hay otro PDF que lo sea: revisar a mano.",
    REVIEW_NO_PRIMARY: "SEACE no lista un archivo de requerimiento y ningún anexo es el TDR/EETT: revisar a mano.",
    REVIEW_AMBIGUOUS: "Varios PDF adjuntos parecen ser el requerimiento: elegir a mano.",
}


def file_name(item: dict[str, Any]) -> str:
    return str(item.get("nombre") or item.get("filename") or "")


def is_pdf_file(item: dict[str, Any]) -> bool:
    mime = str(item.get("descripcionMime") or item.get("mime") or "").lower()
    extension = str(item.get("descripcionExtension") or "").lower()
    return mime == "application/pdf" or extension == ".pdf" or file_name(item).lower().endswith(".pdf")


def is_archive_file(item: dict[str, Any]) -> bool:
    return is_archive_name(file_name(item)) or str(item.get("descripcionExtension") or "").lower() in {
        ".rar", ".zip", ".7z"}


@dataclass
class ReadingSelection:
    files: list[dict[str, Any]] = field(default_factory=list)  # Files to download, in order.
    fallback: dict[str, Any] | None = None  # The attachment chosen instead of the primary file.
    manual_review: dict[str, Any] | None = None  # {"reason", "message", "files"} or None.


def select_reading_files(primary: list[dict[str, Any]], attachments: list[dict[str, Any]],
                         objeto_codigo: int | None = None) -> ReadingSelection:
    """``primary`` are category-1 items and ``attachments`` category-2 items of the SEACE listing.

    Every primary file is still downloaded as before; the attachment is added only when no
    primary file is a PDF. ``objeto_codigo`` (1 goods, 2 services) breaks the tie between an
    EETT and a TDR attachment.
    """
    selection = ReadingSelection(files=list(primary))
    if any(is_pdf_file(item) for item in primary):
        return selection

    candidates = [(item, classify_requirement_file(file_name(item)))
                  for item in attachments if is_pdf_file(item)]
    candidates = [(item, role) for item, role in candidates if role]
    preferred = "eett" if objeto_codigo == 1 else "tdr" if objeto_codigo == 2 else None
    if preferred and any(role == preferred for _, role in candidates):
        candidates = [(item, role) for item, role in candidates if role == preferred]
    if len(candidates) == 1:
        selection.fallback = candidates[0][0]
        selection.files.append(candidates[0][0])
        return selection

    if len(candidates) > 1:
        reason, names = REVIEW_AMBIGUOUS, [file_name(item) for item, _ in candidates]
    elif any(is_archive_file(item) for item in primary):
        reason, names = REVIEW_ARCHIVE, [file_name(item) for item in primary if is_archive_file(item)]
    elif primary:
        reason, names = REVIEW_NOT_PDF, [file_name(item) for item in primary]
    else:
        reason, names = REVIEW_NO_PRIMARY, []
    selection.manual_review = {"reason": reason, "message": REVIEW_MESSAGES[reason], "files": names[:5]}
    return selection


def listing_items(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Category-1 and category-2 listing items rebuilt from ``contract_documents`` rows.

    ``raw_file_json`` is the SEACE item; a row without it falls back to its columns.
    """
    primary: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row.get("raw_file_json") or {})
        item.setdefault("nombre", row.get("filename"))
        item.setdefault("descripcionMime", row.get("mime"))
        item["idContratoArchivo"] = int(row["id_contrato_archivo"])
        (primary if int(row.get("categoria") or 1) == 1 else attachments).append(item)
    return primary, attachments
