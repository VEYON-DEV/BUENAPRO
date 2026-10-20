"""Pure title-only request identity, frozen manifests, strict JEV answer parsing.

No keyword guesses, PDF input, profiles, network, or database writes here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

POLICY = "title_commercial_classification_v1"
MODEL = "jev-1.13.0"
TEMPLATE_FILE_SHA256 = "907d4193da66aca2ac3a2372768f47d394a7b2f04f7a4fcfe30116c1a8061746"
CATEGORIES = ("licencias_suscripciones", "desarrollo_software", "ia_automatizacion", "industrial",
              "cloud_infraestructura", "soporte_mantenimiento")
INPUT_PRICE = Decimal("0.042") / Decimal(1_000_000)
MAX_REQUEST_BYTES = 16_000
RESERVED_INPUT_TOKENS = 32_768


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value) -> str:
    return sha(encode(value))


def normalize_title(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A non-empty full title is required")
    # Keep case, brands, punctuation, numbers, accents and negations unchanged.
    return " ".join(value.split())


def validate_template(value: dict) -> None:
    if (not isinstance(value, dict) or set(value) != {"model", "state", "questions"}
            or value["model"] != MODEL or set(value["state"]) != {"titulo"}
            or set(value["questions"]) != {*CATEGORIES, "informacion"}):
        raise ValueError("Unexpected frozen title request schema")
    for key in CATEGORIES:
        question = value["questions"][key]
        if set(question) != {"type", "instructions"} or question["type"] != "noul":
            raise ValueError("Category questions must be frozen noul primitives")
    info = value["questions"]["informacion"]
    if (set(info) != {"type", "instructions", "criteria"} or info["type"] != "choice"
            or set(info["criteria"]) != {"claro", "insuficiente"}):
        raise ValueError("Unexpected clarity question")


def load_template(path: Path) -> dict:
    raw = path.read_bytes()
    if sha(raw) != TEMPLATE_FILE_SHA256:
        raise ValueError("Approved request template bytes changed")
    value = json.loads(raw)
    validate_template(value)
    return value


def request_for(title: str, template: dict) -> dict:
    validate_template(template)
    payload = deepcopy(template)
    payload["state"] = {"titulo": normalize_title(title)}
    if len(encode(payload)) > MAX_REQUEST_BYTES:
        raise ValueError("Title request exceeds the finite local size guard; never truncate")
    return payload


def request_identity(payload: dict) -> dict:
    validate_template(payload)
    if normalize_title(payload["state"]["titulo"]) != payload["state"]["titulo"]:
        raise ValueError("Title was not whitespace-normalized")
    return {"request_hash": digest({"policy": POLICY, "payload": payload,
                                    "catalog": CATEGORIES}),
            "title_sha256": sha(payload["state"]["titulo"].encode()),
            "catalog_sha256": digest(CATEGORIES), "prompt_sha256": digest(payload["questions"])}


def build_manifest(rows: list[dict], template: dict, run_id: str, *, budget_usd="0.10") -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", run_id):
        raise ValueError("Invalid run ID")
    budget = Decimal(str(budget_usd))
    if not budget.is_finite() or not Decimal("0") < budget <= Decimal("0.10"):
        raise ValueError("Budget must be positive and at most the authorized USD 0.10")
    cases, requests, seen = [], {}, set()
    for row in sorted(rows, key=lambda row: int(row["id_convocatoria"])):
        conv, title = int(row["id_convocatoria"]), row["description"]
        if conv <= 0 or conv in seen:
            raise ValueError("Duplicate or invalid convocatoria identity")
        seen.add(conv)
        payload = request_for(title, template)
        identity = request_identity(payload)
        key = identity["request_hash"]
        cases.append({"convocation_id": conv, "source_title": title,
                      "source_title_sha256": sha(title.encode()), "request_hash": key})
        requests.setdefault(key, {**identity, "payload": payload})
    if not cases:
        raise ValueError("Empty scope")
    return {"version": 1, "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_version": POLICY, "model": MODEL, "template_sha256": digest(template),
            "template": template, "budget_usd": str(budget), "scope": "existing_prod2_only",
            "cases": cases, "requests": list(requests.values())}


def validate_manifest(value: dict, expected_sha256: str | None = None) -> str:
    checksum = digest(value)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ValueError("Frozen manifest hash mismatch")
    if (value.get("version") != 1 or value.get("policy_version") != POLICY
            or value.get("model") != MODEL or value.get("scope") != "existing_prod2_only"):
        raise ValueError("Frozen manifest identity mismatch")
    validate_template(value["template"])
    rebuilt = build_manifest([{"id_convocatoria": c["convocation_id"], "description": c["source_title"]}
                              for c in value["cases"]], value["template"], value["run_id"],
                             budget_usd=value["budget_usd"])
    for key in ("cases", "requests", "template_sha256"):
        if rebuilt[key] != value[key]:
            raise ValueError("Frozen title, request or prompt binding mismatch")
    return checksum


def finite(value, low=0, high=1) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Malformed JEV numeric answer")
    return float(value)


def parse_response(payload: dict, data: dict) -> dict:
    if (not isinstance(data, dict) or data.get("model") != MODEL
            or not isinstance(data.get("answers"), dict)
            or set(data["answers"]) != set(payload["questions"])):
        raise ValueError("JEV model or exact question coverage mismatch")
    usage = data.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                         for k in ("input_tokens", "output_tokens")):
        raise ValueError("Missing valid provider token usage")
    scores = {}
    for category in CATEGORIES:
        answer = data["answers"][category]
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise ValueError("JEV category primitive mismatch")
        scores[category] = finite(answer.get("noul"))
        # Preserve actual confidence if supplied; do not invent one for noul.
        if "confidence" in answer:
            finite(answer["confidence"])
    clarity = data["answers"]["informacion"]
    if (not isinstance(clarity, dict) or clarity.get("type") != "choice"
            or clarity.get("choice") not in {"claro", "insuficiente"}):
        raise ValueError("JEV clarity primitive mismatch")
    confidence = finite(clarity.get("confidence"))
    probabilities = clarity.get("probabilities")
    if (not isinstance(probabilities, dict) or set(probabilities) != {"claro", "insuficiente"}
            or abs(sum(finite(p) for p in probabilities.values()) - 1) > .02):
        raise ValueError("JEV clarity distribution mismatch")
    categories = [c for c in CATEGORIES if scores[c] >= .8]
    # The range between low and high signals is explicitly unresolved.
    clear = (clarity["choice"] == "claro" and confidence >= .8 and probabilities["claro"] >= .8)
    if not clear or any(.2 < score < .8 for score in scores.values()):
        categories, decision = [], "unclear"
    elif categories:
        decision = "classified"
    elif all(score <= .2 for score in scores.values()):
        decision = "other"
    else:
        categories, decision = [], "unclear"
    return {"categories": categories, "decision": decision, "policy_version": POLICY,
            "basis": "title_jev", "preliminary": True, "signals": scores,
            "clarity": deepcopy(clarity), "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cost_usd": str(Decimal(usage["input_tokens"]) * INPUT_PRICE),
            "not_qualification_or_win_probability": True}
