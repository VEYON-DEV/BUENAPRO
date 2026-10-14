"""Compact metadata is opt-in; every literal evidence byte remains text-only."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import analysis, input_policy, selected_extractor, selected_luna
from buenapro_worker.prod2.luna_policy import luna_settings
from test_prod2_input_integration import context
from test_prod2_retrieved_input_transport import visual_review_prepared, retrieved_receipt
from test_prod2_selected_extractor import sdk, settings
from test_prod2_selected_luna import body as luna_body
from test_prod2_selected_completion import sync
from test_prod2_llm1_completion import finish, NOW


def compact_prepared(tmp_path):
    prepared, result = visual_review_prepared(tmp_path)
    prepared['selection_context'][input_policy.RETRIEVED_TRANSPORT_POLICY_KEY] = input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY
    prepared['document_set_identity']['selection_context'] = deepcopy(prepared['selection_context'])
    prepared['document_set_hash'] = prepared['selection_hash'] = canonical_hash(prepared['document_set_identity'])
    old = prepared['pdf_path'].parent
    new = old.parent / ('selected-analysis-' + prepared['selection_hash'])
    old.rename(new)
    prepared['pdf_path'] = new / 'analysis.pdf'
    for chunk in prepared['chunks']:
        chunk['pdf_path'] = new / chunk['pdf_path'].name
    return prepared, result


@pytest.mark.parametrize('policy', [None, True, 'unsupported'])
def test_invalid_compact_alias_rejected_before_transport(policy):
    selection = {input_policy.INPUT_POLICY_KEY: input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY,
        input_policy.VISUAL_REVIEW_POLICY_KEY: input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY,
        input_policy.RETRIEVED_TRANSPORT_POLICY_KEY: policy}
    with pytest.raises(ValueError, match='explicit visual-review'):
        input_policy.get_input_policy(selection)


@pytest.mark.parametrize('route', [None, input_policy.NATIVE_TEXT_OR_PDF_POLICY,
    input_policy.EMBEDDING_RETRIEVED_TEXT_POLICY])
def test_compact_transport_cannot_drop_visual_review_or_relabel_pdf(route):
    selection = {input_policy.RETRIEVED_TRANSPORT_POLICY_KEY: input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY}
    if route:
        selection[input_policy.INPUT_POLICY_KEY] = route
    with pytest.raises(ValueError):
        input_policy.get_input_policy(selection)


def test_compact_metadata_omits_previews_scores_ids_and_text_without_mutating_proof(tmp_path):
    prepared, result = compact_prepared(tmp_path)
    manifest = prepared['source_document_manifest']
    manifest[0]['preview_text'] = 'SOURCE_PREVIEW_NOT_EVIDENCE'
    result['source_manifest_hash'] = canonical_hash(manifest)
    result['coverage_by_field'] = {'provider_experience': {
        'fields': ['requirements.provider'], 'search_executed': True,
        'literal_evidence_identified': True, 'complete_material_coverage_proven': False,
        'absence_semantics': None, 'semantic_seed_chunk_ids': ['c' * 64],
        'expanded_block_ids': ['block-0001'], 'semantic_scores': [{'cosine': .9}],
        'unresolved_reference_ids': ['first', 'second']}}
    prepared['llm1_input'] = input_policy.input_snapshot(result, prepared)
    before = deepcopy(prepared['llm1_input'])
    value = input_policy.compact_retrieved_metadata(prepared['selection_context'], before, manifest)
    encoded = json.dumps(value)
    for absent in ('SOURCE_PREVIEW_NOT_EVIDENCE', 'semantic_scores', 'expanded_block_ids',
                   'selected_chunk_ids', 'source_sha256', 'block-0001', result['selected_blocks'][0]['text']):
        assert absent not in encoded
    assert value['sources'][0]['global_pages'] == [1, 1]
    assert value['coverage_by_field']['provider_experience']['unresolved_reference_count'] == 2
    assert value['coverage_by_field']['provider_experience']['complete_material_coverage_proven'] is False
    assert value['needs_review'] is True
    assert before == prepared['llm1_input']


def test_v4_prompt_is_new_and_old_prompt_and_schema_remain_exact(tmp_path):
    prepared, _ = compact_prepared(tmp_path)
    selection = prepared['selection_context']
    old = deepcopy(selection)
    old.pop(input_policy.RETRIEVED_TRANSPORT_POLICY_KEY)
    old_prompt = input_policy.prompt_for_input('bases_service', old, 'LEGACY')
    new_prompt = input_policy.prompt_for_input('bases_service', selection, 'LEGACY')
    assert new_prompt.startswith(old_prompt)
    assert input_policy.prompt_version_for_input('bases_service', old).endswith('_prompt_v2')
    assert input_policy.prompt_version_for_input('bases_service', selection).endswith('_prompt_v4')
    assert input_policy.prompt_for_input('bases_service', old, 'LEGACY') == old_prompt
    assert canonical_hash(input_policy.schema_for_input('bases_service', old).model_json_schema()) == canonical_hash(
        input_policy.schema_for_input('bases_service', selection).model_json_schema())
    assert input_policy.schema_version_for_input('bases_service', old) == input_policy.schema_version_for_input('bases_service', selection)


def test_runtime_v4_keeps_full_corpus_queries_and_retrieval_policy_of_v3():
    from buenapro_worker.prod2.embedding_runtime import load_config, config_hash
    root = Path(__file__).resolve().parents[3]
    old = load_config(root / 'docs/sources/prod2-embedding-reading.runtime.v3.json')
    new = load_config(root / 'docs/sources/prod2-embedding-reading.runtime.v4.json')
    for key in ('corpus', 'retrieval', 'embedding', 'storage'):
        assert new[key] == old[key]
    assert new['reading']['transport_policy'] == input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY
    assert config_hash(new) != config_hash(old)
    assert new['automatic_full_pdf_fallback'] is False


def test_compact_metadata_rejects_unstructured_reference_uncertainty(tmp_path):
    prepared, result = compact_prepared(tmp_path)
    result['coverage_by_field']['provider_requirements']['unresolved_reference_ids'] = 'not-a-list'
    proof = input_policy.input_snapshot(result, prepared)
    with pytest.raises(ValueError, match='list of IDs'):
        input_policy.compact_retrieved_metadata(prepared['selection_context'], proof,
            prepared['source_document_manifest'])


def test_compact_gemini_exact_text_once_new_request_and_durable_full_proof(sdk, tmp_path):
    prepared, result = compact_prepared(tmp_path)
    data = prepared['pdf_path'].read_bytes()
    args = {'object_type': 'service', 'manifest': prepared['source_document_manifest'],
        'document_context': context(prepared)}
    expected = analysis.extraction_request_hash(settings(), data, **args)
    callback = MagicMock()
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
            callback, local_text_result=result) as reader:
        bundle = analysis.extract_document(settings(), data, **args, extractor=reader)
    call = sdk.client.models.generate_content.call_args.kwargs
    assert callback.call_count == sdk.client.models.generate_content.call_count == 1
    sdk.client.files.upload.assert_not_called()
    assert len(call['contents']) == 1 and call['contents'][0].text == result['context_text']
    assert call['contents'][0].inline_data is None and call['contents'][0].file_data is None
    prompt = call['config'].system_instruction
    assert 'RECUPERACION_DE_PROCEDENCIA_COMPACTA=' in prompt
    assert 'REVISION_VISUAL=prod2_ocr_text_layout_review_v2' in prompt
    assert result['selected_blocks'][0]['text'] not in prompt
    assert bundle['cache_key'] == 'prod2:llm1:' + expected
    assert canonical_hash(prompt) == bundle['prompt_hash']
    assert bundle['prompt_version'].endswith('_prompt_v4')
    assert bundle['llm1_input'] == prepared['llm1_input']
    assert bundle['raw_extraction_json']['retrieval_context_text'] == result['context_text']
    assert bundle['requires_human_review']


def test_compact_luna_exact_text_once_and_provider_hash(monkeypatch, tmp_path):
    prepared, result = compact_prepared(tmp_path)
    client = MagicMock()
    def generate(**kwargs):
        import httpx
        response = luna_body()
        kwargs['on_response'](httpx.Response(200, json=response))
        return response
    client.generate.side_effect = generate
    monkeypatch.setattr(selected_luna, 'LunaResponsesClient', lambda _: client)
    s = luna_settings(settings())
    s.openai_api_key = 'synthetic-key'
    data = prepared['pdf_path'].read_bytes()
    args = {'object_type': 'service', 'manifest': prepared['source_document_manifest'],
        'document_context': context(prepared)}
    with selected_luna.SelectedLunaExtractor(s, prepared, tmp_path, local_text_result=result) as reader:
        bundle = analysis.extract_document(s, data, **args, extractor=reader)
    call = client.generate.call_args.kwargs
    assert client.generate.call_count == 1
    assert call['contents'] == [{'type': 'input_text', 'text': result['context_text']}]
    assert 'RECUPERACION_DE_PROCEDENCIA_COMPACTA=' in call['instructions']
    assert result['selected_blocks'][0]['text'] not in call['instructions']
    assert canonical_hash(call['instructions']) == bundle['prompt_hash']
    assert bundle['prompt_version'].endswith('_prompt_v4')
    assert bundle['llm1_input'] == prepared['llm1_input'] and bundle['requires_human_review']


def test_v4_completion_preserves_canonical_participation_and_full_durable_input(monkeypatch, tmp_path):
    conn, extraction = retrieved_receipt(monkeypatch, tmp_path)
    selection = extraction['selection_context']
    selection[input_policy.VISUAL_REVIEW_POLICY_KEY] = input_policy.OCR_TEXT_LAYOUT_REVIEW_POLICY
    selection[input_policy.RETRIEVED_TRANSPORT_POLICY_KEY] = input_policy.COMPACT_RETRIEVED_TRANSPORT_POLICY
    extraction['prompt_version'] = input_policy.prompt_version_for_input('bases_service', selection)
    proof = deepcopy(extraction['llm1_input'])
    sync(extraction, conn)
    conn.canonical.update(consortium_status='conditional', subcontracting_status='prohibited', updated_at=NOW,
        participation_terms_json={'source': 'seace_prod4', 'document_sha256': 'e' * 64, 'extraction_id': 41})
    before = deepcopy(conn.canonical)
    saved = finish(conn, extraction)
    assert saved['canonical_participation_preserved'] and saved['projection_pending']
    assert conn.canonical == before and not conn.updates and len(conn.persist_calls) == 1
    assert extraction['llm1_input'] == proof
