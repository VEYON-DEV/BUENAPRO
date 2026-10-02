from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from buenapro_worker.jobs.prod4_documents import (
    _original_document_sha256,
    extract_prod4_document_job,
    mark_unsupported_current_document,
    select_official_requirements_pdf,
)
from buenapro_worker.settings import Settings
from buenapro_worker.jobs.poll_prod4 import _upsert_detail


def doc(code, name, extension, date):
    return {"codigo_alfresco": UUID(code), "name": name,
            "document_type": name, "extension": extension,
            "published_at": datetime.fromisoformat(date).replace(tzinfo=timezone.utc)}


RAR_BASES = doc("d4015dfe-056e-4234-a6d6-95896087501c", "Bases Administrativas", "rar", "2026-09-23T18:50")
BN_PDF = doc("59d111d4-fc33-4085-8c34-4fcb5e6697ec", "Bases Administrativas", "pdf", "2026-08-28T17:15")
BN_INTEGRATED = doc("31316bad-4a78-47ba-af3c-8e9b799277c7", "Bases Integradas", "docx", "2026-09-25T17:19")


@pytest.mark.parametrize("pid,documents,code", [
    (1252661, [RAR_BASES], RAR_BASES["codigo_alfresco"]),
    (1245560, [BN_PDF, BN_INTEGRATED], BN_INTEGRATED["codigo_alfresco"]),
])
def test_verified_live_formats_do_not_fall_back_to_old_pdf(pid, documents, code):
    assert select_official_requirements_pdf(documents) is None
    repo = MagicMock()
    assert mark_unsupported_current_document(repo, pid, documents)
    assert repo.conn.execute.call_args.args[1] == ("unsupported_current_format", code, pid)
    assert any("DELETE FROM opportunity_matches" in call.args[0] for call in repo.conn.execute.call_args_list)


def test_old_queued_pdf_is_not_downloaded_or_sent_to_gemini():
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {
        "opportunity_id": UUID("11111111-1111-4111-8111-111111111111"),
        "object_type": "service", "technology_relevant": True, "missing_since": None,
    }
    repo.conn.execute.return_value.fetchall.return_value = [BN_PDF, BN_INTEGRATED]
    client, extractor = MagicMock(), MagicMock()
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    result = extract_prod4_document_job(settings, repo, id_procedimiento=1245560,
                                        codigo_alfresco=str(BN_PDF["codigo_alfresco"]),
                                        client=client, extractor=extractor)
    assert result == {"skipped": "unsupported_current_format"}
    client.download_document.assert_not_called()
    extractor.extract.assert_not_called()


def test_pdf_twin_of_same_authoritative_version_is_supported():
    twin = {**BN_INTEGRATED, "extension": "pdf", "codigo_alfresco": BN_PDF["codigo_alfresco"]}
    assert select_official_requirements_pdf([BN_PDF, BN_INTEGRATED, twin]) == twin


def test_newer_same_priority_non_pdf_also_supersedes_old_pdf():
    replacement = {**BN_PDF, "extension": "docx", "published_at": BN_INTEGRATED["published_at"]}
    assert select_official_requirements_pdf([BN_PDF, replacement]) is None


def test_unrelated_docx_annex_does_not_block_bases_pdf():
    annex = {**BN_INTEGRATED, "name": "Anexo declaración jurada", "document_type": "Anexo"}
    assert select_official_requirements_pdf([BN_PDF, annex]) == BN_PDF
    assert not mark_unsupported_current_document(MagicMock(), 1245560, [BN_PDF, annex])


def test_explicit_local_converter_selects_integrated_docx_not_old_pdf():
    assert select_official_requirements_pdf([BN_PDF, BN_INTEGRATED], supported_extensions={"pdf", "docx"}) == BN_INTEGRATED
    assert select_official_requirements_pdf([RAR_BASES], supported_extensions={"pdf", "rar"}) == RAR_BASES
    assert not mark_unsupported_current_document(MagicMock(), 1245560, [BN_PDF, BN_INTEGRATED], supported_extensions={"pdf", "docx"})
    zipped = {**RAR_BASES, "extension": "zip"}
    assert select_official_requirements_pdf([zipped], supported_extensions={"pdf", "docx", "rar", "zip"}) == zipped


@pytest.mark.parametrize("extension", ["docx", "rar", "zip"])
def test_pdf_only_server_preserves_valid_local_extraction_of_same_source(extension):
    official = {**BN_INTEGRATED, "extension": extension}
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"id": 25, "sha256_original": "a" * 64}
    assert not mark_unsupported_current_document(repo, 1245560, [official])
    statements = [call.args[0] for call in repo.conn.execute.call_args_list]
    assert len(statements) == 1 and "SELECT id, sha256_original" in statements[0]
    assert "codigo_alfresco = %s" in statements[0] and "quality <> 'failed'" in statements[0]


@pytest.mark.parametrize("sha", [None, "", "invalid", "a" * 63])
def test_unverified_original_hash_is_not_reused(sha):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"id": 25, "sha256_original": sha}
    assert mark_unsupported_current_document(repo, 1245560, [BN_INTEGRATED])
    assert any("DELETE FROM opportunity_matches" in call.args[0] for call in repo.conn.execute.call_args_list)


@pytest.mark.parametrize("extension", ["docx", "rar", "zip"])
def test_server_detail_refresh_does_not_invalidate_local_conversion(extension):
    official = {**BN_INTEGRATED, "extension": extension}
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"opportunity_id": UUID("11111111-1111-4111-8111-111111111111"),
         "analyzed_document_code": official["codigo_alfresco"]},
        {"id": 25, "sha256_original": "b" * 64},
    ]
    repo.conn.execute.return_value.fetchall.return_value = [official]
    _upsert_detail(repo, 1245560, {"listaDocumentos": [{
        "codigoAlfresco": str(official["codigo_alfresco"]), "nombreArchivo": "Bases Integradas",
        "tipoDocumento": "Bases Integradas", "extension": extension,
    }]})
    statements = [call.args[0] for call in repo.conn.execute.call_args_list]
    assert not any("DELETE FROM opportunity_matches" in sql for sql in statements)
    assert not any("UPDATE prod4_document_extractions SET is_current = false" in sql for sql in statements)
    assert not any("document_analysis_status = 'skipped'" in sql for sql in statements)


def test_converter_keeps_original_fingerprint_not_converted_pdf_hash():
    from types import SimpleNamespace
    import hashlib
    original = b"PK-real-official-docx"
    converted = b"%PDF-1.7 converted representation"
    original_hash = hashlib.sha256(original).hexdigest()
    assert _original_document_sha256(SimpleNamespace(original_sha256=original_hash), converted, "docx") == original_hash
    assert _original_document_sha256(SimpleNamespace(), converted, "pdf") == hashlib.sha256(converted).hexdigest()
    with pytest.raises(ValueError, match="original SHA"):
        _original_document_sha256(SimpleNamespace(), converted, "docx")


def test_extraction_persists_official_docx_fingerprint():
    from types import SimpleNamespace
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"opportunity_id": UUID("11111111-1111-4111-8111-111111111111"),
         "object_type": "service", "technology_relevant": True, "missing_since": None},
        {"eligible": True}, None, {"id": 75},
    ]
    repo.conn.execute.return_value.fetchall.return_value = [BN_PDF, BN_INTEGRATED]
    client = MagicMock()
    client.supported_extensions = {"pdf", "docx"}
    client.original_sha256 = "a" * 64
    client.download_document.return_value = b"%PDF-1.7 converted fixture"
    extractor = MagicMock()
    extractor.extract.return_value = SimpleNamespace(
        raw_json={"requirements": []}, model="test", prompt_version="test", schema_version="test",
        input_tokens=1, output_tokens=1, cost_usd=0, requires_human_review=False,
    )
    settings = Settings(database_url="postgresql://unused:unused@localhost/unused", gemini_api_key="unused")
    extract_prod4_document_job(settings, repo, id_procedimiento=1245560,
                               codigo_alfresco=str(BN_INTEGRATED["codigo_alfresco"]), client=client, extractor=extractor)
    insert = next(call for call in repo.conn.execute.call_args_list if "INSERT INTO prod4_document_extractions" in call.args[0])
    assert insert.args[1][3] == "a" * 64
    extractor.extract.assert_called_once_with(client.download_document.return_value, mime="application/pdf", doc_class="bases_service")
