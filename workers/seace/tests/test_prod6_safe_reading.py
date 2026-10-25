"""Offline receipt/payment contracts; providers, originals and DB stay untouched."""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from buenapro_worker.extraction.gemini import (
    EETT_PROMPT_VERSION, EETT_SCHEMA_VERSION, PROMPT_VERSION, SCHEMA_VERSION,
)
from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.prod6 import reading
from buenapro_worker.prod6.reading_schema import EettExtractionV3, TdrExtractionV3


PDF = b"%PDF-1.7 private document bytes"
PAYLOAD = {"contract": {"objeto": "Servicio de soporte"},
           "summary": {"descripcion_corta": "Soporte de una plataforma."}}


def response(text=None, *, usage=True, truncated=False):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=text or json.dumps(PAYLOAD))]),
            finish_reason="MAX_TOKENS" if truncated else "STOP")],
        usage_metadata=(types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=50) if usage else None),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    client = MagicMock()
    client.models.generate_content.return_value = response()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(reading.genai, "Client", factory)
    settings = SimpleNamespace(gemini_model="gemini-3.1-flash-lite",
                               gemini_fallback_model="gemini-2.5-flash",
                               gemini_api_key="synthetic-secret")
    directory = tmp_path / "private-receipts"
    return reading.GuardedGeminiExtractor(settings, directory), client, factory, directory


def receipts(directory, suffix):
    return list(directory.glob(f"*.{suffix}.private.json"))


def test_single_attempt_config_real_schema_and_private_receipts(setup):
    extractor, client, factory, directory = setup
    result = extractor.extract(PDF)
    options = factory.call_args.kwargs["http_options"]
    assert options.retry_options.attempts == 1
    config = client.models.generate_content.call_args.kwargs["config"]
    assert config.response_schema is TdrExtractionV3
    assert "participation.consorcio" in config.system_instruction
    assert (result.prompt_version, result.schema_version) == (
        reading.QUALITY_GENERATION.prompt_versions["tdr"], reading.QUALITY_GENERATION.schema_versions["tdr"])
    assert (result.input_tokens, result.output_tokens) == (100, 50)
    assert result.cost_usd == pytest.approx(0.0001)
    assert len(receipts(directory, "started")) == len(receipts(directory, "response")) == 1
    assert len(receipts(directory, "result")) == 1
    assert not extractor.guard_path.exists()
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert PDF not in path.read_bytes()
        assert b"synthetic-secret" not in path.read_bytes()
    assert directory.stat().st_mode & 0o777 == 0o700
    client.close.assert_called_once()


def test_result_replay_after_caller_persistence_failure_does_not_create_client(setup):
    extractor, client, factory, _ = setup
    first = extractor.extract(PDF)
    # A caller may fail its DB transaction after receiving this result. A new
    # extractor/runtime recovers the exact paid response instead of paying again.
    replay = reading.GuardedGeminiExtractor(extractor.settings, extractor.receipt_dir).extract(PDF)
    assert replay == first
    factory.assert_called_once()
    client.models.generate_content.assert_called_once()


LEGACY_METADATA = (
    ("part", "audio_transcription"),
    ("part", "media_processing"),
    ("part", "speech_metadata"),
    ("candidate", "continuation_token"),
)


def metadata_target(raw, location):
    candidate = raw["candidates"][0]
    return candidate["content"]["parts"][0] if location == "part" else candidate


def replace_synthetic_response(directory, mutate):
    response_path = receipts(directory, "response")[0]
    saved = json.loads(response_path.read_bytes())
    mutate(saved["response"])
    saved["response_sha256"] = reading._sha(reading._canonical(saved["response"]))
    response_path.write_bytes(reading._canonical(saved))
    result_path = receipts(directory, "result")[0]
    result = json.loads(result_path.read_bytes())
    result["response_sha256"] = saved["response_sha256"]
    result_path.write_bytes(reading._canonical(result))


def test_legacy_null_metadata_replay_preserves_receipts_and_never_sends(setup, monkeypatch):
    extractor, client, factory, directory = setup
    first = extractor.extract(PDF)

    def add_metadata(raw):
        for location, field in LEGACY_METADATA:
            metadata_target(raw, location)[field] = None

    replace_synthetic_response(directory, add_metadata)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    factory.reset_mock()

    def forbid_receipt_write(*args):
        pytest.fail("Existing paid receipts must replay without being rewritten")

    monkeypatch.setattr(reading, "_write_json", forbid_receipt_write)
    assert extractor.extract(PDF) == first
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert not extractor.guard_path.exists()
    factory.assert_not_called()
    client.models.generate_content.assert_not_called()


@pytest.mark.parametrize("location,field", LEGACY_METADATA)
def test_unsupported_nonnull_legacy_metadata_blocks_without_resending(setup, location, field):
    model = types.Part if location == "part" else types.Candidate
    if field in model.model_fields:
        pytest.skip("The installed SDK owns validation of its supported metadata")
    extractor, client, factory, directory = setup
    extractor.extract(PDF)
    replace_synthetic_response(directory, lambda raw: metadata_target(raw, location).update(
        {field: "unsupported non-null metadata"}))
    before = {path.name: path.read_bytes() for path in directory.glob("*.private.json")}
    factory.reset_mock()
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert {path.name: path.read_bytes() for path in directory.glob("*.private.json")} == before
    assert extractor.guard_path.exists()
    factory.assert_not_called()
    client.models.generate_content.assert_not_called()


@pytest.mark.parametrize("location", ["part", "candidate", "content", "response"])
def test_unknown_null_metadata_is_still_rejected_without_resending(setup, location):
    extractor, client, factory, directory = setup
    extractor.extract(PDF)

    def add_unknown(raw):
        target = (raw if location == "response" else raw["candidates"][0]["content"]
                  if location == "content" else metadata_target(raw, location))
        target["unknown_sdk_metadata"] = None

    replace_synthetic_response(directory, add_unknown)
    factory.reset_mock()
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    factory.assert_not_called()
    client.models.generate_content.assert_not_called()


@pytest.mark.parametrize("location,field", LEGACY_METADATA)
def test_sdk_supported_metadata_is_never_removed(monkeypatch, location, field):
    model = types.Part if location == "part" else types.Candidate
    monkeypatch.setitem(model.model_fields, field, object())
    raw = response().model_dump(mode="json")
    metadata_target(raw, location)[field] = None
    before = deepcopy(raw)
    compatible = reading._sdk_compatible_response(raw)
    assert field in metadata_target(compatible, location)
    assert compatible == before
    assert raw == before


def test_legacy_null_metadata_cannot_hide_response_hash_tampering(setup):
    extractor, client, factory, directory = setup
    extractor.extract(PDF)
    path = receipts(directory, "response")[0]
    saved = json.loads(path.read_bytes())
    for location, field in LEGACY_METADATA:
        metadata_target(saved["response"], location)[field] = None
    # Keep the original hash: adaptation must not erase a receipt change.
    path.write_bytes(reading._canonical(saved))
    factory.reset_mock()
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    factory.assert_not_called()
    client.models.generate_content.assert_not_called()


def test_raw_response_replays_when_result_persistence_was_interrupted(setup):
    extractor, client, factory, directory = setup
    first = extractor.extract(PDF)
    receipts(directory, "result")[0].unlink()
    assert extractor.extract(PDF) == first
    factory.assert_called_once()
    client.models.generate_content.assert_called_once()


def test_response_is_durable_before_validation_and_can_recover_locally(setup, monkeypatch):
    extractor, client, _, directory = setup
    original = extractor._result

    def fail_validation(*args):
        assert len(receipts(directory, "response")) == 1
        raise RuntimeError("PRIVATE invalid body includes synthetic-secret")

    monkeypatch.setattr(extractor, "_result", fail_validation)
    with pytest.raises(reading.GuardedReadingError) as failure:
        extractor.extract(PDF)
    assert "PRIVATE" not in str(failure.value)
    guard = json.loads(extractor.guard_path.read_text())
    assert guard["cost_known"] is True
    monkeypatch.setattr(extractor, "_result", original)
    assert extractor.extract(PDF).raw_json == PAYLOAD
    assert extractor.guard_path.exists()  # Replay does not clear authorization.
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"new")
    client.models.generate_content.assert_called_once()


def test_result_write_failure_keeps_response_replayable(setup, monkeypatch):
    extractor, client, _, directory = setup
    write = reading._write_json

    def fail_result(path, value):
        if path.name.endswith(".result.private.json"):
            raise OSError("private persistence detail")
        write(path, value)

    monkeypatch.setattr(reading, "_write_json", fail_result)
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert len(receipts(directory, "response")) == 1
    monkeypatch.setattr(reading, "_write_json", write)
    assert extractor.extract(PDF).raw_json == PAYLOAD
    client.models.generate_content.assert_called_once()


def test_sent_failure_blocks_same_and_other_documents_without_raw_exception(setup, caplog):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = TimeoutError("API key synthetic-secret PRIVATE body")
    with pytest.raises(reading.GuardedReadingError) as error:
        extractor.extract(PDF)
    assert "synthetic-secret" not in str(error.value)
    assert "PRIVATE" not in extractor.guard_path.read_text()
    assert "synthetic-secret" not in caplog.text
    started = json.loads(receipts(directory, "started")[0].read_text())
    assert started["cost_usd"] is None
    assert started["cost_status"] == "uncertain"
    for content in [PDF, PDF + b"another"]:
        with pytest.raises(reading.ReadingBlockedError):
            reading.GuardedGeminiExtractor(extractor.settings, directory).extract(content)
    factory.assert_called_once()
    client.models.generate_content.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("kind", ["missing_usage", "bad_json", "truncated"])
def test_known_response_never_falls_back_or_retries(setup, kind):
    extractor, client, _, directory = setup
    client.models.generate_content.return_value = response(
        text="INVALID PRIVATE text" if kind == "bad_json" else None,
        usage=kind != "missing_usage", truncated=kind == "truncated")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    saved = json.loads(receipts(directory, "response")[0].read_text())
    assert saved["cost_usd"] is None if kind == "missing_usage" else saved["cost_usd"] > 0
    assert not receipts(directory, "result")
    assert extractor.guard_path.exists()
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"new")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)  # Local replay of malformed response; never send.
    client.models.generate_content.assert_called_once()


def test_crash_after_started_receipt_remains_blocking_even_if_guard_removed(setup):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    extractor.guard_path.unlink()
    factory.reset_mock()
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"different")
    factory.assert_not_called()
    assert extractor.guard_path.exists()
    assert len(receipts(directory, "started")) == 1


def test_other_pending_response_blocks_new_send_but_allows_local_replay(setup):
    extractor, client, factory, directory = setup
    first = extractor.extract(PDF)
    receipts(directory, "result")[0].unlink()
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"another")
    assert extractor.extract(PDF) == first
    factory.assert_called_once()
    client.models.generate_content.assert_called_once()


def test_lock_prevents_two_paid_sends_for_same_document(setup):
    extractor, client, factory, directory = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: reading.GuardedGeminiExtractor(extractor.settings, directory).extract(PDF),
            range(2)))
    assert results[0] == results[1]
    client.models.generate_content.assert_called_once()
    factory.assert_called_once()


def test_eett_schema_prompt_and_identity_are_distinct(setup):
    extractor, client, _, directory = setup
    client.models.generate_content.return_value = response(text=json.dumps(
        PAYLOAD | {"goods": {"items": [{"nombre": "Laptop", "cantidad": 2}]}}))
    result = extractor.extract(PDF, doc_class="eett")
    assert isinstance(result.extraction, EettExtractionV3)
    assert (result.prompt_version, result.schema_version) == (
        reading.QUALITY_GENERATION.prompt_versions["eett"], reading.QUALITY_GENERATION.schema_versions["eett"])
    assert client.models.generate_content.call_args.kwargs["config"].response_schema is EettExtractionV3
    extractor.extract(PDF, doc_class="tdr")
    assert len(receipts(directory, "started")) == 2


def test_tampered_response_usage_or_result_blocks_without_network(setup):
    extractor, _, factory, directory = setup
    extractor.extract(PDF)
    path = receipts(directory, "result")[0]
    saved = json.loads(path.read_text())
    saved["raw_json"]["summary"]["descripcion_corta"] = "tampered"
    path.write_text(json.dumps(saved))
    factory.reset_mock()
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    factory.assert_not_called()
    assert extractor.guard_path.exists()


def test_rejects_bad_inputs_before_client_or_receipts(setup):
    extractor, _, factory, directory = setup
    for kwargs in [{"doc_class": "bases_good"}, {"mime": "application/msword"}]:
        with pytest.raises(ValueError):
            extractor.extract(PDF, **kwargs)
    with pytest.raises(ValueError):
        extractor.extract(b"")
    assert not receipts(directory, "started")
    factory.assert_not_called()
def test_output_cost_includes_provider_thinking_tokens():
    usage = reading.GuardedGeminiExtractor._usage({"prompt_token_count": 100,
                                          "candidates_token_count": 50,
                                          "thoughts_token_count": 20})
    assert usage["known"]
    assert usage["output_tokens"] == 70
    assert usage["candidate_tokens"] == 50
    assert usage["thought_tokens"] == 20


# --- HTTP errors answered by the provider -------------------------------------------------

SECRET_MESSAGE = "prepayment credits are depleted for key synthetic-secret"


def api_error(code, status="RESOURCE_EXHAUSTED", cls=None):
    cls = cls or (genai_errors.ServerError if code >= 500 else genai_errors.ClientError)
    return cls(code, {"error": {"code": code, "message": SECRET_MESSAGE, "status": status}})


@pytest.mark.parametrize("code,status", [(402, "RESOURCE_EXHAUSTED"), (429, "RESOURCE_EXHAUSTED"),
                                         (400, "INVALID_ARGUMENT"), (503, "UNAVAILABLE")])
def test_provider_http_error_opens_no_guard_and_leaves_only_a_private_error_receipt(setup, code, status):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = api_error(code, status)
    with pytest.raises(reading.ProviderRejectedError) as error:
        extractor.extract(PDF)
    assert not isinstance(error.value, reading.GuardedReadingError)
    assert (error.value.code, error.value.status) == (code, status)
    assert "synthetic-secret" not in str(error.value) and SECRET_MESSAGE not in str(error.value)
    assert not extractor.guard_path.exists()
    assert not receipts(directory, "started") and not receipts(directory, "response")
    (path,) = receipts(directory, "provider_error")
    saved = json.loads(path.read_text())
    assert (saved["http_status"], saved["provider_status"]) == (code, status)
    assert saved["state"] == "provider_error" and saved["cost_usd"] is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"synthetic-secret" not in path.read_bytes() and b"depleted" not in path.read_bytes()
    assert PDF not in path.read_bytes()
    client.close.assert_called_once()
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_after_a_provider_error_the_same_and_other_documents_can_send(setup):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = [api_error(402), response(), response()]
    with pytest.raises(reading.ProviderRejectedError):
        extractor.extract(PDF)
    assert extractor.extract(PDF).raw_json == PAYLOAD  # The retry sends again...
    assert not receipts(directory, "provider_error")  # ...and consumes the error receipt.
    assert extractor.extract(PDF + b"other").raw_json == PAYLOAD
    assert client.models.generate_content.call_count == 3
    assert extractor.provider_answered
    assert not extractor.guard_path.exists()


def test_a_provider_error_for_one_document_does_not_unblock_an_uncertain_one(setup):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    extractor.guard_path.unlink()
    client.models.generate_content.side_effect = api_error(503, "UNAVAILABLE")
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"other")  # The earlier started receipt still blocks.
    assert client.models.generate_content.call_count == 1


@pytest.mark.parametrize("failure", [TimeoutError("unknown"), httpx.ReadTimeout("slow"),
                                     httpx.RemoteProtocolError("cut"), ConnectionResetError("reset"),
                                     KeyboardInterrupt(), SystemExit(1)])
def test_any_other_failure_after_sending_stays_uncertain(setup, failure):
    extractor, client, _, directory = setup
    client.models.generate_content.side_effect = failure
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert extractor.guard_path.exists() and receipts(directory, "started")
    assert not receipts(directory, "provider_error")
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF)
    client.models.generate_content.assert_called_once()


@pytest.mark.parametrize("code", [None, 0, 302, "402", True, 700])
def test_api_error_without_an_http_error_code_stays_uncertain(setup, code):
    extractor, client, _, directory = setup
    error = genai_errors.APIError(1, {"error": {"status": "X"}})
    error.code = code
    client.models.generate_content.side_effect = error
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert extractor.guard_path.exists() and not receipts(directory, "provider_error")


@pytest.mark.parametrize("status", ["Payment Required", "resource_exhausted", "X" * 60,
                                    "KEY synthetic-secret", None, 402])
def test_only_a_plain_api_status_word_is_stored(setup, status):
    extractor, client, _, directory = setup
    client.models.generate_content.side_effect = api_error(402, status)
    with pytest.raises(reading.ProviderRejectedError) as error:
        extractor.extract(PDF)
    assert error.value.status is None
    assert json.loads(receipts(directory, "provider_error")[0].read_text())["provider_status"] is None


def test_provider_error_receipt_write_failure_stays_uncertain(setup, monkeypatch):
    extractor, client, _, directory = setup
    client.models.generate_content.side_effect = api_error(402)
    write = reading._write_json

    def fail_error_receipt(path, value):
        if ".provider_error." in path.name:
            raise OSError("disk")
        write(path, value)

    monkeypatch.setattr(reading, "_write_json", fail_error_receipt)
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert extractor.guard_path.exists() and receipts(directory, "started")


def test_crash_between_the_error_receipt_and_removing_started_is_still_resolved(setup):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = api_error(402)
    with pytest.raises(reading.ProviderRejectedError):
        extractor.extract(PDF)
    (error_path,) = receipts(directory, "provider_error")
    saved = json.loads(error_path.read_text())
    started = directory / f"{saved['request_key']}.started.private.json"
    reading._write_json(started, {"version": 1, "request_key": saved["request_key"],
        "state": "started", "started_at": saved["started_at"]})
    client.models.generate_content.side_effect = None
    client.models.generate_content.return_value = response()
    assert extractor.extract(PDF + b"other").raw_json == PAYLOAD
    # A started receipt from a different, later attempt is not resolved by that error.
    reading._write_json(started, {"version": 1, "request_key": saved["request_key"],
        "state": "started", "started_at": "2999-01-01T00:00:00+00:00"})
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"another")


def test_malformed_or_foreign_error_receipt_does_not_resolve_started(setup):
    extractor, client, _, directory = setup
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    (started,) = receipts(directory, "started")
    key = started.name.removesuffix(".started.private.json")
    extractor.guard_path.unlink()
    error_path = directory / f"{key}.provider_error.private.json"
    error_path.write_text("not json")
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"other")
    extractor.guard_path.unlink()
    reading._write_json(error_path, {"version": 1, "request_key": "f" * 64, "state": "provider_error",
        "http_status": 402, "started_at": json.loads(started.read_text())["started_at"]})
    with pytest.raises(reading.ReadingBlockedError):
        extractor.extract(PDF + b"other")
    client.models.generate_content.assert_called_once()


def read_only_extractor(extractor, directory):
    return reading.GuardedGeminiExtractor(extractor.settings, directory, allow_new_requests=False)


def test_replay_only_job_retries_after_a_provider_error_but_not_after_an_uncertain_send(setup):
    extractor, client, factory, directory = setup
    client.models.generate_content.side_effect = api_error(429)
    with pytest.raises(reading.ProviderRejectedError):
        extractor.extract(PDF)
    retry = read_only_extractor(extractor, directory)
    assert retry.recover_before_download(reading._sha(PDF), mime="application/pdf", doc_class="tdr") is None
    client.models.generate_content.side_effect = None
    client.models.generate_content.return_value = response()
    assert retry.extract(PDF).raw_json == PAYLOAD
    # The same state after an uncertain send never reaches the network.
    other = PDF + b"uncertain"
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(other)
    extractor.guard_path.unlink()
    with pytest.raises(reading.ReadingBlockedError):
        read_only_extractor(extractor, directory).recover_before_download(
            reading._sha(other), mime="application/pdf", doc_class="tdr")
    with pytest.raises(reading.ReadingBlockedError):
        read_only_extractor(extractor, directory).extract(other)
    assert client.models.generate_content.call_count == 3


def test_replay_only_job_without_any_receipt_is_still_blocked(setup):
    extractor, client, factory, directory = setup
    with pytest.raises(reading.ReadingBlockedError):
        read_only_extractor(extractor, directory).recover_before_download(
            reading._sha(PDF), mime="application/pdf", doc_class="tdr")
    with pytest.raises(reading.ReadingBlockedError):
        read_only_extractor(extractor, directory).extract(PDF)
    factory.assert_not_called()


def test_a_new_attempt_consumes_the_error_receipt_so_an_uncertain_retry_blocks_replay_only_jobs(setup):
    extractor, client, _, directory = setup
    client.models.generate_content.side_effect = api_error(402)
    with pytest.raises(reading.ProviderRejectedError):
        extractor.extract(PDF)
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        extractor.extract(PDF)
    assert not receipts(directory, "provider_error")
    extractor.guard_path.unlink()
    with pytest.raises(reading.ReadingBlockedError):
        read_only_extractor(extractor, directory).recover_before_download(
            reading._sha(PDF), mime="application/pdf", doc_class="tdr")
    assert client.models.generate_content.call_count == 2


def test_provider_answered_is_false_for_local_replays(setup):
    extractor, client, _, _ = setup
    extractor.extract(PDF)
    assert extractor.provider_answered
    replay = reading.GuardedGeminiExtractor(extractor.settings, extractor.receipt_dir)
    replay.extract(PDF)
    assert not replay.provider_answered
