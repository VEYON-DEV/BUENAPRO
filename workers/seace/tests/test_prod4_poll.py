from __future__ import annotations

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import pytest

from buenapro_worker.jobs.poll_prod4 import (
    _date,
    _money,
    _upsert_detail,
    _upsert_listing,
    poll_prod4,
    select_technology_processes,
)
from buenapro_worker.settings import Settings


def settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql://unused:unused@localhost/unused",
        gemini_api_key="unused",
        prod4_technology_segments="43,81",
        **overrides,
    )


def row(procedure_id: int, object_code: int, cubso: str, *, item: int = 1) -> dict:
    return {
        "idProcedimiento": procedure_id,
        "codObjeto": str(object_code),
        "detObjeto": "Bien" if object_code == 62 else "Servicio",
        "nroItem": str(item),
        "codCubso": cubso,
        "nomenclatura": f"LP-ABR-{procedure_id}-2026",
        "sintesisProceso": "Servicio de software" if object_code == 65 else "Equipos informáticos",
    }


def test_selects_technology_with_authoritative_object_and_dedupes_items() -> None:
    goods = [row(101, 62, "4322264000220190"), row(101, 62, "4322264000220189", item=2)]
    services = [row(202, 65, "8111210100232427"), row(303, 65, "8110152100329748")]
    # Segment codObjeto is actually an item-like identifier, not 62 or 65.
    segment_43 = [{**goods[0], "codObjeto": "21380063"}, {**goods[1], "codObjeto": "21380064"}]
    segment_81 = [
        {**services[0], "codObjeto": "21374936"},
        {**services[1], "codObjeto": "21374937"},
        {**row(404, 65, "8111250100375850"), "detObjeto": "Consultoría de Obra"},
    ]

    selected = select_technology_processes(goods, services, {43: segment_43, 81: segment_81}, ["811121"], ["software"])

    assert set(selected) == {101, 202}
    assert selected[101][0] == "good"
    assert len(selected[101][1]) == 2
    assert selected[101][2] == ["cubso:43"]
    assert selected[202][0] == "service"
    assert selected[202][2] == ["cubso:811121", "text:technology"]


def test_service_cubso_without_technology_language_is_excluded() -> None:
    medical = {**row(505, 65, "8111240100232694"), "sintesisProceso": "Alquiler de equipos biomédicos neonatales"}
    documents = {**row(606, 65, "8111200200345480"), "sintesisProceso": "Consolidación y verificación documentaria"}
    selected = select_technology_processes(
        [], [medical, documents], {81: [medical, documents]}, ["811124", "811120"], ["software", "informatic", "tecnolog"],
    )
    assert selected == {}


def test_service_technology_terms_preserve_data_links_and_named_database_products() -> None:
    link = {**row(707, 65, "8111210100232427"), "sintesisProceso": "Transmisión de datos entre sedes"}
    database = {**row(808, 65, "8111180500232427"), "sintesisProceso": "Soporte y mantenimiento de productos Informix"}
    selected = select_technology_processes(
        [], [link, database], {81: [link, database]}, ["811121", "811118"], settings().prod4_terms,
    )
    assert set(selected) == {707, 808}


def test_rejects_ambiguous_native_object_classification() -> None:
    with pytest.raises(ValueError, match="both goods and services"):
        select_technology_processes(
            [row(101, 62, "43")], [row(101, 65, "811121")],
            {43: [row(101, 62, "43")]}, ["811121"], ["software"],
        )


def test_dates_and_reserved_amount_keep_distinct_deadlines() -> None:
    registration_close = _date("08/10/2026 23:59:00")
    proposal_close = _date("09/10/2026", hour="23:59")

    assert registration_close is not None and proposal_close is not None
    assert registration_close < proposal_close
    assert registration_close.utcoffset().total_seconds() == -5 * 3600
    assert _money("---") is None
    assert _money("10,354.50") == 10354.50


def test_failed_slice_does_not_mark_any_process_missing() -> None:
    source = MagicMock()
    source.by_object.side_effect = [[], RuntimeError("source unavailable")]
    repo = MagicMock()

    with pytest.raises(RuntimeError, match="source unavailable"):
        poll_prod4(settings(), repo, source)

    repo.conn.execute.assert_not_called()


def test_disabled_poll_does_not_call_source_or_database() -> None:
    source = MagicMock()
    repo = MagicMock()
    assert poll_prod4(settings(prod4_enabled=False), repo, source) == {
        "selected": 0, "details": 0, "missing": 0,
    }
    source.by_object.assert_not_called()
    repo.conn.execute.assert_not_called()


@pytest.mark.parametrize("hour", ["16:30", None])
def test_detail_syncs_registration_close_with_official_precision(hour) -> None:
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = None
    _upsert_detail(repo, 101, {"listaCronograma": [
        {"nombreEtapa": "REGISTRO DE PARTICIPANTES", "fechaFin": "02/10/2026", "horaFin": hour},
    ]})
    calls = [call for call in repo.conn.execute.call_args_list
             if "UPDATE prod4_processes SET" in call.args[0]]
    assert len(calls) == 1
    sql, params = calls[0].args
    assert "registration_closes_at = %s" in sql
    assert params[9] == (datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc) if hour else None)


@patch("buenapro_worker.jobs.poll_prod4._ensure_identity", return_value="11111111-1111-4111-8111-111111111111")
@patch("buenapro_worker.jobs.poll_prod4._existing")
def test_listing_preserves_existing_official_registration_and_does_not_invent_clock(existing, _identity) -> None:
    repo = MagicMock()
    current_row = {**row(101, 62, "43"), "fecFinParticipantes": "02/10/2026",
                   "fechaPresentacionPropuestas": "03/10/2026"}
    scan = datetime(2026, 10, 1, tzinfo=timezone.utc)
    existing.return_value = {"raw_listing": [current_row], "raw_detail": {"listaCronograma": []},
                             "detail_fetched_at": scan}
    _upsert_listing(repo, 101, "good", [current_row], ["cubso:43"], scan)
    sql, params = repo.conn.execute.call_args.args
    assert "registration_closes_at = CASE WHEN prod4_processes.raw_detail IS NULL" in sql
    assert params[9] is None and params[10] is None


@pytest.mark.parametrize("procedure_id", [101, 1234567])
@pytest.mark.parametrize("already_exists", [False, True])
@patch("buenapro_worker.jobs.poll_prod4._ensure_identity", return_value="11111111-1111-4111-8111-111111111111")
@patch("buenapro_worker.jobs.poll_prod4._existing")
def test_listing_uses_process_ficha_url_on_insert_and_conflict(
    existing, _identity, procedure_id: int, already_exists: bool,
) -> None:
    repo = MagicMock()
    current_row = row(procedure_id, 62, "43")
    scan = datetime(2026, 10, 1, tzinfo=timezone.utc)
    existing.return_value = (
        {"raw_listing": [current_row], "raw_detail": {"listaCronograma": []},
         "detail_fetched_at": scan}
        if already_exists else None
    )

    needs_detail = _upsert_listing(repo, procedure_id, "good", [current_row], ["cubso:43"], scan)

    calls = [call for call in repo.conn.execute.call_args_list
             if "INSERT INTO prod4_processes" in call.args[0]]
    assert len(calls) == 1
    sql, params = calls[0].args
    assert params[0] == procedure_id
    assert params[13] == f"https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/{procedure_id}"
    conflict_sql = sql.split("ON CONFLICT (id_procedimiento) DO UPDATE SET", 1)[1]
    assert "source_url = EXCLUDED.source_url" in conflict_sql
    assert "prod4_processes.source_url IS DISTINCT FROM EXCLUDED.source_url" in conflict_sql
    assert needs_detail is not already_exists


@patch("buenapro_worker.jobs.poll_prod4._upsert_detail", side_effect=RuntimeError("bad detail SQL"))
@patch("buenapro_worker.jobs.poll_prod4._upsert_listing", return_value=True)
def test_bad_detail_uses_savepoint_and_snapshot_continues(_listing: MagicMock, _detail: MagicMock) -> None:
    source = MagicMock()
    source.by_object.side_effect = [[row(101, 62, "432226")], [row(202, 65, "811121")]]
    source.by_segment.side_effect = [[row(101, 62, "432226")], [row(202, 65, "811121")]]
    source.detail.return_value = {"nidExpediente": 101}
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"total": 0}
    repo.conn.execute.return_value.fetchall.return_value = []

    stats = poll_prod4(settings(prod4_detail_limit=1), repo, source)

    assert stats == {"selected": 2, "details": 0, "missing": 0}
    repo.conn.transaction.assert_called_once_with()
    assert _detail.call_count == 1
    assert "UPDATE prod4_processes SET missing_since" in repo.conn.execute.call_args.args[0]
