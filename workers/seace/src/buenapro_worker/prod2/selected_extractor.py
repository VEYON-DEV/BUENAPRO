"""One reserved Gemini call over all selected PDF pages, with private audit.

Files API transport has no model fallback/retry. Mutable ownership is shared by
shallow extractor copies so the analysis prompt adapter cannot leak uploads.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import time
from typing import Any, Callable
from uuid import uuid4

from google import genai
from google.genai import types
from pydantic import ValidationError

from buenapro_worker.extraction.gemini import (
    BASES_GOODS_INSTRUCTION, BASES_PROMPT_FILENAME, EETT_PROMPT_FILENAME,
    GeminiExtractor, MODEL_PRICES_USD_PER_MILLION, PARTICIPATION_INSTRUCTION,
    PROMPT_FILENAME, estimate_cost, read_prompt,
)
from buenapro_worker.normalization.facets import canonical_hash
from .pdf_preparation import SELECTED_DOCUMENTS_POLICY
from .reading_policy import reading_instruction, reading_policy
from .extraction_policy import get_scope_policy, validate_scope_payload
from .input_policy import (input_snapshot, validate_input_snapshot,
    prompt_for_input as prompt_for_scope, prompt_version_for_input as prompt_version_for_scope,
    schema_for_input as schema_for_scope, schema_version_for_input as schema_version_for_scope)


SELECTED_TRANSPORT_POLICY = "prod2_selected_files_transport_v1"
INLINE_MAX_BYTES = 15 * 1024 * 1024
FILE_PROCESSING_TIMEOUT = 60


class SelectedTransportError(RuntimeError):
    """The exact selected PDF set could not be transported or audited safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_write(path: Path, value: dict) -> None:
    from .batch_runner import private_write
    private_write(path, value)


def _response_text_snapshot(response: Any) -> dict:
    """Keep only generated text, never SDK inputs, image parts or opaque fields.

    Some SDK responses have no accessible text (including refusals); the audit
    must still preserve usage without coercing arbitrary objects to strings.
    """
    try:
        value = getattr(response, "text", None)
    except Exception:
        value = None
    if not isinstance(value, str):
        return {"response_text": None, "response_text_status": "unavailable",
                "response_text_sha256": None, "response_text_bytes": None}
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return {"response_text": None, "response_text_status": "unavailable",
                "response_text_sha256": None, "response_text_bytes": None}
    return {"response_text": value, "response_text_status": "captured",
            "response_text_sha256": hashlib.sha256(encoded).hexdigest(),
            "response_text_bytes": len(encoded)}


def _parse_diagnostic(error: Exception, schema: Any) -> dict | None:
    """Expose parse locations and types without the generated values/messages."""
    if isinstance(error, json.JSONDecodeError):
        return {"kind": "json", "position": error.pos,
                "line": error.lineno, "column": error.colno}
    if not isinstance(error, ValidationError):
        return None
    try:
        properties = set()
        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get("properties"), dict):
                    properties.update(value["properties"])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(schema.model_json_schema())
        rows = error.errors(include_url=False, include_input=False, include_context=False)
        details = []
        for row in rows[:100]:
            location = [part if (type(part) is int and part >= 0
                                or isinstance(part, str) and part in properties)
                        else "<key>" for part in row.get("loc", ())]
            error_type = row.get("type")
            details.append({"location": location,
                "type": error_type if isinstance(error_type, str)
                and re.fullmatch(r"[a-z0-9_]{1,80}", error_type) else "unknown"})
        return {"kind": "schema", "error_count": len(rows), "errors": details,
                "details_truncated": len(rows) > len(details)}
    except Exception:
        return {"kind": "schema", "details_unavailable": True}


class _Models:
    def __init__(self, owner):
        self.owner = owner

    def generate_content(self, **kwargs):
        return self.owner._generate(kwargs)


class _Client:
    def __init__(self, owner, actual):
        self.models = _Models(owner)
        self.files = actual.files


class SelectedGeminiExtractor(GeminiExtractor):
    supports_selected_input_policies = True

    def __init__(self, settings, prepared: dict, folder: Path,
                 before_request: Callable[[], Any] | None = None, *,
                 max_output_tokens: int = 24576, timeout_ms: int = 180_000,
                 local_text_result: dict | None = None):
        self.settings = settings
        self.prepared = copy.deepcopy(prepared)
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir() or Path(folder).is_symlink():
            raise SelectedTransportError("Private extraction audit folder is invalid")
        self._validate_prepared()
        self.local_text_result = copy.deepcopy(local_text_result)
        proof = self.prepared.get("llm1_input")
        if proof is not None:
            if (self.local_text_result is None
                    or input_snapshot(self.local_text_result, self.prepared) != proof):
                raise SelectedTransportError("Local text diagnostic differs from the frozen input proof")
        if settings.gemini_model not in MODEL_PRICES_USD_PER_MILLION:
            raise SelectedTransportError("Selected model has no configured cost estimate")
        if before_request is not None and not callable(before_request):
            raise SelectedTransportError("Model reservation callback is invalid")
        if (type(max_output_tokens) is not int or max_output_tokens not in {24576, 65536}
                or type(timeout_ms) is not int or timeout_ms not in {180_000, 300_000}):
            raise SelectedTransportError("Selected generation limits require an explicit supported setting")
        self.max_output_tokens, self.timeout_ms = max_output_tokens, timeout_ms
        actual = genai.Client(api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1)))
        self._state = {"actual": actual, "before_request": before_request, "used": False,
                       "closed": False, "remote": set(), "attempts": [], "attempt": None,
                       "response": None, "generation_started": False, "budget_failed": False,
                       "upload_unknown": False}
        self.client = _Client(self, actual)
        self.prompts = {
            "tdr": read_prompt(PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
            "eett": read_prompt(EETT_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
            "bases_good": read_prompt(BASES_PROMPT_FILENAME) + BASES_GOODS_INSTRUCTION + PARTICIPATION_INSTRUCTION,
            "bases_service": read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
        }
        self.prompts = {name: prompt_for_scope(name, self.prepared["selection_context"], prompt)
                       for name, prompt in self.prompts.items()}

    def _response_schema(self, doc_class):
        return schema_for_scope(doc_class, self.prepared["selection_context"])

    def _prompt_version(self, doc_class):
        return prompt_version_for_scope(doc_class, self.prepared["selection_context"])

    def _schema_version(self, doc_class):
        return schema_version_for_scope(doc_class, self.prepared["selection_context"])

    def _validate_raw_json(self, raw_json, doc_class):
        validate_scope_payload(raw_json, self.prepared["selection_context"])

    def _local_bytes(self, path: Path, sha: str, expected_bytes: int) -> bytes:
        path = Path(path)
        artifact_folder = Path(self.prepared["pdf_path"]).parent
        expected_name = "selected-analysis-" + self.prepared["selection_hash"]
        if (artifact_folder.is_symlink() or artifact_folder.name != expected_name
                or path.is_symlink() or path.resolve().parent != artifact_folder.resolve()):
            raise SelectedTransportError("Prepared PDF path escaped its private folder")
        try:
            if path.stat().st_size != expected_bytes:
                raise ValueError()
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError()
            return content
        except (ValueError, OSError) as error:
            raise SelectedTransportError("Prepared PDF bytes changed before transport") from error

    def _validate_prepared(self) -> None:
        value = self.prepared
        try:
            identity = value["document_set_identity"]
            digest = canonical_hash(identity)
            reading_policy(value["selection_context"])
            get_scope_policy(value["selection_context"])
            validate_input_snapshot(value["selection_context"], value.get("llm1_input"),
                value["analysis_sha256"], value["source_document_manifest"], value["page_count"])
            if (value["document_scope"] != SELECTED_DOCUMENTS_POLICY
                    or Path(value["pdf_path"]).name != "analysis.pdf"
                    or digest != value["selection_hash"] or digest != value["document_set_hash"]
                    or identity["document_scope"] != SELECTED_DOCUMENTS_POLICY
                    or identity["document_set"] != value["document_set"]
                    or identity["selection_context"] != value["selection_context"]
                    or type(value["page_count"]) is not int or not 0 < value["page_count"] <= 1000
                    or not value["chunks"]):
                raise ValueError()
            self._local_bytes(value["pdf_path"], value["analysis_sha256"], value["analysis_bytes"])
            next_page = 1
            for number, chunk in enumerate(value["chunks"], 1):
                if (any(type(chunk[key]) is not int for key in ("start_page", "end_page", "page_offset", "page_count", "bytes"))
                        or Path(chunk["pdf_path"]).name != f"part-{number:04d}.pdf"
                        or chunk["start_page"] != next_page or chunk["page_offset"] != next_page - 1
                        or chunk["end_page"] < next_page
                        or chunk["page_count"] != chunk["end_page"] - next_page + 1
                        or not 0 < chunk["bytes"] <= identity["max_analysis_bytes"] < 50_000_000):
                    raise ValueError()
                self._local_bytes(chunk["pdf_path"], chunk["sha256"], chunk["bytes"])
                next_page = chunk["end_page"] + 1
            if next_page != value["page_count"] + 1:
                raise ValueError()
        except SelectedTransportError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise SelectedTransportError("Prepared selection identity or global page ranges are invalid") from error

    def _contents(self, pdf_bytes: bytes, *, mime: str) -> list[Any]:
        if mime != "application/pdf" or hashlib.sha256(pdf_bytes).hexdigest() != self.prepared["analysis_sha256"]:
            raise SelectedTransportError("Extraction bytes do not bind the complete selected PDF")
        self._validate_prepared()
        if self.prepared.get("llm1_input", {}).get("route") == "retrieved_text":
            from .input_policy import read_verified_retrieved_text
            return [types.Part.from_text(text=read_verified_retrieved_text(self.local_text_result, self.prepared))]
        if self.prepared.get("llm1_input", {}).get("route") == "text":
            from .local_text import read_verified_text
            if input_snapshot(self.local_text_result, self.prepared) != self.prepared["llm1_input"]:
                raise SelectedTransportError("Local text proof changed before transport")
            return [types.Part.from_text(text=read_verified_text(self.local_text_result, self.prepared))]
        chunks = self.prepared["chunks"]
        use_files = len(chunks) > 1 or len(pdf_bytes) > INLINE_MAX_BYTES
        contents = []
        for number, chunk in enumerate(chunks, 1):
            text = (
                f"FRAGMENTO {number}/{len(chunks)}. Paginas GLOBALES inclusivas "
                f"{chunk['start_page']}..{chunk['end_page']}; offset={chunk['page_offset']}. "
                "Para toda cita: pagina_global = pagina_local + offset. "
                "Lee todos los fragmentos en orden como un solo expediente seleccionado; "
                "devuelve un unico JSON y nunca reinicies la numeracion entre fragmentos."
            )
            contents.append(types.Part.from_text(text=text))
            data = self._local_bytes(chunk["pdf_path"], chunk["sha256"], chunk["bytes"])
            if use_files:
                self._state["upload_unknown"] = True
                with io.BytesIO(data) as source:
                    uploaded = self.client.files.upload(file=source,
                        config=types.UploadFileConfig(mime_type=mime,
                            display_name=f"prod2-{self.prepared['analysis_sha256'][:20]}-{number}"))
                if not isinstance(uploaded.name, str) or not uploaded.name:
                    raise SelectedTransportError("Uploaded PDF did not return a cleanup resource name")
                self._state["remote"].add(uploaded.name)
                self._state["upload_unknown"] = False
                resource_name = uploaded.name
                deadline = time.monotonic() + FILE_PROCESSING_TIMEOUT
                while True:
                    state = getattr(uploaded.state, "value", uploaded.state)
                    if state == "ACTIVE":
                        break
                    if state != "PROCESSING" or time.monotonic() >= deadline:
                        raise SelectedTransportError("Uploaded PDF did not become readable")
                    time.sleep(1)
                    uploaded = self.client.files.get(name=resource_name)
                    if uploaded.name != resource_name:
                        raise SelectedTransportError("Uploaded PDF readiness resource changed")
                if not isinstance(uploaded.uri, str) or not uploaded.uri:
                    raise SelectedTransportError("Uploaded PDF has no usable file URI")
                contents.append(types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime))
            else:
                contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        return contents

    def _generate(self, kwargs: dict):
        state, attempt = self._state, self._state["attempt"]
        if state["generation_started"] or not attempt:
            raise SelectedTransportError("Only one reserved generation is allowed")
        callback = state["before_request"]
        if callback is not None:
            try:
                callback()
            except Exception:
                state["budget_failed"] = True
                raise
        prompt = kwargs["config"].system_instruction
        attempt["prompt_hash"] = canonical_hash(prompt)
        _private_write(self.folder / (attempt["attempt_id"] + ".started.private.json"),
            {**attempt, "status": "generation_reserved", "reserved_at": _now()})
        state["generation_started"] = True
        response = state["actual"].models.generate_content(**kwargs)
        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", None)
        candidates_tokens = getattr(usage, "candidates_token_count", None)
        thoughts_tokens = getattr(usage, "thoughts_token_count", None)
        known = (type(input_tokens) is int and input_tokens >= 0
                 and type(candidates_tokens) is int and candidates_tokens >= 0
                 and (thoughts_tokens is None or type(thoughts_tokens) is int and thoughts_tokens >= 0))
        output_tokens = candidates_tokens + (thoughts_tokens or 0) if known else None
        details = {field: getattr(usage, field, None) for field in (
            "prompt_token_count", "candidates_token_count", "total_token_count",
            "thoughts_token_count", "cached_content_token_count")}
        details = {key: value if type(value) is int and value >= 0 else None for key, value in details.items()}
        finish_reasons = [str(getattr(reason, "value", reason)).rsplit(".", 1)[-1]
            for candidate in getattr(response, "candidates", []) or []
            if (reason := getattr(candidate, "finish_reason", None)) is not None]
        text_snapshot = _response_text_snapshot(response)
        audit = {**attempt, "status": "response_received", "response_received_at": _now(),
            "provider_usage_known": known, "usage": details,
            "finish_reason": finish_reasons[0] if finish_reasons else None,
            "finish_reasons": finish_reasons,
            "input_tokens": input_tokens if known else None,
            "output_tokens": output_tokens if known else None,
            "cost_usd": estimate_cost(kwargs["model"], input_tokens, output_tokens) if known else None,
            "cost_is_estimate": True, "output_tokens_include_thinking": True,
            **{key: value for key, value in text_snapshot.items() if key != "response_text"}}
        state["response"] = audit
        # Save generated text and usage before parsing can fail. Keep the body
        # only in this private receipt; outcomes/attempts used by logs omit it.
        _private_write(self.folder / (attempt["attempt_id"] + ".response.private.json"),
                       {**audit, "response_text": text_snapshot["response_text"]})
        if not known:
            raise SelectedTransportError("Provider response has unknown token usage")
        return response

    def extract(self, pdf_bytes: bytes, *, mime="application/pdf", doc_class="tdr"):
        state = self._state
        if state["closed"] or state["used"]:
            raise SelectedTransportError("Selected extractor is closed or was already used")
        if doc_class not in self.prompts:
            raise SelectedTransportError("Selected document class is unsupported")
        compact_instruction = reading_instruction(self.prepared["selection_context"])
        if compact_instruction and compact_instruction not in self.prompts[doc_class]:
            raise SelectedTransportError("Selected compact reading policy is absent from the effective prompt")
        state["used"] = True
        started_clock = time.perf_counter()
        state["attempt"] = {"attempt_id": "gemini-selected-" + uuid4().hex,
            "started_at": _now(), "model": self.settings.gemini_model,
            "transport_policy": SELECTED_TRANSPORT_POLICY,
            "document_scope": SELECTED_DOCUMENTS_POLICY,
            "document_set_hash": self.prepared["document_set_hash"],
            "document_set_identity": self.prepared["document_set_identity"],
            "selection_context": self.prepared["selection_context"],
            "analysis_sha256": self.prepared["analysis_sha256"],
            "page_count": self.prepared["page_count"], "part_count": len(self.prepared["chunks"]),
            "max_output_tokens": self.max_output_tokens, "timeout_ms": self.timeout_ms}
        if "llm1_input" in self.prepared:
            state["attempt"]["llm1_input"] = copy.deepcopy(self.prepared["llm1_input"])
        status, error_type, parse_diagnostic = "success", None, None
        try:
            # Deliberately bypass GeminiExtractor.extract's retries/fallback.
            result = self._extract_with_model(self.settings.gemini_model, pdf_bytes,
                mime=mime, doc_class=doc_class, max_output_tokens=self.max_output_tokens)
            audit = state["response"]
            return replace(result, input_tokens=audit["input_tokens"],
                output_tokens=audit["output_tokens"], cost_usd=audit["cost_usd"])
        except Exception as error:
            error_type = type(error).__name__
            if state["response"] is not None:
                parse_diagnostic = _parse_diagnostic(error, self._response_schema(doc_class))
            status = ("response_parse_failure" if state["response"] is not None else
                      "error_unknown" if state["generation_started"] else
                      "budget_rejected" if state["budget_failed"] else "transport_failed")
            raise
        finally:
            self._cleanup()
            audit = {**(state["response"] or state["attempt"]), "status": status,
                "ended_at": _now(), "error_type": error_type,
                **({"parse_diagnostic": parse_diagnostic} if parse_diagnostic is not None else {}),
                "elapsed_seconds": round(max(0, time.perf_counter() - started_clock), 3),
                "model_call_started": state["generation_started"],
                "provider_usage_known": (state["response"] or {}).get("provider_usage_known", False),
                "cost_usd": (state["response"] or {}).get("cost_usd",
                    None if state["generation_started"] else 0),
                "remote_cleanup_pending": len(state["remote"]),
                "remote_files_pending": sorted(state["remote"]),
                "remote_cleanup_unknown": state["upload_unknown"]}
            _private_write(self.folder / (state["attempt"]["attempt_id"] + ".outcome.private.json"), audit)
            state["attempts"].append(audit)

    def _cleanup(self):
        state = self._state
        for name in tuple(state["remote"]):
            try:
                state["actual"].files.delete(name=name)
            except Exception:
                continue
            state["remote"].discard(name)

    @property
    def attempts(self) -> list[dict]:
        return copy.deepcopy(self._state["attempts"])

    @property
    def remote_cleanup_pending(self) -> int:
        return len(self._state["remote"])

    def count_tokens(self, *args, **kwargs):
        raise SelectedTransportError("Selected extractor performs no token-count model request")

    def close(self):
        state = self._state
        if not state["closed"]:
            self._cleanup()
            if state["attempt"]:
                _private_write(self.folder / (state["attempt"]["attempt_id"] + ".cleanup.private.json"),
                    {"attempt_id": state["attempt"]["attempt_id"], "ended_at": _now(),
                     "remote_cleanup_pending": len(state["remote"]),
                     "remote_files_pending": sorted(state["remote"]),
                     "remote_cleanup_unknown": state["upload_unknown"]})
            state["actual"].close()
            state["closed"] = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
