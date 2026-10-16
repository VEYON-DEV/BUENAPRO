"""Explicit immutable scopes for the two authorized DIN matching cohorts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,date
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class BatchScope:
    name: str
    fit_level: int
    folder_name: str
    event_prefix: str
    publication_event: str
    execution_name: str
    new_only: bool = False
    maximum_cases: int | None = None
    maximum_per_source: int | None = None


THREE_DOT_SCOPE = BatchScope(
    name="dinaut_three_dot", fit_level=3, folder_name="dinaut-three-dot-20261015",
    event_prefix="dinaut_three_dot_", publication_event="dinaut_three_dot_analysis",
    execution_name="dinaut_three_dot_explicit_batch",
)
TWO_DOT_50_SCOPE = BatchScope(
    name="dinaut_two_dot_50", fit_level=2, folder_name="dinaut-two-dot-50-20261015",
    event_prefix="dinaut_two_dot_50_", publication_event="dinaut_two_dot_50_analysis",
    execution_name="dinaut_two_dot_50_explicit_batch", new_only=True,
    maximum_cases=50, maximum_per_source=25,
)


def validate_scope(scope: BatchScope) -> BatchScope:
    if type(scope) is not BatchScope or scope not in (THREE_DOT_SCOPE, TWO_DOT_50_SCOPE):
        raise ValueError("Only the two explicitly authorized immutable DIN scopes are supported")
    return scope


def scoped_source_sql(sql: str, scope: BatchScope) -> str:
    validate_scope(scope)
    if scope == THREE_DOT_SCOPE:
        return sql
    if sql.count("fit.fit_level=3") != 1:
        raise ValueError("Unexpected canonical affinity selector; cannot safely bind the two-dot scope")
    result=sql.replace("fit.fit_level=3", "fit.fit_level=2")
    prod2="p.buyer_name, d.proposals_closes_at AS closes_at,"
    minor="c.entidad_nombre AS buyer_name,c.fec_fin_cotizacion AS closes_at,"
    if result.count(prod2)==1:
        result=result.replace(prod2,"p.buyer_name, p.published_at AS published_at, d.proposals_closes_at AS closes_at,")
    elif result.count(minor)==1:
        result=result.replace(minor,"c.entidad_nombre AS buyer_name,c.fec_publica AS published_at,c.fec_fin_cotizacion AS closes_at,")
    else:
        raise ValueError("Canonical publication-date projection changed after the two-dot freeze")
    return result


def iso_snapshot(value):
    """Mirror the immutable two-dot freeze's JSON boundary, without touching it."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("A naive datetime cannot match the timezone-aware frozen cohort")
        return value.isoformat()
    if isinstance(value, (date,Decimal,UUID)):
        return str(value)
    if isinstance(value, dict):
        return {key:iso_snapshot(item) for key,item in value.items()}
    if isinstance(value, (list,tuple)):
        return [iso_snapshot(item) for item in value]
    return value
