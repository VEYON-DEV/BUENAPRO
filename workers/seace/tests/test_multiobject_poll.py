from __future__ import annotations

from unittest.mock import MagicMock, patch

from buenapro_worker.jobs.poll_search import poll_search
from buenapro_worker.jobs.scheduler import enqueue_poll_search_jobs
from buenapro_worker.seace.schemas import SearchResponse
from buenapro_worker.settings import Settings


def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused:unused@localhost/unused",
        gemini_api_key="unused",
        seace_allowed_codigo_objeto="1,2",
        seace_allowed_segments="43,81",
    )


def response(objeto: int, contract_id: int) -> SearchResponse:
    return SearchResponse.model_validate({
        "data": [{
            "idContrato": contract_id,
            "desContratacion": f"CM-{contract_id}-2026",
            "idObjetoContrato": objeto,
            "desObjetoContrato": "Tecnología",
            "idEstadoContrato": 2,
        }],
        "pageable": {"pageNumber": 1, "pageSize": 100, "totalElements": 1},
    })


def test_scheduler_splits_jobs_by_object_and_segment() -> None:
    repo = MagicMock()
    repo.enqueue.side_effect = [1, 2, 3, 4]

    jobs = enqueue_poll_search_jobs(settings(), repo, year=2026)

    assert set(jobs) == {
        "poll_search:2026:1:43", "poll_search:2026:1:81",
        "poll_search:2026:2:43", "poll_search:2026:2:81",
    }
    assert repo.enqueue.call_args_list[0].args == (
        "poll_search", {"anio": 2026, "objects": [1], "segments": [43]},
    )


@patch("buenapro_worker.jobs.poll_search.upsert_search_item", return_value=True)
@patch("buenapro_worker.jobs.poll_search.SeaceClient")
def test_poll_covers_goods_and_services(client_class: MagicMock, upsert: MagicMock) -> None:
    client = client_class.return_value.__enter__.return_value
    client.search_contracts.side_effect = [response(1, 101), response(2, 202)]
    repo = MagicMock()
    repo.enqueue.side_effect = [1, 2]

    stats = poll_search(settings(), repo, anio=2026, segments=[43])

    assert stats["seen"] == 2
    assert stats["changed"] == 2
    assert stats["enqueued"] == 2
    assert [call.kwargs["objeto"] for call in client.search_contracts.call_args_list] == [1, 2]
    assert upsert.call_count == 2


@patch("buenapro_worker.jobs.poll_search.SeaceClient")
def test_poll_rejects_unconfigured_object(client_class: MagicMock) -> None:
    try:
        poll_search(settings(), MagicMock(), anio=2026, objects=[3])
    except ValueError as exc:
        assert "SEACE_ALLOWED_CODIGO_OBJETO" in str(exc)
    else:
        raise AssertionError("Expected unconfigured object to be rejected")
    client_class.assert_not_called()
