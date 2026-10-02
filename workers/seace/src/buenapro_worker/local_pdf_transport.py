"""Lossless PDF transport partitioning; no OCR, scoring, cropping or downsampling.

Every chunk contains consecutive original pages. Offsets must accompany model
prompts so citations can refer to the original document, not chunk-local pages.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO


class PdfTransportError(ValueError):
    """The complete source cannot safely fit the requested transport policy."""


@dataclass(frozen=True)
class PdfChunk:
    content: bytes
    start_page: int  # Inclusive, one-based original page number.
    end_page: int  # Inclusive, one-based original page number.

    @property
    def page_offset(self) -> int:
        return self.start_page - 1

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


def partition_pdf(
    pdf_bytes: bytes, *, max_chunk_bytes: int = 45_000_000, max_pages: int = 1000,
) -> tuple[PdfChunk, ...]:
    """Partition all pages in memory without changing image/text streams.

    Original bytes are returned unchanged when already within the byte policy.
    Oversize sources are re-containerized with pypdf, preserving all pages and
    their order. The source is never written or deleted. Reject encrypted PDFs,
    excessive page counts and any single page larger than the transport limit.
    """
    if max_chunk_bytes <= 0 or max_pages <= 0 or max_pages > 1000:
        raise ValueError("Invalid PDF transport limits (maximum 1000 pages)")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PdfTransportError("Source is not a PDF")
    from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise PdfTransportError("Encrypted PDF cannot be partitioned")
        count = len(reader.pages)
    except PdfTransportError:
        raise
    except Exception as exc:
        raise PdfTransportError("PDF could not be parsed") from exc
    if count == 0 or count > max_pages:
        raise PdfTransportError(f"PDF page count {count} outside supported range")
    if len(pdf_bytes) <= max_chunk_bytes:
        return (PdfChunk(pdf_bytes, 1, count),)

    def serialize(start: int, end: int) -> bytes:
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        target = BytesIO()
        writer.write(target)
        return target.getvalue()

    chunks: list[PdfChunk] = []
    start = 0
    while start < count:
        single = serialize(start, start + 1)
        if len(single) > max_chunk_bytes:
            raise PdfTransportError(f"Original page {start + 1} exceeds transport byte limit")
        best_end, best_content = start + 1, single
        low, high = start + 2, count
        while low <= high:
            end = (low + high) // 2
            content = serialize(start, end)
            if len(content) <= max_chunk_bytes:
                best_end, best_content = end, content
                low = end + 1
            else:
                high = end - 1
        chunks.append(PdfChunk(best_content, start + 1, best_end))
        start = best_end
    if sum(chunk.page_count for chunk in chunks) != count:
        raise PdfTransportError("Partition did not preserve all source pages")
    return tuple(chunks)
