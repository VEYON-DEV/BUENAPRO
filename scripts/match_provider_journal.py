"""Durable one-shot HTTP receipts for an explicitly authorized matching batch.

No credential is persisted. A sent request without a saved response is never
retried automatically. Importing this module performs no I/O or network call.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import requests


class ProviderBlocked(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def private_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_WRONLY | (os.O_EXCL if exclusive else os.O_TRUNC)
    if exclusive:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporary = path.with_name(path.name + ".writing")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def dispatch_once(folder: Path, stage: str, payload: dict, *, url: str,
                  api_key: str, header_name: str, post=None) -> dict:
    if stage not in {"jev", "gemma"}:
        raise ValueError("Unsupported batch provider stage")
    expected_url = ("https://api.typesafe.ai/v1/systemone" if stage == "jev" else
                    "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:generateContent")
    if url != expected_url or not api_key or header_name != ("Authorization" if stage == "jev" else "x-goog-api-key"):
        raise ValueError("Unexpected provider endpoint or credential binding")
    request_hash = fingerprint(payload)
    response_path = folder / f"{stage}-response.private.json"
    started_path = folder / f"{stage}-started.private.json"
    if response_path.exists():
        saved = json.loads(response_path.read_text())
        if saved.get("request_sha256") != request_hash:
            raise ProviderBlocked("Existing response belongs to a different input")
        if saved.get("http_status") != 200:
            raise ProviderBlocked(f"Saved {stage} HTTP rejection; no automatic retry")
        return saved
    if started_path.exists():
        raise ProviderBlocked(f"Unresolved sent {stage} request; no automatic retry")
    private_json(folder / f"{stage}-request.private.json", payload, exclusive=True)
    private_json(started_path, {"request_sha256": request_hash,
                               "started_utc": datetime.now(timezone.utc).isoformat(),
                               "status": "sent_or_in_flight_cost_not_yet_known"}, exclusive=True)
    started = time.monotonic()
    headers = {header_name: f"Bearer {api_key}" if header_name == "Authorization" else api_key,
               "Content-Type": "application/json"}
    try:
        response = (post or requests.post)(url, json=payload, headers=headers,
                                           timeout=(15, 180), allow_redirects=False)
    except requests.RequestException as exc:
        private_json(folder / f"{stage}-transport-error.private.json",
                     {"request_sha256": request_hash, "error_class": type(exc).__name__,
                      "cost_status": "uncertain", "automatic_retry": False})
        raise ProviderBlocked(f"{stage} transport error; sent request preserved") from None
    # The response body is private; never save request headers, URLs with keys,
    # or exception strings that could contain credentials.
    text = response.text.replace(api_key, "[REDACTED]")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {"unparsed_body": text}
    saved = {"request_sha256": request_hash, "http_status": response.status_code,
             "seconds": round(time.monotonic() - started, 3), "body": body,
             "completed_utc": datetime.now(timezone.utc).isoformat()}
    private_json(response_path, saved, exclusive=True)
    if response.status_code != 200:
        raise ProviderBlocked(f"{stage} HTTP {response.status_code}; response preserved, no retry")
    return saved
