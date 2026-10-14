"""Pure, conservative participation-window assessment of an observed PROD2 ficha.

This is a schedule indicator, not an eligibility decision. ``Convocado`` alone
does not establish an open window. No clock time is assigned to date-only data;
the original ficha values and their precision remain in the returned JSON.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
import unicodedata
from zoneinfo import ZoneInfo


LIMA = ZoneInfo("America/Lima")
POLICY_VERSION = "prod2_participation_windows_v1"
STATES = frozenset({
    "closed", "adjudicado", "cancelado", "desierto", "suspendido", "unknown",
    "pending_registration", "registration_open", "registered_only", "submission_open",
})
_TERMINAL = {
    "adjudicado": "adjudicado", "adjudicada": "adjudicado",
    "consentido": "adjudicado", "consentida": "adjudicado",
    "buena pro consentida": "adjudicado",
    "cancelado": "cancelado", "cancelada": "cancelado",
    "anulado": "cancelado", "anulada": "cancelado",
    "desierto": "desierto", "desierta": "desierto",
    "suspendido": "suspendido", "suspendida": "suspendido",
    "contratado": "closed", "contratada": "closed",
    "culminado": "closed", "concluido": "closed", "cerrado": "closed",
}


def _normal(text: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    ).split())


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return deepcopy(value)


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()


def _time(text) -> dict:
    result = {"text": _json_value(text), "precision": "unknown", "date": None, "at": None}
    if text is None or text == "":
        result["precision"] = "missing"
        return result
    if not isinstance(text, str):
        return result
    for pattern, fmt, precision in (
        (r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y", "day"),
        (r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}", "%d/%m/%Y %H:%M", "minute"),
        (r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", "%d/%m/%Y %H:%M:%S", "second"),
    ):
        if not re.fullmatch(pattern, text.strip()):
            continue
        try:
            value = datetime.strptime(text.strip(), fmt)
        except ValueError:
            return result
        result.update(precision=precision, date=value.date().isoformat())
        if precision != "day":
            result["at"] = value.replace(tzinfo=LIMA).isoformat()
        return result
    return result


def _stage_kind(stage: str) -> str | None:
    normalized = _normal(stage)
    if re.search(r"\b(?:registro|inscripcion) de participantes\b", normalized):
        return "registration"
    if re.search(r"\bpresentacion (?:y apertura )?de (?:propuestas|ofertas)\b", normalized):
        return "submission"
    return None


def _window_status(start: dict, end: dict, as_of: datetime) -> tuple[str, str | None]:
    if start["date"] is None or end["date"] is None:
        return "unknown", "missing_or_unparseable_dates"
    if start["date"] > end["date"]:
        return "unknown", "invalid_date_order"
    if start["at"] and end["at"] and start["at"] >= end["at"]:
        return "unknown", "invalid_date_order"
    today = as_of.date().isoformat()
    if end["precision"] == "day" and end["date"] < today:
        return "closed", None
    if start["precision"] == "day" and start["date"] > today:
        return "pending", None
    start_at = datetime.fromisoformat(start["at"]) if start["at"] else None
    end_at = datetime.fromisoformat(end["at"]) if end["at"] else None
    if end_at:
        # Display precision does not establish the exact second within a minute.
        end_upper = end_at + (timedelta(minutes=1) if end["precision"] == "minute" else timedelta())
        if as_of >= end_upper:
            return "closed", None
    if start_at and as_of < start_at:
        return "pending", None
    if not start_at or not end_at:
        return "unknown", "day_only_open_window_not_confirmed"
    start_upper = start_at + (timedelta(minutes=1) if start["precision"] == "minute" else timedelta())
    if as_of < start_upper or as_of >= end_at:
        return "unknown", "time_precision_boundary"
    return "open", None


def _schedule(schedule, as_of: datetime) -> tuple[list[dict], dict, list[str]]:
    parsed, reasons = [], []
    windows = {kind: {"status": "unknown", "row_indices": [], "reason": "stage_missing"}
               for kind in ("registration", "submission")}
    if not isinstance(schedule, list):
        return parsed, windows, ["schedule_invalid"]
    for index, row in enumerate(schedule):
        if not isinstance(row, dict) or not isinstance(row.get("stage"), str):
            reasons.append("schedule_row_invalid")
            continue
        kind = _stage_kind(row["stage"])
        start, end = _time(row.get("starts_at_text")), _time(row.get("ends_at_text"))
        status, reason = _window_status(start, end, as_of)
        parsed.append({"row_index": index, "stage": row["stage"], "kind": kind,
                       "start": start, "end": end, "status": status, "reason": reason})
        if re.search(r"reprogram|rectifica|prorroga|posterg|suspensi", _normal(row["stage"])):
            reasons.append("schedule_revision_ambiguous")
    for kind in windows:
        rows = [row for row in parsed if row["kind"] == kind]
        if not rows:
            reasons.append(f"{kind}_stage_missing")
            continue
        identities = {(_normal(row["stage"]), row["start"]["precision"], row["start"]["date"],
                       row["start"]["at"], row["end"]["precision"], row["end"]["date"],
                       row["end"]["at"]) for row in rows}
        if len(identities) != 1:
            windows[kind] = {"status": "unknown", "row_indices": [r["row_index"] for r in rows],
                             "reason": "conflicting_stages"}
            reasons.append(f"{kind}_conflicting_stages")
            continue
        row = rows[0]
        windows[kind] = {"status": row["status"], "row_indices": [r["row_index"] for r in rows],
                         "reason": row["reason"], "start": row["start"], "end": row["end"]}
        if row["reason"]:
            reasons.append(f"{kind}_{row['reason']}")
    return parsed, windows, list(dict.fromkeys(reasons))


def _coherent(windows: dict) -> bool:
    registration, submission = windows["registration"], windows["submission"]
    if "start" not in registration or "start" not in submission:
        return False
    for left, right in ((registration["start"], submission["start"]),
                        (registration["end"], submission["end"])):
        if not left["date"] or not right["date"]:
            return False
        if left["date"] > right["date"]:
            return False
        if left["date"] == right["date"] and bool(left["at"]) != bool(right["at"]):
            return False
        if left["at"] and right["at"] and left["at"] > right["at"]:
            return False
    return True


def decide(detail: dict, as_of: datetime) -> dict:
    """Assess observed windows at an explicit aware instant; perform no I/O.

    ``can_registered_company_submit`` is conditional on prior registration and
    says nothing about legal/company eligibility. ``registered_only`` means the
    enrollment window has ended while proposal submission is still future.
    During proposal submission, ``submission_open`` and the separate enrollment
    flag identify whether new enrollment is also still possible.

    A date-only window can establish a past/future day, but never an affirmative
    open flag. The caller should refresh stale fichas before acting on this
    snapshot; this function cannot detect a later unpublished reprogramming.
    """
    if not isinstance(detail, dict):
        raise TypeError("detail must be a parsed ficha dict")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    local_now = as_of.astimezone(LIMA)
    snapshot = _json_value({key: detail.get(key) for key in (
        "procedure_id", "convocatoria_id", "selection_type", "item_states", "schedule",
    )})
    parsed, windows, reasons = _schedule(detail.get("schedule"), local_now)
    result = {
        "policy_version": POLICY_VERSION, "as_of": local_now.isoformat(), "timezone": str(LIMA),
        "state": "unknown", "new_company_registration": "unknown",
        "registered_company_submission": "unknown", "can_new_company_register": None,
        "can_registered_company_submit": None, "confirmed_open": False,
        "requires_review": True, "eligibility_verified": False,
        "registered_submission_requires_prior_registration": True,
        "item_scope": "observed_distinct_states_without_item_ids",
        "item_states_coverage_verified": False,
        "snapshot": snapshot, "snapshot_sha256": _hash(snapshot),
        "evidence": {"schedule": parsed, "windows": windows}, "reason_codes": [],
    }

    def finish(state: str, codes: list[str], *, confirmed: bool = False) -> dict:
        result["state"] = state
        result["reason_codes"] = list(dict.fromkeys(codes))
        result["confirmed_open"] = confirmed
        result["requires_review"] = state == "unknown"
        if state in {"closed", "adjudicado", "cancelado", "desierto", "suspendido"}:
            result.update(new_company_registration="closed", registered_company_submission="closed",
                          can_new_company_register=False, can_registered_company_submit=False)
        elif state != "unknown":
            result.update(new_company_registration=windows["registration"]["status"],
                          registered_company_submission=windows["submission"]["status"],
                          can_new_company_register=windows["registration"]["status"] == "open",
                          can_registered_company_submit=windows["submission"]["status"] == "open")
        result["assessment_sha256"] = _hash(result)
        return result

    observed_states = detail.get("item_states")
    if not isinstance(observed_states, list) or not observed_states or any(
        not isinstance(state, str) or not state.strip() for state in observed_states
    ):
        return finish("unknown", ["item_states_missing_or_invalid", *reasons])
    normalized_states = {_normal(state) for state in observed_states}
    categories = {_TERMINAL.get(state, "candidate" if state == "convocado" else "unknown")
                  for state in normalized_states}
    if len(categories) > 1:
        return finish("unknown", ["mixed_item_states", *reasons])
    item_category = categories.pop()
    if item_category == "unknown":
        return finish("unknown", ["unrecognized_item_state", *reasons])
    if item_category != "candidate":
        return finish(item_category, ["explicit_item_state", *reasons])

    structural_ambiguity = any(code in {
        "schedule_invalid", "schedule_row_invalid", "schedule_revision_ambiguous",
        "registration_conflicting_stages", "submission_conflicting_stages",
    } for code in reasons)
    if structural_ambiguity:
        return finish("unknown", reasons)
    if all("start" in window and window["start"]["date"] and window["end"]["date"]
           for window in windows.values()) and not _coherent(windows):
        return finish("unknown", ["participation_schedule_incomplete_or_inconsistent", *reasons])
    if windows["submission"]["status"] == "closed":
        return finish("closed", ["submission_window_ended", *reasons])
    selection = detail.get("selection_type")
    if not isinstance(selection, str) or not selection.strip():
        return finish("unknown", ["selection_type_missing", *reasons])
    restricted = re.search(r"contratacion directa|consultor(?:es)? individual(?:es)?|consultoria individual|\binvitacion\b",
                           _normal(selection))
    invitation = any(re.search(r"\binvitacion\b", _normal(row["stage"])) for row in parsed)
    if restricted or invitation:
        return finish("unknown", ["participation_modality_requires_review", *reasons])
    if not _coherent(windows):
        return finish("unknown", ["participation_schedule_incomplete_or_inconsistent", *reasons])
    registration, submission = windows["registration"]["status"], windows["submission"]["status"]
    if "unknown" in (registration, submission):
        return finish("unknown", reasons)
    if submission == "open":
        return finish("submission_open", ["observed_submission_window_open"], confirmed=True)
    if registration == "open" and submission == "pending":
        return finish("registration_open", ["observed_registration_window_open"], confirmed=True)
    if registration == "pending" and submission == "pending":
        return finish("pending_registration", ["registration_window_not_started"])
    if registration == "closed" and submission == "pending":
        return finish("registered_only", ["registration_closed_submission_not_started"])
    return finish("unknown", ["participation_windows_inconsistent", *reasons])
