from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs.poll_prod4 import (
    _date,
    _money,
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
