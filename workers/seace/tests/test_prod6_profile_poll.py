from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs.poll_prod6_profile import matches_profile_scope, poll_prod6_profile
from buenapro_worker.jobs.scheduler import enqueue_poll_search_jobs
from buenapro_worker.seace.schemas import SearchResponse
from buenapro_worker.settings import Settings


LINES = [{"cubso_segmentos": ["39"], "keyword_phrases": ["tableros eléctricos"],
          "keyword_terms": ["ccm", "variadores"], "keywords": []}]


def settings(**kwargs):
    return Settings(database_url="postgresql://unused", gemini_api_key="unused",
                    seace_allowed_codigo_objeto="1,2", seace_allowed_segments="43,81", **kwargs)


def response(items, total=1):
    return SearchResponse.model_validate({"data": items,
        "pageable": {"pageNumber": 1, "pageSize": 100, "totalElements": total}})


def item(id, text, obj=1, state=2, year=2026):
    return {"idContrato": id, "desContratacion": f"CM-{id}-{year}",
            "idObjetoContrato": obj, "desObjetoContrato": text,
            "idEstadoContrato": state, "fecPublica": f"01/10/{year} 10:00:00"}


def test_scope_specific_phrase_or_two_terms_and_boundaries():
    assert matches_profile_scope("Instalación de TABLEROS ELÉCTRICOS", LINES, 39)
    assert matches_profile_scope("CCM con variadores", LINES, 39)
    assert not matches_profile_scope("Variadores solamente", LINES, 39)
    assert not matches_profile_scope("Tableros eléctricos", LINES, 43)
    assert not matches_profile_scope("xxccm xxvariadores", LINES, 39)


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_poll_only_current_relevant_vigente_goods_services(client_cls, upsert):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"id": "profile"}
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "tableros eléctricos"), item(2, "papelería"),
                  item(3, "tableros eléctricos", state=3), item(4, "tableros eléctricos", year=2025)]),
        response([item(5, "Mantenimiento de tableros eléctricos", obj=2)])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile")
    assert stats["candidates"] == stats["changed"] == stats["enqueued"] == 2
    assert stats["skipped"] == 3
    assert upsert.call_count == 2
    calls = client_cls.return_value.__enter__.return_value.search_contracts.call_args_list
    assert [(call.kwargs["objeto"], call.kwargs["segmento"]) for call in calls] == [(1,39),(2,39)]


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_unchanged_or_filtered_page_does_not_stop_pagination(client_cls, upsert):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [{"id": "profile"}, None]
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "papelería")], total=201),
        response([item(2, "tableros eléctricos")], total=201),
        response([item(3, "tableros eléctricos")], total=201), response([])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile", max_pages_per_segment=2)
    assert stats["candidates"] == 1
    assert stats["capped_segments"] == 1
    assert stats["enqueued"] == 1


def test_scheduler_profile_discovery_requires_opt_in():
    repo = MagicMock()
    assert "poll_prod6_profiles" not in enqueue_poll_search_jobs(settings(), repo, year=2026)
    assert "poll_prod6_profiles" in enqueue_poll_search_jobs(settings(prod6_profile_poll_enabled=True), repo, year=2026)


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_completed_extraction_not_requeued_and_expired_not_saved(client_cls, upsert):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [{"id": "profile"}, {"exists": 1}]
    repo.conn.execute.return_value.fetchall.return_value = LINES
    expired = item(2, "tableros eléctricos") | {"fecFinCotizacion": "01/01/2020 10:00:00"}
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "tableros eléctricos"), expired]), response([])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile")
    assert stats["candidates"] == 1
    assert stats["enqueued"] == 0
    assert stats["skipped"] == 1
    repo.enqueue.assert_not_called()
    upsert.assert_called_once()


def test_rejects_unbounded_or_inactive_profile():
    with pytest.raises(ValueError):
        poll_prod6_profile(settings(), MagicMock(), profile_id="profile", max_candidates=0)
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = None
    with pytest.raises(ValueError, match="Active company"):
        poll_prod6_profile(settings(), repo, profile_id="missing")
