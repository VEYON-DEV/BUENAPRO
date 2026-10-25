"""Deterministic review of stored PROD6 readings: golden cases from real minor-contract readings.

The fixture holds 23 readings that Gemini produced for real minor contracts (trimmed to the
sections the rules read). Five contradict themselves or their title and must be flagged; the
other eighteen are correct and must not be.
"""
import json
import os
import random
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import derive_summary
from buenapro_worker.prod6 import recompute
from buenapro_worker.prod6.review import (
    RULES_VERSION, build_reading_summary, review_reading,
)

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "prod6_readings_20261025.json").read_text(encoding="utf-8"))
BY_CODE = {case["codigo"]: case for case in GOLDEN}
FLAGGED = {
    "CM-1514-2026-SEDAPAR SA": ["term_missing"],
    "CM-233-2026-CEPLAN": ["payment_count_mismatch", "payment_percent_sum", "payment_type_incoherent"],
    "CM-411-2026-AABAST-GRTCLL": ["title_spec_mismatch"],
    "CM-239-2026-AGROMERCADO": ["term_not_literal"],
    "CM-515-2026-PLD/TIC": ["experience_currency_mismatch"],
}


def codes(raw, **kwargs):
    return [reason["code"] for reason in review_reading(raw, **kwargs)]


# --- golden readings ---------------------------------------------------------------------


def test_golden_set_is_the_23_real_readings():
    assert len(GOLDEN) == 23
    assert {case["prompt_version"] for case in GOLDEN} == {"tdr_extraction_v3", "eett_extraction_v2"}


@pytest.mark.parametrize("case", GOLDEN, ids=lambda case: case["codigo"])
def test_golden_reading_is_flagged_only_when_it_is_wrong(case):
    summary, flag = build_reading_summary(case["raw"], contract_title=case["title"],
                                         prompt_version=case["prompt_version"])
    expected = FLAGGED.get(case["codigo"], [])
    assert [reason["code"] for reason in summary["review_reasons"]] == expected
    assert flag is bool(expected)
    assert all(reason["message"] for reason in summary["review_reasons"])
    assert (summary["modalidad"], summary["plazo_ejecucion_dias"]) == (
        case["expect"]["modalidad"], case["expect"]["plazo_ejecucion_dias"])


def test_the_known_contradictions_name_their_cause():
    nas = review_reading(BY_CODE["CM-411-2026-AABAST-GRTCLL"]["raw"],
                         contract_title=BY_CODE["CM-411-2026-AABAST-GRTCLL"]["title"])
    assert "GHz título 2.2 / lectura 1.7" in nas[0]["message"] and "GB título 8" in nas[0]["message"]
    ceplan = review_reading(BY_CODE["CM-233-2026-CEPLAN"]["raw"])
    assert "12 pagos" in ceplan[0]["message"] and "1 filas" in ceplan[0]["message"]
    seal = review_reading(BY_CODE["CM-515-2026-PLD/TIC"]["raw"])
    assert "USD" in seal[0]["message"] and "PEN" in seal[0]["message"]


def test_without_the_title_only_internal_contradictions_remain():
    assert codes(BY_CODE["CM-411-2026-AABAST-GRTCLL"]["raw"]) == []
    assert codes(BY_CODE["CM-233-2026-CEPLAN"]["raw"]) != []


def test_modalidad_is_no_longer_always_no_indicada():
    modalities = {case["codigo"]: derive_summary(case["raw"])["modalidad"] for case in GOLDEN}
    assert {case["old_summary"]["modalidad"] for case in GOLDEN} == {"no_indicada"}
    assert modalities["CM-446-2026-SERFOR"] == "virtual"          # lugar: Remoto
    assert modalities["CM-184-2026-SENASA"] == "virtual"          # De manera virtual
    assert modalities["CM-239-2026-AGROMERCADO"] == "virtual"
    assert modalities["CM-233-2026-CEPLAN"] == "mixta"            # Híbrido
    assert modalities["CM-128-2026-UNACHU"] == "mixta"            # remota + actividades presenciales
    assert modalities["CM-515-2026-PLD/TIC"] == "mixta"           # presencial o remotamente
    assert modalities["CM-786-2026-EPS EMAPA CAÑETE S.A."] == "presencial"  # entrega en almacén
    assert sum(1 for value in modalities.values() if value != "no_indicada") >= 22


def test_goods_term_comes_from_the_delivery():
    sedapar = BY_CODE["CM-1514-2026-SEDAPAR SA"]
    assert sedapar["raw"]["contract"]["plazo_dias"] is None
    assert derive_summary(sedapar["raw"])["plazo_ejecucion_dias"] == 20
    only_delivery = {"goods": {"entrega": {"plazo_dias": 12}}}
    assert derive_summary(only_delivery)["plazo_ejecucion_dias"] == 12
    assert derive_summary({"goods": {"entrega": {"plazo_texto": "quince (15) dias calendario"}}})[
        "plazo_ejecucion_dias"] == 15
    # The contract's own term still wins over the delivery's.
    assert derive_summary({"contract": {"plazo_dias": 30}, "goods": {"entrega": {"plazo_dias": 12}}})[
        "plazo_ejecucion_dias"] == 30


# --- derive_summary: modalidad -----------------------------------------------------------


@pytest.mark.parametrize("contract,execution,expected", [
    ({"modalidad": "Suma alzada", "lugar_ejecucion": "Remoto"}, {}, "virtual"),
    ({"modalidad": "Suma alzada"}, {}, "no_indicada"),
    ({"modalidad": "Suma alzada", "lugar_ejecucion": "Oficinas de la entidad, Av. Lima 123"}, {}, "presencial"),
    ({"lugar_ejecucion": "Remoto, con visitas presenciales a la sede"}, {}, "mixta"),
    ({"lugar_ejecucion": "Híbrido"}, {}, "mixta"),
    ({"forma_ejecucion": "remota", "lugar_ejecucion": "Oficinas de la entidad"}, {}, "virtual"),
    ({"forma_ejecucion": "presencial", "lugar_ejecucion": "Remoto"}, {}, "presencial"),
    ({"forma_ejecucion": "mixta"}, {}, "mixta"),
    ({"modalidad": "Trabajo remoto virtual"}, {}, "virtual"),   # older readings kept the form here
    ({}, {"lugar": "Laboratorio del proveedor"}, "presencial"),
    ({"lugar_ejecucion": "No indica"}, {}, "no_indicada"),
    ({}, {}, "no_indicada"),
])
def test_modalidad_follows_the_place_or_form_of_execution(contract, execution, expected):
    assert derive_summary({"contract": contract, "execution": execution})["modalidad"] == expected


def test_goods_delivery_place_gives_modalidad_when_the_contract_has_none():
    raw = {"goods": {"entrega": {"lugar": "Almacén central, Av. Industrial 45"}}}
    assert derive_summary(raw)["modalidad"] == "presencial"


# --- rules -------------------------------------------------------------------------------


def test_term_rules():
    assert codes({"contract": {"plazo_dias": None, "plazo_texto": "treinta (30) días calendario"}}) == ["term_missing"]
    assert codes({"contract": {"plazo_dias": None}, "goods": {"entrega": {"plazo_dias": 10}}}) == ["term_missing"]
    assert codes({"contract": {"plazo_dias": None, "plazo_texto": "a coordinar"}}) == []
    assert codes({"contract": {}}) == []
    # Literal in days, months (30 days each, or the year), weeks and years: no suspicion.
    for days, text in ((30, "treinta (30) días calendario"), (60, "dos (2) meses"), (365, "doce (12) meses"),
                       (365, "01 año (365 días)"), (28, "4 semanas"), (365, "1 año")):
        assert codes({"contract": {"plazo_dias": days, "plazo_texto": text}}) == [], text
    # A sum of two terms is not in the text.
    assert codes({"contract": {"plazo_dias": 375, "plazo_texto": "activación en 10 días; vigencia 365 días"}}) == [
        "term_not_literal"]
    assert codes({"contract": {"plazo_dias": 45, "plazo_texto": "30 días calendario"}}) == ["term_not_literal"]
    assert codes({"contract": {"plazo_dias": "30", "plazo_texto": "30 días"}}) == []
    assert codes({"contract": {"plazo_dias": 30}, "goods": {"entrega": {"plazo_dias": 15}}}) == ["term_conflict"]
    assert codes({"contract": {"plazo_dias": 15, "plazo_texto": "plazo a convenir"}}) == []


def test_payment_count_rules():
    twelve = {"payment": {"tipo": "armadas", "detalle": "Pago en doce (12) armadas iguales",
                          "armadas": [{"numero": 1, "porcentaje": 100.0}]}}
    assert "payment_count_mismatch" in codes(twelve)
    declared = {"payment": {"tipo": "armadas", "numero_pagos_declarado": 3, "armadas": [
        {"numero": index, "porcentaje": 33.33 if index < 3 else 33.34} for index in (1, 2, 3)]}}
    assert codes(declared) == []
    declared["payment"]["numero_pagos_declarado"] = 4
    assert codes(declared) == ["payment_count_mismatch"]
    words = {"payment": {"tipo": "armadas", "detalle": "Dos pagos parciales iguales",
                         "armadas": [{"porcentaje": 50}, {"porcentaje": 50}]}}
    assert codes(words) == []
    assert codes({"payment": {"tipo": "mensual", "detalle": "12 pagos mensuales", "armadas": []}}) == []
    assert codes({"payment": {"tipo": "armadas", "armadas": [{"porcentaje": 40}, {"porcentaje": 40}]}}) == [
        "payment_percent_sum"]


def test_payment_type_rules():
    two = [{"porcentaje": 50}, {"porcentaje": 50}]
    assert codes({"payment": {"tipo": "pago_unico", "armadas": two}}) == ["payment_type_incoherent"]
    assert codes({"payment": {"tipo": "armadas", "armadas": []}}) == ["payment_type_incoherent"]
    assert codes({"payment": {"tipo": "pago_unico", "detalle": "Pago único tras la conformidad",
                              "armadas": [{"porcentaje": 100}]}}) == []
    assert codes({"payment": {"tipo": "armadas", "detalle": "Pago mensual previa conformidad",
                              "armadas": two}}) == ["payment_type_incoherent"]
    assert codes({"payment": {"tipo": "por_entregable", "detalle": "Cada entregable, con informe mensual de avance"}}) == []
    assert codes({"payment": {"tipo": "mensual", "detalle": "Se pagará en un solo pago al final"}}) == [
        "payment_type_incoherent"]
    assert codes({"payment": {"tipo": "mensual", "detalle": "Pago mensual previa conformidad"}}) == []
    assert codes({"payment": {}}) == []


def test_experience_currency_rules():
    def experience(text, contract_currency="PEN", **extra):
        return {"contract": {"moneda": contract_currency}, "requirements": {"provider": [
            {"tipo": "economic_experience", "descripcion": text, "monto": 100, **extra}]}}
    assert codes(experience("Facturación acumulada de S/ 20,000.00")) == []
    assert codes(experience("Al menos 6 servicios de $40,000.00 USD cada uno")) == ["experience_currency_mismatch"]
    assert codes(experience("Monto de US$ 15 000")) == ["experience_currency_mismatch"]
    assert codes(experience("Monto de US$ 15 000", contract_currency="USD")) == []
    assert codes(experience("S/ 10 000 (equivalente a US$ 3 000)")) == []
    assert codes(experience("Experiencia de 5 años")) == []
    assert codes(experience("Monto acumulado", moneda="USD")) == ["experience_currency_mismatch"]
    assert codes(experience("Monto acumulado", moneda="PEN")) == []
    other = experience("US$ 5 000")
    other["requirements"]["provider"][0]["tipo"] = "other"  # Only experience requirements are compared.
    assert codes(other) == []


def test_low_evidence_rule():
    def provider(*evidence):
        return {"requirements": {"provider": [
            {"tipo": "specific_experience", "descripcion": f"req {index}", "evidencia": quote}
            for index, quote in enumerate(evidence)]}}
    assert codes(provider(None, None, "cita")) == ["low_evidence"]
    assert codes(provider("a", "b", None)) == []
    assert codes(provider(None)) == []           # One requirement says too little.
    assert codes(provider(None, None)) == ["low_evidence"]
    boilerplate = {"requirements": {"provider": [{"tipo": tipo, "descripcion": tipo}
                                                 for tipo in ("rnp", "ruc_status", "not_disqualified", "cci")]}}
    assert codes(boilerplate) == []              # Standard clauses are not judged by their quote.
    personnel = {"requirements": {"provider": [], "key_personnel": [{"rol": "A"}, {"rol": "B"}]}}
    assert codes(personnel) == []                # The old prompt never asked a quote for people...
    assert codes(personnel, prompt_version="prod6_tdr_reading_v1") == ["low_evidence"]  # ...the new one does.


def test_title_numbers_against_specifications():
    def goods(*specs, quantity=1):
        return {"goods": {"items": [{"nombre": "Servidor", "cantidad": quantity, "especificaciones": list(specs)}]}}
    title = "ADQUISICIÓN DE SERVIDOR 2.2 GHz 4 NUCLEOS 8 GB"
    assert codes(goods("Procesador 1.7 GHz", "RAM 4 GB DDR4"), contract_title=title) == ["title_spec_mismatch"]
    assert codes(goods("Procesador 2,2 GHz", "RAM 8 GB DDR4", "4 núcleos"), contract_title=title) == []
    assert codes(goods("Procesador 2200 MHz", "RAM 8192 MB", "4 cores"), contract_title=title) == []
    # Gigabit rates and Gb/s links are not memory.
    assert codes(goods("RAM 8 GB", "2 puertos 2.5 GbE", "SATA 6 Gb/s"), contract_title=title + "") == []
    # Terabytes compare with gigabytes (decimal or binary).
    assert codes(goods("Disco 2 TB"), contract_title="NAS 2000 GB") == []
    assert codes(goods("Disco 1 TB"), contract_title="NAS 2 TB") == ["title_spec_mismatch"]
    # Units seen on one side only are not compared.
    assert codes(goods("Procesador 3 GHz"), contract_title="SERVIDOR 8 GB") == []
    assert codes(goods("Motor 5 HP"), contract_title="BOMBA DE 7.5 HP") == ["title_spec_mismatch"]
    assert codes(goods("Transformador 250 kVA"), contract_title="TRANSFORMADOR DE 250 KVA") == []
    # A service reading has no specifications to compare.
    assert codes({"contract": {"objeto": "Soporte 8 GB"}}, contract_title="SOPORTE 16 GB") == []


def test_title_quantity_and_model():
    def goods(*quantities, name="Variador"):
        return {"goods": {"items": [{"nombre": f"{name} {index}", "cantidad": value}
                                    for index, value in enumerate(quantities)]}}
    title = "ADQUISICIÓN DE DOS (02) VARIADORES DE VELOCIDAD"
    assert codes(goods(2), contract_title=title) == []
    assert codes(goods(1, 1), contract_title=title) == []      # Two items of one unit add up.
    assert codes(goods(5), contract_title=title) == ["title_quantity_mismatch"]
    assert codes(goods(), contract_title=title) == []           # No quantities to compare.
    assert codes(goods(35), contract_title="SUSCRIPCIÓN DE TREINTA Y CINCO (35) LICENCIAS") == []
    assert codes(goods(3), contract_title="Req. (2026) del área") == []   # Not a quantity.
    lenovo = {"goods": {"items": [{"nombre": "Unidad de cintas Lenovo TS4300"}]}}
    assert codes(lenovo, contract_title="SOPORTE PARA LENOVO TS4300") == []
    assert codes(lenovo, contract_title="SOPORTE PARA LENOVO TS-4300") == []
    assert codes(lenovo, contract_title="SOPORTE PARA DELL R750") == ["title_model_missing"]
    assert codes(lenovo, contract_title="COMPRA DE 16GB Y 2.2GHz") == []   # Units glued to digits are not models.
    # Service titles carry project codes that the TDR body may omit.
    assert codes({"contract": {"objeto": "Especialista"}}, contract_title="ESPECIALISTA PARA EL PI AOI0013710") == []


def test_a_rule_that_cannot_read_the_shape_asks_for_review_instead_of_failing(monkeypatch):
    from buenapro_worker.prod6 import review
    monkeypatch.setattr(review, "_payment_reasons", MagicMock(side_effect=TypeError("unexpected shape")))
    assert codes({"contract": {"plazo_dias": None, "plazo_texto": "10 días"}}) == ["term_missing", "review_rule_failed"]
    monkeypatch.undo()
    assert codes("not a dict") == []
    assert codes({"requirements": {"provider": {"tipo": "other"}}, "penalties": "x", "payment": "x"}) == []


# --- what is stored ----------------------------------------------------------------------


def test_review_keys_and_a_pure_derivation():
    case = BY_CODE["CM-233-2026-CEPLAN"]
    summary, flag = build_reading_summary(case["raw"], contract_title=case["title"])
    assert flag is True
    assert summary["review_rules_version"] == RULES_VERSION and summary["review_inherited"] is False
    assert {reason["code"] for reason in summary["review_reasons"]} == set(FLAGGED["CM-233-2026-CEPLAN"])
    assert "review_reasons" not in derive_summary(case["raw"])   # The pure derivation is unchanged.


def test_a_flag_set_before_the_rules_existed_is_kept_and_a_human_decision_is_respected():
    raw = BY_CODE["CM-505-2026-CONCYTEC"]["raw"]
    summary, flag = build_reading_summary(raw, previous_summary={"descripcion_corta": "x"}, previous_flag=True)
    assert flag is True and summary["review_inherited"] is True and summary["review_reasons"] == []
    again, flag_again = build_reading_summary(raw, previous_summary=summary, previous_flag=flag)
    assert (again, flag_again) == (summary, True)
    # A person who reviewed it decides; rules only add information.
    ceplan = BY_CODE["CM-233-2026-CEPLAN"]["raw"]
    reviewed, flag_reviewed = build_reading_summary(ceplan, previous_flag=False, quality="reviewed")
    assert flag_reviewed is False and reviewed["review_reasons"]
    corrected, flag_corrected = build_reading_summary(ceplan, previous_flag=True, quality="corrected")
    assert flag_corrected is True
    # Rules that stop objecting release a flag they raised themselves.
    fixed, flag_fixed = build_reading_summary(raw, previous_summary={
        "review_rules_version": RULES_VERSION, "review_inherited": False,
        "review_reasons": [{"code": "x", "message": "y"}]}, previous_flag=True)
    assert flag_fixed is False and fixed["review_reasons"] == []


def test_derivation_is_deterministic_and_does_not_mutate_the_reading():
    for case in GOLDEN:
        raw = deepcopy(case["raw"])
        first = build_reading_summary(raw, contract_title=case["title"], prompt_version=case["prompt_version"])
        second = build_reading_summary(raw, contract_title=case["title"], prompt_version=case["prompt_version"],
                                       previous_summary=first[0], previous_flag=first[1])
        assert raw == case["raw"] and second == first


# --- PostgreSQL: derive_summary_job and the local recompute (always rolled back) ---------

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


def store_reading(db, case, *, flag=False, quality="auto", summary=None, filter_index=True):
    """A contract, its document and a current reading holding the golden raw JSON."""
    contract = random.randint(9_000_000_000, 9_900_000_000)
    db.execute(
        """INSERT INTO seace_contracts (id_contrato, codigo, anio, objeto_codigo, estado_codigo, descripcion,
             hash_search, raw_search_json) VALUES (%s, %s, 2026, %s, 2, %s, 'h', '{}'::jsonb)""",
        (contract, f"TEST-{contract}", 1 if "goods" in case["raw"] else 2, case["title"]))
    document = db.execute(
        """INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename,
             sha256_original, seace_download_url) VALUES (%s, %s, 1, 'tdr.pdf', %s, 'https://example.invalid')
           RETURNING id""", (contract, contract, f"{contract:064d}")).fetchone()["id"]
    extraction = db.execute(
        """INSERT INTO tdr_extractions (contract_document_id, model, prompt_version, schema_version,
             raw_extraction_json, summary_json, requires_human_review, quality)
           VALUES (%s, 'm', %s, 's', %s::jsonb, %s::jsonb, %s, %s) RETURNING id""",
        (document, case["prompt_version"], json.dumps(case["raw"]), json.dumps(summary or case["old_summary"]),
         flag, quality)).fetchone()["id"]
    if filter_index:
        db.execute("INSERT INTO contract_filter_index (id_contrato, plazo_ejecucion_dias) VALUES (%s, NULL)",
                   (contract,))
    return contract, extraction


def stored(db, extraction):
    return db.execute("SELECT summary_json, requires_human_review, quality FROM tdr_extractions WHERE id = %s",
                      (extraction,)).fetchone()


@needs_db
def test_derive_summary_job_stores_summary_review_and_reasons(db):
    from buenapro_worker.jobs.derive_summary import derive_summary_job
    from buenapro_worker.queue.repository import JobRepository
    contract, extraction = store_reading(db, BY_CODE["CM-233-2026-CEPLAN"])
    result = derive_summary_job(JobRepository(db), id_contrato=contract, extraction_id=extraction)
    assert result["requires_human_review"] is True
    row = stored(db, extraction)
    assert row["requires_human_review"] is True and row["quality"] == "auto"
    assert [reason["code"] for reason in row["summary_json"]["review_reasons"]] == FLAGGED["CM-233-2026-CEPLAN"]
    assert row["summary_json"]["modalidad"] == "mixta"
    assert db.execute("SELECT plazo_ejecucion_dias FROM contract_filter_index WHERE id_contrato = %s",
                      (contract,)).fetchone()["plazo_ejecucion_dias"] == 365


@needs_db
def test_derive_summary_job_leaves_a_correct_reading_unflagged_and_a_reviewed_one_alone(db):
    from buenapro_worker.jobs.derive_summary import derive_summary_job
    from buenapro_worker.queue.repository import JobRepository
    contract, extraction = store_reading(db, BY_CODE["CM-446-2026-SERFOR"])
    derive_summary_job(JobRepository(db), id_contrato=contract, extraction_id=extraction)
    row = stored(db, extraction)
    assert row["requires_human_review"] is False and row["summary_json"]["review_reasons"] == []
    assert row["summary_json"]["modalidad"] == "virtual"
    contract, reviewed = store_reading(db, BY_CODE["CM-233-2026-CEPLAN"], quality="reviewed")
    derive_summary_job(JobRepository(db), id_contrato=contract, extraction_id=reviewed)
    row = stored(db, reviewed)
    assert row["requires_human_review"] is False and row["quality"] == "reviewed"
    assert row["summary_json"]["review_reasons"]


@needs_db
def test_recompute_dry_run_writes_nothing_and_apply_is_idempotent(db):
    ids = {code: store_reading(db, BY_CODE[code]) for code in
           ("CM-233-2026-CEPLAN", "CM-446-2026-SERFOR", "CM-1514-2026-SEDAPAR SA")}
    selected = [extraction for _, extraction in ids.values()]
    before = {extraction: stored(db, extraction) for extraction in selected}
    dry = recompute.recompute_readings(db, ids=selected)
    assert dry["mode"] == "dry_run" and dry["selected"] == 3 and dry["changed"] == 3
    assert dry["flagged"] == 2 and dry["newly_flagged"] == 2 and dry["term_filled"] == 1
    assert {extraction: stored(db, extraction) for extraction in selected} == before
    applied = recompute.recompute_readings(db, ids=selected, apply=True)
    assert applied["changed"] == 3 and applied["flagged"] == 2
    assert stored(db, ids["CM-233-2026-CEPLAN"][1])["requires_human_review"] is True
    assert stored(db, ids["CM-446-2026-SERFOR"][1])["requires_human_review"] is False
    sedapar_contract = ids["CM-1514-2026-SEDAPAR SA"][0]
    assert db.execute("SELECT plazo_ejecucion_dias FROM contract_filter_index WHERE id_contrato = %s",
                      (sedapar_contract,)).fetchone()["plazo_ejecucion_dias"] == 20
    again = recompute.recompute_readings(db, ids=selected, apply=True)
    assert again["changed"] == 0 and again["unchanged"] == 3 and again["flagged"] == 2
    assert again["newly_flagged"] == 0 and again["no_longer_flagged"] == 0
    raw = db.execute("SELECT raw_extraction_json FROM tdr_extractions WHERE id = %s",
                     (ids["CM-233-2026-CEPLAN"][1],)).fetchone()["raw_extraction_json"]
    assert raw == BY_CODE["CM-233-2026-CEPLAN"]["raw"]


@needs_db
def test_recompute_never_creates_a_filter_index_row_and_requires_a_selector(db):
    contract, extraction = store_reading(db, BY_CODE["CM-446-2026-SERFOR"], filter_index=False)
    recompute.recompute_readings(db, ids=[extraction], apply=True)
    assert db.execute("SELECT 1 FROM contract_filter_index WHERE id_contrato = %s", (contract,)).fetchone() is None
    with pytest.raises(ValueError):
        recompute.recompute_readings(db)
