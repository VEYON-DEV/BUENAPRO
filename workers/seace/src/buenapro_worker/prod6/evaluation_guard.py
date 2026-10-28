"""Single-attempt PROD6 evaluation (LLM2) with private, durable payment receipts.

Same contract as ``prod6/reading.py``, for the one Gemini call of ``analyze_match``:

* A ``started`` receipt is written (and fsynced) before the request leaves. Without a durable
  answer it blocks every new send: a request whose outcome is uncertain is never repeated.
* An HTTP error answered by the provider (``google.genai.errors.APIError``) proves no billable
  response exists: a ``provider_error`` receipt resolves the ``started`` one, the attempt raises
  ``ProviderRejectedError`` and a later retry is allowed.
* The answer (text and token usage) is written before it is parsed or stored in PostgreSQL, so a
  crash or a failed database write is recovered by replaying the receipt, never by paying again.
  Known answers are always replayed, also while the guard is open.
* Any other failure after sending (timeout, dropped connection, interrupt, local write, an answer
  that cannot be parsed) opens ``guard.json``: no new send until an operator reviews it.

The request identity hashes the model, the prompt text and the exact payload, so a different
profile, requirement set or prompt is a different request. The directory belongs to one
coordinated runtime. The receipts hold the model's JSON answer, never the request payload.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from google.genai import errors as genai_errors

from buenapro_worker.extraction.gemini import (
    MODEL_PRICES_USD_PER_MILLION, TruncatedResponseError, is_truncated,
)
from buenapro_worker.matching.analyzer import MatchAnalysisResult, MatchAnalyzer
from buenapro_worker.prod6.reading import (
    GuardedReadingError, ProviderRejectedError, ReadingBlockedError, _canonical, _now, _read_json,
    _remove_receipt, _sha, _write_json, provider_rejection, quota_window,
)
from buenapro_worker.prod6.gemini_pacing import GEMINI_PACER, SATURATION_CODES, saturation_fallback_model
from buenapro_worker.settings import Settings

RECEIPT_KIND = "prod6_match_analysis_v1"
GUARD_FILENAME = "guard.json"


class GuardedMatchAnalyzer(MatchAnalyzer):
    receipt_kind = RECEIPT_KIND  # Part of every request identity; PROD2 uses its own.

    def __init__(self, settings: Settings, receipt_dir: Path, *, allow_new_requests: bool = True,
                 **options: Any) -> None:
        super().__init__(settings, **options)
        if type(allow_new_requests) is not bool:
            raise GuardedReadingError("PROD6 request permission must be explicit")
        self.allow_new_requests = allow_new_requests
        # True once the provider returned an answer in this instance (not a local replay).
        self.provider_answered = False
        self.receipt_dir = Path(receipt_dir)
        self.guard_path = self.receipt_dir / GUARD_FILENAME
        self.receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipt_dir.chmod(0o700)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # Closing a transport cannot invalidate a durable answer.
            pass

    # --- identity and receipts ------------------------------------------------------------

    def _identity(self, model: str, payload: str) -> tuple[dict[str, Any], str]:
        if model not in MODEL_PRICES_USD_PER_MILLION:
            raise ValueError("PROD6 guarded evaluation requires a model with known local prices")
        identity = {"kind": self.receipt_kind, "model": model, "prompt_version": self.prompt_version,
                    "prompt_sha256": _sha(self.prompt.encode("utf-8")),
                    "payload_sha256": _sha(payload.encode("utf-8"))}
        return identity, _sha(_canonical(identity))

    def _path(self, key: str, kind: str) -> Path:
        return self.receipt_dir / f"{key}.{kind}.private.json"

    @contextmanager
    def _lock(self):
        with (self.receipt_dir / ".evaluation.lock").open("a+b") as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _guard(self, key: str, reason: str) -> None:
        # Never replace an earlier unresolved incident with a later one.
        if self.guard_path.exists():
            return
        try:
            _write_json(self.guard_path, {"version": 1, "status": "open", "request_key": key,
                                          "reason": reason, "opened_at": _now()})
        except Exception:
            raise GuardedReadingError(
                "PROD6 evaluation guard persistence failed; retain private receipts and stop") from None

    def _provider_error(self, key: str) -> dict[str, Any] | None:
        """The valid provider_error receipt of ``key``; unreadable or foreign ones do not count."""
        try:
            receipt = _read_json(self._path(key, "provider_error"))
        except Exception:
            return None
        valid = (receipt.get("version") == 1 and receipt.get("request_key") == key
                 and receipt.get("state") == "provider_error"
                 and type(receipt.get("http_status")) is int
                 and isinstance(receipt.get("started_at"), str))
        return receipt if valid else None

    def _resolved(self, key: str, started: Path) -> bool:
        """The started receipt has a durable answer or belongs to an attempt the provider rejected."""
        if self._path(key, "response").exists():
            return True
        receipt = self._provider_error(key)
        if receipt is None:
            return False
        try:
            return receipt["started_at"] == _read_json(started).get("started_at")
        except Exception:
            return False

    def _check_new_send(self) -> None:
        if self.guard_path.exists():
            # A malformed guard is equally blocking: it is resolved by an operator, never ignored.
            raise ReadingBlockedError("PROD6 evaluation guard is open; new sends blocked")
        for started in self.receipt_dir.glob("*.started.private.json"):
            existing = started.name.removesuffix(".started.private.json")
            if not self._resolved(existing, started):
                self._guard(existing, "started_request_without_durable_response")
                raise ReadingBlockedError("PROD6 unresolved evaluation receipt blocks new sends")

    # --- the paid call --------------------------------------------------------------------

    def _analyze_with_model(self, model: str, payload: str) -> MatchAnalysisResult:
        """The main model, or the saturation fallback once when the main one is overloaded."""
        fallback = saturation_fallback_model()
        if fallback is None or fallback == model:
            return self._send(model, payload)
        fallback_key = self._identity(fallback, payload)[1]
        if self._path(fallback_key, "response").exists():
            return self._send(fallback, payload)  # The fallback already answered: replay it.
        try:
            return self._send(model, payload)
        except ProviderRejectedError as error:
            # A replay-only job never starts a new paid request on the fallback.
            if error.code not in SATURATION_CODES or not (
                    self.allow_new_requests or self._provider_error(fallback_key) is not None):
                raise
            return self._send(fallback, payload)

    def _send(self, model: str, payload: str) -> MatchAnalysisResult:
        identity, key = self._identity(model, payload)
        with self._lock():
            response_path = self._path(key, "response")
            if response_path.exists():
                return self._parse(key, identity, self._load_response(response_path, identity, key))
            self._check_new_send()
            if not self.allow_new_requests and self._provider_error(key) is None:
                raise ReadingBlockedError("Prior PROD6 attempt permits exact local recovery only")
            # A new attempt consumes the earlier HTTP-error receipt, so it cannot authorize yet
            # another send if this one ends uncertain.
            _remove_receipt(self._path(key, "provider_error"))
            GEMINI_PACER.wait()
            started = {"version": 1, "request_key": key, "identity": identity,
                       "state": "started", "started_at": _now(), "cost_status": "uncertain"}
            started_path = self._path(key, "started")
            _write_json(started_path, started)
            try:
                try:
                    response = self.client.models.generate_content(
                        model=model, contents=[payload], config=self._config())
                except genai_errors.APIError as error:
                    rejection = self._record_provider_error(key, identity, started, started_path, error)
                    if rejection is not None:
                        raise rejection from None
                    raise
                self.provider_answered = True
                usage = response.usage_metadata
                saved = self._saved(key, identity, response.text, is_truncated(response), {
                    name: getattr(usage, name, None)
                    for name in ("prompt_token_count", "candidates_token_count", "thoughts_token_count")})
                _write_json(response_path, saved)
                GEMINI_PACER.spent(saved["usage"]["input_tokens"], saved["usage"]["output_tokens"])
            except ProviderRejectedError:
                raise
            except BaseException:
                self._guard(key, "sent_request_without_durable_response")
                raise GuardedReadingError(
                    "PROD6 evaluation request may have been sent; response/usage uncertain; "
                    "new sends blocked") from None
            return self._parse(key, identity, saved)

    def _record_provider_error(self, key: str, identity: dict[str, Any], started: dict[str, Any],
                               started_path: Path, error: BaseException) -> ProviderRejectedError | None:
        """Receipt for an HTTP error answered by the provider; None keeps the attempt uncertain."""
        rejection = provider_rejection(error)
        if rejection is None:
            return None
        code, status = rejection
        try:
            _write_json(self._path(key, "provider_error"), {
                "version": 1, "request_key": key, "identity": identity,
                "state": "provider_error", "http_status": code, "provider_status": status,
                "started_at": started["started_at"], "failed_at": _now(), "cost_status": "no_response"})
        except Exception:
            return None
        try:
            _remove_receipt(started_path)
        except OSError:
            pass  # The matching provider_error receipt already resolves it.
        return ProviderRejectedError(code, status, quota_window(error))

    @staticmethod
    def _usage(raw: dict[str, Any]) -> dict[str, Any]:
        prompt, candidates = raw.get("prompt_token_count"), raw.get("candidates_token_count")
        thoughts = raw.get("thoughts_token_count") or 0  # Billed as output, not part of the candidates.
        known = all(type(value) is int and value >= 0 for value in (prompt, candidates, thoughts))
        return {"known": known, "input_tokens": prompt if known else None,
                "output_tokens": candidates + thoughts if known else None,
                "thought_tokens": thoughts if known else None}

    def _saved(self, key: str, identity: dict[str, Any], text: str | None, truncated: bool,
               usage: dict[str, Any]) -> dict[str, Any]:
        answer = {"text": text, "truncated": bool(truncated), "usage": self._usage(usage)}
        return {"version": 1, "request_key": key, "identity": identity, "state": "response_received",
                "received_at": _now(), **answer, "response_sha256": _sha(_canonical(answer))}

    def _load_response(self, path: Path, identity: dict[str, Any], key: str) -> dict[str, Any]:
        saved = _read_json(path)
        answer = {name: saved.get(name) for name in ("text", "truncated", "usage")}
        if (saved.get("identity") != identity or saved.get("request_key") != key
                or saved.get("response_sha256") != _sha(_canonical(answer))):
            raise GuardedReadingError("PROD6 evaluation receipt does not match its request")
        return saved

    def _parse(self, key: str, identity: dict[str, Any], saved: dict[str, Any]) -> MatchAnalysisResult:
        try:
            usage = saved["usage"]
            if not usage.get("known"):
                raise ValueError("Provider usage is missing; cost remains unknown")
            if saved["truncated"]:
                raise TruncatedResponseError("Match analysis truncated by max output tokens")
            return self._result_from_text(identity["model"], saved["text"],
                                          usage["input_tokens"], usage["output_tokens"])
        except BaseException:
            self._guard(key, "durable_response_requires_local_review")
            raise GuardedReadingError(
                "PROD6 evaluation response preserved; local validation needs review; "
                "new sends blocked") from None
