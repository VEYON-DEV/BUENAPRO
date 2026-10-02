import hashlib
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from buenapro_worker.local_document_conversion import (
    bounded_command, convert_docx, extract_archive, prepare_pdf, safe_member, select_bases_member,
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
        path.with_suffix(".pdf").write_bytes(b"%PDF-test")
    with patch("buenapro_worker.local_document_conversion.shutil.which", return_value="/mock/soffice"), \
         patch("buenapro_worker.local_document_conversion.subprocess.run", side_effect=convert):
        assert convert_docx(path, max_bytes=100_000).read_bytes() == b"%PDF-test"
    assert "MacroSecurityLevel" in (tmp_path / "libreoffice-profile/user/registrymodifications.xcu").read_text()


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
