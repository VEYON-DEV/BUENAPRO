"""Offline configuration, receipt, reuse and guard contracts for PROD6 opt-in."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from google.genai import types
import pytest

from buenapro_worker.extraction.commercial_classification import POLICY_KEY, TAXONOMY_VERSION
from buenapro_worker.extraction.gemini import (
    EETT_PROMPT_FILENAME, EETT_PROMPT_VERSION, EETT_SCHEMA_VERSION, PARTICIPATION_INSTRUCTION,
    PROMPT_FILENAME, PROMPT_VERSION, SCHEMA_VERSION, read_prompt,
)
from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.jobs import extract_tdr as extraction_job
from buenapro_worker.prod6 import reading, runtime
from buenapro_worker.queue.repository import Job, JobRepository

OPT_IN = {POLICY_KEY: TAXONOMY_VERSION}
PDF = b"%PDF-1.7 offline policy fixture"
SHA = hashlib.sha256(PDF).hexdigest()


def payload():
    quote = "Servicio de soporte y mantenimiento de software."
    return {"summary": {"descripcion_corta": "Soporte de software"},
            "commercial_classification": {"taxonomy_version": TAXONOMY_VERSION,
                "primary_area": "software_ti", "secondary_areas": [],
                "work_types": ["soporte_mantenimiento"], "specialties": [],
                "support_status": "evidence_backed", "evidence": [
                    {"axis": axis, "value": value, "citation": quote, "page": 1}
                    for axis, value in (("area", "software_ti"), ("work_type", "soporte_mantenimiento"))]}}


def sdk_response(value):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(parts=[types.Part(text=json.dumps(value))]), finish_reason="STOP")],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=50))


@pytest.fixture
def local_sdk(monkeypatch, tmp_path):
    client = MagicMock()
    client.models.generate_content.return_value = sdk_response(payload())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(reading.genai, "Client", factory)
    return SimpleNamespace(client=client, factory=factory, folder=tmp_path / "readings",
        settings=SimpleNamespace(gemini_model="gemini-3.1-flash-lite", gemini_api_key="offline-placeholder"))


def snapshot(folder):
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


@pytest.mark.parametrize("doc_class", ["tdr", "eett"])
def test_absence_retains_exact_legacy_identity_prompt_schema_and_replay(local_sdk, doc_class):
    h = local_sdk
    value = {"summary": {"descripcion_corta": "Legacy"}}
    if doc_class == "eett":
        value["goods"] = {"items": []}
    h.client.models.generate_content.return_value = sdk_response(value)
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder)
    first = reader.extract(PDF, doc_class=doc_class)
    prompt = read_prompt(EETT_PROMPT_FILENAME if doc_class == "eett" else PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION
    expected = {"document_sha256": SHA, "doc_class": doc_class, "mime": "application/pdf",
        "model": h.settings.gemini_model,
        "prompt_version": EETT_PROMPT_VERSION if doc_class == "eett" else PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_version": EETT_SCHEMA_VERSION if doc_class == "eett" else SCHEMA_VERSION}
    key = reading._sha(reading._canonical(expected))
    saved = json.loads((h.folder / f"{key}.started.private.json").read_bytes())
    assert saved["identity"] == expected and saved["request_key"] == key
    config = h.client.models.generate_content.call_args.kwargs["config"]
    assert config.system_instruction == prompt
    assert config.response_schema is (EettExtractionV1 if doc_class == "eett" else TdrExtractionV2)
    assert not (h.folder / reading.READING_POLICY_FILENAME).exists()
    before = snapshot(h.folder)
    replay = reading.GuardedGeminiExtractor(h.settings, h.folder).extract(PDF, doc_class=doc_class)
    assert replay == first and snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == h.factory.call_count == 1


@pytest.mark.parametrize("doc_class", ["tdr", "eett"])
def test_new_opt_in_is_bound_to_schema_prompt_manifest_receipts_and_local_replay(local_sdk, doc_class):
    h = local_sdk
    value = payload()
    if doc_class == "eett":
        value["goods"] = {"items": [{"nombre": "Software"}]}
    h.client.models.generate_content.return_value = sdk_response(value)
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN)
    first = reader.extract(PDF, doc_class=doc_class)
    frozen = json.loads((h.folder / reading.READING_POLICY_FILENAME).read_bytes())
    assert frozen == {"version": 1, "policy_context": OPT_IN}
    assert (h.folder / reading.READING_POLICY_FILENAME).stat().st_mode & 0o777 == 0o600
    config = h.client.models.generate_content.call_args.kwargs["config"]
    assert "commercial_classification" in config.response_schema.model_fields
    assert config.system_instruction.count("CLASIFICACION_COMERCIAL=") == 1
    saved = json.loads(next(h.folder.glob("*.started.private.json")).read_bytes())
    assert saved["identity"][POLICY_KEY] == TAXONOMY_VERSION
    assert saved["identity"]["prompt_sha256"] == hashlib.sha256(config.system_instruction.encode()).hexdigest()
    assert saved["identity"]["prompt_version"].endswith("_" + TAXONOMY_VERSION)
    assert saved["identity"]["schema_version"].endswith("_" + TAXONOMY_VERSION)
    assert first.raw_json == value
    before = snapshot(h.folder)
    assert reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(
        PDF, doc_class=doc_class) == first
    assert snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == h.factory.call_count == 1


@pytest.mark.parametrize("context", [{POLICY_KEY: None}, {POLICY_KEY: "unknown"},
                                      {POLICY_KEY: True}, {"unbound": "anything"}, []])
def test_invalid_context_fails_before_directory_client_or_receipts(local_sdk, context):
    h = local_sdk
    with pytest.raises(reading.ReadingPolicyError):
        reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=context)
    assert not h.folder.exists()
    h.factory.assert_not_called()


def test_default_binding_creates_no_directory_or_manifest(tmp_path):
    directory = tmp_path / "absent"
    assert reading.bind_reading_policy(directory) == {}
    assert not directory.exists()


def test_completed_legacy_namespace_cannot_be_upgraded_or_repaid_for_tags(local_sdk):
    h = local_sdk
    h.client.models.generate_content.return_value = sdk_response({"summary": {"descripcion_corta": "Legacy"}})
    reading.GuardedGeminiExtractor(h.settings, h.folder).extract(PDF)
    before = snapshot(h.folder)
    with pytest.raises(reading.ReadingPolicyError):
        reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF)
    assert snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == 1
    assert not (h.folder / reading.READING_POLICY_FILENAME).exists()


def test_uncertain_legacy_namespace_cannot_be_switched_even_when_guard_is_removed(local_sdk):
    h = local_sdk
    h.client.models.generate_content.side_effect = TimeoutError("offline uncertain")
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder)
    with pytest.raises(reading.GuardedReadingError):
        reader.extract(PDF)
    reader.guard_path.unlink()
    before = snapshot(h.folder)
    with pytest.raises(reading.ReadingPolicyError):
        reading.bind_reading_policy(h.folder, OPT_IN)
    assert snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == 1


def test_opt_in_uncertain_request_stays_guarded_and_never_falls_back(local_sdk):
    h = local_sdk
    h.client.models.generate_content.side_effect = TimeoutError("offline uncertain")
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN)
    with pytest.raises(reading.GuardedReadingError):
        reader.extract(PDF)
    before = snapshot(h.folder)
    for content in (PDF, PDF + b"another"):
        with pytest.raises(reading.ReadingBlockedError):
            reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(content)
    with pytest.raises(reading.ReadingPolicyError):
        reading.GuardedGeminiExtractor(h.settings, h.folder).extract(PDF)
    assert snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == h.factory.call_count == 1


def test_paid_opt_in_payload_missing_classification_is_preserved_and_blocks_without_retry(local_sdk):
    h = local_sdk
    h.client.models.generate_content.return_value = sdk_response({"summary": {"descripcion_corta": "Incomplete"}})
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN)
    for _ in range(2):
        with pytest.raises(reading.GuardedReadingError):
            reader.extract(PDF)
    assert reader.guard_path.exists()
    assert len(list(h.folder.glob("*.response.private.json"))) == 1
    assert not list(h.folder.glob("*.result.private.json"))
    assert h.client.models.generate_content.call_count == 1


def test_tampered_policy_manifest_does_not_authorize_send_or_rewrite_receipts(local_sdk):
    h = local_sdk
    reading.bind_reading_policy(h.folder, OPT_IN)
    (h.folder / reading.READING_POLICY_FILENAME).write_text('{"version":2,"policy_context":{}}')
    before = snapshot(h.folder)
    with pytest.raises(reading.ReadingPolicyError):
        reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF)
    assert snapshot(h.folder) == before
    h.factory.assert_not_called()


def test_boolean_policy_manifest_version_and_symlink_are_rejected_before_requests(local_sdk):
    h = local_sdk
    reading.bind_reading_policy(h.folder, OPT_IN)
    marker = h.folder / reading.READING_POLICY_FILENAME
    marker.write_text(json.dumps({"version": True, "policy_context": OPT_IN}))
    with pytest.raises(reading.ReadingPolicyError):
        reading.bind_reading_policy(h.folder, OPT_IN)
    marker.unlink()
    marker.symlink_to(h.folder / "missing-target")
    with pytest.raises(reading.ReadingPolicyError):
        reading.bind_reading_policy(h.folder)
    h.factory.assert_not_called()


def test_opt_in_enqueue_is_bound_while_default_payload_and_attempt_policy_are_unchanged(monkeypatch):
    enqueue = MagicMock(return_value=1)
    monkeypatch.setattr(JobRepository, "enqueue", enqueue)
    conn = MagicMock()
    legacy = runtime.ScopedRepository(conn, profile_id="company", batch_id="new")
    opted = runtime.ScopedRepository(conn, profile_id="company", batch_id="new", reading_policy=OPT_IN)
    legacy.enqueue("process_contract", {"id_contrato": 1}, dedup_key="candidate")
    baseline_payload = deepcopy(enqueue.call_args.args[1])
    baseline_kwargs = dict(enqueue.call_args.kwargs)
    opted.enqueue("process_contract", {"id_contrato": 1}, dedup_key="candidate")
    assert enqueue.call_args.args[1] == {**baseline_payload, **OPT_IN}
    assert enqueue.call_args.kwargs == baseline_kwargs
    assert enqueue.call_args.kwargs["max_attempts"] == 1


@pytest.mark.parametrize("job_type", ["extract_tdr", "process_contract", "download_file"])
def test_new_policy_namespace_cannot_adopt_legacy_job_before_download_or_provider(monkeypatch, tmp_path, job_type):
    repo = runtime.ScopedRepository(MagicMock(), profile_id="company", batch_id="new", reading_policy=OPT_IN)
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value=None))
    download, extract, source = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(runtime, "download_file_job", download)
    monkeypatch.setattr(runtime, "extract_tdr_job", extract)
    monkeypatch.setattr(runtime, "process_contract", source)
    job = Job(1, job_type, "llm" if job_type == "extract_tdr" else "io",
              {"id_contrato": 1, "id_contrato_archivo": 2, "batch_id": "legacy"}, 2)
    with pytest.raises(reading.ReadingPolicyError):
        runtime.dispatch(MagicMock(), repo, job, tmp_path)
    download.assert_not_called()
    extract.assert_not_called()
    source.assert_not_called()
    repo.conn.execute.assert_not_called()
    assert not (tmp_path / "readings").exists()


def test_io_policy_mismatch_is_rejected_before_any_connection(monkeypatch, tmp_path):
    connect = MagicMock()
    monkeypatch.setattr(runtime, "connect", connect)
    job = Job(1, "download_file", "io", {"id_contrato": 1, "batch_id": "legacy"}, 1)
    with pytest.raises(reading.ReadingPolicyError):
        runtime.execute_io_job(MagicMock(), "company", job, tmp_path, reading_policy=OPT_IN)
    connect.assert_not_called()


def test_current_paid_read_is_preserved_even_when_queued_policy_is_historical(monkeypatch, tmp_path):
    repo = runtime.ScopedRepository(MagicMock(), profile_id="company", batch_id="new", reading_policy=OPT_IN)
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value={"id": 42}))
    enqueue, extract = MagicMock(), MagicMock()
    monkeypatch.setattr(runtime, "enqueue_derive_summary", enqueue)
    monkeypatch.setattr(runtime, "extract_tdr_job", extract)
    job = Job(1, "extract_tdr", "llm", {"id_contrato": 1, "id_contrato_archivo": 2}, 1)
    assert runtime.dispatch(MagicMock(), repo, job, tmp_path) == {
        "outcome": "current_reading_preserved", "extraction_id": 42}
    enqueue.assert_called_once()
    extract.assert_not_called()


def test_cli_exposes_explicit_opt_in_without_enabling_default(monkeypatch, tmp_path):
    run = MagicMock(return_value=0)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "Settings", MagicMock())
    base = ["prod6-runtime", "--profile-id", "company", "--receipt-dir", str(tmp_path)]
    for extra, expected in (([], None), (["--llm1-commercial-taxonomy", TAXONOMY_VERSION], TAXONOMY_VERSION)):
        monkeypatch.setattr("sys.argv", base + extra)
        with pytest.raises(SystemExit) as stopped:
            runtime.main()
        assert stopped.value.code == 0
        assert run.call_args.kwargs["llm1_commercial_taxonomy"] == expected


def test_runtime_freezes_under_lock_before_source_and_refuses_changed_resume(monkeypatch, tmp_path):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"locked": True}
    connect = MagicMock()
    connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(runtime, "connect", connect)
    monkeypatch.setattr(runtime.signal, "signal", MagicMock())
    monkeypatch.setattr(runtime, "profile_enabled", MagicMock(return_value=False))
    poll = MagicMock()
    monkeypatch.setattr(runtime, "poll_prod6_profile", poll)
    assert runtime.run(MagicMock(), profile_ids=["company"], receipt_dir=tmp_path,
                       once=True, llm1_commercial_taxonomy=TAXONOMY_VERSION) == 2
    state = (tmp_path / "state.json").read_bytes()
    assert json.loads(state)[POLICY_KEY] == TAXONOMY_VERSION
    assert (tmp_path / "readings" / reading.READING_POLICY_FILENAME).exists()
    assert runtime.run(MagicMock(), profile_ids=["company"], receipt_dir=tmp_path, once=True) == 2
    assert (tmp_path / "state.json").read_bytes() == state
    startup = json.loads(next(tmp_path.glob("startup-*.json")).read_bytes())
    assert startup["phase"] == "reading_policy" and startup["error_type"] == "ReadingPolicyError"
    poll.assert_not_called()


@pytest.mark.parametrize("value", ["unknown", True, ""])
def test_runtime_invalid_policy_is_rejected_before_database_or_source(monkeypatch, tmp_path, value):
    connect = MagicMock()
    monkeypatch.setattr(runtime, "connect", connect)
    with pytest.raises(reading.ReadingPolicyError):
        runtime.run(MagicMock(), profile_ids=["company"], receipt_dir=tmp_path,
                    llm1_commercial_taxonomy=value)
    connect.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cached_policy", ["commercial", "legacy", "malformed"])
def test_hash_reuse_happens_before_source_and_preserves_paid_legacy_without_backfill(
        local_sdk, monkeypatch, cached_policy):
    h = local_sdk
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN)
    bound = runtime.HashBoundExtractor(reader, SHA)
    repo = SimpleNamespace(conn=MagicMock())
    repo.conn.execute.return_value.fetchone.return_value = {"id": 7, "mime": "application/pdf",
        "sha256_original": SHA, "objeto_codigo": 2}
    source = {"id": 6, "model": h.settings.gemini_model, "schema_version": SCHEMA_VERSION,
              "prompt_version": PROMPT_VERSION, "raw_extraction_json": {"summary": {"descripcion_corta": "Old"}}}
    if cached_policy != "legacy":
        source.update(schema_version=SCHEMA_VERSION + "_" + TAXONOMY_VERSION,
                      prompt_version=PROMPT_VERSION + "_" + TAXONOMY_VERSION,
                      raw_extraction_json=payload() if cached_policy == "commercial" else {})
    before = deepcopy(source)
    reusable = MagicMock(side_effect=[None, source] if cached_policy == "legacy" else [source])
    insert, download = MagicMock(return_value=99), MagicMock(side_effect=AssertionError("No download"))
    monkeypatch.setattr(extraction_job, "find_reusable_extraction", reusable)
    monkeypatch.setattr(extraction_job, "insert_reused_extraction", insert)
    monkeypatch.setattr(extraction_job, "mark_contract_extracted", MagicMock())
    monkeypatch.setattr(extraction_job, "enqueue_derive_summary", MagicMock())
    monkeypatch.setattr(extraction_job, "SeaceClient", download)
    if cached_policy == "malformed":
        with pytest.raises(ValueError):
            extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2, extractor=bound)
        insert.assert_not_called()
    else:
        assert extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2, extractor=bound) == 99
        assert insert.call_args.kwargs["source"] == before
    assert reusable.call_args_list[0].kwargs["prompt_version"] == PROMPT_VERSION + "_" + TAXONOMY_VERSION
    if cached_policy == "legacy":
        assert reusable.call_args_list[1].kwargs["prompt_version"] == PROMPT_VERSION
    assert source == before
    download.assert_not_called()
    h.factory.assert_not_called()


def test_new_document_job_uses_bound_commercial_reader_and_persists_same_paid_block_once(local_sdk, monkeypatch):
    h = local_sdk
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN)
    repo = SimpleNamespace(conn=MagicMock())
    doc = {"id": 7, "mime": "application/pdf", "sha256_original": SHA, "objeto_codigo": 2}
    repo.conn.execute.return_value.fetchone.side_effect = [doc, None, {"id": 99}]
    reusable = MagicMock(return_value=None)
    monkeypatch.setattr(extraction_job, "find_reusable_extraction", reusable)
    client = MagicMock()
    client.__enter__.return_value.download_file.return_value = PDF
    monkeypatch.setattr(extraction_job, "SeaceClient", MagicMock(return_value=client))
    monkeypatch.setattr(extraction_job, "mark_contract_extracted", MagicMock())
    monkeypatch.setattr(extraction_job, "enqueue_derive_summary", MagicMock())
    assert extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2,
        extractor=runtime.HashBoundExtractor(reader, SHA)) == 99
    inserted = repo.conn.execute.call_args.args[1]
    assert json.loads(inserted[7])["commercial_classification"] == payload()["commercial_classification"]
    assert inserted[2].endswith("_" + TAXONOMY_VERSION) and inserted[3].endswith("_" + TAXONOMY_VERSION)
    assert reusable.call_count == 2
    assert h.client.models.generate_content.call_count == 1


@pytest.mark.parametrize("unrelated_receipt", [False, True])
def test_retried_job_without_exact_response_is_blocked_before_source_even_in_new_folder(
        local_sdk, monkeypatch, unrelated_receipt):
    h = local_sdk
    if unrelated_receipt:
        reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF + b"unrelated")
    calls = h.client.models.generate_content.call_count
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN, allow_new_requests=False)
    repo = SimpleNamespace(conn=MagicMock())
    repo.conn.execute.return_value.fetchone.return_value = {
        "id": 7, "mime": "application/pdf", "sha256_original": SHA, "objeto_codigo": 2}
    source = MagicMock(side_effect=AssertionError("No source download on prior attempt"))
    monkeypatch.setattr(extraction_job, "SeaceClient", source)
    monkeypatch.setattr(extraction_job, "find_reusable_extraction", MagicMock(return_value=None))
    with pytest.raises(reading.ReadingBlockedError):
        extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2,
            extractor=runtime.HashBoundExtractor(reader, SHA))
    source.assert_not_called()
    assert h.client.models.generate_content.call_count == calls
    request_key = reader._request_identity(SHA, mime="application/pdf", doc_class="tdr")[2]
    assert not (h.folder / f"{request_key}.started.private.json").exists()


def test_previously_attempted_job_recovers_only_exact_known_response_without_download_or_generation(local_sdk, monkeypatch):
    h = local_sdk
    original = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF)
    # An earlier process may have durably saved the response before dying prior
    # to parsing or committing PostgreSQL. Both operations below remain local.
    next(h.folder.glob("*.result.private.json")).unlink()
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN, allow_new_requests=False)
    repo = SimpleNamespace(conn=MagicMock())
    repo.conn.execute.return_value.fetchone.side_effect = [
        {"id": 7, "mime": "application/pdf", "sha256_original": SHA, "objeto_codigo": 2}, None, {"id": 99}]
    source = MagicMock(side_effect=AssertionError("No source required for exact durable recovery"))
    monkeypatch.setattr(extraction_job, "SeaceClient", source)
    monkeypatch.setattr(extraction_job, "find_reusable_extraction", MagicMock(return_value=None))
    monkeypatch.setattr(extraction_job, "mark_contract_extracted", MagicMock())
    monkeypatch.setattr(extraction_job, "enqueue_derive_summary", MagicMock())
    assert extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2,
        extractor=runtime.HashBoundExtractor(reader, SHA)) == 99
    inserted = repo.conn.execute.call_args.args[1]
    assert json.loads(inserted[7]) == original.raw_json
    source.assert_not_called()
    assert h.client.models.generate_content.call_count == 1


def test_exact_response_with_unknown_usage_stays_blocked_before_download(local_sdk, monkeypatch):
    h = local_sdk
    response = sdk_response(payload())
    response.usage_metadata = None
    h.client.models.generate_content.return_value = response
    with pytest.raises(reading.GuardedReadingError):
        reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF)
    reader = reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN, allow_new_requests=False)
    repo = SimpleNamespace(conn=MagicMock())
    repo.conn.execute.return_value.fetchone.return_value = {
        "id": 7, "mime": "application/pdf", "sha256_original": SHA, "objeto_codigo": 2}
    source = MagicMock(side_effect=AssertionError("Unknown usage cannot authorize source or inference"))
    monkeypatch.setattr(extraction_job, "SeaceClient", source)
    monkeypatch.setattr(extraction_job, "find_reusable_extraction", MagicMock(return_value=None))
    with pytest.raises(reading.GuardedReadingError):
        extraction_job.extract_tdr_job(h.settings, repo, id_contrato=1, id_contrato_archivo=2,
            extractor=runtime.HashBoundExtractor(reader, SHA))
    source.assert_not_called()
    assert h.client.models.generate_content.call_count == h.factory.call_count == 1


@pytest.mark.parametrize("reason", ["new_document_sha", "new_doc_class", "new_model"])
def test_local_recovery_requires_exact_receipt_identity_not_any_completed_response(local_sdk, reason):
    h = local_sdk
    reading.GuardedGeminiExtractor(h.settings, h.folder, policy_context=OPT_IN).extract(PDF)
    settings = h.settings
    sha, doc_class = SHA, "tdr"
    if reason == "new_document_sha":
        sha = "a" * 64
    elif reason == "new_doc_class":
        doc_class = "eett"
    else:
        settings = SimpleNamespace(**{**vars(settings), "gemini_model": "gemini-2.5-flash-lite"})
    reader = reading.GuardedGeminiExtractor(settings, h.folder, policy_context=OPT_IN, allow_new_requests=False)
    before = snapshot(h.folder)
    with pytest.raises(reading.ReadingBlockedError):
        reader.recover_before_download(sha, mime="application/pdf", doc_class=doc_class)
    assert snapshot(h.folder) == before
    assert h.client.models.generate_content.call_count == 1


@pytest.mark.parametrize("previous_other_job,attempts", [(False, 2), (True, 1)])
def test_runtime_marks_same_namespace_prior_attempts_replay_only_before_job_download(
        local_sdk, monkeypatch, previous_other_job, attempts):
    h = local_sdk
    repo = runtime.ScopedRepository(MagicMock(), profile_id="company", batch_id="new", reading_policy=OPT_IN)
    # The external namespace guard remains separate and unchanged. The next
    # query finds an earlier job under this namespace, excluding the current ID.
    repo.conn.execute.return_value.fetchone.side_effect = [None, {"id": 44} if previous_other_job else None,
                                                           {"sha256_original": SHA}]
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value=None))
    extracted = MagicMock(return_value=99)
    monkeypatch.setattr(runtime, "extract_tdr_job", extracted)
    job = Job(7, "extract_tdr", "llm", {"id_contrato": 1, "id_contrato_archivo": 2, **OPT_IN}, attempts)
    assert runtime.dispatch(h.settings, repo, job, h.folder.parent)["extraction_id"] == 99
    adapter = extracted.call_args.kwargs["extractor"]
    assert adapter.extractor.allow_new_requests is False
    sql, params = repo.conn.execute.call_args_list[1].args
    assert "id<>%s" in sql and params == (7, "1", "2", runtime.NAMESPACE)
    with pytest.raises(reading.ReadingBlockedError):
        adapter.recover_before_download(SHA, mime="application/pdf", doc_class="tdr")
    h.factory.assert_not_called()
