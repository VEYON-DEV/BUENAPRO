"""A new reading prompt must not strand paid receipts of the old one; offline, no provider."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from buenapro_worker.extraction.commercial_classification import POLICY_KEY, TAXONOMY_VERSION
from buenapro_worker.extraction.gemini import EETT_PROMPT_VERSION, PROMPT_VERSION, read_prompt
from buenapro_worker.prod6 import reading
from buenapro_worker.prod6.reading_schema import (
    EettExtractionV3, TdrExtractionV3, lenient_page, normalize_currency,
)

PDF = b"%PDF-1.7 private document bytes"
SHA = "ab" * 32
PAYLOAD = {"contract": {"objeto": "Servicio de soporte"},
           "summary": {"descripcion_corta": "Soporte de una plataforma."}}
OPT_IN = {POLICY_KEY: TAXONOMY_VERSION}
# Request keys of the frozen legacy generation for SHA, computed before the quality prompt existed.
LEGACY_KEYS = {
    ("tdr", False): "d4b5cb9a94370924d3153cd3e2ee29145543c7b7c4fb9c54056fb31c39dc3cd5",
    ("eett", False): "1779b2e5852bbffa89c6f83d5ffbf37c4af6ea48345b7e912576e53c7aedea83",
    ("tdr", True): "71a9678c2c9407e3a479e34fdfa37575e2cfc3af57f99412d23d829ddba28d7a",
    ("eett", True): "62475b71dd72f73a83b6c32fa3953ac07e3d0feebe6ed38298234811492928f1",
}


def response(payload=None):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(payload or PAYLOAD))]),
            finish_reason="STOP")],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=50))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    client = MagicMock()
    client.models.generate_content.return_value = response()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(reading.genai, "Client", factory)
    settings = SimpleNamespace(gemini_model="gemini-3.1-flash-lite",
                               gemini_fallback_model="gemini-2.5-flash", gemini_api_key="synthetic-secret")
    return settings, client, tmp_path / "readings"


def legacy(settings, directory, **options):
    return reading.GuardedGeminiExtractor(settings, directory, generations=(reading.LEGACY_GENERATION,), **options)


def current(settings, directory, **options):
    return reading.GuardedGeminiExtractor(settings, directory, **options)


def files(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def api_error(code):
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "private", "status": "RESOURCE_EXHAUSTED"}})


# --- the legacy identity is frozen -------------------------------------------------------


@pytest.mark.parametrize("doc_class", ["tdr", "eett"])
@pytest.mark.parametrize("policy", [None, OPT_IN])
def test_legacy_request_keys_are_pinned(setup, doc_class, policy):
    settings, _, directory = setup
    reader = legacy(settings, directory, policy_context=policy)
    _, identity, key = reader._request_identity(SHA, mime="application/pdf", doc_class=doc_class)
    assert key == LEGACY_KEYS[(doc_class, policy is not None)]
    assert "reading_generation" not in identity


def test_legacy_generation_must_remain_in_the_chain(setup):
    settings, _, directory = setup
    with pytest.raises(reading.ReadingPolicyError):
        reading.GuardedGeminiExtractor(settings, directory, generations=(reading.QUALITY_GENERATION,))


@pytest.mark.parametrize("doc_class", ["tdr", "eett"])
def test_current_generation_has_its_own_prompt_version_schema_and_key(setup, doc_class):
    settings, _, directory = setup
    prompt, identity, key = current(settings, directory)._request_identity(
        SHA, mime="application/pdf", doc_class=doc_class)
    assert identity["reading_generation"] == reading.QUALITY_GENERATION_MARKER
    assert identity["prompt_version"] == f"prod6_{doc_class}_reading_v1"
    assert identity["prompt_version"] not in {PROMPT_VERSION, EETT_PROMPT_VERSION}
    assert key != LEGACY_KEYS[(doc_class, False)]
    # Legacy prompt files stay untouched: their hash is part of every old request key.
    assert read_prompt("tdr_extraction_v2.txt") != read_prompt("prod6_tdr_reading_v1.txt")
    assert "participation.consorcio" in prompt


def test_quality_prompts_carry_the_required_rules():
    for name in ("prod6_tdr_reading_v1.txt", "prod6_eett_reading_v1.txt"):
        text = read_prompt(name)
        folded = text.lower()
        for fragment in ("cita literal", "pagina", "moneda", "no sumes", "mensual", "no expandas siglas",
                         "no completes datos ausentes", "contando desde 1"):
            assert fragment in folded, (name, fragment)
    # The service prompt separates the contracting system from the form of execution.
    assert "forma_ejecucion" in read_prompt("prod6_tdr_reading_v1.txt")
    assert "NO la forma de ejecucion" in read_prompt("prod6_tdr_reading_v1.txt")


# --- old receipts stay recoverable -------------------------------------------------------


@pytest.mark.parametrize("doc_class", ["tdr", "eett"])
def test_durable_result_of_the_legacy_prompt_replays_without_any_send(setup, doc_class):
    settings, client, directory = setup
    first = legacy(settings, directory).extract(PDF, doc_class=doc_class)
    before = files(directory)
    replay = current(settings, directory)
    again = replay.extract(PDF, doc_class=doc_class)
    assert again == first
    assert again.prompt_version == (EETT_PROMPT_VERSION if doc_class == "eett" else PROMPT_VERSION)
    assert files(directory) == before
    assert client.models.generate_content.call_count == 1 and not replay.provider_answered


def test_legacy_response_without_result_is_parsed_locally_not_resent(setup):
    settings, client, directory = setup
    first = legacy(settings, directory).extract(PDF)
    next(directory.glob("*.result.private.json")).unlink()
    again = current(settings, directory).extract(PDF)
    assert again == first
    assert len(list(directory.glob("*.result.private.json"))) == 1
    assert client.models.generate_content.call_count == 1


def test_recovery_only_job_finds_the_legacy_response_before_downloading(setup):
    settings, client, directory = setup
    first = legacy(settings, directory).extract(PDF)
    next(directory.glob("*.result.private.json")).unlink()
    recovery = current(settings, directory, allow_new_requests=False)
    result = recovery.recover_before_download(reading._sha(PDF), mime="application/pdf",
                                              doc_class="tdr")
    assert result == first
    assert client.models.generate_content.call_count == 1


def test_unresolved_legacy_send_still_blocks_every_new_send(setup):
    settings, client, directory = setup
    client.models.generate_content.side_effect = TimeoutError("unknown")
    with pytest.raises(reading.GuardedReadingError):
        legacy(settings, directory).extract(PDF)
    guard = directory / "guard.json"
    guard.unlink()
    client.models.generate_content.reset_mock(side_effect=True)
    for content in (PDF, PDF + b"another"):
        with pytest.raises(reading.ReadingBlockedError):
            current(settings, directory).extract(content)
    with pytest.raises(reading.ReadingBlockedError):
        current(settings, directory, allow_new_requests=False).recover_before_download(
            reading._sha(PDF), mime="application/pdf", doc_class="tdr")
    client.models.generate_content.assert_not_called()


def test_legacy_http_error_may_be_retried_with_the_current_prompt_and_is_consumed(setup):
    settings, client, directory = setup
    client.models.generate_content.side_effect = api_error(429)
    with pytest.raises(reading.ProviderRejectedError):
        legacy(settings, directory).extract(PDF)
    assert len(list(directory.glob("*.provider_error.private.json"))) == 1
    client.models.generate_content.side_effect = None
    retry = current(settings, directory, allow_new_requests=False)
    assert retry.recover_before_download(reading._sha(PDF), mime="application/pdf", doc_class="tdr") is None
    result = retry.extract(PDF)
    assert result.prompt_version == "prod6_tdr_reading_v1"
    config = client.models.generate_content.call_args.kwargs["config"]
    assert config.response_schema is TdrExtractionV3
    assert "cita literal" in config.system_instruction
    assert not list(directory.glob("*.provider_error.private.json"))
    # The answered request is now the one that replays, whatever the order of generations.
    assert current(settings, directory).extract(PDF) == result


def test_reading_policy_marker_does_not_depend_on_the_prompt_generation(setup):
    settings, client, directory = setup
    quote = "Servicio de soporte y mantenimiento de software."
    client.models.generate_content.return_value = response(PAYLOAD | {"commercial_classification": {
        "taxonomy_version": TAXONOMY_VERSION, "primary_area": "software_ti", "secondary_areas": [],
        "work_types": ["soporte_mantenimiento"], "specialties": [], "support_status": "evidence_backed",
        "evidence": [{"axis": axis, "value": value, "citation": quote, "page": 1}
                     for axis, value in (("area", "software_ti"), ("work_type", "soporte_mantenimiento"))]}})
    legacy(settings, directory, policy_context=OPT_IN).extract(PDF)
    marker = (directory / reading.READING_POLICY_FILENAME).read_bytes()
    first = current(settings, directory, policy_context=OPT_IN).extract(PDF)
    assert (directory / reading.READING_POLICY_FILENAME).read_bytes() == marker
    assert first.prompt_version.endswith("_" + TAXONOMY_VERSION)
    assert client.models.generate_content.call_count == 1
    # A namespace frozen for a policy still refuses the other one.
    with pytest.raises(reading.ReadingPolicyError):
        current(settings, directory).extract(PDF + b"other")


def test_a_fresh_document_is_read_with_the_current_generation_only(setup):
    settings, client, directory = setup
    result = current(settings, directory).extract(PDF, doc_class="eett")
    assert result.prompt_version == "prod6_eett_reading_v1"
    assert result.schema_version == "prod6_eett_schema_v1"
    assert isinstance(result.extraction, EettExtractionV3)
    saved = json.loads(next(directory.glob("*.started.private.json")).read_text())
    assert saved["identity"]["reading_generation"] == reading.QUALITY_GENERATION_MARKER
    assert client.models.generate_content.call_count == 1


# --- reuse of readings already paid ------------------------------------------------------


def test_reuse_looks_for_the_current_prompt_first_and_keeps_every_older_one(setup):
    settings, _, directory = setup
    assert current(settings, directory).reuse_prompt_versions("tdr") == ("prod6_tdr_reading_v1", PROMPT_VERSION)
    assert current(settings, directory).reuse_prompt_versions("eett") == ("prod6_eett_reading_v1", EETT_PROMPT_VERSION)
    assert current(settings, directory, policy_context=OPT_IN).reuse_prompt_versions("tdr") == (
        "prod6_tdr_reading_v1_" + TAXONOMY_VERSION, "prod6_tdr_reading_v1",
        PROMPT_VERSION + "_" + TAXONOMY_VERSION, PROMPT_VERSION)
    assert legacy(settings, directory).reuse_prompt_versions("tdr") == (PROMPT_VERSION,)


def test_reused_commercial_reading_of_an_older_generation_is_still_validated(setup):
    settings, _, directory = setup
    reader = current(settings, directory, policy_context=OPT_IN)
    source = {"prompt_version": PROMPT_VERSION + "_" + TAXONOMY_VERSION,
              "schema_version": "wrong", "raw_extraction_json": {}}
    with pytest.raises(reading.ReadingPolicyError):
        reader.validate_reused_extraction(source, "tdr")
    reader.validate_reused_extraction({"prompt_version": PROMPT_VERSION, "raw_extraction_json": {}}, "tdr")


# --- the new schema tolerates what the old one never wrote -------------------------------


def test_old_readings_validate_against_the_new_schemas():
    old = {"contract": {"plazo_dias": 30, "modalidad": "Suma alzada"},
           "payment": {"tipo": "armadas", "armadas": [{"numero": 1, "porcentaje": 50.0}]},
           "requirements": {"provider": [{"tipo": "rnp", "descripcion": "RNP", "obligatorio": True}],
                            "key_personnel": [{"rol": "Analista"}], "equipment": [{"nombre": "Laptop"}]}}
    for schema in (TdrExtractionV3, EettExtractionV3):
        extraction = schema.model_validate(old)
        assert extraction.requirements.provider[0].pagina is None
        assert extraction.contract.forma_ejecucion is None


def test_unusable_page_currency_or_form_never_blocks_a_paid_response():
    extraction = TdrExtractionV3.model_validate({
        "contract": {"forma_ejecucion": "Híbrido"},
        "payment": {"numero_pagos_declarado": "doce"},
        "requirements": {"provider": [
            {"tipo": "economic_experience", "pagina": "12-13", "moneda": "US$", "evidencia": "S/ 5 000"},
            {"tipo": "specific_experience", "pagina": 0, "moneda": "pesos"},
            {"tipo": "other", "pagina": "7", "moneda": "soles"}],
         "key_personnel": [{"rol": "Jefe", "pagina": True, "evidencia": "Jefe de proyecto"}]}})
    assert extraction.contract.forma_ejecucion == "mixta"
    assert extraction.payment.numero_pagos_declarado is None
    provider = extraction.requirements.provider
    assert [(item.pagina, item.moneda) for item in provider] == [(None, "USD"), (None, None), (7, "PEN")]
    assert extraction.requirements.key_personnel[0].pagina is None
    assert lenient_page(3.0) == 3 and lenient_page(-1) is None
    assert normalize_currency("€") == "EUR" and normalize_currency(None) is None


def test_new_fields_are_part_of_the_schema_sent_to_the_provider():
    schema = TdrExtractionV3.model_json_schema()
    definitions = schema["$defs"]
    assert {"pagina", "moneda", "evidencia"} <= set(definitions["ProviderRequirementV3"]["properties"])
    assert "numero_pagos_declarado" in definitions["PaymentV3"]["properties"]
    assert "forma_ejecucion" in definitions["ContractV3"]["properties"]
    assert deepcopy(schema)  # serializable for the SDK
