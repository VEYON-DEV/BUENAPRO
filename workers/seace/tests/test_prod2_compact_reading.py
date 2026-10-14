"""Offline compact reading contracts: same full PDF/schema, new exact request."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_prod2_selected as selected
import recover_prod2_selected as recovery
from buenapro_worker.prod2 import analysis, llm1_completion, pdf_preparation, selected_extractor
from buenapro_worker.prod2 import compact_reading
from buenapro_worker.prod2.reading_policy import (
    COMPACT_UNIQUE_REQUIREMENTS_POLICY as POLICY,
    COMPACT_UNIQUE_REQUIREMENTS_INSTRUCTION as INSTRUCTION,
    reading_instruction, reading_policy,
)
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.extraction.gemini import BASES_PROMPT_FILENAME, PARTICIPATION_INSTRUCTION, read_prompt
from test_prod2_document_previews import pdf_bytes
from test_prod2_selected_extractor import sdk, response
from test_prod2_selected_completion import selected_fixture, sync, finish, selected_bundle
from test_prod2_selected_recovery import fixture as recovery_fixture


def prepare(tmp_path, *, compact=False):
    path = tmp_path / 'source.pdf'
    content = pdf_bytes([f'Requisito material en pagina {n}: experiencia S/100000 y plazo 24 meses ' * 4 for n in range(1, 26)])
    path.write_bytes(content)
    code = '00000000-0000-4000-8000-000000000001'
    row = {'candidate_id': 'loose:' + code, 'document_uuid': code, 'source_kind': 'loose',
           'filename': 'source.pdf', 'type': 'Bases', 'stage': 'Convocatoria',
           'published_at': '06/10/2026 12:00', 'source_url': 'https://example.invalid/' + code,
           'original_sha256': hashlib.sha256(content).hexdigest()}
    context = {'official_inventory': [row], 'ficha_description': 'SOFTWARE', 'selector_request_hash': 'a' * 64,
               'selector_receipt_hash': 'b' * 64, 'candidates': [row], 'decisions': [], 'thresholds': {'high': 80, 'low': 20}}
    if compact: context['llm1_reading_policy'] = POLICY
    return pdf_preparation.prepare_selected_document_set(tmp_path, [row], {row['candidate_id']: path},
        context, optimize_lossless=False)


def context(prepared):
    return {key: prepared[key] for key in ('document_scope', 'document_set', 'document_set_hash', 'document_set_identity', 'selection_context')}


def settings():
    return SimpleNamespace(gemini_api_key='fake-unit-key', gemini_model='gemini-2.5-flash-lite', gemini_fallback_model='gemini-2.5-flash')


def rewrite(path, value):
    path.unlink(missing_ok=True)
    recovery.private_write(path, value)


def test_compact_preparation_keeps_all_pages_and_material_but_changes_only_policy_identity(tmp_path):
    old = prepare(tmp_path)
    compact = prepare(tmp_path, compact=True)
    assert compact['page_count'] == old['page_count'] == 25
    assert compact['pdf_path'].read_bytes() == old['pdf_path'].read_bytes()
    assert compact['analysis_sha256'] == old['analysis_sha256']
    assert compact['source_document_manifest'] == old['source_document_manifest']
    assert compact['document_set'] == old['document_set']
    for n, page in enumerate(PdfReader(compact['pdf_path']).pages, 1):
        assert f'pagina {n}' in page.extract_text() and 'S/100000' in page.extract_text()
    identity = deepcopy(compact['document_set_identity'])
    assert identity['selection_context'].pop('llm1_reading_policy') == POLICY
    assert identity == old['document_set_identity']
    assert compact['document_set_hash'] != old['document_set_hash']


@pytest.mark.parametrize('object_type', ['service', 'good'])
def test_effective_prompt_hash_model_schema_and_full_wire_match_without_hidden_retry(sdk, tmp_path, object_type):
    if object_type == 'good':
        sdk.client.models.generate_content.return_value = response(json.dumps({'goods': {'items': []}, 'summary': {'descripcion_corta': 'Software'}}))
    old, compact = prepare(tmp_path), prepare(tmp_path, compact=True)
    cached, requests = {}, []
    for prepared in (old, compact):
        pdf = prepared['pdf_path'].read_bytes()
        kwargs = {'object_type': object_type, 'original_sha256': prepared['document_set'][0]['original_sha256'],
                  'manifest': prepared['source_document_manifest'], 'document_context': context(prepared)}
        expected = analysis._document_input(settings(), pdf, object_type, kwargs['original_sha256'], kwargs['manifest'], kwargs['document_context'])
        request = analysis.extraction_request_hash(settings(), pdf, **kwargs)
        with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path,
                max_output_tokens=65536, timeout_ms=300000) as reader:
            result = analysis.extract_document(settings(), pdf, **kwargs, cache=cached, extractor=reader)
            actual = sdk.client.models.generate_content.call_args.kwargs
            assert actual['model'] == result['model'] == 'gemini-2.5-flash-lite'
            assert canonical_hash(actual['config'].system_instruction) == expected['prompt_hash'] == result['prompt_hash']
            assert actual['config'].max_output_tokens == 65536
            assert result['cache_key'] == 'prod2:llm1:' + request and not result['cache_hit']
            assert result['selection_context'] == result['raw_extraction_json']['selection_context'] == prepared['selection_context']
            assert result['requires_human_review'] is True
            assert actual['contents'][1].inline_data.data == pdf
            assert len(PdfReader(prepared['pdf_path']).pages) == 25
            assert reader.attempts[0]['prompt_hash'] == result['prompt_hash']
            assert reader.attempts[0]['selection_context'] == prepared['selection_context']
            requests.append((request, actual['config'].system_instruction, actual['config'].response_schema.model_json_schema()))
        assert analysis.extract_document(settings(), pdf, **kwargs, cache=cached)['cache_hit']
    assert sdk.client.models.generate_content.call_count == 2
    assert requests[0][0] != requests[1][0]
    assert INSTRUCTION not in requests[0][1] and requests[1][1].count(INSTRUCTION) == 1
    assert requests[0][2] == requests[1][2]
    assert len(cached) == 2


def test_legacy_instruction_and_absent_plan_field_are_unchanged(tmp_path):
    prepared = prepare(tmp_path)
    assert reading_policy({}) is None and reading_instruction({}) == ''
    assert 'llm1_reading_policy' not in prepared['selection_context']
    inputs = analysis._document_input(settings(), prepared['pdf_path'].read_bytes(), 'service', None,
                                     prepared['source_document_manifest'], context(prepared))
    expected = read_prompt(BASES_PROMPT_FILENAME) + PARTICIPATION_INSTRUCTION + analysis._document_set_instruction(prepared['source_document_manifest'], context(prepared))
    assert INSTRUCTION not in expected and inputs['prompt_hash'] == canonical_hash(expected)
    args = selected.parser().parse_args(['--folder', str(tmp_path / 'new')])
    assert 'llm1_reading_policy' not in selected.plan(args)
    args.compact_unique_requirements = True
    assert selected.plan(args)['llm1_reading_policy'] == POLICY


@pytest.mark.parametrize('value', [None, '', 'future', {}, True, ['compact_unique_requirements_v1']])
def test_unknown_reading_policy_fails_before_enrichment_or_client(value, tmp_path, sdk):
    with pytest.raises(ValueError, match='reading policy'):
        selected.enrich(None, None, None, None, {'llm1_reading_policy': value}, None, None, None)
    prepared = prepare(tmp_path)
    prepared['selection_context']['llm1_reading_policy'] = value
    prepared['document_set_identity']['selection_context'] = deepcopy(prepared['selection_context'])
    prepared['selection_hash'] = prepared['document_set_hash'] = canonical_hash(prepared['document_set_identity'])
    with pytest.raises(selected_extractor.SelectedTransportError):
        selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path)
    sdk.constructor.assert_not_called()


def test_direct_extractor_cannot_claim_compact_policy_without_real_instructions(sdk, tmp_path):
    prepared = prepare(tmp_path, compact=True)
    with selected_extractor.SelectedGeminiExtractor(settings(), prepared, tmp_path) as reader:
        with pytest.raises(selected_extractor.SelectedTransportError, match='absent from the effective prompt'):
            reader.extract(prepared['pdf_path'].read_bytes(), doc_class='bases_service')
    sdk.client.models.generate_content.assert_not_called()
    assert not list(tmp_path.glob('gemini-selected-*'))


def test_compact_persistence_preserves_selector_material_requirements_and_policy(monkeypatch):
    conn, extraction = selected_fixture(monkeypatch)
    old = deepcopy(extraction)
    extraction['selection_context']['llm1_reading_policy'] = POLICY
    sync(extraction, conn)
    result = finish(conn, extraction)
    assert result['projection_pending'] and result['requires_human_review']
    metadata = json.loads(conn.updates[0][1][2])
    assert metadata['selection_context']['llm1_reading_policy'] == POLICY
    exact = deepcopy(metadata['selection_context'])
    exact.pop('llm1_reading_policy')
    assert exact == old['selection_context']
    assert metadata['source_document_manifest'] == old['raw_extraction_json']['source_document_manifest']
    assert extraction['raw_extraction_json']['participation'] == old['raw_extraction_json']['participation']


@pytest.mark.parametrize('policy', [None, 'unknown'])
def test_persistence_rejects_unknown_policy_even_with_rehashed_identity(monkeypatch, policy):
    conn, extraction = selected_fixture(monkeypatch)
    extraction['selection_context']['llm1_reading_policy'] = policy
    sync(extraction, conn)
    with pytest.raises(llm1_completion.Llm1CompletionError, match='reading policy'):
        finish(conn, extraction)
    assert not conn.persist_calls and not conn.updates


def test_recovery_policy_is_explicit_and_frozen_without_changing_original(tmp_path):
    args, legacy, original_outcome, *_ = truncation_fixture(tmp_path)
    before = original_outcome.read_bytes()
    assert 'llm1_reading_policy' not in legacy
    args.compact_unique_requirements = True
    args.compact_source_folder = [args.original_folder]
    args.extended_llm1_output = True
    compact = recovery.plan(args)
    assert compact['llm1_reading_policy'] == POLICY
    assert compact['gemini_model'] == legacy['gemini_model']
    assert compact['llm1_generation_limits']['max_output_tokens'] == 65536
    recovery.private_write(args.folder / 'manifest.private.json', compact)
    assert recovery.plan(args) == compact
    args.compact_unique_requirements = False
    args.compact_source_folder = []
    with pytest.raises(ValueError, match='Pinned recovery inputs changed'):
        recovery.plan(args)
    assert original_outcome.read_bytes() == before


def truncation_fixture(tmp_path, *, maximum=24576):
    args, _, proof, _, original_outcome = recovery_fixture(tmp_path)
    manifest_path = args.original_folder / 'manifest.private.json'
    manifest = recovery.read_private(manifest_path)
    manifest['gemini_model'] = settings().gemini_model
    if maximum == 65536:
        manifest['llm1_generation_limits'] = {'policy_version': 'prod2_explicit_extended_output_v1', 'max_output_tokens': 65536, 'timeout_ms': 300000}
    manifest['pending_snapshot'] = [{'candidate_id': '123', 'payload': {'row': proof['row'], 'query': proof['query']}}]
    rewrite(manifest_path, manifest)
    owner = args.original_folder / '123'
    previous = prepare(owner)
    first = previous['document_set'][0]
    pdf = previous['pdf_path'].read_bytes()
    kwargs = {'object_type': 'service', 'original_sha256': first['original_sha256'],
              'manifest': previous['source_document_manifest'], 'document_context': context(previous)}
    request = analysis.extraction_request_hash(settings(), pdf, **kwargs)
    inputs = analysis._document_input(settings(), pdf, 'service', kwargs['original_sha256'], kwargs['manifest'], kwargs['document_context'])
    manifest['cache_folder'] = str(owner)
    rewrite(manifest_path, manifest)
    claim = {'status': 'claimed', 'id': 27, 'claim_token': str(uuid4()), 'evaluation_key': canonical_hash({
        'convocation': 123, 'kind': 'gemini_extraction', 'request_hash': request, 'model': settings().gemini_model,
        'policy_version': 'prod2_document_extraction_v1', 'profile_id': manifest['profile_id']})}
    recovery.private_write(owner / ('gemini_extraction-' + request + '.claim.private.json'), claim)
    attempt_id = 'gemini-selected-' + uuid4().hex
    attempt = {'attempt_id': attempt_id, 'model': settings().gemini_model, 'prompt_hash': inputs['prompt_hash'],
        **{k: previous[k] for k in ('analysis_sha256', 'document_set_hash', 'document_set_identity', 'selection_context', 'page_count')},
        'max_output_tokens': maximum, 'timeout_ms': 180000 if maximum == 24576 else 300000,
        'transport_policy': selected_extractor.SELECTED_TRANSPORT_POLICY,
        'model_call_started': True, 'provider_usage_known': True, 'finish_reason': 'MAX_TOKENS',
        'input_tokens': 1000, 'output_tokens': maximum, 'cost_usd': selected_extractor.estimate_cost(settings().gemini_model, 1000, maximum)}
    for suffix, status in [('started', 'generation_reserved'), ('response', 'response_received'), ('outcome', 'response_parse_failure')]:
        recovery.private_write(owner / (attempt_id + '.' + suffix + '.private.json'), {**attempt, 'status': status})
    rewrite(original_outcome, {'convocation_id': 123, 'accepted': True, 'status': 'needs_review', 'error_type': 'TruncatedResponseError'})
    legacy = recovery.plan(args)
    return args, legacy, original_outcome, previous, attempt, request


@pytest.mark.parametrize('maximum', [24576, 65536])
def test_known_truncation_binding_and_exact_pdf_rebind_are_offline_immutable(tmp_path, maximum, monkeypatch):
    args, legacy, original_outcome, previous, attempt, request = truncation_fixture(tmp_path, maximum=maximum)
    original = args.original_folder
    original_manifest = recovery.read_private(original / 'manifest.private.json')
    protected = {path: compact_reading.file_sha(path) for path in original.rglob('*.private.json')}
    binding = compact_reading.compact_source_bindings(original, original_manifest, [original], [123])['123']
    assert binding['request_hash'] == request and binding['model'] == settings().gemini_model
    assert binding['original_max_output_tokens'] == maximum and binding['original_cost_usd'] == attempt['cost_usd']
    newfolder = tmp_path / 'compact-work'
    newfolder.mkdir()
    original_source = original / '123/source.pdf'
    copied = newfolder / 'source.pdf'
    copied.write_bytes(original_source.read_bytes())
    newcontext = {**previous['selection_context'], 'llm1_reading_policy': POLICY}
    row = previous['document_set'][0]
    monkeypatch.setattr(pdf_preparation, 'prepare_pdf', lambda *a, **kw: pytest.fail('Compact cannot reconvert'))
    current = compact_reading.prepare_compact_document_set(newfolder, previous['document_set'], {row['candidate_id']: copied},
        newcontext, binding, cache_folder=tmp_path / 'compact-cache', max_source_bytes=previous['max_source_bytes'])
    assert compact_reading.compare_compact_prepared(previous, current)
    assert current['pdf_path'].read_bytes() == previous['pdf_path'].read_bytes()
    assert current['page_count'] == 25 and current['document_set_hash'] != previous['document_set_hash']
    assert all(compact_reading.file_sha(path) == sha for path, sha in protected.items())
    assert compact_reading.prepare_compact_document_set(newfolder, previous['document_set'], {row['candidate_id']: copied},
        newcontext, binding, cache_folder=tmp_path / 'compact-cache', max_source_bytes=previous['max_source_bytes']) == current


@pytest.mark.parametrize('field,value', [('provider_usage_known', False), ('cost_usd', 0), ('model', 'unknown'),
    ('max_output_tokens', 10000), ('max_output_tokens', 24576.0), ('timeout_ms', 180000.0),
    ('finish_reason', 'STOP'), ('output_tokens', -1), ('input_tokens', True)])
def test_source_rejects_unknown_or_changed_usage(tmp_path, field, value):
    args, _, _, _, attempt, _ = truncation_fixture(tmp_path)
    owner = args.original_folder / '123'
    changed = recovery.read_private(owner / (attempt['attempt_id'] + '.outcome.private.json'))
    changed[field] = value
    rewrite(owner / (attempt['attempt_id'] + '.outcome.private.json'), changed)
    with pytest.raises(pdf_preparation.PdfPreparationError):
        compact_reading.compact_source_bindings(args.original_folder, recovery.read_private(args.original_folder / 'manifest.private.json'), [args.original_folder], [123])


def test_frozen_source_change_or_valid_response_stops_new_reading(tmp_path):
    args, _, _, previous, attempt, request = truncation_fixture(tmp_path)
    original_manifest = recovery.read_private(args.original_folder / 'manifest.private.json')
    binding = compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [123])['123']
    path = args.original_folder / '123' / (attempt['attempt_id'] + '.started.private.json')
    value = recovery.read_private(path)
    value['reserved_at'] = 'changed'
    rewrite(path, value)
    with pytest.raises(pdf_preparation.PdfPreparationError, match='evidence changed'):
        compact_reading.load_compact_source(binding)
    recovery.private_write(args.original_folder / '123' / ('gemini_extraction-' + request + '.response.private.json'), {'completed': True})
    with pytest.raises(pdf_preparation.PdfPreparationError, match='already has a valid response'):
        compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [123])


def test_old_owner_response_created_after_preflight_stops_compact_generation(tmp_path):
    args, _, _, _, _, request = truncation_fixture(tmp_path)
    original_manifest = recovery.read_private(args.original_folder / 'manifest.private.json')
    binding = compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [123])['123']
    recovery.private_write(args.original_folder / '123' / ('gemini_extraction-' + request + '.response.private.json'), {'completed': True})
    with pytest.raises(pdf_preparation.PdfPreparationError, match='completed while awaiting'):
        compact_reading.load_compact_source(binding)


@pytest.mark.parametrize('field', ['analysis_sha256', 'page_count', 'source_document_manifest', 'document_set'])
def test_comparator_never_relaxes_pdf_or_attribution(tmp_path, field):
    previous, current = prepare(tmp_path), prepare(tmp_path, compact=True)
    if field == 'page_count': current[field] -= 1
    elif field == 'analysis_sha256': current[field] = 'a' * 64
    elif field == 'source_document_manifest': current[field][0]['page_offset'] = 1
    else: current[field] = []
    with pytest.raises(pdf_preparation.PdfPreparationError):
        compact_reading.compare_compact_prepared(previous, current)


def test_explicit_source_roots_and_frozen_pending_scope_cannot_expand(tmp_path):
    args, _, _, _, _, _ = truncation_fixture(tmp_path)
    original_manifest = recovery.read_private(args.original_folder / 'manifest.private.json')
    with pytest.raises(pdf_preparation.PdfPreparationError, match='frozen pending IDs'):
        compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [456])
    with pytest.raises(pdf_preparation.PdfPreparationError, match='distinct explicit folders'):
        compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder] * 2, [123])
    args.compact_unique_requirements = True
    with pytest.raises(ValueError, match='explicit compact source folders'):
        recovery.plan(args)
    with pytest.raises(pdf_preparation.PdfPreparationError, match='configured fallback model'):
        compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [123], fallback_model=None)


def test_recomputed_request_hash_must_bind_the_claim_filename(tmp_path):
    args, _, _, _, _, request = truncation_fixture(tmp_path)
    original_manifest = recovery.read_private(args.original_folder / 'manifest.private.json')
    owner = args.original_folder / '123'
    old = owner / ('gemini_extraction-' + request + '.claim.private.json')
    claim = recovery.read_private(old)
    forged = 'a' * 64
    claim['evaluation_key'] = canonical_hash({'convocation': 123, 'kind': 'gemini_extraction', 'request_hash': forged,
        'model': settings().gemini_model, 'policy_version': 'prod2_document_extraction_v1', 'profile_id': original_manifest['profile_id']})
    old.unlink()
    recovery.private_write(owner / ('gemini_extraction-' + forged + '.claim.private.json'), claim)
    with pytest.raises(pdf_preparation.PdfPreparationError, match='claim/prompt is inconsistent'):
        compact_reading.compact_source_bindings(args.original_folder, original_manifest, [args.original_folder], [123])


def test_legacy_missing_generation_fields_remain_null_and_do_not_invent_audit_data(tmp_path):
    args, _, _, _, attempt, _ = truncation_fixture(tmp_path)
    owner = args.original_folder / '123'
    for suffix in ('started', 'response', 'outcome'):
        path = owner / (attempt['attempt_id'] + '.' + suffix + '.private.json')
        receipt = recovery.read_private(path)
        receipt.pop('max_output_tokens')
        receipt.pop('timeout_ms')
        rewrite(path, receipt)
    protected = {path: compact_reading.file_sha(path) for path in owner.glob('*.private.json')}
    binding = compact_reading.compact_source_bindings(args.original_folder, recovery.read_private(args.original_folder / 'manifest.private.json'), [args.original_folder], [123])['123']
    assert binding['original_generation_limits_policy'] == 'legacy_selected_defaults_absent'
    assert binding['original_max_output_tokens'] is None and binding['original_timeout_ms'] is None
    assert all(compact_reading.file_sha(path) == sha for path, sha in protected.items())


@pytest.mark.parametrize('variant', ['partial_one', 'partial_pair', 'explicit_null', 'declared_but_absent'])
def test_legacy_limit_absence_does_not_allow_partial_null_or_declared_missing_limits(tmp_path, variant):
    args, _, _, _, attempt, _ = truncation_fixture(tmp_path)
    owner = args.original_folder / '123'
    for suffix in ('started', 'response', 'outcome'):
        path = owner / (attempt['attempt_id'] + '.' + suffix + '.private.json')
        receipt = recovery.read_private(path)
        receipt.pop('max_output_tokens')
        receipt.pop('timeout_ms')
        if variant == 'explicit_null': receipt.update(max_output_tokens=None, timeout_ms=None)
        if variant == 'partial_one' and suffix == 'started': receipt['max_output_tokens'] = 24576
        if variant == 'partial_pair' and suffix == 'started': receipt.update(max_output_tokens=24576, timeout_ms=180000)
        rewrite(path, receipt)
    manifest_path = args.original_folder / 'manifest.private.json'
    manifest = recovery.read_private(manifest_path)
    if variant == 'declared_but_absent':
        manifest['llm1_generation_limits'] = {'policy_version': 'prod2_explicit_extended_output_v1', 'max_output_tokens': 65536, 'timeout_ms': 300000}
        rewrite(manifest_path, manifest)
    with pytest.raises(pdf_preparation.PdfPreparationError):
        compact_reading.compact_source_bindings(args.original_folder, manifest, [args.original_folder], [123])
