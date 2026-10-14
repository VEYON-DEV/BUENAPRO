"""Penalty caps must not come from availability percentages or daily rates."""
from copy import deepcopy

import pytest

from buenapro_worker.normalization.facets import derive_facets, derive_summary


def cap(penalties):
    return derive_summary({"penalties": penalties})["penalidad_tope_pct"]


def test_structured_caps_take_priority_over_uptime_example_and_preserve_raw():
    raw = {"penalties": [
        {"tipo": "mora", "descripcion": "Penalidad por mora hasta un máximo del 10%.", "tope_pct": 10.0},
        {"tipo": "otra", "descripcion": (
            "Tiempo de atención máximo: cuatro horas. UPTIME de ejemplo = 99.0%. "
            "Disponibilidad >= 99.0%, < 99.5%: penalidad 0.50%. "
            "La suma de penalidades no debe exceder el 10% del contrato."
        ), "tope_pct": 10.0},
    ]}
    original, facets = deepcopy(raw), derive_facets(raw)
    assert derive_summary(raw)["penalidad_tope_pct"] == 10.0
    assert derive_facets(raw) == facets and raw == original


def test_daily_penalty_rate_does_not_replace_its_cap_without_typed_fields():
    assert cap([{"descripcion": "Penalidad diaria de 0.5%, hasta un máximo de 10%."}]) == 10


def test_legacy_uptime_description_binds_only_its_explicit_cap():
    assert cap([{"descripcion": (
        "El tiempo de atención máximo establecido regula la disponibilidad. "
        "El ejemplo de UPTIME es 99.0%, con penalidad mensual de 0.5%. "
        "El tope es 10% del monto del contrato."
    )}]) == 10
    assert cap([{"descripcion": (
        "El tiempo de atención máximo establecido regula la disponibilidad. "
        "El ejemplo de UPTIME es 99.0%. La penalidad tiene un tope del 10%."
    )}]) == 10


@pytest.mark.parametrize("text,expected", [
    ("La penalidad diaria es 0.5%, hasta el 10% del monto.", 10),
    ("La multa tiene un máximo de 5,5%.", 5.5),
    ("La penalidad tiene un límite: 8% del contrato.", 8),
    ("La penalidad no debe exceder el 10%.", 10),
    ("La penalidad no excederá el 10%.", 10),
    ("The penalty cap is 7.5%.", 7.5),
    ("The penalty limit is 6%.", 6),
    ("Maximum of 12%.", 12),
    ("Penalidad con tope de 5% y otra con límite del 10%.", 10),
    ("Tiempo máximo de atención: 4 horas; disponibilidad 99.9%.", None),
    ("Capacidad de atención máxima; uptime 99.5%.", None),
    ("Disponibilidad >=99.5%; tasa de penalidad mensual 0.5%.", None),
    ("El contrato requiere un máximo de 20 servidores y uptime 99.9%.", None),
    ("Hasta un máximo de 101%.", None),
])
def test_legacy_text_caps_are_locally_bound_to_percentages(text, expected):
    assert cap(text) == expected


def test_caps_are_resolved_per_penalty_without_hiding_other_legacy_entries():
    assert cap([
        {"tope_pct": 5, "descripcion": "UPTIME 99.9%; tiempo máximo de atención."},
        {"descripcion": "Penalidad diaria del 1%, con máximo del 10%."},
    ]) == 10


@pytest.mark.parametrize("value", [True, False, -1, 0, 101, float("nan"), float("inf"), None])
def test_invalid_structured_cap_does_not_become_a_percentage(value):
    assert cap({"tope_pct": value}) is None


@pytest.mark.parametrize("key", ["tope_pct", "penalty_cap_pct", "max_pct", "maximum_percentage", "limite_pct"])
def test_explicit_cap_aliases_and_decimal_strings_remain_supported(key):
    assert cap({key: "7,5%", "descripcion": "Tiempo máximo de atención; uptime 99.9%."}) == 7.5


def test_capacity_field_is_not_a_penalty_cap():
    assert cap({"capacidad": "99.9%", "descripcion": "Disponibilidad del servicio."}) is None


def test_nested_penalty_families_are_still_aggregated():
    assert cap({"mora": {"tope_pct": 10}, "otras_penalidades": [{"tope_pct": 5}]}) == 10
