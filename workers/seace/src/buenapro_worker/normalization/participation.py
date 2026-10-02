from __future__ import annotations

from typing import Any


_STATUSES = {"permitted", "prohibited", "conditional", "not_identified"}


def derive_participation(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize LLM1 terms without treating missing or invalid evidence as permission."""
    participation = raw.get("participation")
    participation = participation if isinstance(participation, dict) else {}
    return {
        term: _normalize_term(participation.get(term))
        for term in ("consorcio", "subcontratacion")
    }


def _normalize_term(value: Any) -> dict[str, Any]:
    term = value if isinstance(value, dict) else {}
    clause = term.get("clause")
    clause = clause.strip() if isinstance(clause, str) and clause.strip() else None
    page = term.get("page")
    page = page if type(page) is int and page >= 1 else None
    conditions_raw = term.get("conditions")
    conditions = (
        [item.strip() for item in conditions_raw if isinstance(item, str) and item.strip()]
        if isinstance(conditions_raw, list) else []
    )
    status = term.get("status")
    if not isinstance(status, str) or status not in _STATUSES or not clause or page is None:
        status = "not_identified"
    elif status == "permitted" and conditions:
        status = "conditional"
    return {"status": status, "clause": clause, "page": page, "conditions": conditions}
