"""Lossless compact-state transport for the frozen DIN JEV request.

No HTTP, PG, env reads or inference on import. Dispatch is delegated to the
runner's reserved_dispatch callback. Only an unsent request or the exact saved
400/max_tokens_exceeded permits this single alternate representation. Any sent
uncertainty or other rejection blocks further dispatch; no long context/split.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable

from match_provider_journal import ProviderBlocked, canonical, fingerprint, private_json

TRANSPORT_VERSION = "jev_compact_json_state_v1"
CHILD_FOLDER = "jev-compact-json"
MANIFEST_FILENAME = "jev-transport-manifest.private.json"
SOURCE_FILENAME = "source-payload.private.json"
WIRE_FILENAME = "wire-payload.private.json"


def _load(path: Path) -> dict:
    if path.is_symlink():
        raise ProviderBlocked("Symlink provider evidence is not an authorized transport")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ProviderBlocked("Invalid or unreadable private transport evidence") from None
    if not isinstance(value, dict):
        raise ProviderBlocked("Provider transport evidence must be an object")
    return value


def _once(path: Path, value: dict) -> None:
    if path.exists():
        if fingerprint(_load(path)) != fingerprint(value):
            raise ProviderBlocked("Immutable transport evidence changed")
    else:
        private_json(path, value, exclusive=True)


def _child(folder: Path) -> Path:
    child = folder / CHILD_FOLDER
    if child.is_symlink() or child.resolve().parent != folder.resolve():
        raise ProviderBlocked("Unexpected compact JEV transport path")
    return child


def compact_jev_payload(source_payload: dict) -> dict:
    """Canonical compact JSON string for state; exact same model/questions."""
    if (not isinstance(source_payload, dict) or set(source_payload) != {"model", "state", "questions"}
            or not isinstance(source_payload["state"], dict)
            or not isinstance(source_payload["questions"], dict)
            or not isinstance(source_payload["model"], str)):
        raise ProviderBlocked("Expected the unchanged frozen object-state JEV payload")
    wire = copy.deepcopy(source_payload)
    wire["state"] = canonical(source_payload["state"])
    if json.loads(wire["state"]) != source_payload["state"]:
        raise ProviderBlocked("Compact JEV state is not a lossless JSON representation")
    return wire


def _root_state(folder: Path, source_payload: dict | None) -> tuple[str, dict | None]:
    request_path = folder / "jev-request.private.json"
    started_path = folder / "jev-started.private.json"
    response_path = folder / "jev-response.private.json"
    request = _load(request_path) if request_path.exists() else None
    if request is not None and source_payload is not None and fingerprint(request) != fingerprint(source_payload):
        raise ProviderBlocked("Frozen root JEV request binding changed")
    if started_path.exists():
        started = _load(started_path)
        if request is None or started.get("request_sha256") != fingerprint(request):
            raise ProviderBlocked("Root sent JEV evidence has no exact request binding")
    if response_path.exists():
        receipt = _load(response_path)
        if request is None or receipt.get("request_sha256") != fingerprint(request):
            raise ProviderBlocked("Root JEV receipt belongs to another request")
        if type(receipt.get("http_status")) is int and receipt["http_status"] == 200:
            return "root_success", receipt
        # Exact diagnosed structural rejection only; no answers or usage may be
        # hidden in an extended body. This does not assert that billed cost is 0.
        if (type(receipt.get("http_status")) is int and receipt["http_status"] == 400
                and receipt.get("body") == {"detail": {"error_type": "max_tokens_exceeded"}}
                and "answers" not in receipt and "usage" not in receipt):
            return "exact_context_limit_rejection", receipt
        raise ProviderBlocked("Saved root JEV rejection is not the authorized exact context-limit case")
    if started_path.exists():
        raise ProviderBlocked("Unresolved sent root JEV request; alternate transport is forbidden")
    if (folder / "jev-transport-error.private.json").exists():
        raise ProviderBlocked("Unresolved root transport failure; no alternate JEV call")
    return "unsent", None


def _manifest(source: dict, wire: dict, root_disposition: str, root_receipt: dict | None) -> dict:
    return {
        "transport_version": TRANSPORT_VERSION,
        "source_payload_sha256": fingerprint(source), "wire_payload_sha256": fingerprint(wire),
        "semantic_state_sha256": fingerprint(source["state"]),
        "questions_sha256": fingerprint(source["questions"]), "model": source["model"],
        "root_disposition": root_disposition,
        "root_rejection_receipt_sha256": fingerprint(root_receipt) if root_receipt is not None else None,
        "source_file": SOURCE_FILENAME, "wire_file": WIRE_FILENAME,
        "long_context": False, "split": False, "raw_rejection_cost_inference": "not_assumed_zero",
    }


def _verified_child(folder: Path, source: dict | None, disposition: str, root_receipt: dict | None) -> tuple[Path, dict] | None:
    child = _child(folder)
    manifest_path = child / MANIFEST_FILENAME
    if not manifest_path.exists():
        if any((child / name).exists() for name in ("jev-started.private.json", "jev-response.private.json", "jev-request.private.json")):
            raise ProviderBlocked("Child JEV attempt exists without its semantic transport binding")
        return None
    stored_source = _load(child / SOURCE_FILENAME)
    if source is not None and fingerprint(stored_source) != fingerprint(source):
        raise ProviderBlocked("Compact transport belongs to a different frozen source")
    expected_wire = compact_jev_payload(stored_source)
    wire = _load(child / WIRE_FILENAME)
    if fingerprint(wire) != fingerprint(expected_wire):
        raise ProviderBlocked("Compact JEV wire payload changed")
    if _load(manifest_path) != _manifest(stored_source, wire, disposition, root_receipt):
        raise ProviderBlocked("Compact JEV semantic binding or root diagnostic changed")
    request_path = child / "jev-request.private.json"
    started_path = child / "jev-started.private.json"
    response_path = child / "jev-response.private.json"
    if request_path.exists() and fingerprint(_load(request_path)) != fingerprint(wire):
        raise ProviderBlocked("Child sent request differs from its frozen wire payload")
    if started_path.exists() and _load(started_path).get("request_sha256") != fingerprint(wire):
        raise ProviderBlocked("Child sent-attempt request hash changed")
    if response_path.exists():
        receipt = _load(response_path)
        if not request_path.exists() or receipt.get("request_sha256") != fingerprint(wire):
            raise ProviderBlocked("Child JEV response request binding changed")
        if type(receipt.get("http_status")) is not int or receipt["http_status"] != 200:
            raise ProviderBlocked("Saved compact JEV rejection; no further representation or retry")
        return child, receipt
    if started_path.exists() or request_path.exists() or (child / "jev-transport-error.private.json").exists():
        raise ProviderBlocked("Unresolved compact JEV attempt; no further call")
    return None


def get_cached_jev_receipt(folder: Path, expected_payload: dict | None = None) -> dict | None:
    """Resolve root200 versus bound child200 without mutating evidence."""
    folder = Path(folder)
    if expected_payload is None and (folder / "jev-request.private.json").exists():
        expected_payload = _load(folder / "jev-request.private.json")
    if expected_payload is not None:
        compact_jev_payload(expected_payload)
    disposition, receipt = _root_state(folder, expected_payload)
    child = _child(folder)
    if disposition == "root_success":
        if any((child / name).exists() for name in ("jev-started.private.json", "jev-response.private.json")):
            raise ProviderBlocked("Both root and alternate JEV sent evidence exist; reconcile without repaying")
        return receipt
    verified = _verified_child(folder, expected_payload, disposition, receipt)
    return verified[1] if verified is not None else None


def prepare_jev_transport(folder: Path, source_payload: dict) -> dict:
    """Prepare the only alternate wire request; never modify root evidence."""
    folder = Path(folder)
    wire = compact_jev_payload(source_payload)
    disposition, root_receipt = _root_state(folder, source_payload)
    if disposition == "root_success":
        return {"folder": folder, "payload": copy.deepcopy(source_payload), "manifest": None,
                "cached_receipt": get_cached_jev_receipt(folder, source_payload)}
    # An existing attempt must resolve before any file or callback mutation.
    cached = get_cached_jev_receipt(folder, source_payload)
    child = _child(folder)
    manifest = _manifest(source_payload, wire, disposition, root_receipt)
    _once(child / SOURCE_FILENAME, source_payload)
    _once(child / WIRE_FILENAME, wire)
    _once(child / MANIFEST_FILENAME, manifest)
    return {"folder": child, "payload": wire, "manifest": manifest, "cached_receipt": cached}


def dispatch_jev_transport(folder: Path, source_payload: dict, *, reserved_dispatch: Callable, **kwargs) -> dict:
    """Caller supplies its existing PG reservation + one-shot journal callback."""
    prepared = prepare_jev_transport(folder, source_payload)
    if prepared["cached_receipt"] is not None:
        return prepared["cached_receipt"]
    receipt = reserved_dispatch(prepared["folder"], "jev", prepared["payload"], **kwargs)
    saved = get_cached_jev_receipt(folder, source_payload)
    if saved is None or fingerprint(receipt) != fingerprint(saved):
        raise ProviderBlocked("Provider callback did not preserve the exact compact JEV receipt")
    return saved


def compact_transport_metrics(source_payload: dict, *, token_counter: Callable[[str], int]) -> dict:
    """Offline proxy metrics only; no claims about the provider's tokenizer."""
    wire = compact_jev_payload(source_payload)
    values = {"object_state_tokens": token_counter(canonical(source_payload["state"])),
              "compact_string_state_tokens": token_counter(wire["state"]),
              "wire_request_tokens": token_counter(canonical(wire))}
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise ValueError("Invalid offline transport token counter")
    return {**values, "source_payload_sha256": fingerprint(source_payload),
            "wire_payload_sha256": fingerprint(wire), "semantic_roundtrip_equal": True,
            "model_calls": 0, "tokenizer": "caller_local_proxy_not_provider_billing", "long_context": False}
