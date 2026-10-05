from buenapro_worker.matching.profile_evidence import evidence_only_profile, guard_participation_actions
from buenapro_worker.jobs.analyze_match import _apply_econ_rule, _econ_capacity


def test_demo_templates_do_not_become_accredited_capacity():
    profile = {
        "identity_json": {"legal_name": "Dinaut", "demo_requested_experience_pen": 5_000_000},
        "econ_experience_json": {},
        "hireable_roles_json": ["DEMO · Ingeniero PLC", "Electricista"],
        "team_json": [{"nombre": "DEMO · Supervisor"}, {"nombre": "Ana", "verified": False},
                      {"nombre": "Real", "experience": [{"status": "demo"}, {"years": 3}]}],
        "experience_json": [{"verification_status": "public_record_not_accredited", "monto": 9}],
    }
    cleaned = evidence_only_profile(profile)
    assert cleaned["identity_json"] == {"legal_name": "Dinaut"}
    assert cleaned["econ_experience_json"] == {}
    assert cleaned["hireable_roles_json"] == ["Electricista"]
    assert cleaned["team_json"] == [{"nombre": "Real", "experience": [{"years": 3}]}]
    assert cleaned["experience_json"] == []
    assert "demo_requested_experience_pen" in profile["identity_json"]


def test_legacy_self_reported_capacity_is_not_deleted():
    profile = {"econ_experience_json": {"servicios": 24_000}, "certifications_json": ["ISO 9001"]}
    assert evidence_only_profile(profile) == profile


def test_area_capacity_does_not_use_unrelated_global_amount_or_sum_currencies():
    profile = {"econ_experience_json": {"servicios": 9_000_000, "areas": [
        {"rubro": "Software", "monto": 200_000, "moneda": "PEN"},
        {"rubro": "Servidores", "monto": 400_000, "moneda": "USD"},
    ]}}
    assert _econ_capacity(profile) is None
    assert _econ_capacity({"econ_experience_json": {"servicios": 24_000, "areas": []}}) == 24_000
    assert _econ_capacity({"econ_experience_json": {"servicios": 24_000}}) == 24_000


def test_prod6_areas_require_compatibility_review_regardless_of_model_state():
    for estado in ("cumple", "no_cumple", "cumple_con_accion"):
        req = [{"categoria": "experiencia_economica", "estado": estado},
               {"categoria": "legal", "estado": "cumple"}]
        _apply_econ_rule(req, exigido=100, capacidad=None, consorcio_status="permitted")
        assert req[0]["estado"] == "requiere_revision"
        assert "especialidad, moneda y respaldo" in req[0]["accion"]
        assert req[1]["estado"] == "cumple"


def test_area_review_does_not_depend_on_extracted_numeric_minimum():
    req = [{"categoria": "experiencia_economica", "estado": "cumple"}]
    _apply_econ_rule(req, exigido=None, capacidad=None)
    assert req[0]["estado"] == "requiere_revision"


def test_unknown_consortium_does_not_generate_an_executable_economic_action():
    req = [{"categoria": "experiencia_economica", "estado": "cumple"}]
    _apply_econ_rule(req, exigido=100, capacidad=40)
    assert req[0]["estado"] == "requiere_revision"
    assert "Formar consorcio" not in req[0]["accion"]


def test_prohibited_consortium_does_not_generate_an_economic_action():
    req = [{"categoria": "experiencia_economica", "estado": "cumple"}]
    _apply_econ_rule(req, exigido=100, capacidad=40, consorcio_status="prohibited")
    assert req[0]["estado"] == "no_cumple"
    assert "consorcio" not in req[0]["accion"]


def test_prohibited_or_conditional_actions_are_guarded_for_both_sources():
    req = [{"estado": "cumple_con_accion", "accion": "Formar consorcio con socio"},
           {"estado": "cumple_con_accion", "accion": "Subcontratar instalación"}]
    actions = guard_participation_actions(req, ["Formar consorcio", "Subcontratar instalación"], {
        "participation": {"consorcio": {"status": "prohibited"},
                          "subcontratacion": {"status": "conditional"}}})
    assert all(item["estado"] == "requiere_revision" for item in req)
    assert actions == ["Buscar alternativa permitida por las bases", "Verificar autorización y condiciones en las bases"]


def test_explicitly_permitted_action_remains():
    req = [{"estado": "cumple_con_accion", "accion": "Formar consorcio"}]
    actions = guard_participation_actions(req, ["Formar consorcio"], {"participation": {"consorcio": {"status": "permitted"}}})
    assert req[0]["estado"] == "cumple_con_accion"
    assert actions == ["Formar consorcio"]


def test_prod4_routing_can_target_one_tenant_profile():
    from unittest.mock import MagicMock
    from buenapro_worker.jobs.prod4_match import route_prod4_profiles_job
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = []
    result = route_prod4_profiles_job(MagicMock(), repo, id_procedimiento=123,
                                     extraction_id=456, profile_id="dinaut-profile")
    sql, args = repo.conn.execute.call_args.args
    assert "cp.id = %s::uuid" in sql
    assert args == (456, "dinaut-profile", "dinaut-profile", 123)
    assert result["enqueued"] == 0
