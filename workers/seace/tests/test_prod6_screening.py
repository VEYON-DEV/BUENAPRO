import os
import random
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profile
from buenapro_worker.prod6.screening import (
    POLICY_VERSION, decide, existing_decision, prefilter, profile_snapshot, record_decision,
)
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.schemas import SearchItem, SearchResponse
from buenapro_worker.settings import Settings

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def line(nombre="Tableros", segments=("39", "43"), phrases=("tableros eléctricos",),
         terms=("ccm", "variadores"), umbral=70):
    return {"nombre": nombre, "cubso_segmentos": list(segments), "keyword_phrases": list(phrases),
            "keyword_terms": list(terms), "keywords": [], "score_umbral": umbral}


def snapshot(*lines):
    return {"id": "p", "descripcion": "", "lines": list(lines) or [line()], "profile_hash": "h"}


def search_item(text="Mantenimiento de tableros eléctricos", **fields):
    data = {"idContrato": 1, "desContratacion": "CM-1-2026", "idObjetoContrato": 2,
            "desObjetoContrato": text, "idEstadoContrato": 2,
            "fecPublica": "01/10/2026 10:00:00", "fecFinCotizacion": "30/12/2026 10:00:00"} | fields
    return SearchItem.model_validate(data)


def run_prefilter(item, snap=None, segment=39):
    return prefilter(item, snap or snapshot(), year=2026, now=NOW, segment=segment)


def test_prefilter_passes_and_reports_keyword_lines():
    assert run_prefilter(search_item()) == (True, [], ["Tableros"])


@pytest.mark.parametrize("fields, segment, reason", [
    ({"idEstadoContrato": 3}, 39, "not_vigente"),
    ({"idObjetoContrato": 3}, 39, "object_out_of_scope"),
    ({"fecPublica": "01/10/2025 10:00:00"}, 39, "publication_year_mismatch"),
    ({"fecPublica": None}, 39, "publication_year_mismatch"),
    ({"fecFinCotizacion": "01/01/2026 10:00:00"}, 39, "closed"),
    ({}, 81, "segment_out_of_profile"),
])
def test_prefilter_hard_rules_one_reason_each(fields, segment, reason):
    passed, reasons, _ = run_prefilter(search_item(**fields), segment=segment)
    assert not passed
    assert reasons == [reason]


def test_prefilter_open_ended_close_date_passes():
    assert run_prefilter(search_item(fecFinCotizacion=None))[0]


def test_keywords_never_block_the_prefilter():
    passed, reasons, keyword_lines = run_prefilter(search_item("Compra de papelería"))
    assert (passed, reasons, keyword_lines) == (True, [], [])


def test_keyword_lines_only_from_lines_of_the_stream_segment():
    snap = snapshot(line("Tableros", segments=("39",)), line("Otra", segments=("43",)))
    assert run_prefilter(search_item(), snap, segment=39)[2] == ["Tableros"]
    assert run_prefilter(search_item(), snap, segment=43)[2] == ["Otra"]


def test_keyword_lines_two_terms_or_phrase_within_one_line():
    snap = snapshot(line("A", phrases=(), terms=("ccm", "variadores")), line("B", phrases=("scada",), terms=()))
    assert run_prefilter(search_item("CCM con variadores"), snap)[2] == ["A"]
    assert run_prefilter(search_item("Solo variadores"), snap)[2] == []
    assert run_prefilter(search_item("Sistema SCADA"), snap)[2] == ["B"]


def test_prefilter_collects_every_failed_rule():
    _, reasons, _ = run_prefilter(search_item(idEstadoContrato=3, fecFinCotizacion="01/01/2026 10:00:00"))
    assert reasons == ["not_vigente", "closed"]


@pytest.mark.parametrize("passed, keyword_lines, expected", [
    (False, ["Tableros"], ("rejected", "prefilter")),
    (False, [], ("rejected", "prefilter")),
    (True, ["Tableros"], ("accepted", "keywords")),
    (True, [], ("pending", "keywords")),
])
def test_m1_decision_rule(passed, keyword_lines, expected):
    assert decide(passed, keyword_lines) == expected


def profile_rows(*lines):
    return [{"nombre": l["nombre"], "cubso_segmentos": l["cubso_segmentos"],
             "keyword_phrases": l["keyword_phrases"], "keyword_terms": l["keyword_terms"],
             "keywords": l["keywords"], "score_umbral": l["score_umbral"]} for l in lines]


def snapshot_from(rows, description="Empresa de automatización", profile_id="p"):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"id": profile_id, "descripcion": description}
    conn.execute.return_value.fetchall.return_value = rows
    return profile_snapshot(conn, profile_id)


def test_profile_hash_ignores_row_and_array_order():
    a, b = line("A"), line("B", segments=("81",), terms=("plc", "scada"))
    first = snapshot_from(profile_rows(a, b))
    shuffled_b = dict(b, cubso_segmentos=["81"], keyword_terms=["scada", "plc"])
    second = snapshot_from(profile_rows(shuffled_b, dict(a, cubso_segmentos=["43", "39"])))
    assert first["profile_hash"] == second["profile_hash"]
    assert [l["nombre"] for l in first["lines"]] == ["A", "B"]


@pytest.mark.parametrize("change", [
    lambda a, b: (a, dict(b, score_umbral=60)),
    lambda a, b: (a, dict(b, cubso_segmentos=["81", "72"])),
    lambda a, b: (a, dict(b, keyword_phrases=["nueva frase"])),
    lambda a, b: (a,),
    lambda a, b: (a, b, line("C")),
])
def test_profile_hash_changes_with_lines_threshold_or_segments(change):
    a, b = line("A"), line("B", segments=("81",))
    base = snapshot_from(profile_rows(a, b))["profile_hash"]
    assert snapshot_from(profile_rows(*change(a, b)))["profile_hash"] != base


def test_profile_hash_changes_with_description_and_rejects_inactive_profile():
    rows = profile_rows(line())
    assert snapshot_from(rows)["profile_hash"] != snapshot_from(rows, description="otra")["profile_hash"]
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    with pytest.raises(ValueError, match="Active company"):
        profile_snapshot(conn, "missing")


# --- PostgreSQL integration: only with BUENAPRO_TEST_DATABASE_URL; always rolled back ---

DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")


@pytest.fixture
def db():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def profile(db):
    tenant = db.execute("INSERT INTO tenants (name) VALUES ('screening-test') RETURNING id").fetchone()
    profile_id = db.execute(
        """INSERT INTO company_profiles (tenant_id, ruc, razon_social, identity_json)
           VALUES (%s, '20999999991', 'Screening Test SAC', '{"descripcion": "Automatización"}')
           RETURNING id""", (tenant["id"],)).fetchone()["id"]
    db.execute(
        """INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keyword_phrases, keyword_terms)
           VALUES (%s, 'Tableros', '{39}', '{tableros eléctricos}', '{ccm,variadores}')""", (profile_id,))
    return str(profile_id)


def settings():
    return Settings(database_url="postgresql://unused", gemini_api_key="unused",
                    seace_allowed_codigo_objeto="1,2", seace_allowed_segments="43,81")


def new_contract_id():
    return random.randint(9_000_000_000, 9_900_000_000)


def decision_fields(id_contrato, profile_id, **overrides):
    return dict(item=search_item(idContrato=id_contrato, desContratacion=f"TEST-{id_contrato}"),
                segment=39, profile_id=profile_id, contract_hash="c1", profile_hash="p1",
                policy_version=POLICY_VERSION, prefilter_passed=True, prefilter_reasons=[],
                keyword_lines=["Tableros"], decision="accepted", decided_by="keywords") | overrides


@needs_db
def test_record_decision_is_idempotent_and_found_by_key(db, profile):
    id_contrato = new_contract_id()  # No seace_contracts row: decisions stand alone.
    key = (id_contrato, profile, "c1", "p1", POLICY_VERSION)
    assert existing_decision(db, *key) is None
    assert record_decision(db, **decision_fields(id_contrato, profile)) is True
    # A second attempt, even with another verdict, neither duplicates nor overwrites.
    assert record_decision(db, **decision_fields(id_contrato, profile, decision="rejected",
                                                 decided_by="prefilter", prefilter_passed=False)) is False
    found = existing_decision(db, *key)
    assert (found["decision"], found["decided_by"]) == ("accepted", "keywords")
    assert db.execute("SELECT count(*) AS n FROM prod6_profile_decisions WHERE id_contrato = %s",
                      (id_contrato,)).fetchone()["n"] == 1
    row = db.execute("SELECT * FROM prod6_profile_decisions WHERE id_contrato = %s", (id_contrato,)).fetchone()
    assert (row["codigo"], row["cubso_segmento"], row["objeto_codigo"]) == (f"TEST-{id_contrato}", "39", 2)
    assert row["fec_fin_cotizacion"].year == 2026 and row["listing_json"]["idContrato"] == id_contrato
    # A different search-card hash is a new key: the contract is decided again.
    assert existing_decision(db, id_contrato, profile, "c2", "p1", POLICY_VERSION) is None
    assert record_decision(db, **decision_fields(id_contrato, profile, contract_hash="c2")) is True


@needs_db
def test_profile_snapshot_reads_active_lines(db, profile):
    snap = profile_snapshot(db, profile)
    assert snap["descripcion"] == "Automatización"
    assert [(l["nombre"], l["cubso_segmentos"], l["score_umbral"]) for l in snap["lines"]] == [
        ("Tableros", ["39"], 70)]
    assert snapshot_hash_is_stable(db, profile, snap)


def snapshot_hash_is_stable(db, profile_id, snap):
    db.execute("UPDATE company_profiles SET updated_at = now() + interval '1 day' WHERE id = %s", (profile_id,))
    return profile_snapshot(db, profile_id)["profile_hash"] == snap["profile_hash"]


def poll_item(id, text, **fields):
    return {"idContrato": id, "desContratacion": f"TEST-{id}", "idObjetoContrato": 2,
            "desObjetoContrato": text, "idEstadoContrato": 2,
            "fecPublica": "01/10/2026 10:00:00", "fecFinCotizacion": "30/12/2099 10:00:00"} | fields


class FakeClient:
    def __init__(self, items):
        self.items = items

    def __call__(self, _settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def search_contracts(self, *, objeto, **_query):
        data = self.items if objeto == 2 else []
        return SearchResponse.model_validate({
            "data": data, "pageable": {"pageNumber": 1, "pageSize": 100, "totalElements": len(data)}})


def opportunity_count(db):
    return db.execute("SELECT count(*) AS n FROM opportunities").fetchone()["n"]


def run_poll(db, profile, items):
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", FakeClient(items)):
        return poll_prod6_profile(settings(), JobRepository(db), profile_id=profile, anio=2026)


@needs_db
def test_poll_records_accepted_pending_and_rejected_and_enqueues_only_accepted(db, profile):
    opportunities_before = opportunity_count(db)
    ids = [new_contract_id() for _ in range(5)]
    accepted, pending, closed, wrong_year, other_state = ids
    items = [
        poll_item(accepted, "Mantenimiento de tableros eléctricos"),
        poll_item(pending, "Servicio de papelería"),
        poll_item(closed, "Tableros eléctricos", fecFinCotizacion="01/01/2026 10:00:00"),
        poll_item(wrong_year, "Tableros eléctricos", fecPublica="01/10/2025 10:00:00"),
        poll_item(other_state, "Tableros eléctricos", idEstadoContrato=3),
    ]
    stats = run_poll(db, profile, items)
    rows = {row["id_contrato"]: row for row in db.execute(
        """SELECT * FROM prod6_profile_decisions WHERE profile_id = %s""", (profile,)).fetchall()}
    assert set(rows) == {accepted, pending, closed, other_state}  # wrong-year rows are not decided
    assert (rows[accepted]["decision"], rows[accepted]["decided_by"]) == ("accepted", "keywords")
    assert rows[accepted]["keyword_lines"] == ["Tableros"]
    assert (rows[pending]["decision"], rows[pending]["prefilter_passed"]) == ("pending", True)
    assert (rows[closed]["decision"], rows[closed]["prefilter_reasons"]) == ("rejected", ["closed"])
    assert rows[other_state]["prefilter_reasons"] == ["not_vigente"]
    assert all(row["score"] is None and row["attempts"] == 0 for row in rows.values())
    # Only the accepted contract reaches seace_contracts and opportunities (what the web lists).
    contracts = db.execute("SELECT id_contrato, opportunity_id FROM seace_contracts "
                           "WHERE id_contrato = ANY(%s)", (ids,)).fetchall()
    assert [c["id_contrato"] for c in contracts] == [accepted] and contracts[0]["opportunity_id"]
    assert db.execute("SELECT count(*) AS n FROM opportunity_sources WHERE source_key = 'seace_prod6' "
                      "AND external_id = ANY(%s)", ([str(i) for i in ids],)).fetchone()["n"] == 1
    assert opportunity_count(db) == opportunities_before + 1
    jobs = db.execute("SELECT payload FROM worker_jobs WHERE job_type = 'process_contract' "
                      "AND (payload->>'id_contrato')::bigint = ANY(%s)", (ids,)).fetchall()
    assert [job["payload"]["id_contrato"] for job in jobs] == [accepted]
    assert (stats["candidates"], stats["enqueued"], stats["skipped"]) == (1, 1, 4)
    assert (stats["decisions_recorded"], stats["decisions_reused"], stats["prefilter_rejected"]) == (4, 0, 2)


@needs_db
def test_poll_does_not_reevaluate_decided_contracts_but_redecides_on_changes(db, profile):
    ids = [new_contract_id(), new_contract_id()]
    items = [poll_item(ids[0], "Mantenimiento de tableros eléctricos"), poll_item(ids[1], "Papelería")]
    run_poll(db, profile, items)
    stats = run_poll(db, profile, items)
    assert (stats["decisions_recorded"], stats["decisions_reused"]) == (0, 2)
    count = "SELECT count(*) AS n FROM prod6_profile_decisions WHERE id_contrato = ANY(%s)"
    assert db.execute(count, (ids,)).fetchone()["n"] == 2
    # A changed search card (new hash) is decided again; so is a changed profile.
    changed = [poll_item(ids[1], "Papelería y variadores con CCM")]
    assert run_poll(db, profile, changed)["decisions_recorded"] == 1
    db.execute("UPDATE business_lines SET score_umbral = 60 WHERE profile_id = %s", (profile,))
    assert run_poll(db, profile, changed)["decisions_recorded"] == 1
    assert db.execute(count, (ids,)).fetchone()["n"] == 4
