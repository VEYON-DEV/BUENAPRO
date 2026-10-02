from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from buenapro_worker.jobs.prod4_documents import (
    extract_prod4_document_job,
    mark_unsupported_current_document,
    select_official_requirements_pdf,
)
from buenapro_worker.settings import Settings


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
