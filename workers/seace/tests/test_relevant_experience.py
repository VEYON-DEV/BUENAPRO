"""Which experiences LLM2 sees: the closest to the contract, complete. Offline."""
from buenapro_worker.matching.compact_context import MATCH_EXPERIENCE_LIMIT, select_relevant_experience


def experience(objeto, *, anio=2020, rubro="", summary=""):
    item = {"objeto": objeto, "rubro": rubro, "anio": anio}
    if summary:
        item["documentary_summary"] = {"sources": [{"document_id": objeto, "extraction": {"summary": summary}}]}
    return item


def profile(experiences, areas=()):
    return {"razon_social": "DIN", "experience_json": list(experiences),
            "econ_experience_json": {"areas": list(areas), "no_global_sum": True}}


OPPORTUNITY = {"descripcion": "Mantenimiento preventivo de tableros eléctricos y variadores", "resumen": {}}
REQUIREMENTS = [{"requisito": "Experiencia en mantenimiento de variadores de velocidad"}]


def test_a_profile_within_the_limit_is_sent_exactly_as_before():
    small = profile([experience(f"Obra {n}") for n in range(MATCH_EXPERIENCE_LIMIT)])
    assert select_relevant_experience(small, OPPORTUNITY, REQUIREMENTS) is small


def test_the_closest_experiences_are_kept_complete_and_in_their_order():
    unrelated = [experience(f"Capacitación docente {n}", rubro="EDUCACION") for n in range(9)]
    close = experience("Mantenimiento de variadores de velocidad", anio=2019)
    by_document = experience("Servicio industrial", summary="Mantenimiento preventivo de tableros eléctricos")
    full = profile([*unrelated[:4], close, *unrelated[4:], by_document])
    selected = select_relevant_experience(full, OPPORTUNITY, REQUIREMENTS, limit=3)
    objects = [item["objeto"] for item in selected["experience_json"]]
    assert len(objects) == 3 and objects.index(close["objeto"]) < objects.index(by_document["objeto"])
    assert by_document in selected["experience_json"] and close in selected["experience_json"]
    assert selected["experience_note"] == ("Se incluyen las 3 experiencias más relacionadas con esta contratación "
                                           "de 11 registradas por la empresa.")
    assert full["experience_json"][4] is close  # The stored profile is not changed.


def test_object_words_weigh_more_and_a_tie_goes_to_the_most_recent_year():
    old = experience("Mantenimiento de variadores", anio=2015)
    new = experience("Mantenimiento de variadores", anio=2023)
    documents_only = experience("Proyecto", summary="variadores mantenimiento tableros")
    full = profile([old, documents_only, new, *[experience(f"Aula {n}") for n in range(8)]])
    selected = select_relevant_experience(full, OPPORTUNITY, REQUIREMENTS, limit=1)
    assert selected["experience_json"] == [new]


def test_generic_procurement_words_do_not_select_an_experience():
    generic = experience("Servicio de contratación y adquisición de bienes en general")
    relevant = experience("Tableros eléctricos")
    full = profile([generic, relevant, *[experience(f"Aula {n}") for n in range(8)]])
    opportunity = {"descripcion": "Servicio de adquisición de tableros eléctricos", "resumen": {}}
    assert select_relevant_experience(full, opportunity, [], limit=1)["experience_json"] == [relevant]


def test_economic_areas_follow_their_experience():
    experiences = [experience(f"Aula {n}") for n in range(9)] + [experience("Mantenimiento de variadores")]
    areas = [{"monto": "100", "source_record_index": 9}, {"monto": "50", "source_record_index": 0},
             {"monto": "10"}]
    selected = select_relevant_experience(profile(experiences, areas), OPPORTUNITY, REQUIREMENTS, limit=2)
    position = selected["experience_json"].index(experiences[9])
    assert [area.get("source_record_index") for area in selected["econ_experience_json"]["areas"]] == [
        position, 0 if experiences[0] in selected["experience_json"] else None, None]
    assert selected["econ_experience_json"]["no_global_sum"] is True


def test_the_selection_is_deterministic():
    full = profile([experience(f"Mantenimiento {n}", anio=2020) for n in range(12)])
    first = select_relevant_experience(full, OPPORTUNITY, REQUIREMENTS)
    assert first == select_relevant_experience(full, OPPORTUNITY, REQUIREMENTS)
    assert len(first["experience_json"]) == MATCH_EXPERIENCE_LIMIT
