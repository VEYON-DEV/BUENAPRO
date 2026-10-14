"""One explicitly reserved Luna read of the complete, verified selected input.

No Gemini client is constructed. The shared state survives shallow prompt
adapters, and provider output/usage is committed before application parsing.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import time
from typing import Any, Callable
from uuid import uuid4

from buenapro_worker.extraction.gemini import (
    BASES_GOODS_INSTRUCTION, BASES_PROMPT_FILENAME, EETT_PROMPT_FILENAME,
    EmptyExtractionError, ExtractionResult, PARTICIPATION_INSTRUCTION,
    PROMPT_FILENAME, TruncatedResponseError, parse_json_response, read_prompt,
)
from buenapro_worker.normalization.facets import canonical_hash
from .input_policy import input_snapshot, prompt_for_input as prompt_for_scope
from .luna_policy import (
    LUNA_REQUEST_POLICY, LUNA_SELECTED_TRANSPORT_POLICY, provider_instruction,
)
from .luna_transport import (
    LunaResponsesClient, LunaTransportError, MAX_PDF_BYTES, luna_pdf_part,
    parse_luna_response, parse_luna_usage,
)
from .pdf_preparation import SELECTED_DOCUMENTS_POLICY
from .reading_policy import reading_instruction
from .selected_extractor import (
    SelectedGeminiExtractor, SelectedTransportError, _now, _private_write,
)


_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _provider_output(body: dict) -> dict:
    """Preserve generated output without the Responses API's echoed input fields.

In particular, never persist instructions, request headers, inline files, or
input text. Unexpected output is still rejected by the transport parser.
"""
    result = {key: body[key] for key in ("id", "object", "model", "status")
              if isinstance(body.get(key), str)}
    incomplete = body.get("incomplete_details")
    if isinstance(incomplete, dict) and isinstance(incomplete.get("reason"), str):
        result["incomplete_details"] = {"reason": incomplete["reason"]}
    usage = body.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {key: usage[key] for key in
                           ("input_tokens", "output_tokens", "total_tokens")
                           if type(usage.get(key)) is int}
        for name, key in (("input_tokens_details", "cached_tokens"),
                          ("output_tokens_details", "reasoning_tokens")):
            details = usage.get(name)
            if isinstance(details, dict) and type(details.get(key)) is int:
                result["usage"][name] = {key: details[key]}
    error = body.get("error")
    if isinstance(error, dict):
        result["error"] = {key: error[key] for key in ("code", "type")
                           if isinstance(error.get(key), str)}
    output = body.get("output")
    if isinstance(output, list):
        saved = []
        for item in output:
            if not isinstance(item, dict):
                continue
            # Raw assistant text, including a truncated JSON, is retained.
            if item.get("type") == "message" and item.get("role") == "assistant":
                message = {key: item[key] for key in ("id", "type", "role", "status")
                           if isinstance(item.get(key), str)}
                message["content"] = []
                if isinstance(item.get("content"), list):
                    for part in item["content"]:
                        if not isinstance(part, dict):
                            continue
                        kind, key = part.get("type"), None
                        if kind == "output_text":
                            key = "text"
                        elif kind == "refusal":
                            key = "refusal"
                        if key and isinstance(part.get(key), str):
                            message["content"].append({"type": kind, key: part[key]})
                saved.append(message)
            elif item.get("type") == "reasoning":
                # Reasoning accounting is in usage. Its encrypted payload and
                # other opaque fields are not needed for application recovery.
                saved.append({key: item[key] for key in ("id", "type", "status")
                              if isinstance(item.get(key), str)})
        result["output"] = saved
    return result


class SelectedLunaExtractor(SelectedGeminiExtractor):
    """Compatible selected-reader protocol; inherited methods validate local PDF identity."""

    provider = "openai"
    supports_selected_input_policies = True

    def __init__(self, settings, prepared: dict, folder: Path,
                 before_request: Callable[[], Any] | None = None, *,
                 local_text_result: dict | None = None, request_hash: str | None = None):
        # Do not call the superclass constructor: it creates a Gemini client.
        self.settings = settings
        self.prepared = copy.deepcopy(prepared)
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir() or Path(folder).is_symlink():
            raise SelectedTransportError("Private extraction audit folder is invalid")
        self._validate_prepared()
        self.local_text_result = copy.deepcopy(local_text_result)
        proof = self.prepared.get("llm1_input")
        if proof is not None and (self.local_text_result is None
                or input_snapshot(self.local_text_result, self.prepared) != proof):
            raise SelectedTransportError("Local text diagnostic differs from the frozen input proof")
        if before_request is not None and not callable(before_request):
            raise SelectedTransportError("Model reservation callback is invalid")
        if request_hash is not None and (not isinstance(request_hash, str)
                or not _SHA.fullmatch(request_hash)):
            raise SelectedTransportError("Selected request hash is invalid")
        self.max_output_tokens = LUNA_REQUEST_POLICY["max_output_tokens"]
        self.timeout_ms = 300_000
        self.prompts = {
            "tdr": read_prompt(PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
            "eett": read_prompt(EETT_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
            "bases_good": (read_prompt(BASES_PROMPT_FILENAME)
                           + BASES_GOODS_INSTRUCTION + PARTICIPATION_INSTRUCTION),
            "bases_service": read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION,
        }
        self.prompts = {name: prompt_for_scope(name, self.prepared["selection_context"], prompt)
                       for name, prompt in self.prompts.items()}
        actual = LunaResponsesClient(getattr(settings, "openai_api_key", None))
        self._state = {"actual": actual, "before_request": before_request, "used": False,
                       "closed": False, "attempts": [], "attempt": None, "response": None,
                       "generation_started": False, "budget_failed": False,
                       "request_hash": request_hash}

    def _contents(self, pdf_bytes: bytes, *, mime: str) -> list[dict]:
        if (mime != "application/pdf" or not isinstance(pdf_bytes, bytes)
                or hashlib.sha256(pdf_bytes).hexdigest() != self.prepared["analysis_sha256"]):
            raise SelectedTransportError("Extraction bytes do not bind the complete selected PDF")
        self._validate_prepared()
        if self.prepared.get("llm1_input", {}).get("route") == "retrieved_text":
            from .input_policy import read_verified_retrieved_text
            return [{"type": "input_text", "text": read_verified_retrieved_text(self.local_text_result, self.prepared)}]
        if self.prepared.get("llm1_input", {}).get("route") == "text":
            from .local_text import read_verified_text
            if input_snapshot(self.local_text_result, self.prepared) != self.prepared["llm1_input"]:
                raise SelectedTransportError("Local text proof changed before transport")
            return [{"type": "input_text",
                     "text": read_verified_text(self.local_text_result, self.prepared)}]
        chunks = self.prepared["chunks"]
        # The aggregate file input has an independent provider ceiling. It is
        # never worked around by omitting a part or making another paid request.
        if sum(chunk["bytes"] for chunk in chunks) >= MAX_PDF_BYTES:
            raise SelectedTransportError("Complete selected PDF set exceeds the Luna inline byte limit")
        contents = []
        for number, chunk in enumerate(chunks, 1):
            contents.append({"type": "input_text", "text": (
                f"FRAGMENTO {number}/{len(chunks)}. Paginas GLOBALES inclusivas "
                f"{chunk['start_page']}..{chunk['end_page']}; offset={chunk['page_offset']}. "
                "Para toda cita: pagina_global = pagina_local + offset. "
                "Lee todos los fragmentos en orden como un solo expediente seleccionado; "
                "devuelve un unico JSON y nunca reinicies la numeracion entre fragmentos."
            )})
            data = self._local_bytes(chunk["pdf_path"], chunk["sha256"], chunk["bytes"])
            contents.append(luna_pdf_part(data, filename=f"part-{number:04d}.pdf"))
        return contents

    def _record_response(self, response) -> None:
        """HTTP callback: durable usage/output before status, JSON or schema validation."""
        state, attempt = self._state, self._state["attempt"]
        if not attempt or not state["generation_started"] or state["response"] is not None:
            raise SelectedTransportError("Unexpected selected provider response")
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        usage = parse_luna_usage(body) if isinstance(body, dict) else None
        audit = {**attempt, "status": "response_received", "response_received_at": _now(),
            "http_status": response.status_code,
            "response_body_sha256": hashlib.sha256(response.content).hexdigest(),
            "response_body_bytes": len(response.content),
            "raw_response_projection": "generated_output_only_v1",
            "provider_response_status": body.get("status") if isinstance(body, dict) else None,
            "raw_response": _provider_output(body) if isinstance(body, dict) else None,
            "provider_usage_known": usage is not None,
            "usage": ({"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                       "cached_input_tokens": usage.cached_input_tokens,
                       "reasoning_tokens": usage.reasoning_tokens, "total_tokens": usage.total_tokens}
                      if usage is not None else None),
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "cost_is_estimate": True, "output_tokens_include_thinking": True}
        if isinstance(body, dict):
            incomplete = body.get("incomplete_details")
            audit["incomplete_reason"] = (incomplete.get("reason")
                                          if isinstance(incomplete, dict) else None)
            output = audit["raw_response"].get("output", [])
            audit["refusal"] = "\n".join(part["refusal"] for item in output
                for part in item.get("content", []) if part.get("type") == "refusal") or None
            audit["finish_reason"] = (audit["incomplete_reason"]
                                      if body.get("status") == "incomplete" else body.get("status"))
        _private_write(self.folder / (attempt["attempt_id"] + ".response.private.json"), audit)
        state["response"] = audit

    def extract(self, pdf_bytes: bytes, *, mime="application/pdf", doc_class="tdr") -> ExtractionResult:
        state = self._state
        if state["closed"] or state["used"]:
            raise SelectedTransportError("Selected extractor is closed or was already used")
        if doc_class not in self.prompts:
            raise SelectedTransportError("Selected document class is unsupported")
        compact = reading_instruction(self.prepared["selection_context"])
        if compact and compact not in self.prompts[doc_class]:
            raise SelectedTransportError("Selected compact reading policy is absent from the effective prompt")
        state["used"] = True
        started_clock = time.perf_counter()
        schema = self._response_schema(doc_class)
        instruction = provider_instruction(self.prompts[doc_class], schema)
        state["attempt"] = {"attempt_id": "openai-selected-" + uuid4().hex,
            "started_at": _now(), "provider": self.provider,
            "model": LUNA_REQUEST_POLICY["model"],
            "reasoning_effort": LUNA_REQUEST_POLICY["reasoning_effort"],
            "provider_request": copy.deepcopy(LUNA_REQUEST_POLICY),
            "transport_policy": LUNA_SELECTED_TRANSPORT_POLICY,
            "document_scope": SELECTED_DOCUMENTS_POLICY,
            "document_set_hash": self.prepared["document_set_hash"],
            "document_set_identity": self.prepared["document_set_identity"],
            "selection_context": self.prepared["selection_context"],
            "analysis_sha256": self.prepared["analysis_sha256"],
            "page_count": self.prepared["page_count"], "part_count": len(self.prepared["chunks"]),
            "max_output_tokens": self.max_output_tokens, "timeout_ms": self.timeout_ms,
            "prompt_hash": canonical_hash(instruction)}
        if state["request_hash"] is not None:
            state["attempt"]["request_hash"] = state["request_hash"]
        if "llm1_input" in self.prepared:
            state["attempt"]["llm1_input"] = copy.deepcopy(self.prepared["llm1_input"])
        status, error_type = "success", None
        try:
            contents = self._contents(pdf_bytes, mime=mime)
            callback = state["before_request"]
            if callback is not None:
                try:
                    callback()
                except Exception:
                    state["budget_failed"] = True
                    raise
            _private_write(self.folder / (state["attempt"]["attempt_id"] + ".started.private.json"),
                           {**state["attempt"], "status": "generation_reserved", "reserved_at": _now()})
            state["generation_started"] = True
            body = state["actual"].generate(model=LUNA_REQUEST_POLICY["model"],
                instructions=instruction, contents=contents, max_output_tokens=self.max_output_tokens,
                on_response=self._record_response)
            # The callback is part of the transport contract, not an optional
            # best-effort log. A response without it cannot be finalized.
            audit = state["response"]
            if audit is None:
                raise SelectedTransportError("Provider response has no durable selected audit")
            if not audit["provider_usage_known"]:
                raise SelectedTransportError("Provider response has unknown token usage")
            parsed = parse_luna_response(body)
            if parsed.truncated:
                raise TruncatedResponseError("Luna response was truncated by max output tokens")
            if parsed.refusal:
                raise SelectedTransportError("Provider refused the selected extraction")
            if not parsed.completed:
                raise SelectedTransportError("Provider response did not complete the selected extraction")
            raw = parse_json_response(parsed.text)
            if not raw or not any(raw.values()):
                raise EmptyExtractionError("Luna returned an empty extraction")
            self._validate_raw_json(raw, doc_class)
            extraction = schema.model_validate(raw)
            return ExtractionResult(extraction=extraction, raw_json=raw,
                model=LUNA_REQUEST_POLICY["model"], prompt_version=self._prompt_version(doc_class),
                schema_version=self._schema_version(doc_class), input_tokens=audit["input_tokens"],
                output_tokens=audit["output_tokens"], cost_usd=audit["cost_usd"],
                requires_human_review=True)
        except Exception as error:
            error_type = type(error).__name__
            status = ("response_parse_failure" if state["response"] is not None else
                      "error_unknown" if state["generation_started"] else
                      "budget_rejected" if state["budget_failed"] else "transport_failed")
            raise
        finally:
            audit = {**(state["response"] or state["attempt"]), "status": status,
                "ended_at": _now(), "error_type": error_type,
                "elapsed_seconds": round(max(0, time.perf_counter() - started_clock), 3),
                "model_call_started": state["generation_started"],
                "provider_usage_known": (state["response"] or {}).get("provider_usage_known", False),
                "cost_usd": (state["response"] or {}).get("cost_usd",
                    None if state["generation_started"] else 0),
                "remote_cleanup_pending": 0, "remote_files_pending": [],
                "remote_cleanup_unknown": False}
            _private_write(self.folder / (state["attempt"]["attempt_id"] + ".outcome.private.json"), audit)
            state["attempts"].append(audit)

    @property
    def remote_cleanup_pending(self) -> int:
        return 0

    def count_tokens(self, *args, **kwargs):
        raise SelectedTransportError("Selected extractor performs no token-count model request")

    def close(self):
        state = self._state
        if not state["closed"]:
            state["actual"].close()
            state["closed"] = True
