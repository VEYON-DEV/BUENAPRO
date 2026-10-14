"""Mock SDK: the routed 2.5 model receives PDF bytes and keeps its own cost/receipt."""
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import pytest

from buenapro_worker.prod2 import model_routing, pdf_preparation, selected_extractor
from buenapro_worker.settings import Settings


MODEL = "gemini-2.5-flash-lite"


@pytest.fixture
def prepared_pdf(tmp_path, monkeypatch):
    # Binary PDF pages deliberately contain no extracted-text substitute.
    writer = PdfWriter()
    for _ in range(21):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    source = tmp_path / "source.pdf"
    content = buffer.getvalue()
    source.write_bytes(content)
    code = "00000000-0000-4000-8000-000000000001"
    candidate = {
        "candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose",
        "filename": "requirements.pdf", "type": "Bases", "stage": "Convocatoria",
        "published_at": "06/10/2026 12:00", "source_url": "https://example.invalid/" + code,
        "original_sha256": hashlib.sha256(content).hexdigest(),
    }
    monkeypatch.setattr(pdf_preparation, "prepare_pdf", lambda path, *args, **kwargs: path)
    bundle = pdf_preparation.prepare_selected_document_set(
        tmp_path, [candidate], {candidate["candidate_id"]: source},
        {"official_inventory": [candidate]}, optimize_lossless=False,
    )
    model, metadata = model_routing.resolve_llm1_model(
        {"llm1_model_routing": model_routing.default_model_routing()},
        bundle["page_count"], "gemini-3.1-flash-lite",
    )
    assert model == MODEL and metadata["page_count"] == 21
    settings = SimpleNamespace(
        gemini_api_key="fake-unit-key", gemini_model=model,
        gemini_fallback_model="gemini-2.5-flash",
    )
    return bundle, settings


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"summary": {"descripcion_corta": "Software"}}),
        usage_metadata=SimpleNamespace(
            prompt_token_count=1000, candidates_token_count=200, total_token_count=1200,
            thoughts_token_count=0, cached_content_token_count=0,
        ), candidates=[],
    )
    uploads = []
    def upload(*, file, config):
        assert config.mime_type == "application/pdf"
        uploads.append(file.read())
        return SimpleNamespace(
            name="files/mock-routed", uri="https://mock.invalid/routed", state="ACTIVE",
        )
    client.files.upload.side_effect = upload
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(selected_extractor.genai, "Client", constructor)
    return SimpleNamespace(client=client, constructor=constructor, uploads=uploads)


@pytest.mark.parametrize("use_files", [False, True])
def test_routed_model_receives_pdf_and_audits_its_own_tokens_and_price(
    prepared_pdf, sdk, tmp_path, monkeypatch, use_files,
):
    bundle, settings = prepared_pdf
    if use_files:
        monkeypatch.setattr(selected_extractor, "INLINE_MAX_BYTES", 1)
    callback = MagicMock()
    pdf = bundle["pdf_path"].read_bytes()
    with selected_extractor.SelectedGeminiExtractor(settings, bundle, tmp_path, callback) as reader:
        result = reader.extract(pdf, mime="application/pdf", doc_class="bases_service")
        assert result.model == MODEL
        assert (result.input_tokens, result.output_tokens) == (1000, 200)
        assert result.cost_usd == pytest.approx(0.00018)
        audit = reader.attempts[0]
        assert audit["model"] == MODEL and audit["page_count"] == 21
        assert audit["cost_usd"] == pytest.approx(0.00018)
        assert audit["status"] == "success" and audit["provider_usage_known"] is True
        assert audit["remote_cleanup_pending"] == 0
    callback.assert_called_once()
    sdk.client.models.generate_content.assert_called_once()
    request = sdk.client.models.generate_content.call_args.kwargs
    assert request["model"] == MODEL
    assert request["config"].response_mime_type == "application/json"
    assert request["config"].response_schema is not None
    assert len(request["contents"]) == 2
    assert "1..21" in request["contents"][0].text
    if use_files:
        assert request["contents"][1].file_data.mime_type == "application/pdf"
        assert sdk.uploads == [pdf]
        sdk.client.files.delete.assert_called_once_with(name="files/mock-routed")
    else:
        assert request["contents"][1].inline_data.mime_type == "application/pdf"
        assert request["contents"][1].inline_data.data == pdf
        sdk.client.files.upload.assert_not_called()
    assert sdk.constructor.call_args.kwargs["http_options"].retry_options.attempts == 1
    receipts = list(tmp_path.glob("gemini-selected-*.outcome.private.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["model"] == MODEL and receipt["cost_usd"] == pytest.approx(0.00018)
    sdk.client.close.assert_called_once()


@pytest.mark.parametrize("failure", ["invalid_json", "timeout"])
def test_routed_model_failure_never_retries_or_falls_back(prepared_pdf, sdk, tmp_path, failure):
    bundle, settings = prepared_pdf
    if failure == "timeout":
        sdk.client.models.generate_content.side_effect = TimeoutError("Mock timeout")
    else:
        sdk.client.models.generate_content.return_value.text = "invalid JSON"
    with selected_extractor.SelectedGeminiExtractor(settings, bundle, tmp_path) as reader:
        with pytest.raises(TimeoutError if failure == "timeout" else ValueError):
            reader.extract(bundle["pdf_path"].read_bytes(), mime="application/pdf")
        audit = reader.attempts[0]
        assert audit["model"] == MODEL
        if failure == "timeout":
            assert audit["cost_usd"] is None and audit["provider_usage_known"] is False
        else:
            assert audit["cost_usd"] == pytest.approx(0.00018)
            assert audit["status"] == "response_parse_failure"
    sdk.client.models.generate_content.assert_called_once()
    assert sdk.client.models.generate_content.call_args.kwargs["model"] == MODEL
    assert MODEL != settings.gemini_fallback_model


def test_two_twelve_page_documents_route_by_the_complete_twenty_four_page_set(tmp_path, monkeypatch):
    candidates, files = [], {}
    for number in (1, 2):
        writer = PdfWriter()
        for _ in range(12):
            page = writer.add_blank_page(width=612, height=792)
            stream = DecodedStreamObject()
            stream.set_data(f"% distinct source document {number}\n".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        content = buffer.getvalue()
        path = tmp_path / f"source-{number}.pdf"
        path.write_bytes(content)
        code = f"00000000-0000-4000-8000-{number:012d}"
        candidate = {
            "candidate_id": "loose:" + code, "document_uuid": code, "source_kind": "loose",
            "filename": path.name, "type": "Bases", "stage": "Convocatoria",
            "published_at": "06/10/2026 12:00", "source_url": "https://example.invalid/" + code,
            "original_sha256": hashlib.sha256(content).hexdigest(),
        }
        candidates.append(candidate)
        files[candidate["candidate_id"]] = path
    assert candidates[0]["original_sha256"] != candidates[1]["original_sha256"]
    monkeypatch.setattr(pdf_preparation, "prepare_pdf", lambda path, *args, **kwargs: path)
    bundle = pdf_preparation.prepare_selected_document_set(
        tmp_path, candidates, files, {"official_inventory": candidates}, optimize_lossless=False,
    )
    assert bundle["page_count"] == 24
    assert [row["end_page"] - row["start_page"] + 1
            for row in bundle["source_document_manifest"]] == [12, 12]
    shared = Settings(
        _env_file=None, database_url="postgresql://example.invalid/unit",
        gemini_api_key="fake-unit-key", gemini_model="gemini-3.1-flash-lite",
    )
    before = shared.model_dump()
    policy_manifest = {"llm1_model_routing": model_routing.default_model_routing()}
    resolved, metadata = model_routing.resolve_llm1_model(
        policy_manifest, bundle["page_count"], shared.gemini_model,
    )
    local = shared.model_copy(update={"gemini_model": resolved})
    assert resolved == local.gemini_model == MODEL
    assert local is not shared and shared.model_dump() == before
    assert metadata["page_count"] == 24
    # Neither an individual source nor a transport part chooses the model.
    assert model_routing.resolve_llm1_model(policy_manifest, 12, shared.gemini_model)[0] == shared.gemini_model
    assert model_routing.resolve_llm1_model({}, 24, shared.gemini_model) == (shared.gemini_model, None)
