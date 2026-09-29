from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from buenapro_worker.jobs.poll_prod4 import _upsert_detail
from buenapro_worker.jobs.prod4_documents import (
    _has_substantive_requirements,
    enqueue_current_prod4_document_sweep,
    enqueue_prod4_document_if_eligible,
    extract_prod4_document_job,
    select_official_requirements_pdf,
)
from buenapro_worker.jobs.prod4_match import (
    _guard_prod4_economic_claim,
    analyze_prod4_match_job,
    route_prod4_profiles_job,
)
from buenapro_worker.prod4.client import DocumentTooLargeError, Prod4Client
from buenapro_worker.settings import Settings


CODE_A = UUID("11111111-1111-4111-8111-111111111111")
CODE_B = UUID("22222222-2222-4222-8222-222222222222")


def settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql://unused:unused@localhost/unused",
        gemini_api_key="unused",
        **overrides,
    )


def document(code: UUID, name: str, published: datetime | None = None) -> dict:
    return {
        "codigo_alfresco": code,
        "name": name,
        "document_type": name,
        "extension": "pdf",
        "published_at": published,
    }


def test_selects_latest_integrated_bases_not_announcement_or_annex() -> None:
    rows = [
        document(CODE_A, "Bases Administrativas"),
        document(CODE_B, "Bases Integradas"),
        document(uuid4(), "Acta de otorgamiento", datetime.now(timezone.utc)),
        {**document(uuid4(), "Bases Integradas"), "extension": "docx"},
    ]
    assert select_official_requirements_pdf(rows)["codigo_alfresco"] == CODE_B


def test_no_final_verdict_from_generic_or_administrative_requirements() -> None:
    assert not _has_substantive_requirements([])
    assert not _has_substantive_requirements([
        {"facet": "business_line", "required": True},
        {"facet": "rnp", "required": True},
        {"facet": "proposal_document", "required": True},
    ])
    assert _has_substantive_requirements([{"facet": "specific_experience", "required": True}])


def test_bounded_sweep_progresses_past_already_processed_rows() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"total": 0}
    repo.conn.execute.return_value.fetchall.return_value = [
        {"id_procedimiento": index} for index in range(1, 73)
    ]
    with patch(
        "buenapro_worker.jobs.prod4_documents.enqueue_prod4_document_if_eligible",
        side_effect=[False] * 12 + [True] * 10,
    ) as enqueue:
        result = enqueue_current_prod4_document_sweep(settings(), repo, limit=10)
    assert result == {"checked": 22, "enqueued": 10, "already_pending": 0}
    assert enqueue.call_count == 22


def test_sweep_obeys_pending_and_daily_budget() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"total": 8},  # outstanding
        {"total": 9},  # created today
    ]
    repo.conn.execute.return_value.fetchall.return_value = [{"id_procedimiento": 10}]
    with patch(
        "buenapro_worker.jobs.prod4_documents.enqueue_prod4_document_if_eligible",
        return_value=True,
    ) as enqueue:
        result = enqueue_current_prod4_document_sweep(settings(), repo, limit=10)
    assert result == {"checked": 1, "enqueued": 1, "already_pending": 8}
    enqueue.assert_called_once()


def test_daily_cap_stops_before_touching_opportunity() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"total": 10}
    assert not enqueue_prod4_document_if_eligible(settings(), repo, 123)
    assert repo.conn.execute.call_count == 2  # advisory lock + count only
    repo.enqueue.assert_not_called()


def test_detail_refresh_invalidates_final_match_when_bases_change() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {
        "opportunity_id": uuid4(), "analyzed_document_code": CODE_A,
    }
    repo.conn.execute.return_value.fetchall.return_value = [document(CODE_B, "Bases Integradas")]
    _upsert_detail(repo, 123, {
        "nidExpediente": 123,
        "listaDocumentos": [{
            "codigoAlfresco": str(CODE_B), "nombreArchivo": "Bases Integradas",
            "tipoDocumento": "Bases Integradas", "extension": "pdf",
        }],
    })
    statements = [call.args[0] for call in repo.conn.execute.call_args_list]
    assert any("DELETE FROM opportunity_matches" in sql for sql in statements)
    assert any("UPDATE prod4_requirement_facets SET is_current = false" in sql for sql in statements)


def test_large_official_pdf_skips_gemini_and_records_reason() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"opportunity_id": uuid4(), "object_type": "good", "technology_relevant": True, "missing_since": None},
        {"eligible": True},
        None,
    ]
    repo.conn.execute.return_value.fetchall.return_value = [document(CODE_A, "Bases Integradas")]
    client = MagicMock()
    client.download_document.side_effect = DocumentTooLargeError("too big")
    extractor = MagicMock()
    result = extract_prod4_document_job(
        settings(), repo, id_procedimiento=123, codigo_alfresco=str(CODE_A),
        client=client, extractor=extractor,
    )
    assert result == {"skipped": "pdf_exceeds_analysis_limit"}
    extractor.extract.assert_not_called()
    assert any(
        "document_analysis_status = 'skipped'" in call.args[0]
        for call in repo.conn.execute.call_args_list
    )


def test_generic_bases_extraction_is_saved_without_final_scoring() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"opportunity_id": uuid4(), "object_type": "service", "technology_relevant": True, "missing_since": None},
        {"eligible": True},
        None,
        {"id": 75},
    ]
    repo.conn.execute.return_value.fetchall.return_value = [document(CODE_A, "Bases Integradas")]
    client = MagicMock()
    client.download_document.return_value = b"%PDF-1.7\nminimal test fixture"
    extractor = MagicMock()
    extractor.extract.return_value = SimpleNamespace(
        raw_json={
            "requirements": {"provider": [
                {"tipo": "business_line", "descripcion": "Objeto general", "obligatorio": True}
            ]},
            "summary": {"descripcion_corta": "Objeto de prueba"},
        },
        model="gemini-test", prompt_version="bases_extraction_v1",
        schema_version="bases_extraction_schema_v1", input_tokens=100,
        output_tokens=20, cost_usd=0.001, requires_human_review=False,
    )
    result = extract_prod4_document_job(
        settings(), repo, id_procedimiento=123, codigo_alfresco=str(CODE_A),
        client=client, extractor=extractor,
    )
    assert result == {"extraction_id": 75, "facets": 1, "match_eligible": False}
    repo.enqueue.assert_not_called()


def test_substantive_bases_extraction_routes_profile_evaluation() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"opportunity_id": uuid4(), "object_type": "service", "technology_relevant": True, "missing_since": None},
        {"eligible": True},
        None,
        {"id": 76},
    ]
    repo.conn.execute.return_value.fetchall.return_value = [document(CODE_A, "Bases Integradas")]
    client = MagicMock()
    client.download_document.return_value = b"%PDF-1.7\nminimal test fixture"
    extractor = MagicMock()
    extractor.extract.return_value = SimpleNamespace(
        raw_json={
            "requirements": {"provider": [
                {"tipo": "specific_experience", "descripcion": "Dos contratos de software", "obligatorio": True}
            ]},
            "summary": {"descripcion_corta": "Objeto de prueba"},
        },
        model="gemini-test", prompt_version="bases_extraction_v1",
        schema_version="bases_extraction_schema_v1", input_tokens=100,
        output_tokens=20, cost_usd=0.001, requires_human_review=False,
    )
    result = extract_prod4_document_job(
        settings(), repo, id_procedimiento=123, codigo_alfresco=str(CODE_A),
        client=client, extractor=extractor,
    )
    assert result == {"extraction_id": 76, "facets": 1, "match_eligible": True}
    assert repo.enqueue.call_args.args[0] == "route_prod4_profiles"


def test_final_match_requires_current_validated_document() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"profile_hash": "hash", "is_active": True}, None,
    ]
    analyzer = MagicMock()
    result = analyze_prod4_match_job(
        settings(), repo, id_procedimiento=123, extraction_id=10,
        profile_id=str(uuid4()), analyzer=analyzer,
    )
    assert result == {"skipped": "not_current_or_unvalidated"}
    analyzer.analyze.assert_not_called()


def test_default_per_profile_daily_cap_prevents_final_llm_fanout() -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = [{
        "profile_id": uuid4(), "opportunity_id": uuid4(), "enabled": True,
        "min_fit_level": 2, "max_daily_evaluations": 0,
        "min_hours_before_close": 0, "business_line_id": uuid4(),
        "fit_points": 20, "fit_score": 70, "fit_level": 2,
        "keyword_hits": [], "proposals_closes_at": None,
    }]
    repo.conn.execute.return_value.fetchone.return_value = {"total": 10}
    stats = route_prod4_profiles_job(settings(), repo, id_procedimiento=123, extraction_id=42)
    assert stats == {"profiles": 1, "eligible": 1, "enqueued": 0, "limited": 1}
    repo.enqueue.assert_not_called()


def test_prod4_economic_shortfall_never_becomes_unverified_cumple() -> None:
    requirements = [{"categoria": "experiencia_economica", "estado": "cumple"}]
    _guard_prod4_economic_claim(requirements, exigido=100_000, capacidad=15_000)
    assert requirements[0]["estado"] == "requiere_revision"
    assert "Verificar" in requirements[0]["accion"]


def test_browser_headers_and_pdf_size_guard() -> None:
    upstream = MagicMock()
    upstream.headers = {"Content-Length": "100"}
    upstream.raise_for_status.return_value = None
    stream_context = MagicMock()
    stream_context.__enter__.return_value = upstream
    client = Prod4Client(settings())
    try:
        client._client.stream = MagicMock(return_value=stream_context)
        with pytest.raises(DocumentTooLargeError):
            client.download_document(CODE_A, max_bytes=20)
        kwargs = client._client.stream.call_args.kwargs
        assert kwargs["headers"]["Referer"].startswith("https://prod4.seace.gob.pe/")
        assert kwargs["params"]["fileCode"] == str(CODE_A)
    finally:
        client._client.close()
