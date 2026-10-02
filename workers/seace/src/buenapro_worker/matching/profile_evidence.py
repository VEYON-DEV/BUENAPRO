from __future__ import annotations

from typing import Any

EVIDENCE_POLICY_VERSION = "profile_evidence_participation_v1"


def evidence_only_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Exclude explicitly hypothetical capabilities, preserving legacy real records.

    This is not external verification: unmarked legacy facts remain self-reported.
    Demo experience is metadata, never an accredited economic amount.
    """
    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value if not hypothetical(item)]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if not key.startswith("demo_")}
        return value

    def hypothetical(value: Any) -> bool:
        if isinstance(value, str):
            return "demo" in value.casefold() or "por acreditar" in value.casefold()
        if not isinstance(value, dict):
            return False
        if value.get("verified") is False or value.get("is_demo") is True:
            return True
        status = str(value.get("verification_status") or value.get("status") or "").casefold()
        if status in {"demo", "unverified", "hypothetical", "template", "public_record_not_accredited"}:
            return True
        return any(hypothetical(value.get(key)) for key in ("nombre", "name", "role", "descripcion"))

    return clean(profile)


def guard_participation_actions(requirements: list[dict], actions: list[str], summary: dict | None) -> list[str]:
    """A model must not turn missing/conditional authorization into an executable action."""
    participation = (summary or {}).get("participation") or {}

    def replacement(action: str | None) -> str | None:
        if not action:
            return action
        for term, word in (("consorcio", "consor"), ("subcontratacion", "subcontr")):
            if word not in action.casefold():
                continue
            status = (participation.get(term) or {}).get("status", "not_identified")
            if status == "prohibited":
                return "Buscar alternativa permitida por las bases"
            if status != "permitted":
                return "Verificar autorización y condiciones en las bases"
        return action

    for requirement in requirements:
        action = requirement.get("accion")
        guarded = replacement(action)
        if action != guarded:
            requirement["accion"] = guarded
            if requirement.get("estado") == "cumple_con_accion":
                requirement["estado"] = "requiere_revision"
    return [replacement(action) for action in actions if action]
