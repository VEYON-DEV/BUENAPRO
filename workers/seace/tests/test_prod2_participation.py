from copy import deepcopy
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from buenapro_worker.prod2.participation import POLICY_VERSION, STATES, decide


LIMA = ZoneInfo("America/Lima")


def instant(day=5, hour=12, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=LIMA)


def row(stage, start, end):
    return {"stage": stage, "starts_at_text": start, "ends_at_text": end}


def ficha():
    return {
        "procedure_id": 123, "convocatoria_id": 456,
        "selection_type": "Concurso Público", "item_states": ["Convocado"],
        "schedule": [
            row("Convocatoria", "02/10/2026", "02/10/2026"),
            row("Registro de participantes(Electronica)", "03/10/2026 00:01", "14/10/2026 23:59"),
            row("Presentación de propuestas(Electronica)", "15/10/2026 00:01", "15/10/2026 23:59"),
            row("Otorgamiento de la Buena Pro", "19/10/2026 08:30", "19/10/2026"),
        ],
    }


def test_registration_open_is_only_a_window_indicator_and_preserves_ficha():
    detail = ficha()
    before = deepcopy(detail)
    result = decide(detail, instant())
    assert detail == before
    assert result["state"] == "registration_open"
    assert result["confirmed_open"] is True
    assert result["can_new_company_register"] is True
    assert result["can_registered_company_submit"] is False
    assert result["registered_company_submission"] == "pending"
    assert result["eligibility_verified"] is False
    assert result["item_states_coverage_verified"] is False
    assert result["snapshot"]["schedule"] == before["schedule"]
    assert result["evidence"]["schedule"][0]["start"]["precision"] == "day"
    assert result["evidence"]["schedule"][0]["start"]["at"] is None
    assert result["policy_version"] == POLICY_VERSION
    assert "vigente" not in result
    json.dumps(result)


@pytest.mark.parametrize("at,expected,new,registered", [
    (instant(2), "pending_registration", False, False),
    (instant(5), "registration_open", True, False),
    (instant(15, 0, 0), "registered_only", False, False),
    (instant(15), "submission_open", False, True),
    (instant(16), "closed", False, False),
])
def test_distinguishes_new_enrollment_from_already_registered_supplier(at, expected, new, registered):
    result = decide(ficha(), at)
    assert result["state"] == expected
    assert result["can_new_company_register"] is new
    assert result["can_registered_company_submit"] is registered
    assert result["registered_submission_requires_prior_registration"] is True


def test_submission_and_enrollment_can_overlap_with_separate_flags():
    detail = ficha()
    detail["schedule"][1]["ends_at_text"] = "15/10/2026 23:00"
    result = decide(detail, instant(15))
    assert result["state"] == "submission_open"
    assert result["can_new_company_register"] is True
    assert result["can_registered_company_submit"] is True


@pytest.mark.parametrize("state,expected", [
    ("Adjudicado", "adjudicado"), ("Consentido", "adjudicado"),
    ("Cancelado", "cancelado"), ("Desierto", "desierto"),
    ("Suspendido", "suspendido"), ("Contratado", "closed"),
])
def test_explicit_item_states_override_apparently_open_schedule(state, expected):
    detail = ficha()
    detail["item_states"] = [state]
    result = decide(detail, instant())
    assert result["state"] == expected
    assert result["confirmed_open"] is False
    assert result["can_new_company_register"] is False
    assert "explicit_item_state" in result["reason_codes"]


@pytest.mark.parametrize("states", [
    [], None, [""], ["Otro"], ["Convocado", "Desierto"],
    ["Adjudicado", "Cancelado"], ["Convocado", 1],
])
def test_missing_unknown_and_mixed_items_never_grant_participation(states):
    detail = ficha()
    detail["item_states"] = states
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert result["can_new_company_register"] is None
    assert result["can_registered_company_submit"] is None
    assert result["confirmed_open"] is False


def test_convocado_without_proposal_stage_does_not_mean_open():
    detail = ficha()
    detail["schedule"] = detail["schedule"][:2]
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert "submission_stage_missing" in result["reason_codes"]
    assert result["evidence"]["windows"]["registration"]["status"] == "open"
    assert result["can_new_company_register"] is None


@pytest.mark.parametrize("selection", ["Contratación Directa", "Selección de Consultores Individuales",
                                      "Procedimiento por invitación", None])
def test_company_or_public_access_cannot_be_inferred_from_alternate_modality(selection):
    detail = ficha()
    detail["selection_type"] = selection
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert result["requires_review"] is True
    assert result["confirmed_open"] is False


def test_invitation_with_open_proposals_does_not_open_new_company_access():
    detail = ficha()
    detail["schedule"] = [
        row("Invitación CORREO ELECTRÓNICO", "02/10/2026", "02/10/2026"),
        row("Presentación de propuestas(Electronica)", "05/10/2026 00:01", "05/10/2026 23:59"),
    ]
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert "participation_modality_requires_review" in result["reason_codes"]
    assert result["evidence"]["windows"]["submission"]["status"] == "open"
    assert result["can_registered_company_submit"] is None


@pytest.mark.parametrize("at,expected", [(instant(2), "pending_registration"),
                                         (instant(5), "unknown"), (instant(16), "closed")])
def test_day_only_dates_preserve_precision_and_do_not_invent_open_hours(at, expected):
    detail = ficha()
    detail["schedule"][1].update(starts_at_text="03/10/2026", ends_at_text="14/10/2026")
    detail["schedule"][2].update(starts_at_text="15/10/2026", ends_at_text="15/10/2026")
    result = decide(detail, at)
    assert result["state"] == expected
    assert result["confirmed_open"] is False
    for window in result["evidence"]["windows"].values():
        assert window["start"]["at"] is None
        assert window["end"]["at"] is None


def test_day_only_current_deadline_is_unknown_even_if_repository_has_midnight_field():
    detail = ficha()
    detail["schedule"][1].update(ends_at_text="05/10/2026", ends_at="2026-10-05T00:00:00-05:00")
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert result["evidence"]["windows"]["registration"]["end"]["at"] is None
    assert result["snapshot"]["schedule"][1]["ends_at"] == "2026-10-05T00:00:00-05:00"


@pytest.mark.parametrize("at,expected", [
    (instant(3, 0, 1, 30), "unknown"),
    (instant(3, 0, 2), "registration_open"),
    (instant(14, 23, 59, 30), "unknown"),
    (instant(15, 0, 0), "registered_only"),
])
def test_minute_precision_boundary_is_uncertain_until_unambiguous(at, expected):
    assert decide(ficha(), at)["state"] == expected


def test_second_precision_boundaries_use_explicit_seconds():
    detail = ficha()
    detail["schedule"][1].update(starts_at_text="03/10/2026 00:01:00", ends_at_text="14/10/2026 23:59:00")
    assert decide(detail, instant(3, 0, 1))["state"] == "registration_open"
    assert decide(detail, instant(14, 23, 59))["state"] == "registered_only"


@pytest.mark.parametrize("mutation", ["conflicting_registration", "conflicting_submission", "revision",
                                       "bad_date", "reversed", "zero_window", "missing_start", "incoherent"])
def test_invalid_or_reprogrammed_windows_do_not_grant_access(mutation):
    detail = ficha()
    if mutation.startswith("conflicting_"):
        index = 1 if mutation.endswith("registration") else 2
        another = deepcopy(detail["schedule"][index])
        another["ends_at_text"] = "16/10/2026 23:59"
        detail["schedule"].append(another)
    elif mutation == "revision":
        detail["schedule"][1]["stage"] += " REPROGRAMADO"
    elif mutation == "bad_date":
        detail["schedule"][1]["starts_at_text"] = "31/09/2026 00:01"
    elif mutation == "reversed":
        detail["schedule"][1]["starts_at_text"] = "16/10/2026 00:01"
    elif mutation == "zero_window":
        detail["schedule"][1]["starts_at_text"] = detail["schedule"][1]["ends_at_text"]
    elif mutation == "missing_start":
        detail["schedule"][1].pop("starts_at_text")
    else:
        detail["schedule"][1]["ends_at_text"] = "16/10/2026 23:59"
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert result["confirmed_open"] is False
    assert result["can_new_company_register"] is None


def test_identical_duplicates_do_not_change_window_but_evidence_keeps_both_rows():
    detail = ficha()
    detail["schedule"].append(deepcopy(detail["schedule"][1]))
    result = decide(detail, instant())
    assert result["state"] == "registration_open"
    assert result["evidence"]["windows"]["registration"]["row_indices"] == [1, 4]


def test_expired_buena_pro_date_does_not_invent_award_or_close_submission():
    detail = ficha()
    detail["schedule"][3].update(starts_at_text="01/10/2026", ends_at_text="01/10/2026")
    assert decide(detail, instant())["state"] == "registration_open"


def test_missing_registration_can_only_establish_already_expired_submission():
    detail = ficha()
    detail["schedule"] = [detail["schedule"][2]]
    assert decide(detail, instant(15))["state"] == "unknown"
    result = decide(detail, instant(16))
    assert result["state"] == "closed"
    assert "submission_window_ended" in result["reason_codes"]


def test_timezone_normalization_deterministic_hashes_and_no_ambient_clock():
    detail = ficha()
    first = decide(detail, instant())
    utc = instant().astimezone(timezone.utc)
    assert first == decide(detail, utc)
    second = decide(detail, instant(5, 13))
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["assessment_sha256"] != second["assessment_sha256"]
    detail["schedule"][1]["ends_at_text"] = "13/10/2026 23:59"
    assert first["snapshot_sha256"] != decide(detail, instant())["snapshot_sha256"]


@pytest.mark.parametrize("schedule", [None, "invalid", [{"starts_at_text": "03/10/2026"}]])
def test_malformed_schedule_is_retained_without_granting_access(schedule):
    detail = ficha()
    detail["schedule"] = schedule
    result = decide(detail, instant())
    assert result["state"] == "unknown"
    assert result["snapshot"]["schedule"] == schedule
    assert result["confirmed_open"] is False


def test_past_submission_with_later_registration_is_conflicting_not_closed_or_open():
    detail = ficha()
    detail["schedule"][2].update(starts_at_text="01/10/2026 00:01", ends_at_text="01/10/2026 23:59")
    assert decide(detail, instant())["state"] == "unknown"


@pytest.mark.parametrize("as_of", [None, "2026-10-05", datetime(2026, 10, 5)])
def test_requires_explicit_timezone_aware_as_of(as_of):
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(ficha(), as_of)


def test_contract_state_allowlist_is_exact():
    assert STATES == {
        "closed", "adjudicado", "cancelado", "desierto", "suspendido", "unknown",
        "pending_registration", "registration_open", "registered_only", "submission_open",
    }
