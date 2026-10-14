"""Opt-in real SQL contract: synthetic shadow schema, unconditional rollback.

No source requests, provider calls or public-table writes. Public serial defaults
are isolated before inserts. Transport diagnostics never expose configuration.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2 import repository
from buenapro_worker.prod2.client import SEARCH_URL
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.document_set import DIRECT_DOCUMENTS_POLICY
from buenapro_worker.prod2.llm1_completion import persist_completed_llm1, Llm1CompletionError
from buenapro_worker.prod2.runtime import production_settings
from test_prod2_batch_postgres_integration import _shadow_schema


@pytest.mark.skipif(os.environ.get('BUENAPRO_PG_SCRATCH_TEST') != '1', reason='Explicit scratch opt-in required')
def test_loose_document_set_paid_projection_replay_and_stale_nonanchor_rollback_in_pg():
    root, schema = Path(__file__).resolve().parents[3], 'prod2_direct_scratch_' + uuid4().hex
    conn, stage = None, 'tunnel'
    try:
        with production_settings(ssh_host='ubuntu@168.129.177.199',
                                 ssh_key=root / 'credentials/ssh-key-2026-07-14.key') as settings:
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
            stage = 'isolated_schema'
            public_sequences = _shadow_schema(conn, schema, root)
            observed = datetime.now(timezone.utc)
            local = observed.astimezone(repository.LIMA)
            published = local.strftime('%d/%m/%Y %H:%M')
            conv, procedure = 9700000001, 8700000001
            row = {'procedure_id': procedure, 'convocatoria_id': conv, 'entity': 'SYNTHETIC BUYER',
                'published_at': published, 'nomenclature': f'SCRATCH-{local.year}', 'object': 'Servicio',
                'description': 'SYNTHETIC SOFTWARE', 'amount_text': '100.00', 'currency': 'PEN', 'seace_version': '3'}
            query = {'keyword': 'SOFTWARE', 'object_type': 'service', 'year': local.year,
                'published_from': f'{local.year}-01-01', 'published_to': local.date().isoformat(), 'seace_version': '3'}
            discovery = repository.upsert_pilot_discovery(conn, row, query,
                batch_id='direct-scratch-' + uuid4().hex, observed_at=observed)
            codes = [f'00000000-0000-4000-8000-{n:012d}' for n in (1, 2, 3)]
            names = ['documento sin clasificar.pdf', 'Acta.pdf', 'bases.zip']
            documents = [{'uuid': code, 'filename': name, 'type': 'Documento', 'stage': 'Convocatoria',
                'published_at': published, 'seace_version': '3'} for code, name in zip(codes, names)]
            detail = {**{key: row[key] for key in ('procedure_id', 'convocatoria_id', 'entity', 'nomenclature', 'object', 'description')},
                'seace_version': '3', 'schedule': [], 'item_states': [], 'documents': documents,
                'document_inventory_total': 3, 'document_inventory_complete': True}
            repository.persist_detail(conn, detail, observed_at=observed, source_url=SEARCH_URL)
            direct = [{'document_uuid': doc['uuid'], 'filename': doc['filename'], 'stage': doc['stage'],
                'type': doc['type'], 'published_at': published, 'source_url': METADATA_URL + '?id=' + doc['uuid'],
                'original_sha256': str(index + 1) * 64} for index, doc in enumerate(documents[:2])]
            excluded = [{key: value for key, value in {
                'document_uuid': codes[2], 'filename': names[2], 'stage': 'Convocatoria', 'type': 'Documento',
                'published_at': published, 'source_url': METADATA_URL + '?id=' + codes[2],
                'reason': 'archive_deferred'}.items()}]
            context = {'document_scope': DIRECT_DOCUMENTS_POLICY, 'document_set': direct, 'excluded_documents': excluded}
            context['document_set_hash'] = canonical_hash(context)
            pages = [{**doc, 'analysis_sha256': str(index + 4) * 64, 'start_page': index + 1,
                      'end_page': index + 1} for index, doc in enumerate(direct)]
            for doc in direct:
                repository.record_document_download(conn, conv, {'document_uuid': doc['document_uuid'],
                    'sha256': doc['original_sha256'], 'bytes': 1000, 'content_type': 'application/pdf', 'format': 'pdf'})
            stage = 'paid_set_projection'
            request = canonical_hash({'synthetic_input': context, 'pages': pages})
            claim = repository.claim_evaluation(conn, conv, kind='gemini_extraction', request_hash=request,
                model=settings.gemini_model, policy_version='prod2_document_extraction_v1')
            raw = {**context, 'source_document_manifest': pages,
                'requirements': {'provider': [{'tipo': 'specific_experience', 'descripcion': 'SYNTHETIC SUPPORT', 'obligatorio': True}]},
                'participation': {'subcontratacion': {'status': 'conditional', 'clause': 'SYNTHETIC CLAUSE',
                    'page': 2, 'conditions': ['SYNTHETIC AUTHORIZATION']}}}
            extraction = {**context, 'cache_key': 'prod2:llm1:' + request, 'original_sha256': direct[0]['original_sha256'],
                'analysis_sha256': 'a' * 64, 'doc_class': 'bases_service', 'model': settings.gemini_model,
                'prompt_version': 'bases_extraction_v2', 'prompt_hash': 'b' * 64,
                'schema_version': 'bases_extraction_schema_v2', 'raw_extraction_json': raw,
                'requires_human_review': True, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0,
                'document_uuid': codes[0], 'document_sha256': direct[0]['original_sha256']}
            repository.complete_evaluation(conn, claim['id'], claim['claim_token'], extraction,
                input_tokens=0, output_tokens=0, cost_usd=0)
            extraction.update(evaluation_id=claim['id'], allowed_models=[settings.gemini_model, settings.gemini_fallback_model])
            def finish():
                return persist_completed_llm1(conn, convocation_id=conv, document_uuid=codes[0], extraction=extraction,
                    document_sha256=direct[0]['original_sha256'], source_url=direct[0]['source_url'])
            first, replay = finish(), finish()
            assert first['extraction_id'] == replay['extraction_id'] and first['projection_pending'] is True
            stored = conn.execute('SELECT raw_extraction_json FROM prod2_document_extractions WHERE id=%s',
                                  (first['extraction_id'],)).fetchone()['raw_extraction_json']
            assert stored == raw and stored['participation']['subcontratacion']['status'] == 'conditional'
            canonical = conn.execute('SELECT participation_terms_json,subcontracting_status FROM opportunities WHERE id=%s',
                (discovery['opportunity_id'],)).fetchone()
            assert canonical['subcontracting_status'] == 'not_identified'
            assert canonical['participation_terms_json']['projection_pending'] is True
            assert canonical['participation_terms_json']['source_document_manifest'] == pages
            assert conn.execute('SELECT count(*) AS n FROM prod2_document_extractions').fetchone()['n'] == 1
            stage = 'nonanchor_revision_invalidates_existing_paid_set'
            repository.record_document_download(conn, conv, {'document_uuid': codes[1], 'sha256': 'c' * 64,
                'bytes': 1001, 'content_type': 'application/pdf', 'format': 'pdf'})
            # Transaction-stable now() can tie downloads; either a newer or tied
            # differing SHA must prevent the former input from projecting again.
            with pytest.raises(Llm1CompletionError, match='downloaded revision'):
                finish()
            stage = 'no_public_sequence_mutation_and_rollback'
            for name, before in public_sequences.items():
                assert conn.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.SQL(name))).fetchone() == before
            conn.rollback()
            assert conn.execute('SELECT to_regnamespace(%s) AS ns', (schema,)).fetchone()['ns'] is None
            conn.rollback()
    except Exception as exc:
        pytest.fail(f'Direct-set scratch failed at {stage}: {type(exc).__name__}; SQLSTATE={getattr(exc, "sqlstate", None)}', pytrace=False)
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except psycopg.Error:
                # The owned tunnel is closed on exception; closing this
                # connection also aborts every uncommitted scratch operation.
                pass
            conn.close()
