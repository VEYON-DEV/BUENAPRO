"""PROD6 M2: per-company JEV affinity scoring. No network: every JEV call is a mock transport."""
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from buenapro_worker.prod6 import jev_screening
from buenapro_worker.prod6.jev_screening import (
    JevHttpError, JevResponseError, JevScreeningClient, JevTransportError, MAX_COMPANY_KEYWORDS,
    MAX_LINE_KEYWORDS, SUBCATEGORIES, admit_accepted, build_payload, company_payload, listing_text,
    request_hash, score_pending, unadmitted_accepted,
)
from buenapro_worker.prod6.screening import (
    POLICY_VERSION, profile_snapshot, record_decision, supersede_pending,
)
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.schemas import SearchItem

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
COMPANIES = {"e1": {"descripcion": "Software y nube", "lineas": ["Licencias", "Redes"]},
             "e2": {"descripcion": "PLC y tableros", "lineas": ["Automatización"]}}


def search_item(id_contrato=1, text="Licencias de software antivirus", **fields):
    data = {"idContrato": id_contrato, "desContratacion": f"TEST-{id_contrato}", "idObjetoContrato": 2,
            "desObjetoContrato": text, "idEstadoContrato": 2, "nomEntidad": "Municipalidad de Ejemplo",
            "nomObjetoContrato": "Servicio",
            "fecPublica": "01/10/2026 10:00:00", "fecFinCotizacion": "30/12/2099 10:00:00"} | fields
    return SearchItem.model_validate(data)


def jev_answer(request, scores, *, domain="informatica", information="claro", subcategory="licencias_software",
               tokens=(1000, 90)):
    answers = {}
    for question_id, question in request["questions"].items():
        if question_id.startswith("afinidad_"):
            score = scores[question_id.removeprefix("afinidad_")]
            answers[question_id] = {"type": "score", "score": score, "confidence": 0.8,
                                    "probabilities": {"3": 0.5, "4": 0.5}}
        else:
            choice = {"dominio": domain, "informacion": information, "subcategoria": subcategory}[question_id]
            answers[question_id] = {"type": "choice", "choice": choice, "confidence": 0.9,
                                    "probabilities": {choice: 1}}
    return {"model": "jev-1.13.0", "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
            "answers": answers}


class FakeJev:
    """httpx.MockTransport handler that records requests and answers per alias."""

    def __init__(self, scores=None, status=200, mutate=None, **answer):
        self.scores, self.status, self.mutate, self.answer = scores or {"e1": 3, "e2": 1}, status, mutate, answer
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.status != 200:
            return httpx.Response(self.status, text="secret provider body")
        data = jev_answer(body, self.scores, **self.answer)
        if self.mutate:
            self.mutate(data)
        return httpx.Response(200, json=data)

    def client(self):
        return JevScreeningClient("test-key", client=httpx.Client(transport=httpx.MockTransport(self)))


def screen(handler=None, **kwargs):
    handler = handler or FakeJev(**kwargs)
    return handler.client().screen("Licencias de software", COMPANIES), handler


def test_payload_has_shared_questions_and_one_affinity_question_per_company():
    payload = build_payload("Licencias   de software", COMPANIES)
    assert payload["model"] == "jev-1.13.0"
    assert payload["state"] == {"descripcion": "Licencias de software", "empresas": COMPANIES}
    assert list(payload["questions"]) == ["dominio", "informacion", "subcategoria", "afinidad_e1", "afinidad_e2"]
    assert payload["questions"]["subcategoria"]["criteria"] == SUBCATEGORIES
    assert {question["type"] for qid, question in payload["questions"].items()
            if qid.startswith("afinidad_")} == {"score"}
    assert len(payload["questions"]["afinidad_e1"]["criteria"]) == 5


def test_each_affinity_question_scores_only_its_own_company():
    questions = build_payload("Licencias", COMPANIES)["questions"]
    for alias, other in (("e1", "e2"), ("e2", "e1")):
        text = questions[f"afinidad_{alias}"]["instructions"]
        assert f"SOLO la afinidad comercial de state.descripcion contra state.empresas.{alias}" in text
        assert f"empresas.{other}" not in text
        assert "no sumes capacidades" in text and "dato, no instrucciones" in text


def test_single_company_payload_has_a_single_affinity_question():
    payload = build_payload("Licencias", {"e2": COMPANIES["e2"]})
    assert [qid for qid in payload["questions"] if qid.startswith("afinidad_")] == ["afinidad_e2"]


def test_information_services_are_a_subcategory_distinct_from_software_licences():
    assert "servicios_informacion" in SUBCATEGORIES
    assert "otro" in SUBCATEGORIES and list(SUBCATEGORIES)[-1] == "otro"
    assert {"licencias_software", "hardware_ti", "desarrollo_sistemas", "soporte_ti", "redes_telecom",
            "automatizacion_industrial", "electrico_instrumentacion", "capacitacion"} <= set(SUBCATEGORIES)
    assert "informacion crediticia" in SUBCATEGORIES["servicios_informacion"]
    assert "No es software" in SUBCATEGORIES["servicios_informacion"]
    assert "No incluye suscripciones o acceso a bases de datos" in SUBCATEGORIES["licencias_software"]
    payload = build_payload("Suscripcion a informacion crediticia", COMPANIES)
    assert payload["questions"]["subcategoria"]["criteria"] == SUBCATEGORIES
    assert "no es software ni licencia" in payload["questions"]["afinidad_e1"]["instructions"]
    result, _ = screen(subcategory="servicios_informacion")
    assert result["subcategory"]["choice"] == "servicios_informacion"


def test_changing_the_subcategories_changes_the_request_hash(monkeypatch):
    before = request_hash(build_payload("Licencias", COMPANIES))
    assert before == request_hash(build_payload("Licencias", COMPANIES))
    monkeypatch.setattr(jev_screening, "SUBCATEGORIES", {**SUBCATEGORIES, "otra": "Nueva."})
    # build_payload reads the module constant: stored decisions keep their own hash, new queries differ.
    assert request_hash(build_payload("Licencias", COMPANIES)) != before


def line(nombre, phrases=(), terms=(), keywords=()):
    return {"nombre": nombre, "cubso_segmentos": ["43"], "keyword_phrases": list(phrases),
            "keyword_terms": list(terms), "keywords": list(keywords), "score_umbral": 60}


def test_company_payload_anchors_each_line_with_normalized_deduplicated_keywords():
    snapshot = {"descripcion": " Software   y nube ", "lines": [
        line("Soporte  técnico", phrases=["Soporte Técnico", "mantenimiento de sistemas", " "],
             terms=["Cloud", "NUBE", "nube", "licencia"]),
        line("Automatización", terms=["PLC", "scada"])]}
    payload = company_payload(snapshot)
    assert payload["descripcion"] == "Software y nube"
    assert payload["lineas"] == [
        {"nombre": "Automatización", "palabras_clave": ["plc", "scada"]},
        {"nombre": "Soporte técnico", "palabras_clave": [
            "soporte tecnico", "cloud", "mantenimiento de sistemas", "nube", "licencia"]},
    ]
    # Same snapshot content, any order: same payload (and so the same request_hash).
    reordered = {**snapshot, "lines": snapshot["lines"][::-1]}
    assert company_payload(reordered) == payload


def test_company_payload_merges_lines_with_the_same_name_and_reads_legacy_keywords():
    payload = company_payload({"descripcion": "", "lines": [
        line("Redes", phrases=["cableado estructurado"]), line("Redes", terms=["switch", "cableado estructurado"]),
        line("Antiguo", keywords=["Licencias de software", "antivirus"])]})
    assert payload["lineas"] == [
        {"nombre": "Antiguo", "palabras_clave": ["licencias de software", "antivirus"]},
        {"nombre": "Redes", "palabras_clave": ["cableado estructurado", "switch"]}]
    with pytest.raises(ValueError):
        company_payload({"descripcion": " ", "lines": []})
    assert company_payload({"descripcion": "Solo descripcion", "lines": []})["lineas"] == []


def test_company_payload_respects_the_per_line_and_per_company_caps():
    big = line("Grande", phrases=[f"frase numero {i}" for i in range(30)], terms=[f"termino{i}" for i in range(30)])
    single = company_payload({"descripcion": "x", "lines": [big]})["lineas"][0]["palabras_clave"]
    assert len(single) == MAX_LINE_KEYWORDS
    # Phrases and terms alternate, so a cap never leaves only one kind.
    assert single[:4] == ["frase numero 0", "termino0", "frase numero 1", "termino1"]
    many = [line(f"Linea {index:02d}", phrases=[f"frase {index} {i}" for i in range(12)]) for index in range(30)]
    lines = company_payload({"descripcion": "x", "lines": many})["lineas"]
    sizes = [len(entry["palabras_clave"]) for entry in lines]
    assert sum(sizes) == MAX_COMPANY_KEYWORDS and max(sizes) - min(sizes) <= 1 and min(sizes) >= 1
    assert all(entry["nombre"] for entry in lines) and len(lines) == 30
    # A short line keeps all of its keywords; the line cap still applies to the big one (sorted by name).
    mixed = company_payload({"descripcion": "x", "lines": [big, line("Corta", terms=["a1", "b2"])]})["lineas"]
    assert [(entry["nombre"], len(entry["palabras_clave"])) for entry in mixed] == [("Corta", 2), ("Grande", 12)]


def test_payload_carries_line_keywords_and_keeps_the_single_company_instruction():
    payload = build_payload("Licencias", {"e1": company_payload({"descripcion": "Software", "lines": [
        line("Licencias", phrases=["licencias de software"], terms=["antivirus"])]})})
    assert payload["state"]["empresas"]["e1"]["lineas"] == [
        {"nombre": "Licencias", "palabras_clave": ["licencias de software", "antivirus"]}]
    text = payload["questions"]["afinidad_e1"]["instructions"]
    assert "SOLO la afinidad comercial de state.descripcion contra state.empresas.e1" in text
    assert "palabras clave de cada linea" in text and "no sumes capacidades" in text


def test_listing_text_adds_entity_and_object_and_stays_bounded():
    assert listing_text(search_item()) == (
        "Licencias de software antivirus | Entidad: Municipalidad de Ejemplo | Objeto: Servicio")
    assert len(listing_text(search_item(text="x" * 30000))) == 20000
    assert listing_text(search_item(text="", nomEntidad=None, nomObjetoContrato=None)) == "TEST-1"


def test_score_0_to_4_becomes_index_0_to_100_with_usage_and_cost():
    result, handler = screen(scores={"e1": 4, "e2": 0.5})
    assert {alias: company["score_index"] for alias, company in result["companies"].items()} == {
        "e1": 100, "e2": 12.5}
    assert result["domain"] == {"choice": "informatica", "confidence": 0.9}
    assert result["subcategory"]["choice"] == "licencias_software"
    assert (result["input_tokens"], result["output_tokens"]) == (1000, 90)
    assert result["cost_usd"] == pytest.approx(1000 * 0.042 / 1_000_000)
    request, body = handler.requests[0]
    assert request.headers["authorization"] == "Bearer test-key"
    assert len(handler.requests) == 1 and result["request_hash"]


def test_request_hash_is_stable_and_follows_the_company_text():
    first, _ = screen()
    again, _ = screen()
    changed = FakeJev().client().screen("Licencias de software",
                                       COMPANIES | {"e2": {"descripcion": "Otra", "lineas": ["X"]}})
    assert first["request_hash"] == again["request_hash"] != changed["request_hash"]


@pytest.mark.parametrize("mutate", [
    lambda data: data["answers"].pop("afinidad_e2"),
    lambda data: data["answers"].update(extra={"type": "choice"}),
    lambda data: data["answers"]["afinidad_e1"].update(score=5),
    lambda data: data["answers"]["afinidad_e1"].update(score="alto"),
    lambda data: data["answers"]["afinidad_e1"].update(score=None),
    lambda data: data["answers"]["subcategoria"].update(choice="inventada"),
    lambda data: data["answers"]["dominio"].update(choice="inventado"),
    lambda data: data["answers"]["dominio"].update(probabilities={"informatica": 0.4}),
    lambda data: data["answers"]["dominio"].update(confidence=1.5),
    lambda data: data.update(model="otro-modelo"),
    lambda data: data["usage"].update(input_tokens=-1),
    lambda data: data.pop("usage"),
])
def test_invalid_responses_raise_a_typed_error(mutate):
    with pytest.raises(JevResponseError):
        screen(mutate=mutate)


def test_http_status_and_transport_failures_are_typed_without_provider_text():
    with pytest.raises(JevHttpError) as caught:
        screen(status=503)
    assert caught.value.status == 503 and "secret" not in str(caught.value)

    def boom(request):
        raise httpx.ConnectError("secret connection detail", request=request)

    client = JevScreeningClient("test-key", client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(JevTransportError) as caught:
        client.screen("Licencias", COMPANIES)
    assert "secret" not in str(caught.value)


def test_non_json_body_is_a_response_error():
    client = JevScreeningClient("test-key", client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))))
    with pytest.raises(JevResponseError):
        client.screen("Licencias", COMPANIES)


def test_api_key_is_required():
    with pytest.raises(ValueError):
        JevScreeningClient("")


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
def companies(db):
    """Two active companies; explicit sorted ids make the first one alias e1."""
    tenant = db.execute("INSERT INTO tenants (name) VALUES ('jev-screening-test') RETURNING id").fetchone()
    ids = sorted(str(uuid4()) for _ in range(2))
    for profile_id, ruc, description, line in zip(
            ids, ("20999999991", "20999999992"), ("Software y nube", "PLC y tableros"),
            ("Licencias", "Automatización")):
        db.execute(
            """INSERT INTO company_profiles (id, tenant_id, ruc, razon_social, identity_json)
               VALUES (%s, %s, %s, 'Jev Test SAC', jsonb_build_object('descripcion', %s::text))""",
            (profile_id, tenant["id"], ruc, description))
        db.execute("""INSERT INTO business_lines (profile_id, nombre, cubso_segmentos, keyword_phrases)
                      VALUES (%s, %s, '{43}', '{x}')""", (profile_id, line))
    return ids


def new_contract_id():
    return random.randint(9_000_000_000, 9_900_000_000)


def add_pending(db, profile_id, id_contrato, *, text="Licencias de software", contract_hash="c1",
                profile_hash=None, attempts=0, **fields):
    record_decision(
        db, item=search_item(id_contrato, text, **fields), segment=43, profile_id=profile_id,
        contract_hash=contract_hash, profile_hash=profile_hash or profile_snapshot(db, profile_id)["profile_hash"],
        policy_version=POLICY_VERSION, prefilter_passed=True, prefilter_reasons=[], keyword_lines=[],
        decision="pending", decided_by="jev")
    if attempts:
        db.execute("UPDATE prod6_profile_decisions SET attempts = %s WHERE id_contrato = %s",
                   (attempts, id_contrato))


def decision(db, id_contrato, profile_id):
    return db.execute("SELECT * FROM prod6_profile_decisions WHERE id_contrato = %s AND profile_id = %s",
                      (id_contrato, profile_id)).fetchone()


def run_score(db, handler, companies, **kwargs):
    kwargs = {"max_requests": 10, "now": NOW} | kwargs
    with handler.client() as client:
        return score_pending(db, client, profile_ids=companies, **kwargs)


@needs_db
def test_one_query_scores_both_companies_and_stores_every_verdict(db, companies):
    contract = new_contract_id()
    for profile_id in companies:
        add_pending(db, profile_id, contract)
    handler = FakeJev(scores={"e1": 3, "e2": 1})  # index 75 and 25 against the default threshold 60
    stats, accepted = run_score(db, handler, companies)
    assert len(handler.requests) == 1
    body = handler.requests[0][1]
    assert body["state"]["empresas"]["e1"]["descripcion"] == "Software y nube"
    assert body["state"]["empresas"]["e2"]["lineas"] == [{"nombre": "Automatización", "palabras_clave": ["x"]}]
    first, second = (decision(db, contract, profile_id) for profile_id in companies)
    assert (first["decision"], first["score"], first["decided_by"], first["threshold"]) == ("accepted", 75, "jev", 60)
    assert (second["decision"], second["score"]) == ("rejected", 25)
    for row in (first, second):
        assert (row["domain"], row["subcategory"], row["information"], row["model"]) == (
            "informatica", "licencias_software", "claro", "jev-1.13.0")
        assert row["attempts"] == 1 and row["last_error"] is None and row["request_hash"]
    # The query cost and tokens are split between the rows without losing any.
    assert first["input_tokens"] + second["input_tokens"] == 1000
    assert first["output_tokens"] + second["output_tokens"] == 90
    assert float(first["cost_usd"]) + float(second["cost_usd"]) == pytest.approx(1000 * 0.042 / 1e6)
    assert first["result_json"]["query_cost_usd"] == pytest.approx(1000 * 0.042 / 1e6)
    assert first["result_json"]["alias"] == "e1" and second["result_json"]["alias"] == "e2"
    assert first["result_json"]["response"]["answers"]["afinidad_e1"]["score"] == 3
    assert "test-key" not in json.dumps(first["result_json"])
    assert (stats["requests"], stats["accepted"], stats["rejected"], stats["failed"]) == (1, 1, 1, 0)
    assert stats["cost_usd"] == pytest.approx(1000 * 0.042 / 1e6)
    assert [(entry["id_contrato"], entry["segment"]) for entry in accepted] == [(contract, 43)]
    assert accepted[0]["listing_json"]["idContrato"] == contract


@needs_db
def test_threshold_is_inclusive_and_configurable(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract)
    run_score(db, FakeJev(scores={"e1": 2.4}), companies, threshold=60)  # index exactly 60
    assert decision(db, contract, companies[0])["decision"] == "accepted"
    other = new_contract_id()
    add_pending(db, companies[0], other)
    run_score(db, FakeJev(scores={"e1": 2.4}), companies, threshold=61)
    row = decision(db, other, companies[0])
    assert (row["decision"], row["threshold"]) == ("rejected", 61)


@needs_db
def test_single_company_group_queries_only_that_company(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[1], contract)
    handler = FakeJev(scores={"e2": 4})
    run_score(db, handler, companies)
    assert list(handler.requests[0][1]["state"]["empresas"]) == ["e2"]
    assert decision(db, contract, companies[1])["score"] == 100


@needs_db
def test_closed_contracts_are_skipped_without_spending(db, companies):
    closed, open_ = new_contract_id(), new_contract_id()
    add_pending(db, companies[0], closed, fecFinCotizacion="01/01/2026 10:00:00")
    add_pending(db, companies[0], open_)
    open_ended = new_contract_id()
    add_pending(db, companies[0], open_ended, fecFinCotizacion=None)
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies)
    assert stats["requests"] == 2 and stats["skipped_closed"] == 1
    row = decision(db, closed, companies[0])
    assert (row["decision"], row["attempts"], row["score"]) == ("pending", 0, None)


@needs_db
def test_obsolete_profile_hash_and_superseded_cards_are_ignored(db, companies):
    stale, old_card, current = new_contract_id(), new_contract_id(), new_contract_id()
    add_pending(db, companies[0], stale, profile_hash="old-profile-hash")
    add_pending(db, companies[0], old_card, contract_hash="old-card")
    add_pending(db, companies[0], old_card, contract_hash="new-card", text="Licencias renovadas")
    add_pending(db, companies[0], current)
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies)
    # The poll already retired the old card; the row of an old profile waits for the poll's new decision.
    assert stats["requests"] == 2 and stats["skipped_stale"] == 1
    assert decision(db, stale, companies[0])["decision"] == "pending"
    statuses = db.execute("SELECT contract_hash, decision FROM prod6_profile_decisions WHERE id_contrato = %s "
                          "ORDER BY id", (old_card,)).fetchall()
    assert [(row["contract_hash"], row["decision"]) for row in statuses] == [
        ("old-card", "superseded"), ("new-card", "accepted")]


def insert_row(db, profile_id, id_contrato, *, contract_hash, profile_hash, decision="pending", decided_by="jev"):
    """A row as an older worker version left it: no supersession at write time."""
    db.execute(
        """INSERT INTO prod6_profile_decisions (id_contrato, profile_id, listing_json, contract_hash, profile_hash,
             policy_version, prefilter_passed, decision, decided_by)
           VALUES (%s, %s, %s::jsonb, %s, %s, %s, true, %s, %s)""",
        (id_contrato, profile_id, json.dumps(search_item(id_contrato).model_dump(mode="json", by_alias=True)),
         contract_hash, profile_hash, POLICY_VERSION, decision, decided_by))


def states(db, id_contrato, profile_id):
    return [(row["contract_hash"], row["profile_hash"], row["decision"]) for row in db.execute(
        "SELECT contract_hash, profile_hash, decision FROM prod6_profile_decisions "
        "WHERE id_contrato = %s AND profile_id = %s ORDER BY id", (id_contrato, profile_id)).fetchall()]


@needs_db
def test_supersede_marks_only_pending_rows_that_have_a_newer_row_and_is_idempotent(db, companies):
    a, b, c, d = (new_contract_id() for _ in range(4))
    me, other = companies
    # a: new card (contract_hash) and new profile (profile_hash) after two stale pending rows.
    insert_row(db, me, a, contract_hash="c1", profile_hash="p1")
    insert_row(db, me, a, contract_hash="c2", profile_hash="p1")
    insert_row(db, me, a, contract_hash="c2", profile_hash="p2")
    # b: history never changes: accepted/rejected rows stay even with a newer row.
    insert_row(db, me, b, contract_hash="c1", profile_hash="p1", decision="accepted")
    insert_row(db, me, b, contract_hash="c2", profile_hash="p1", decision="rejected")
    insert_row(db, me, b, contract_hash="c3", profile_hash="p1", decision="pending")
    # c: a single pending row, and the same contract for another company, are untouched.
    insert_row(db, me, c, contract_hash="c1", profile_hash="p1")
    insert_row(db, other, c, contract_hash="c1", profile_hash="p1")
    insert_row(db, other, c, contract_hash="c9", profile_hash="p1")
    # d: a newer row of another policy does not retire the pending one.
    insert_row(db, me, d, contract_hash="c1", profile_hash="p1")
    db.execute("""INSERT INTO prod6_profile_decisions (id_contrato, profile_id, listing_json, contract_hash,
                    profile_hash, policy_version, prefilter_passed, decision, decided_by)
                  VALUES (%s, %s, '{}'::jsonb, 'c2', 'p1', 'otra_politica', true, 'pending', 'jev')""", (d, me))

    assert supersede_pending(db, profile_ids=companies) == 3
    assert states(db, a, me) == [("c1", "p1", "superseded"), ("c2", "p1", "superseded"), ("c2", "p2", "pending")]
    assert states(db, b, me) == [("c1", "p1", "accepted"), ("c2", "p1", "rejected"), ("c3", "p1", "pending")]
    assert states(db, c, me) == [("c1", "p1", "pending")]
    assert states(db, c, other) == [("c1", "p1", "superseded"), ("c9", "p1", "pending")]
    assert states(db, d, me)[0] == ("c1", "p1", "pending")
    before = db.execute("SELECT id, decision, updated_at FROM prod6_profile_decisions "
                        "WHERE id_contrato = ANY(%s) ORDER BY id", ([a, b, c, d],)).fetchall()
    assert supersede_pending(db, profile_ids=companies) == 0
    assert db.execute("SELECT id, decision, updated_at FROM prod6_profile_decisions "
                      "WHERE id_contrato = ANY(%s) ORDER BY id", ([a, b, c, d],)).fetchall() == before
    assert supersede_pending(db, profile_ids=[]) == 0


@needs_db
def test_score_pending_retires_legacy_obsolete_pending_rows_and_never_scores_them(db, companies):
    legacy, fresh = new_contract_id(), new_contract_id()
    current_hash = profile_snapshot(db, companies[0])["profile_hash"]
    insert_row(db, companies[0], legacy, contract_hash="old", profile_hash=current_hash)
    insert_row(db, companies[0], legacy, contract_hash="new", profile_hash=current_hash,
               decision="rejected", decided_by="prefilter")
    add_pending(db, companies[0], fresh)
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies)
    assert (stats["superseded"], stats["requests"], stats["skipped_stale"]) == (1, 1, 0)
    assert [row[2] for row in states(db, legacy, companies[0])] == ["superseded", "rejected"]
    assert decision(db, fresh, companies[0])["decision"] == "accepted"
    again, _ = run_score(db, handler, companies)
    assert (again["superseded"], again["requests"]) == (0, 0)


@needs_db
def test_recording_a_new_decision_retires_older_pending_rows_and_never_touches_history(db, companies):
    contract, history = new_contract_id(), new_contract_id()
    me = companies[0]
    profile_hash = profile_snapshot(db, me)["profile_hash"]
    add_pending(db, me, contract, contract_hash="c1")
    add_pending(db, companies[1], contract, contract_hash="c1")
    add_pending(db, me, contract, contract_hash="c2")
    assert states(db, contract, me) == [("c1", profile_hash, "superseded"), ("c2", profile_hash, "pending")]
    assert states(db, contract, companies[1])[0][2] == "pending", "another company is not affected"
    # An accepted row stays accepted when a newer card arrives.
    add_pending(db, me, history, contract_hash="c1")
    db.execute("UPDATE prod6_profile_decisions SET decision = 'accepted' WHERE id_contrato = %s", (history,))
    add_pending(db, me, history, contract_hash="c2")
    assert [row[2] for row in states(db, history, me)] == ["accepted", "pending"]
    # A repeated decision is reused and changes nothing.
    assert record_decision(
        db, item=search_item(contract), segment=43, profile_id=me, contract_hash="c2", profile_hash=profile_hash,
        policy_version=POLICY_VERSION, prefilter_passed=True, prefilter_reasons=[], keyword_lines=[],
        decision="pending", decided_by="jev") is False
    assert [row[2] for row in states(db, contract, me)] == ["superseded", "pending"]


@needs_db
def test_a_superseded_row_that_becomes_current_again_is_revived(db, companies):
    contract = new_contract_id()
    me = companies[0]
    add_pending(db, me, contract, contract_hash="c1")
    add_pending(db, me, contract, contract_hash="c2")  # c1 superseded
    db.execute("UPDATE prod6_profile_decisions SET attempts = 3, last_error = 'JevHttpError status=503' "
               "WHERE id_contrato = %s AND contract_hash = 'c1'", (contract,))
    revived = record_decision(
        db, item=search_item(contract), segment=43, profile_id=me, contract_hash="c1",
        profile_hash=profile_snapshot(db, me)["profile_hash"], policy_version=POLICY_VERSION,
        prefilter_passed=True, prefilter_reasons=[], keyword_lines=[], decision="pending", decided_by="jev")
    assert revived is True
    assert [row[2] for row in states(db, contract, me)] == ["pending", "superseded"]
    row = db.execute("SELECT attempts, last_error FROM prod6_profile_decisions WHERE id_contrato = %s "
                     "AND contract_hash = 'c1'", (contract,)).fetchone()
    assert (row["attempts"], row["last_error"]) == (0, None)
    # The sweep does not flip it back: the newer row is itself superseded.
    assert supersede_pending(db, profile_ids=companies) == 0
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies)
    assert stats["requests"] == 1 and stats["accepted"] == 1


@needs_db
def test_profile_hash_and_company_payload_follow_the_line_keywords(db, companies):
    me = companies[0]
    before = profile_snapshot(db, me)
    db.execute("UPDATE business_lines SET keyword_terms = '{nube,antivirus}' WHERE profile_id = %s", (me,))
    after = profile_snapshot(db, me)
    assert before["profile_hash"] != after["profile_hash"]
    assert company_payload(before) != company_payload(after)
    assert company_payload(after)["lineas"] == [{"nombre": "Licencias", "palabras_clave": ["x", "antivirus", "nube"]}]


@needs_db
def test_migration_0049_is_reapplicable_and_admits_superseded_without_touching_rows(db):
    migration = (Path(__file__).resolve().parents[3] / "db" / "migrations" / "0049_prod6_decision_superseded.sql")
    before = db.execute("SELECT count(*) AS n, count(*) FILTER (WHERE decision = 'pending') AS p "
                        "FROM prod6_profile_decisions").fetchone()
    for _ in range(2):
        db.execute(migration.read_text(encoding="utf-8"))
    checks = db.execute(
        """SELECT pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
           WHERE c.conrelid = 'prod6_profile_decisions'::regclass AND c.contype = 'c' AND a.attname = 'decision'"""
    ).fetchall()
    assert len(checks) == 1 and "superseded" in checks[0]["definition"]
    assert db.execute("SELECT count(*) AS n, count(*) FILTER (WHERE decision = 'pending') AS p "
                      "FROM prod6_profile_decisions").fetchone() == before
    with pytest.raises(Exception):
        with db.transaction():
            db.execute("""INSERT INTO prod6_profile_decisions (id_contrato, profile_id, listing_json, contract_hash,
                            profile_hash, policy_version, prefilter_passed, decision, decided_by)
                          SELECT 1, id, '{}'::jsonb, 'x', 'x', 'x', true, 'inventada', 'jev'
                          FROM company_profiles LIMIT 1""")


@needs_db
def test_changed_profile_is_scored_only_after_the_poll_redecides_it(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract)
    db.execute("UPDATE company_profiles SET identity_json = jsonb_build_object('descripcion', 'Nueva') "
               "WHERE id = %s", (companies[0],))
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies)
    assert (stats["requests"], stats["skipped_stale"]) == (0, 1) and not handler.requests


@needs_db
def test_http_failure_keeps_rows_pending_and_counts_the_attempt(db, companies):
    contract = new_contract_id()
    for profile_id in companies:
        add_pending(db, profile_id, contract)
    stats, accepted = run_score(db, FakeJev(status=503), companies)
    assert (stats["failed"], stats["accepted"], stats["rejected"], accepted) == (2, 0, 0, [])
    for profile_id in companies:
        row = decision(db, contract, profile_id)
        assert (row["decision"], row["attempts"], row["score"]) == ("pending", 1, None)
        assert row["last_error"] == "JevHttpError status=503" and "secret" not in row["last_error"]


@needs_db
def test_invalid_response_is_not_a_rejection(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract)
    run_score(db, FakeJev(scores={"e1": 3}, mutate=lambda data: data["answers"].pop("dominio")), companies)
    row = decision(db, contract, companies[0])
    assert (row["decision"], row["attempts"], row["last_error"]) == ("pending", 1, "JevResponseError")


@needs_db
def test_retry_after_failure_succeeds_and_clears_the_error(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract)
    run_score(db, FakeJev(status=500), companies)
    run_score(db, FakeJev(scores={"e1": 3}), companies)
    row = decision(db, contract, companies[0])
    assert (row["decision"], row["attempts"], row["last_error"]) == ("accepted", 2, None)


@needs_db
def test_max_attempts_stops_retrying(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract, attempts=5)
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies, max_attempts=5)
    assert (stats["requests"], stats["exhausted"]) == (0, 1) and not handler.requests
    assert decision(db, contract, companies[0])["decision"] == "pending"
    run_score(db, handler, companies, max_attempts=6)
    assert decision(db, contract, companies[0])["decision"] == "accepted"


@needs_db
def test_max_requests_bounds_the_pass_and_the_rest_waits(db, companies):
    ids = [new_contract_id() for _ in range(3)]
    for contract in ids:
        add_pending(db, companies[0], contract)
    handler = FakeJev(scores={"e1": 3})
    stats, _ = run_score(db, handler, companies, max_requests=2)
    assert stats["requests"] == len(handler.requests) == 2
    pending = db.execute("SELECT count(*) AS n FROM prod6_profile_decisions WHERE id_contrato = ANY(%s) "
                         "AND decision = 'pending'", (ids,)).fetchone()["n"]
    assert pending == 1
    assert run_score(db, handler, companies, max_requests=0)[0]["requests"] == 0


@needs_db
def test_second_pass_is_idempotent(db, companies):
    contract = new_contract_id()
    for profile_id in companies:
        add_pending(db, profile_id, contract)
    handler = FakeJev()
    run_score(db, handler, companies)
    before = [dict(decision(db, contract, profile_id)) for profile_id in companies]
    stats, accepted = run_score(db, handler, companies)
    assert len(handler.requests) == 1 and stats["requests"] == 0 and accepted == []
    assert [dict(decision(db, contract, profile_id)) for profile_id in companies] == before


@needs_db
def test_repeated_failures_abort_the_pass_before_burning_every_attempt(db, companies):
    ids = [new_contract_id() for _ in range(6)]
    for contract in ids:
        add_pending(db, companies[0], contract)
    handler = FakeJev(status=502)
    stats, _ = run_score(db, handler, companies)
    assert stats["aborted"] and stats["requests"] == len(handler.requests) == 3
    attempts = db.execute("SELECT attempts FROM prod6_profile_decisions WHERE id_contrato = ANY(%s) "
                          "ORDER BY id_contrato", (ids,)).fetchall()
    assert sorted(row["attempts"] for row in attempts) == [0, 0, 0, 1, 1, 1]


@needs_db
def test_a_success_resets_the_failure_streak(db, companies):
    ids = [new_contract_id() for _ in range(5)]
    for contract in ids:
        add_pending(db, companies[0], contract)
    outcomes = iter([503, 503, 200, 503, 503])
    handler = FakeJev(scores={"e1": 3})

    def alternating(request):
        status = next(outcomes)
        handler.status = status
        return handler(request)

    client = JevScreeningClient("test-key", client=httpx.Client(transport=httpx.MockTransport(alternating)))
    stats, _ = score_pending(db, client, profile_ids=companies, max_requests=10, now=NOW)
    assert not stats["aborted"] and stats["requests"] == 5 and (stats["accepted"], stats["failed"]) == (1, 4)


@needs_db
def test_only_the_requested_companies_are_scored(db, companies):
    contract = new_contract_id()
    for profile_id in companies:
        add_pending(db, profile_id, contract)
    handler = FakeJev(scores={"e1": 3})
    run_score(db, handler, companies[:1])
    assert list(handler.requests[0][1]["state"]["empresas"]) == ["e1"]
    assert decision(db, contract, companies[1])["decision"] == "pending"


def opportunity_count(db):
    return db.execute("SELECT count(*) AS n FROM opportunities").fetchone()["n"]


@needs_db
def test_accepted_by_at_least_one_company_is_admitted_once(db, companies):
    contract = new_contract_id()
    for profile_id in companies:
        add_pending(db, profile_id, contract)
    opportunities_before = opportunity_count(db)
    # Both companies accept: still a single admission and a single queued reading.
    _, accepted = run_score(db, FakeJev(scores={"e1": 4, "e2": 4}), companies)
    repo = JobRepository(db)
    stats = admit_accepted(repo, accepted * 2, batch_id="batch-test")
    assert stats == {"admitted": 1, "changed": 1, "enqueued": 1}
    row = db.execute("SELECT opportunity_id, cubso_segmento, anio FROM seace_contracts WHERE id_contrato = %s",
                     (contract,)).fetchone()
    assert row["opportunity_id"] and (row["cubso_segmento"], row["anio"]) == ("43", 2026)
    assert opportunity_count(db) == opportunities_before + 1
    jobs = db.execute("SELECT payload FROM worker_jobs WHERE job_type = 'process_contract' "
                      "AND (payload->>'id_contrato')::bigint = %s", (contract,)).fetchall()
    assert len(jobs) == 1 and jobs[0]["payload"]["segment"] == 43
    # Admitting it again neither duplicates the contract nor the job.
    again = admit_accepted(repo, accepted, batch_id="batch-test")
    assert again["changed"] == 0
    assert db.execute("SELECT count(*) AS n FROM worker_jobs WHERE job_type = 'process_contract' "
                      "AND (payload->>'id_contrato')::bigint = %s", (contract,)).fetchone()["n"] == 1


@needs_db
def test_accepted_by_one_company_only_is_admitted_and_rejected_by_both_is_not(db, companies):
    one, none = new_contract_id(), new_contract_id()
    for contract in (one, none):
        for profile_id in companies:
            add_pending(db, profile_id, contract, text=f"Compra {contract}")

    def by_text(request):
        body = json.loads(request.content)
        scores = {"e1": 4, "e2": 0} if str(one) in body["state"]["descripcion"] else {"e1": 1, "e2": 0}
        return httpx.Response(200, json=jev_answer(body, scores))

    client = JevScreeningClient("test-key", client=httpx.Client(transport=httpx.MockTransport(by_text)))
    opportunities_before = opportunity_count(db)
    stats, accepted = score_pending(db, client, profile_ids=companies, max_requests=10, now=NOW)
    assert (stats["accepted"], stats["rejected"]) == (1, 3)
    admit_accepted(JobRepository(db), accepted, batch_id="batch-test")
    ids = db.execute("SELECT id_contrato FROM seace_contracts WHERE id_contrato = ANY(%s)",
                     ([one, none],)).fetchall()
    assert [row["id_contrato"] for row in ids] == [one]
    assert opportunity_count(db) == opportunities_before + 1
    assert db.execute("SELECT count(*) AS n FROM worker_jobs WHERE job_type = 'process_contract' "
                      "AND (payload->>'id_contrato')::bigint = ANY(%s)", ([one, none],)).fetchone()["n"] == 1


@needs_db
def test_accepted_but_unadmitted_contracts_are_recovered_after_a_crash(db, companies):
    contract = new_contract_id()
    add_pending(db, companies[0], contract)
    run_score(db, FakeJev(scores={"e1": 4}), companies)  # accepted, but nobody admitted it
    recovered = unadmitted_accepted(db, profile_ids=companies, now=NOW)
    assert [entry["id_contrato"] for entry in recovered] == [contract]
    admit_accepted(JobRepository(db), recovered, batch_id="batch-test")
    assert unadmitted_accepted(db, profile_ids=companies, now=NOW) == []


def test_module_reuses_the_prod2_jev_contract():
    from buenapro_worker.prod2 import analysis
    assert jev_screening.DOMAIN is analysis.DOMAIN and jev_screening.AFFINITY is analysis.AFFINITY
    assert jev_screening.JEV_MODEL == analysis.JEV_MODEL
