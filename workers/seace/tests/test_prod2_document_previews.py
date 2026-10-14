"""Local bounded samples preserve originals, secrets and cache provenance."""
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject as subject_numeric

from buenapro_worker.prod2 import document_previews as subject


def pdf_bytes(labels):
    writer = PdfWriter()
    for label in labels:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 40 740 Td ({label}) Tj ET".encode("latin1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def source(tmp_path, labels=None, *, suffix=".pdf"):
    path = tmp_path / ("original" + suffix)
    path.write_bytes(pdf_bytes(labels or ["Condiciones de servicio software " * 8]))
    return path


def test_native_pdf_source_hash_private_cache_and_exact_replay(tmp_path, monkeypatch):
    path = source(tmp_path, ["Objeto de la contratacion: desarrollo de software " * 6,
                            "Requisitos de calificacion: experiencia " * 6])
    original = path.read_bytes()
    result = subject.document_preview(path, tmp_path / "work")
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["method"] == "native_text" and result["page_count"] == 2
    assert result["preview_pages"] == [1, 2] and result["sample_only"] is True
    assert "software" in result["preview_text"] and "Requisitos" in result["preview_text"]
    assert path.read_bytes() == original
    cache = list((tmp_path / "work/document-preview-cache").glob("*.private.json"))
    assert len(cache) == 1 and cache[0].stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(subject, "_extract_process", lambda *args: pytest.fail("Cache replay must be local"))
    assert subject.document_preview(path, tmp_path / "work") == result


def test_requirements_beyond_opening_pages_are_sampled(tmp_path):
    labels = ["Opening context " * 12] * 5 + ["Requisitos de calificacion software specialized experience " * 5]
    result = subject.document_preview(source(tmp_path, labels), tmp_path / "work", max_ocr_pages=0)
    assert 6 in result["preview_pages"]
    assert "Requisitos de calificacion" in result["preview_text"]


def test_opening_headings_do_not_hide_later_technical_requirements(tmp_path):
    labels = ["Objeto de la contratacion contexto de bases " * 20] * 3
    labels += ["Non requirements context " * 12] * 2
    labels += ["Requisitos de calificacion: experiencia SPECIFIC_LATER_REQUIREMENT " * 5]
    result = subject.document_preview(source(tmp_path, labels), tmp_path / "work", max_ocr_pages=0)
    assert 6 in result["preview_pages"] and "SPECIFIC_LATER_REQUIREMENT" in result["preview_text"]


def test_secrets_identifiers_and_emails_are_redacted_before_truncation(tmp_path):
    value = ("Requisitos: software " * 8 + " soporte@example.pe https://example.pe/?token=secret "
             "12345678 apikey_abcdef_123456 token=abcDEFghi Bearer secretXYZ")
    result = subject.document_preview(source(tmp_path, [value]), tmp_path / "work")
    for secret in ("soporte@example.pe", "https://example.pe", "12345678", "apikey_abcdef_123456",
                   "abcDEFghi", "secretXYZ"):
        assert secret not in json.dumps(result)


def test_modified_original_has_a_new_cache_and_policy_options_are_isolated(tmp_path):
    path = source(tmp_path)
    first = subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)
    second = subject.document_preview(path, tmp_path / "work", max_characters=800, max_ocr_pages=0)
    path.write_bytes(pdf_bytes(["Different software requirements " * 10]))
    third = subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)
    assert first["source_sha256"] == second["source_sha256"] != third["source_sha256"]
    assert first["content_hash"] != second["content_hash"]
    assert len(list((tmp_path / "work/document-preview-cache").glob("*.private.json"))) == 3


def test_tampered_cache_is_rejected(tmp_path):
    path = source(tmp_path)
    subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)
    cache = next((tmp_path / "work/document-preview-cache").glob("*.private.json"))
    data = json.loads(cache.read_text())
    data["preview_text"] = "Modified sample"
    cache.write_text(json.dumps(data))
    with pytest.raises(subject.DocumentPreviewError, match="cache verification"):
        subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)


def test_docx_reads_body_without_conversion_or_relationship_resolution(tmp_path):
    path = tmp_path / "original.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Terminos de referencia desarrollo software</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>soporte@example.pe</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr("word/_rels/document.xml.rels", '<Relationships><Relationship Target="https://example.pe" TargetMode="External"/></Relationships>')
    original = path.read_bytes()
    result = subject.document_preview(path, tmp_path / "work")
    assert result["method"] == "word_text" and result["page_count"] is None
    assert result["preview_pages"] == [] and "software" in result["preview_text"]
    assert "example.pe" not in result["preview_text"] and original == path.read_bytes()


def word_source(tmp_path, xml, *, name="large.docx"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path


def large_word_xml():
    paragraph = '<w:p><w:r><w:t>' + 'Contexto del servicio industrial. ' * 6 + '</w:t></w:r></w:p>'
    opening = '<w:p><w:r><w:t>Terminos de referencia mantenimiento electromecanico</w:t></w:r></w:p>'
    return ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + opening + paragraph * 50000 + '</w:body></w:document>')


def test_realistic_large_docx_streaming_has_bounded_sample_verified_cache_and_no_mutation(tmp_path):
    xml = large_word_xml()
    assert 11 * 1024 * 1024 < len(xml.encode()) < subject.MAX_XML_BYTES
    path = word_source(tmp_path, xml)
    original = path.read_bytes()
    result = subject.document_preview(path, tmp_path / "work")
    assert result["method"] == "word_text" and result["page_count"] is None
    assert result["preview_pages"] == [] and 0 < len(result["preview_text"]) <= 3500
    assert "electromecanico" in result["preview_text"]
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["preview_policy"] == "prod2_local_document_sample_v2"
    assert path.read_bytes() == original
    assert subject.document_preview(path, tmp_path / "work") == result


def test_small_docx_preserves_legacy_sample_exactly_when_streaming_exists(tmp_path):
    xml = large_word_xml().split('<w:p>', 3)[0] + '<w:p><w:r><w:t>Contexto industrial</w:t></w:r></w:p>'
    xml += '<w:p><w:r><w:t>Requisitos de calificacion: experiencia del postor</w:t></w:r></w:p></w:body></w:document>'
    path = word_source(tmp_path, xml, name="small.docx")
    from xml.etree import ElementTree
    root = ElementTree.fromstring(xml)
    body = root.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")
    joined = "\n".join(" ".join(node.itertext()) for node in body.findall(
        ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"))
    expected, _ = subject._bounded_sample([(None, joined)], 3500)
    assert subject._docx_sample(path, 3500) == {"preview_text": expected, "page_count": None,
        "preview_pages": [], "method": "word_text"}


def test_streaming_preserves_nested_word_textbox_paragraph_order(tmp_path, monkeypatch):
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' \
        '<w:p><w:r><w:t>OUTER_BEGIN</w:t></w:r><w:txbxContent><w:p><w:r><w:t>INNER</w:t></w:r></w:p>' \
        '</w:txbxContent><w:r><w:t>OUTER_END</w:t></w:r></w:p>' \
        '<w:p><w:r><w:t>LAST</w:t></w:r></w:p></w:body></w:document>'
    path = word_source(tmp_path, xml)
    legacy = subject._docx_sample(path, 3500)
    monkeypatch.setattr(subject, "FULL_XML_PARSE_BYTES", 1)
    assert subject._docx_sample(path, 3500) == legacy


@pytest.mark.parametrize("marker", [b"<!DOCTYPE", b"<!ENTITY"])
def test_word_xml_guards_chunk_boundaries_and_encoded_entities(tmp_path, marker):
    raw = b" " * (65536 - 4) + marker + b" forbidden>"
    path = word_source(tmp_path, raw)
    with pytest.raises(subject.DocumentPreviewError, match="unsafe XML"):
        subject._docx_sample(path, 3500)
    path = word_source(tmp_path, raw.decode().encode("utf-16"), name="encoded.docx")
    with pytest.raises(subject.DocumentPreviewError, match="unsafe XML"):
        subject._docx_sample(path, 3500)


def test_large_word_xml_validates_tail_size_and_structure_before_pinning_sample(tmp_path, monkeypatch):
    xml = large_word_xml()
    path = word_source(tmp_path, xml[:-len('</w:body></w:document>')])
    with pytest.raises(subject.DocumentPreviewError, match="word_malformed_xml") as failure:
        subject.document_preview(path, tmp_path / "work")
    assert failure.value.reason_code == "word_malformed_xml"
    assert not list((tmp_path / "work/document-preview-cache").glob("*.private.json"))
    monkeypatch.setattr(subject, "MAX_XML_BYTES", 9 * 1024 * 1024)
    path = word_source(tmp_path, xml, name="oversized.docx")
    with pytest.raises(subject.DocumentPreviewError, match="size policy"):
        subject._docx_sample(path, 3500)
    monkeypatch.setattr(subject, "MAX_XML_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(subject, "MAX_XML_PARAGRAPHS", 5)
    with pytest.raises(subject.DocumentPreviewError, match="structure policy"):
        subject._docx_sample(path, 3500)


def test_word_active_content_reason_survives_child_without_document_text(tmp_path):
    path = word_source(tmp_path, '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>PRIVATE_DOCUMENT_CONTENT</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("word/embeddings/object.bin", b"active")
    with pytest.raises(subject.DocumentPreviewError, match="word_active_content") as failure:
        subject.document_preview(path, tmp_path / "work")
    assert failure.value.reason_code == "word_active_content"
    assert "PRIVATE_DOCUMENT_CONTENT" not in str(failure.value)


def test_streaming_rejects_excessive_xml_nesting(tmp_path, monkeypatch):
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' \
        + '<w:wrapper>' * subject.MAX_XML_DEPTH + '<w:p><w:r><w:t>text</w:t></w:r></w:p>' \
        + '</w:wrapper>' * subject.MAX_XML_DEPTH + '</w:body></w:document>'
    monkeypatch.setattr(subject, "FULL_XML_PARSE_BYTES", 1)
    with pytest.raises(subject.DocumentPreviewError, match="structure policy"):
        subject._docx_sample(word_source(tmp_path, xml), 3500)


def test_doc_conversion_is_isolated_preserves_bytes_and_uses_word_method(tmp_path, monkeypatch):
    path = tmp_path / "original.doc"
    path.write_bytes(b"binary Word fixture")
    original = path.read_bytes()
    def convert(copy, extension, *, max_bytes):
        assert copy != path and copy.read_bytes() == original and extension == "doc"
        target = copy.with_suffix(".pdf")
        target.write_bytes(pdf_bytes(["Terminos de referencia software " * 8]))
        return target
    monkeypatch.setattr(subject, "prepare_pdf", convert)
    monkeypatch.setattr(subject, "_extract_process", subject._extract)
    result = subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)
    assert result["method"] == "word_text" and path.read_bytes() == original
    assert not list((tmp_path / "work/document-preview-cache").glob("document-preview-*"))


@pytest.mark.parametrize("options", [{"max_characters": 100000}, {"max_characters": True},
                                    {"max_ocr_pages": 4}, {"max_ocr_pages": -1}])
def test_sampling_bounds(tmp_path, options):
    with pytest.raises(subject.DocumentPreviewError, match="options"):
        subject.document_preview(source(tmp_path), tmp_path / "work", **options)


def test_source_symlink_parent_symlink_unsafe_path_and_unsupported_format(tmp_path):
    path = source(tmp_path)
    link = tmp_path / "linked.pdf"
    link.symlink_to(path)
    with pytest.raises(subject.DocumentPreviewError, match="symlink"):
        subject.document_preview(link, tmp_path / "work")
    parent = tmp_path / "linked"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(subject.DocumentPreviewError, match="symlink"):
        subject.document_preview(parent / path.name, tmp_path / "work")
    with pytest.raises(subject.DocumentPreviewError, match="unsafe"):
        subject.document_preview(tmp_path / ".." / path.name, tmp_path / "work")
    unsupported = tmp_path / "file.zip"
    unsupported.write_bytes(b"zip")
    with pytest.raises(subject.DocumentPreviewError, match="format"):
        subject.document_preview(unsupported, tmp_path / "work")


def test_size_page_encryption_and_timeout_fail_without_caching(tmp_path, monkeypatch):
    path = source(tmp_path)
    monkeypatch.setattr(subject, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(subject.DocumentPreviewError, match="byte policy"):
        subject.document_preview(path, tmp_path / "work")
    monkeypatch.setattr(subject, "MAX_SOURCE_BYTES", 128 * 1024 * 1024)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    with path.open("wb") as target:
        writer.write(target)
    with pytest.raises(subject.DocumentPreviewError, match="Bounded"):
        subject.document_preview(path, tmp_path / "work")
    monkeypatch.setattr(subject, "PROCESS_TIMEOUT_SECONDS", 0)
    with pytest.raises(subject.DocumentPreviewError, match="Bounded"):
        subject.document_preview(path, tmp_path / "work")
    assert not list((tmp_path / "work/document-preview-cache").glob("*.private.json"))


def test_pdf_page_limit_and_symlinked_cache_are_rejected(tmp_path):
    path = tmp_path / "large.pdf"
    writer = PdfWriter()
    for _ in range(subject.MAX_PAGES + 1):
        writer.add_blank_page(width=10, height=10)
    with path.open("wb") as target:
        writer.write(target)
    with pytest.raises(subject.DocumentPreviewError, match="Bounded"):
        subject.document_preview(path, tmp_path / "work", max_ocr_pages=0)
    path = source(tmp_path)
    work = tmp_path / "linked-work"
    work.mkdir()
    (work / "document-preview-cache").symlink_to(tmp_path / "work/document-preview-cache", target_is_directory=True)
    with pytest.raises(subject.DocumentPreviewError, match="symlink"):
        subject.document_preview(path, work)


def test_source_mutation_during_extraction_does_not_pin_cache(tmp_path, monkeypatch):
    path = source(tmp_path)
    def extract(path, *args):
        path.write_bytes(b"changed")
        return {"preview_text": "safe", "page_count": 1, "preview_pages": [1], "method": "native_text"}
    monkeypatch.setattr(subject, "_extract_process", extract)
    with pytest.raises(subject.DocumentPreviewError, match="changed"):
        subject.document_preview(path, tmp_path / "work")
    assert not list((tmp_path / "work/document-preview-cache").glob("*.private.json"))


def test_real_local_scanned_pdf_uses_spanish_ocr_and_preserves_original(tmp_path):
    if not (shutil.which("pdftoppm") or Path("/opt/homebrew/bin/pdftoppm").is_file()) or not (
            shutil.which("tesseract") or Path("/opt/homebrew/bin/tesseract").is_file()):
        pytest.skip("Local OCR tools unavailable")
    vector = source(tmp_path, ["TERMINOS DE REFERENCIA. Desarrollo de software empresarial"])
    raster = tmp_path / "raster"
    subprocess.run([subject._tool("pdftoppm"), "-gray", "-singlefile", "-scale-to", "1800",
                    str(vector), str(raster)], check=True, capture_output=True, timeout=30)
    data = raster.with_suffix(".pgm").read_bytes()
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255\s", data)
    assert header
    width, height = map(int, header.groups())
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): subject_numeric(8),
        NameObject("/Width"): subject_numeric(width), NameObject("/Height"): subject_numeric(height)})
    image.set_data(data[header.end():])
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"):
        DictionaryObject({NameObject("/Im0"): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 612 0 0 792 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "scanned.pdf"
    with path.open("wb") as target:
        writer.write(target)
    original = path.read_bytes()
    result = subject.document_preview(path, tmp_path / "work")
    assert result["method"] == "OCR" and result["preview_pages"] == [1]
    assert "software" in result["preview_text"].lower()
    assert path.read_bytes() == original
    assert not list((tmp_path / "work/document-preview-cache").glob("document-preview-*"))
