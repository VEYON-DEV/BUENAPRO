from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, TextStringObject

from buenapro_worker.local_pdf_transport import PdfTransportError, partition_pdf


def source_pdf(count=4, payload=4000):
    writer = PdfWriter()
    for number in range(count):
        page = writer.add_blank_page(width=100 + number, height=200 + number)
        page[NameObject("/OriginalPage")] = TextStringObject(str(number + 1))
        stream = DecodedStreamObject()
        stream.set_data(b"%" + bytes([65 + number % 26]) * payload + b"\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def test_small_source_is_returned_byte_identical():
    original = source_pdf()
    chunks = partition_pdf(original)
    assert chunks[0].content is original
    assert (chunks[0].start_page, chunks[0].end_page, chunks[0].page_offset) == (1, 4, 0)


def test_all_pages_order_streams_and_geometry_are_preserved():
    original = source_pdf(7)
    chunks = partition_pdf(original, max_chunk_bytes=10_000)
    assert len(chunks) > 1
    seen = []
    offset = 0
    source = PdfReader(BytesIO(original))
    for chunk in chunks:
        assert len(chunk.content) <= 10_000
        assert chunk.page_offset == offset
        pages = PdfReader(BytesIO(chunk.content)).pages
        assert len(pages) == chunk.page_count
        for page in pages:
            index = len(seen)
            seen.append(page["/OriginalPage"])
            assert page.mediabox == source.pages[index].mediabox
            assert page.get_contents().get_data() == source.pages[index].get_contents().get_data()
        offset += chunk.page_count
    assert seen == [str(i) for i in range(1, 8)]
    assert chunks[-1].end_page == 7


def test_single_page_too_large_rejected_without_partial_result():
    with pytest.raises(PdfTransportError, match="page 1 exceeds"):
        partition_pdf(source_pdf(2, payload=20_000), max_chunk_bytes=1000)


def test_page_count_limit_also_checked_for_small_source():
    with pytest.raises(PdfTransportError, match="page count"):
        partition_pdf(source_pdf(5), max_pages=4)


def test_hard_1000_page_limit():
    with pytest.raises(PdfTransportError, match="page count 1001"):
        partition_pdf(source_pdf(1001, payload=0))


def test_encrypted_source_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    data = BytesIO()
    writer.write(data)
    with pytest.raises(PdfTransportError, match="Encrypted"):
        partition_pdf(data.getvalue())


@pytest.mark.parametrize("data", [b"not pdf", b"%PDF-invalid"])
def test_invalid_source_rejected(data):
    with pytest.raises(PdfTransportError):
        partition_pdf(data)


@pytest.mark.parametrize("kwargs", [{"max_chunk_bytes": 0}, {"max_pages": 0}, {"max_pages": 1001}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError, match="Invalid"):
        partition_pdf(source_pdf(), **kwargs)
