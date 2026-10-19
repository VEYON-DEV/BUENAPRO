"""Single-attempt PROD6 reading with private, durable payment receipts.

The directory belongs to one coordinated runtime. A guard requires deliberate
operator recovery; removing it alone cannot authorize resending a started call.
Original documents are never written here. Known responses can always be replayed
locally, including while an unrelated uncertain request blocks new sends.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from buenapro_worker.extraction.gemini import (
    EETT_PROMPT_FILENAME,
    EETT_SCHEMA_VERSION,
    MODEL_PRICES_USD_PER_MILLION,
    PARTICIPATION_INSTRUCTION,
    PROMPT_FILENAME,
    SCHEMA_VERSION,
    ExtractionResult,
    estimate_cost,
    is_truncated,
    parse_json_response,
    prompt_version_for_doc_class,
    read_prompt,
)
from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.settings import Settings


class GuardedReadingError(RuntimeError):
    """Sanitized failure; no provider exception/body is included in logs."""


class ReadingBlockedError(GuardedReadingError):
    """A durable receipt or open guard forbids another paid request."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace atomically and fsync both the private file and its directory."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".receipt-",
                                         delete=False) as stream:
            temporary = stream.name
            os.fchmod(stream.fileno(), 0o600)
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Receipt must be an object")
    return value


def _sdk_compatible_response(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Adapt known null metadata on a copy, after hashing the original receipt."""
    response = deepcopy(raw_response)
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return response
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if ("continuation_token" not in types.Candidate.model_fields
                and candidate.get("continuation_token") is None):
            candidate.pop("continuation_token", None)
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            for field in ("audio_transcription", "media_processing", "speech_metadata"):
                if field not in types.Part.model_fields and part.get(field) is None:
                    part.pop(field, None)
    return response


class GuardedGeminiExtractor:
    def __init__(self, settings: Settings, receipt_dir: Path) -> None:
        self.settings = settings
        self.receipt_dir = Path(receipt_dir)
        self.guard_path = self.receipt_dir / "guard.json"
        self.receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipt_dir.chmod(0o700)

    @contextmanager
    def _lock(self):
        with (self.receipt_dir / ".reading.lock").open("a+b") as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _guard(self, key: str, reason: str, *, cost_known: bool = False) -> None:
        # Never replace an earlier unresolved incident with a later one.
        if self.guard_path.exists():
            return
        try:
            _write_json(self.guard_path, {
                "version": 1, "status": "open", "request_key": key,
                "reason": reason, "cost_known": cost_known, "opened_at": _now(),
            })
        except Exception:
            raise GuardedReadingError(
                "PROD6 guard persistence failed; retain private receipts and stop"
            ) from None

    def _check_new_send(self, key: str) -> None:
        if self.guard_path.exists():
            # A malformed guard is equally blocking. The runtime must explicitly
            # resolve/archive it, never turn invalid JSON into permission to pay.
            raise ReadingBlockedError("PROD6 reading guard is open; new sends blocked")
        for started in self.receipt_dir.glob("*.started.private.json"):
            existing_key = started.name.removesuffix(".started.private.json")
            if not (self.receipt_dir / f"{existing_key}.result.private.json").exists():
                reason = ("response_pending_local_review" if
                          (self.receipt_dir / f"{existing_key}.response.private.json").exists()
                          else "started_request_without_durable_response")
                self._guard(existing_key, reason)
                raise ReadingBlockedError("PROD6 unresolved receipt blocks new sends")

    def extract(self, pdf_bytes: bytes, *, mime: str = "application/pdf",
                doc_class: str = "tdr") -> ExtractionResult:
        if doc_class not in {"tdr", "eett"} or mime != "application/pdf" or not pdf_bytes:
            raise ValueError("PROD6 guarded reading requires a nonempty TDR/EETT PDF")
        model = self.settings.gemini_model
        if model not in MODEL_PRICES_USD_PER_MILLION:
            raise ValueError("PROD6 guarded reading requires a model with known local prices")
        prompt = read_prompt(EETT_PROMPT_FILENAME if doc_class == "eett" else PROMPT_FILENAME)
        prompt += PARTICIPATION_INSTRUCTION
        identity = {
            "document_sha256": _sha(pdf_bytes), "doc_class": doc_class,
            "mime": mime, "model": model,
            "prompt_version": prompt_version_for_doc_class(doc_class),
            "prompt_sha256": _sha(prompt.encode("utf-8")),
            "schema_version": EETT_SCHEMA_VERSION if doc_class == "eett" else SCHEMA_VERSION,
        }
        key = _sha(_canonical(identity))
        with self._lock():
            try:
                return self._extract_locked(pdf_bytes, prompt, identity, key)
            except GuardedReadingError:
                raise
            except Exception:
                # Receipt failures are local integrity problems, not permission
                # to generate another vector/reading. Do not expose private JSON.
                self._guard(key, "local_receipt_integrity_error")
                raise GuardedReadingError("PROD6 private receipt integrity failed") from None

    def _extract_locked(self, pdf_bytes: bytes, prompt: str,
                        identity: dict[str, Any], key: str) -> ExtractionResult:
        result_path = self.receipt_dir / f"{key}.result.private.json"
        response_path = self.receipt_dir / f"{key}.response.private.json"
        started_path = self.receipt_dir / f"{key}.started.private.json"
        if result_path.exists():
            result = _read_json(result_path)
            response = self._load_response(response_path, identity, key)
            if (result.get("identity") != identity or result.get("request_key") != key
                    or result.get("response_sha256") != response["response_sha256"]):
                raise ValueError("Result identity mismatch")
            parsed = self._parsed_json(response["response"])
            if result["raw_json"] != parsed or result.get("usage") != response["usage"]:
                raise ValueError("Result content/usage mismatch")
            return self._result(identity, parsed, response["usage"])
        if response_path.exists():
            response = self._load_response(response_path, identity, key)
            return self._parse_saved_response(identity, key, response, result_path)

        self._check_new_send(key)
        schema = EettExtractionV1 if identity["doc_class"] == "eett" else TdrExtractionV2
        config = types.GenerateContentConfig(
            system_instruction=prompt, response_mime_type="application/json",
            response_schema=schema, temperature=0, max_output_tokens=16384,
        )
        contents = [types.Part.from_bytes(data=pdf_bytes, mime_type=identity["mime"])]
        client = None
        try:
            # Constructing the client/config is local preflight, before recording
            # a potentially sent request. No count_tokens/probe is made.
            client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=1)),
            )
            _write_json(started_path, {
                "version": 1, "request_key": key, "identity": identity,
                "state": "started", "started_at": _now(),
                "cost_usd": None, "cost_status": "uncertain",
            })
            try:
                response = client.models.generate_content(
                    model=identity["model"], contents=contents, config=config)
                raw_response = response.model_dump(mode="json")
                usage = self._usage(raw_response.get("usage_metadata"))
                saved = {
                    "version": 1, "request_key": key, "identity": identity,
                    "state": "response_received", "received_at": _now(),
                    "response": raw_response, "response_sha256": _sha(_canonical(raw_response)),
                    "usage": usage,
                    "cost_usd": (estimate_cost(identity["model"], usage["input_tokens"],
                                               usage["output_tokens"]) if usage["known"] else None),
                    "cost_status": "known" if usage["known"] else "uncertain",
                }
                _write_json(response_path, saved)
            except BaseException:
                self._guard(key, "sent_request_without_durable_response")
                raise GuardedReadingError(
                    "PROD6 request may have been sent; response/usage uncertain; new sends blocked"
                ) from None
            return self._parse_saved_response(identity, key, saved, result_path)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # Closing transports cannot invalidate the durable result or
                    # authorize a retry. Provider exceptions never reach logs.
                    pass

    @staticmethod
    def _usage(raw: Any) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        input_tokens = raw.get("prompt_token_count")
        candidate_tokens = raw.get("candidates_token_count")
        thought_tokens = raw.get("thoughts_token_count") or 0
        known = (type(input_tokens) is int and input_tokens >= 0
                 and type(candidate_tokens) is int and candidate_tokens >= 0
                 and type(thought_tokens) is int and thought_tokens >= 0)
        return {"known": known,
                "input_tokens": input_tokens if known else None,
                "output_tokens": candidate_tokens + thought_tokens if known else None,
                "candidate_tokens": candidate_tokens if known else None,
                "thought_tokens": thought_tokens if known else None}

    def _load_response(self, path: Path, identity: dict[str, Any], key: str) -> dict[str, Any]:
        saved = _read_json(path)
        if (saved.get("identity") != identity or saved.get("request_key") != key
                or saved.get("response_sha256") != _sha(_canonical(saved["response"]))
                or saved.get("usage") != self._usage(saved["response"].get("usage_metadata"))):
            raise ValueError("Response identity/usage mismatch")
        return saved

    def _parse_saved_response(self, identity: dict[str, Any], key: str,
                              saved: dict[str, Any], result_path: Path) -> ExtractionResult:
        cost_known = bool(saved["usage"].get("known"))
        try:
            if not cost_known:
                raise ValueError("Provider usage is missing; cost remains unknown")
            raw_json = self._parsed_json(saved["response"])
            result = self._result(identity, raw_json, saved["usage"])
            _write_json(result_path, {
                "version": 1, "request_key": key, "identity": identity,
                "state": "parsed", "parsed_at": _now(), "raw_json": raw_json,
                "response_sha256": saved["response_sha256"],
                "usage": saved["usage"], "cost_usd": result.cost_usd,
                "requires_human_review": result.requires_human_review,
            })
            return result
        except BaseException:
            self._guard(key, "durable_response_requires_local_review", cost_known=cost_known)
            raise GuardedReadingError(
                "PROD6 response preserved; local validation/persistence needs review; new sends blocked"
            ) from None

    @staticmethod
    def _parsed_json(raw_response: dict[str, Any]) -> dict[str, Any]:
        response = types.GenerateContentResponse.model_validate(
            _sdk_compatible_response(raw_response))
        if is_truncated(response):
            raise ValueError("Response was truncated")
        raw_json = parse_json_response(response.text or "{}")
        if not raw_json or not any(raw_json.values()):
            raise ValueError("Empty extraction")
        return raw_json

    @staticmethod
    def _result(identity: dict[str, Any], raw_json: dict[str, Any],
                usage: dict[str, Any]) -> ExtractionResult:
        if not usage.get("known"):
            raise ValueError("Usage unknown")
        schema = EettExtractionV1 if identity["doc_class"] == "eett" else TdrExtractionV2
        extraction = schema.model_validate(raw_json)
        return ExtractionResult(
            extraction=extraction, raw_json=raw_json,
            model=identity["model"], prompt_version=identity["prompt_version"],
            schema_version=identity["schema_version"],
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cost_usd=estimate_cost(identity["model"], usage["input_tokens"], usage["output_tokens"]),
            requires_human_review=bool(hasattr(extraction, "goods") and not extraction.goods.items),
        )
