"""One Gemini read, optionally one independently reserved Luna backup.

Historical and unknown failures never enter this branch. The legacy database
stage name stays gemini_extraction; each provider owns a different exact hash.
"""
from __future__ import annotations

import copy

from .accepted_runner import paid_llm1
from .analysis import _document_input, extract_document, extraction_request_hash
from .repository import hash_request
from .selected_payments import provider_permission
from . import selected_extractor
from .luna_policy import luna_settings, provider_model


def allowed_llm1_models(model):
    """The routed model and, when configured, the saturation fallback that may have answered for it."""
    from buenapro_worker.prod6.gemini_pacing import saturation_fallback_model
    fallback = saturation_fallback_model()
    return [model] + ([fallback] if fallback and fallback != model else [])


def paid_selected_read(conn, settings, folder, *, convocation_id, profile_id,
                       object_type, anchor, anchor_sha, pdf, prepared,
                       document_context, limits, before_request_factory,
                       local_text_result=None, generation_options=None,
                       fallback_config=None, include_transport_facts=True, paid_stage=None):
    from .luna_fallback import validate_luna_fallback_config, close_current_gemini_max_tokens
    if fallback_config is not None:
        validate_luna_fallback_config(fallback_config)
        if not getattr(settings, "openai_api_key", ""):
            raise ValueError("Configured Luna backup requires OPENAI_API_KEY")
    primary_inputs = _document_input(settings, pdf, object_type, anchor_sha,
        prepared["source_document_manifest"], document_context)
    current = {}
    paid_stage = paid_stage or paid_llm1

    def execute(active_settings, reader_class, *, is_luna=False, failed=None):
        request_hash = extraction_request_hash(active_settings, pdf, object_type=object_type,
            original_sha256=anchor_sha, manifest=prepared["source_document_manifest"],
            document_context=document_context)
        model = provider_model(active_settings)
        evaluation_key = hash_request({"convocation": convocation_id, "kind": "gemini_extraction",
            "request_hash": request_hash, "model": model,
            "policy_version": "prod2_document_extraction_v1", "profile_id": profile_id})
        if not is_luna:
            current.update(request_hash=request_hash, evaluation_key=evaluation_key)

        def read():
            options = {"local_text_result": local_text_result} if local_text_result is not None else {}
            if is_luna:
                options["request_hash"] = request_hash
            else:
                options.update(generation_options or {})
            with reader_class(active_settings, prepared, folder,
                    before_request=before_request_factory(issue), **options) as reader:
                try:
                    bundle = {**extract_document(active_settings, pdf, object_type=object_type,
                        original_sha256=anchor_sha, manifest=prepared["source_document_manifest"],
                        document_context=document_context, extractor=reader),
                        **({"analysis_bytes": prepared["analysis_bytes"], "transport_parts": len(prepared["chunks"])}
                           if include_transport_facts else {}),
                        "document_uuid": anchor, "document_sha256": anchor_sha}
                    if failed is not None:
                        bundle["llm1_fallback_from"] = {key: copy.deepcopy(failed[key])
                            for key in ("evaluation_id", "request_hash", "attempt_id", "finish_reason", "cost_usd")}
                    return bundle
                finally:
                    if not is_luna:
                        current["attempts"] = getattr(reader, "attempts", [])
        with limits.model("llm1:" + evaluation_key), provider_permission(conn, folder, limits,
                provider="llm1", kind="llm1", request_hash=request_hash, evaluation_key=evaluation_key) as issue:
            evaluation_id, bundle, hit = paid_stage(conn, folder, convocation_id, request_hash,
                active_settings, profile_id, read)
        execution = {"primary_cost_usd": (failed or {}).get("cost_usd", 0)}
        if failed is not None:
            execution["primary_failure"] = {key: copy.deepcopy(failed[key]) for key in
                ("evaluation_id", "request_hash", "attempt_id", "finish_reason", "cost_usd")}
        return evaluation_id, bundle, hit, request_hash, execution

    try:
        try:
            return execute(settings, selected_extractor.SelectedGeminiExtractor)
        except selected_extractor.SelectedProviderRejected as error:
            # Gemini is overloaded (nothing billed): the same reading goes once to the fallback
            # model, under its own reservation and request identity.
            from buenapro_worker.prod6.gemini_pacing import SATURATION_CODES, saturation_fallback_model
            fallback = saturation_fallback_model()
            if error.code not in SATURATION_CODES or not fallback or fallback == settings.gemini_model:
                raise
            if hasattr(settings, "model_copy"):
                fallback_settings = settings.model_copy(update={"gemini_model": fallback})
            else:
                fallback_settings = copy.copy(settings)
                fallback_settings.gemini_model = fallback
            return execute(fallback_settings, selected_extractor.SelectedGeminiExtractor)
    except Exception as error:
        from buenapro_worker.extraction.gemini import TruncatedResponseError
        if (fallback_config is None or not isinstance(error, TruncatedResponseError)
                or len(current.get("attempts", [])) != 1):
            raise
        failed = close_current_gemini_max_tokens(conn, folder,
            convocation_id=convocation_id, profile_id=profile_id,
            request_hash=current["request_hash"], evaluation_key=current["evaluation_key"],
            model=provider_model(settings), prompt_hash=primary_inputs["prompt_hash"],
            analysis_sha256=prepared["analysis_sha256"], document_set_hash=prepared["document_set_hash"],
            document_set_identity=prepared["document_set_identity"],
            selection_context=prepared["selection_context"], error=error,
            attempt=current["attempts"][0], request_inputs=primary_inputs,
            llm1_input=prepared.get("llm1_input"))
        from .selected_luna import SelectedLunaExtractor
        return execute(luna_settings(settings), SelectedLunaExtractor, is_luna=True, failed=failed)
