from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from buenapro_worker.jobs.opportunity_identity import (
    sync_historical_prod6_opportunity,
    sync_prod6_opportunity,
)
from buenapro_worker.seace.schemas import SearchItem


def item(object_code: int = 1, state_code: int = 2) -> SearchItem:
    return SearchItem.model_validate({
        "idContrato": 301,
        "desContratacion": "CM-301-2026",
        "idObjetoContrato": object_code,
        "desObjetoContrato": "Equipos",
        "idEstadoContrato": state_code,
    })


def test_creates_source_identity_for_new_prod6_contract() -> None:
    repo = MagicMock()
    canonical_id = uuid4()
    repo.conn.execute.return_value.fetchone.side_effect = [None, {"id": canonical_id}]

    sync_prod6_opportunity(repo, item())

    calls = repo.conn.execute.call_args_list
    assert "pg_advisory_xact_lock" in calls[0].args[0]
    assert "INSERT INTO opportunities" in calls[2].args[0]
    assert calls[2].args[1] == ("good", "open")
    assert "INSERT INTO opportunity_sources" in calls[3].args[0]
    assert calls[3].args[1][0] == canonical_id
    assert calls[3].args[1][1] == "301"
    assert "UPDATE seace_contracts" in calls[4].args[0]


def test_updates_existing_identity_without_creating_duplicate() -> None:
    repo = MagicMock()
    canonical_id = uuid4()
    repo.conn.execute.return_value.fetchone.return_value = {"opportunity_id": canonical_id}

    sync_prod6_opportunity(repo, item(object_code=2, state_code=3))

    calls = repo.conn.execute.call_args_list
    assert len(calls) == 5
    assert "UPDATE opportunities" in calls[2].args[0]
    assert calls[2].args[1] == ("service", "evaluation", canonical_id)
    assert "UPDATE opportunity_sources" in calls[3].args[0]
    assert "INSERT INTO opportunities" not in " ".join(call.args[0] for call in calls)


def test_historical_result_reuses_live_identity() -> None:
    repo = MagicMock()
    canonical_id = uuid4()
    repo.conn.execute.return_value.fetchone.return_value = {"opportunity_id": canonical_id}

    sync_historical_prod6_opportunity(
        repo, id_contrato=301, codigo="CM-301-2026",
        objeto_codigo=1, estado_resultado="ADJUDICADO",
    )

    calls = repo.conn.execute.call_args_list
    assert len(calls) == 5
    assert calls[2].args[1] == ("good", "awarded", canonical_id)
    assert "INSERT INTO opportunities" not in " ".join(call.args[0] for call in calls)
    assert calls[4].args[1] == (canonical_id, 301, canonical_id)


def test_history_only_result_gets_new_identity() -> None:
    repo = MagicMock()
    canonical_id = uuid4()
    repo.conn.execute.return_value.fetchone.side_effect = [None, {"id": canonical_id}]

    sync_historical_prod6_opportunity(
        repo, id_contrato=302, codigo="CM-302-2026",
        objeto_codigo=2, estado_resultado="DESIERTO",
    )

    calls = repo.conn.execute.call_args_list
    assert calls[2].args[1] == ("service", "closed")
    assert calls[3].args[1][0] == canonical_id
    assert calls[4].args[1] == (canonical_id, 302, canonical_id)
