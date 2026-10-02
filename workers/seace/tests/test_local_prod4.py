from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from buenapro_worker.local_prod4 import BudgetedExtractor, LocalFileExtractor, OfficialBrowserDownload, tunnel_url
from buenapro_worker.prod4.client import DocumentTooLargeError
from buenapro_worker.settings import Settings


def test_tunnel_rewrites_only_host_without_losing_encoded_credentials():
    assert tunnel_url("postgresql://worker:p%40ss@postgres:5432/buenapro?sslmode=disable", 43210) == "postgresql://worker:p%40ss@127.0.0.1:43210/buenapro?sslmode=disable"


@pytest.mark.parametrize("url", ["https://example.com", "postgresql://localhost/db"])
def test_tunnel_rejects_invalid_connection_url(url):
    with pytest.raises(ValueError):
        tunnel_url(url, 43210)


def test_budget_blocks_extraction_before_paid_generation():
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    with patch("buenapro_worker.local_prod4.LocalFileExtractor") as klass:
        inner = klass.return_value
        inner.count_tokens.return_value = 50_000
        runner = BudgetedExtractor(settings, 0.01)
        with pytest.raises(ValueError, match="budget"):
            runner.extract(b"%PDF-example", mime="application/pdf", doc_class="bases_service")
        inner.extract.assert_not_called()
        inner.close.assert_called_once()


def test_budget_uses_existing_gemini_pipeline_not_separate_summary():
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    with patch("buenapro_worker.local_prod4.LocalFileExtractor") as klass:
        inner = klass.return_value
        inner.count_tokens.return_value = 20_000
        runner = BudgetedExtractor(settings, 1)
        runner.extract(b"%PDF-example", mime="application/pdf", doc_class="bases_good")
        inner.extract.assert_called_once_with(b"%PDF-example", mime="application/pdf", doc_class="bases_good")


def test_browser_download_retains_oversized_pdf_without_gemini(tmp_path):
    browser = MagicMock()
    context = browser.new_context.return_value
    page = context.new_page.return_value
    page.goto.return_value.status = 200
    link = page.locator.return_value
    link.count.return_value = 1
    link.get_attribute.return_value = "https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=11111111-1111-4111-8111-111111111111"
    download = page.expect_download.return_value.__enter__.return_value.value
    download.failure.return_value = None
    download.save_as.side_effect = lambda path: __import__("pathlib").Path(path).write_bytes(b"%PDF-" + b"x" * 100)
    reader = OfficialBrowserDownload(browser, 123, tmp_path)
    with pytest.raises(DocumentTooLargeError):
        reader.download_document(UUID("11111111-1111-4111-8111-111111111111"), max_bytes=20)
    assert reader.path.exists()
    context.close.assert_called_once()


def test_large_original_uses_one_file_upload_for_count_and_retries():
    reader = object.__new__(LocalFileExtractor)
    reader.client = MagicMock()
    reader.uploaded = None
    reader.uploaded_sha = None
    uploaded = reader.client.files.upload.return_value
    uploaded.state.name = "ACTIVE"
    uploaded.uri = "https://generativelanguage.googleapis.com/v1beta/files/test"
    uploaded.name = "files/test"
    content = b"%PDF-" + b"x" * (18 * 1024 * 1024)
    first = reader._contents(content, mime="application/pdf")
    second = reader._contents(content, mime="application/pdf")
    assert first == second
    assert first[0].file_data.file_uri == uploaded.uri
    reader.client.files.upload.assert_called_once()
    assert reader.client.files.upload.call_args.kwargs["file"].getvalue() == content
    reader.close()
    reader.client.files.delete.assert_called_once_with(name="files/test")


def test_failed_file_upload_is_not_sent_to_model():
    reader = object.__new__(LocalFileExtractor)
    reader.client = MagicMock()
    reader.uploaded = None
    reader.uploaded_sha = None
    reader.client.files.upload.return_value.state.name = "FAILED"
    with pytest.raises(RuntimeError, match="not usable"):
        reader._contents(b"%PDF-" + b"x" * (18 * 1024 * 1024), mime="application/pdf")
    reader.client.models.generate_content.assert_not_called()
    reader.close()
    reader.client.files.delete.assert_called_once()
