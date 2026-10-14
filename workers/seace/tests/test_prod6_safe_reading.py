"""Offline receipt/payment contracts; providers, originals and DB stay untouched."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import types

from buenapro_worker.extraction.gemini import (
    EETT_PROMPT_VERSION, EETT_SCHEMA_VERSION, PROMPT_VERSION, SCHEMA_VERSION,
)
from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.prod6 import reading


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
    assert config.response_schema is TdrExtractionV2
    assert "participation.consorcio" in config.system_instruction
    assert (result.prompt_version, result.schema_version) == (PROMPT_VERSION, SCHEMA_VERSION)
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
    assert isinstance(result.extraction, EettExtractionV1)
    assert (result.prompt_version, result.schema_version) == (EETT_PROMPT_VERSION, EETT_SCHEMA_VERSION)
    assert client.models.generate_content.call_args.kwargs["config"].response_schema is EettExtractionV1
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
