import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID
from xml.etree.ElementTree import ParseError

import pytest

from buenapro_worker.local_document_conversion import (
    WORD_FOOTNOTES_AT_END_POLICY, _footnotes_at_end, bounded_command, convert_doc, convert_docx,
    extract_archive, prepare_pdf, safe_member, select_bases_member,
)
from buenapro_worker.local_prod4 import OfficialBrowserDownload


@pytest.mark.parametrize("name", ["../bases.pdf", "/bases.pdf", "C:\\bases.pdf", "foo/../../bases.pdf"])
def test_archive_paths_are_never_extracted(name):
    with pytest.raises(ValueError):
        safe_member(name)


def test_zip_selects_integrated_bases_without_extracting_other_files(tmp_path):
    archive = tmp_path / "original.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("docs/Bases integradas.pdf", b"%PDF-test")
        out.writestr("anexo.pdf", b"%PDF-other")
    result = extract_archive(archive, "zip", max_bytes=100_000)
    assert result.read_bytes() == b"%PDF-test"
    assert not (tmp_path / "docs").exists()


def test_bases_and_companion_tdr_are_not_ambiguous():
    assert select_bases_member(["CPS 009 BASES ARANDA.pdf", "CPS 009 TDR ARANDA.pdf"]) == "CPS 009 BASES ARANDA.pdf"


def test_archive_directory_does_not_classify_a_resolution_as_bases():
    assert select_bases_member(["BASES/RESOLUCION COMPATIBILIZACION.pdf", "BASES/BASES ADMINISTRATIVAS.pdf"]) == "BASES/BASES ADMINISTRATIVAS.pdf"


def test_explicit_visadas_pdf_is_preferred_to_editable_bases_variant():
    assert select_bases_member(["BASES/Bases Administrativas - Aranda.docx", "BASES/Bases Administrativas visadas.pdf"]) == "BASES/Bases Administrativas visadas.pdf"


def test_archive_reads_bases_and_tdr_with_page_provenance(tmp_path):
    import io
    import json
    from pypdf import PdfWriter, PdfReader
    content = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(content)
    archive = tmp_path / "original.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("BASES proceso.pdf", content.getvalue())
        output.writestr("TDR proceso.pdf", content.getvalue())
    combined = extract_archive(archive, "zip", max_bytes=100_000)
    assert len(PdfReader(combined).pages) == 2
    manifest = json.loads((tmp_path / "local-document-manifest.json").read_text())
    assert manifest == [{"member": "BASES proceso.pdf", "start_page": 1, "end_page": 1},
                        {"member": "TDR proceso.pdf", "start_page": 2, "end_page": 2}]


def test_integrated_bases_do_not_mix_other_versions():
    from buenapro_worker.local_document_conversion import select_requirements_members
    assert select_requirements_members(["BASES INTEGRADAS.pdf", "TDR anterior.pdf"]) == ["BASES INTEGRADAS.pdf"]


@pytest.mark.parametrize("names", [["first.pdf", "second.pdf"], ["bases.pdf", "run.exe"], ["../bases.pdf"]])
def test_ambiguous_and_executable_archives_are_rejected(names):
    with pytest.raises(ValueError):
        select_bases_member(names)


def test_zip_declared_expansion_cannot_exceed_policy(tmp_path):
    archive = tmp_path / "original.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("bases.pdf", b"x" * 10000)
    with pytest.raises(ValueError):
        extract_archive(archive, "zip", max_bytes=100)


def make_docx(path, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)


def native_fodt(position="page"):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<text:notes-configuration text:note-class="footnote" '
        f'text:footnotes-position="{position}"/>'
        '<office:body><text:p>Full requirements with an image: aGVsbG8=</text:p>'
        '<text:note text:note-class="footnote"><text:p>Entire footnote retained.</text:p>'
        '</text:note></office:body></office:document>').encode()


def test_native_footnotes_policy_changes_only_placement_and_preserves_every_other_byte():
    original = native_fodt()
    derived, receipt = _footnotes_at_end(original)
    assert derived == original.replace(b'text:footnotes-position="page"', b'text:footnotes-position="document"')
    assert receipt == {"policy": WORD_FOOTNOTES_AT_END_POLICY, "original_placement": "page",
        "derived_placement": "document", "only_placement_attribute_changed": True, "footnote_count": 1,
        "native_fodt_sha256": hashlib.sha256(original).hexdigest(),
        "derived_fodt_sha256": hashlib.sha256(derived).hexdigest()}
    already, repeated = _footnotes_at_end(derived)
    assert already == derived and repeated["native_fodt_sha256"] == repeated["derived_fodt_sha256"]


@pytest.mark.parametrize("content", [
    b'<!DOCTYPE unsafe [<!ENTITY x "bad">]>' + native_fodt(),
    native_fodt("section"),
    native_fodt().replace(b'<text:notes-configuration', b'<wrong:notes-configuration'),
    native_fodt().replace(b'<text:notes-configuration text:note-class="footnote" text:footnotes-position="page"/>', b''),
    native_fodt().replace(b'</office:body>', b'</office:body><text:notes-configuration text:note-class="footnote" text:footnotes-position="page"/>'),
])
def test_native_footnotes_policy_rejects_unsafe_or_ambiguous_layout(content):
    with pytest.raises((ValueError, ParseError)):
        _footnotes_at_end(content)


@pytest.mark.parametrize("extension", ["doc", "docx"])
def test_explicit_word_policy_keeps_original_and_private_native_conversion_receipt(tmp_path, extension):
    path = tmp_path / ("source." + extension)
    if extension == "docx":
        make_docx(path)
    else:
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic")
    original = path.read_bytes()
    calls = []
    def convert(command, **kwargs):
        calls.append(command)
        if command[0] == "pdfinfo":
            return subprocess.CompletedProcess(command, 0)
        assert kwargs["timeout"] == 120 and kwargs["env"]["SAL_USE_VCLPLUGIN"] == "svp"
        source = Path(command[-1])
        output = Path(command[command.index("--outdir") + 1])
        kind = command[command.index("--convert-to") + 1]
        if kind == "fodt":
            assert source == path
            (output / source.with_suffix(".fodt").name).write_bytes(native_fodt())
        else:
            assert kind == "pdf:writer_pdf_Export"
            assert source.read_bytes() == _footnotes_at_end(native_fodt())[0]
            assert source.stat().st_mode & 0o777 == 0o600
            (output / source.with_suffix(".pdf").name).write_bytes(b"%PDF-test")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert):
        result = prepare_pdf(path, extension, max_bytes=100_000,
                             word_conversion_policy=WORD_FOOTNOTES_AT_END_POLICY)
    assert len(calls) == 3 and path.read_bytes() == original
    receipt_path = result.parent.parent / "word-conversion.private.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert receipt["conversion_source_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["pdf_sha256"] == hashlib.sha256(result.read_bytes()).hexdigest()
    assert receipt["footnote_count"] == 1
    settings = (result.parent.parent / "user/registrymodifications.xcu").read_text()
    assert "DisableMacrosExecution" in settings and "DisableActiveContent" in settings


def test_word_policy_detects_source_mutation_before_pdf_export(tmp_path):
    path = tmp_path / "source.docx"
    make_docx(path)
    def mutate(command, **kwargs):
        output = Path(command[command.index("--outdir") + 1])
        (output / "source.fodt").write_bytes(native_fodt())
        path.write_bytes(b"changed")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=mutate) as run:
        with pytest.raises(ValueError, match="source changed"):
            convert_docx(path, max_bytes=100_000, word_conversion_policy=WORD_FOOTNOTES_AT_END_POLICY)
        assert run.call_count == 1


@pytest.mark.parametrize("output", [None, b"x" * 201])
def test_word_policy_blocks_missing_or_oversized_native_intermediary(tmp_path, output):
    path = tmp_path / "source.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    def convert(command, **kwargs):
        if output is not None:
            folder = Path(command[command.index("--outdir") + 1])
            (folder / "source.fodt").write_bytes(output)
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert) as run:
        with pytest.raises(ValueError, match="intermediary is missing or exceeds size policy"):
            convert_doc(path, max_bytes=100, word_conversion_policy=WORD_FOOTNOTES_AT_END_POLICY)
        assert run.call_count == 1


def test_invalid_word_policy_fails_before_any_conversion_or_file_access(tmp_path):
    with patch("buenapro_worker.local_document_conversion.subprocess.run") as run:
        with pytest.raises(ValueError, match="Unsupported explicit Word conversion policy"):
            prepare_pdf(tmp_path / "missing.docx", "docx", max_bytes=100_000, word_conversion_policy="unknown")
        run.assert_not_called()


@pytest.mark.parametrize("extra", [
    {"word/vbaProject.bin": "macro"},
    {"word/embeddings/oleObject1.bin": "ole"},
    {"word/_rels/document.xml.rels": '<Relationships><Relationship Type="image" TargetMode="External" Target="https://example.com/image"/></Relationships>'},
])
def test_docx_active_contents_are_rejected_before_soffice(tmp_path, extra):
    path = tmp_path / "bases.docx"
    make_docx(path, extra)
    with patch("buenapro_worker.local_document_conversion.subprocess.run") as run:
        with pytest.raises(ValueError):
            convert_docx(path, max_bytes=100_000)
        run.assert_not_called()


def test_docx_normal_normative_hyperlinks_and_isolated_profile(tmp_path):
    path = tmp_path / "bases.docx"
    make_docx(path, {"word/_rels/document.xml.rels": '<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" TargetMode="External" Target="https://www.gob.pe/"/></Relationships>'})
    def convert(command, **kwargs):
        assert "--headless" in command
        assert any("UserInstallation=" in item for item in command)
        assert kwargs["timeout"] == 120
        assert kwargs["env"]["SAL_USE_VCLPLUGIN"] == "svp"
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / path.with_suffix(".pdf").name).write_bytes(b"%PDF-test")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert):
        assert convert_docx(path, max_bytes=100_000).read_bytes() == b"%PDF-test"
    profile = next(tmp_path.glob("libreoffice-profile-*"))
    assert "MacroSecurityLevel" in (profile / "user/registrymodifications.xcu").read_text()


def test_docx_detaches_external_author_template_only_from_derivative(tmp_path):
    path = tmp_path / "bases.docx"
    make_docx(path, {"word/settings.xml": '<settings xmlns:r="urn:r"><attachedTemplate r:id="rId1"/></settings>',
        "word/_rels/settings.xml.rels": '<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" TargetMode="External" Target="file:///C:/author/template.dotx"/></Relationships>'})
    original = path.read_bytes()
    def convert(command, **kwargs):
        source = Path(command[-1])
        assert source.name == "sanitized-bases.docx"
        with zipfile.ZipFile(source) as archive:
            assert b"attachedTemplate" not in archive.read("word/_rels/settings.xml.rels")
            assert b"attachedTemplate" not in archive.read("word/settings.xml")
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / source.with_suffix(".pdf").name).write_bytes(b"%PDF-test")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert):
        assert convert_docx(path, max_bytes=100_000).name == "sanitized-bases.pdf"
    assert path.read_bytes() == original


@pytest.mark.parametrize("content", [b"", b"%PDF-test", b"PK\x03\x04docx", b"{\\rtf1 word}"])
def test_doc_rejects_other_formats_before_libreoffice(tmp_path, content):
    path = tmp_path / "attachment.doc"
    path.write_bytes(content)
    with patch("buenapro_worker.local_document_conversion.subprocess.run") as run:
        with pytest.raises(ValueError, match="binary Word"):
            prepare_pdf(path, "doc", max_bytes=100_000)
        run.assert_not_called()


def test_doc_input_size_is_bounded_before_libreoffice(tmp_path):
    path = tmp_path / "attachment.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 100)
    with patch("buenapro_worker.local_document_conversion.subprocess.run") as run:
        with pytest.raises(ValueError, match="size policy"):
            convert_doc(path, max_bytes=50)
        run.assert_not_called()


def test_doc_uses_isolated_macro_disabled_conversion_and_validates_pdf(tmp_path):
    path = tmp_path / "attachment.doc"
    original = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"mock Word content"
    path.write_bytes(original)
    def run(command, **kwargs):
        if command[0] == "pdfinfo":
            assert kwargs == {"capture_output": True, "check": True, "timeout": 30}
            assert Path(command[1]).read_bytes() == b"%PDF-test"
            return
        assert command[-1] == str(path)
        assert "--headless" in command and "--norestore" in command
        assert command[command.index("--convert-to") + 1] == "pdf:writer_pdf_Export"
        outdir = Path(command[command.index("--outdir") + 1])
        assert f"-env:UserInstallation={outdir.parent.as_uri()}" in command
        assert kwargs["timeout"] == 120 and kwargs["check"] is True
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["SAL_USE_VCLPLUGIN"] == "svp"
        settings = (outdir.parent / "user/registrymodifications.xcu").read_text()
        assert 'MacroSecurityLevel' in settings and '<value>3</value>' in settings
        assert 'DisableMacrosExecution' in settings and 'DisableActiveContent' in settings
        assert 'oor:name="Link"' in settings and '<value>2</value>' in settings
        (outdir / "attachment.pdf").write_bytes(b"%PDF-test")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=run) as called, \
         patch("buenapro_worker.local_document_conversion.extract_archive") as archive:
        result = prepare_pdf(path, "doc", max_bytes=100_000)
        assert result.suffix == ".pdf" and result.parent != path.parent
        assert called.call_count == 2
        archive.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize("output", [None, b"%PDF-" + b"x" * 100])
def test_doc_missing_or_oversized_pdf_is_rejected(tmp_path, output):
    path = tmp_path / "attachment.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    def convert(command, **kwargs):
        if output is not None:
            outdir = Path(command[command.index("--outdir") + 1])
            (outdir / "attachment.pdf").write_bytes(output)
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert):
        with pytest.raises(ValueError, match="conversion failed or exceeds size policy"):
            convert_doc(path, max_bytes=50)


def test_doc_requires_local_libreoffice(tmp_path):
    path = tmp_path / "attachment.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="LibreOffice is unavailable"):
            convert_doc(path, max_bytes=100_000)


def test_doc_real_libreoffice_roundtrip_preserves_text_and_original(tmp_path):
    """Exercise a genuine Word 97 binary, not a renamed DOCX or mocked PDF."""
    soffice = shutil.which("soffice")
    if not soffice or not shutil.which("pdfinfo"):
        pytest.skip("Local Word smoke test requires existing LibreOffice and pdfinfo")
    from pypdf import PdfReader
    source = tmp_path / "synthetic.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>BuenaPro - prueba local de Word</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Documento sintetico sin datos personales.</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Servicio de soporte de software.</w:t></w:r></w:p>'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
            '</w:body></w:document>')
    fixture_profile = tmp_path / "fixture-profile"
    fixture_profile.mkdir()
    subprocess.run([soffice, "--headless", "--nologo", "--nodefault", "--norestore", "--nolockcheck",
        f"-env:UserInstallation={fixture_profile.as_uri()}", "--convert-to", "doc:MS Word 97",
        "--outdir", str(tmp_path), str(source)], check=True, timeout=120,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"})
    legacy = source.with_suffix(".doc")
    original = legacy.read_bytes()
    assert original.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    result = prepare_pdf(legacy, "doc", max_bytes=1_000_000)
    reader = PdfReader(result)
    assert len(reader.pages) == 1 and not reader.is_encrypted
    text = reader.pages[0].extract_text()
    assert "BuenaPro - prueba local de Word" in text
    assert "Documento sintetico sin datos personales." in text
    assert "Servicio de soporte de software." in text
    assert legacy.read_bytes() == original


def test_rar_member_is_streamed_with_size_limits(tmp_path):
    path = tmp_path / "original.rar"
    with patch("buenapro_worker.local_document_conversion.bounded_command", side_effect=[
        b"Bases integradas.pdf\n", b"-rw-r--r--  0 0 0 9 Jan 01 2026 Bases integradas.pdf\n", b"%PDF-test",
    ]) as command:
        result = extract_archive(path, "rar", max_bytes=100)
        assert result.read_bytes() == b"%PDF-test"
        assert command.call_args.kwargs["limit"] == 100
        assert "-xOf" in command.call_args.args[0]


def test_output_is_capped_during_execution():
    with pytest.raises(ValueError, match="size policy"):
        bounded_command(["/bin/echo", "1234567890"], limit=5)


def test_browser_keeps_original_fingerprint_when_docx_converted(tmp_path):
    browser = MagicMock()
    page = browser.new_context.return_value.new_page.return_value
    page.goto.return_value.status = 200
    link = page.locator.return_value
    link.count.return_value = 1
    link.get_attribute.return_value = "https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=11111111-1111-4111-8111-111111111111"
    download = page.expect_download.return_value.__enter__.return_value.value
    download.failure.return_value = None
    download.suggested_filename = "bases.docx"
    download.save_as.side_effect = lambda path: Path(path).write_bytes(b"original-docx")
    converted = tmp_path / "converted.pdf"
    converted.write_bytes(b"%PDF-converted")
    reader = OfficialBrowserDownload(browser, 123, tmp_path)
    with patch("buenapro_worker.local_prod4.prepare_pdf", return_value=converted):
        assert reader.download_document(UUID("11111111-1111-4111-8111-111111111111"), max_bytes=1000) == b"%PDF-converted"
    assert reader.original_sha256 == hashlib.sha256(b"original-docx").hexdigest()
    assert reader.sha256 == reader.original_sha256
    assert reader.original_path.exists()
    assert reader.path == converted
