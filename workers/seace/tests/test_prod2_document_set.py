"""Loose document coverage, exact replay and pending archives; no external I/O."""
import hashlib
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
import pytest

from buenapro_worker.prod2 import document_set as subject


def pdf_bytes(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 50 740 Td (' + text + ') Tj ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    content = io.BytesIO()
    writer.write(content)
    return content.getvalue()


def attachment(folder, number, filename, text='SYNTHETIC REQUIREMENT'):
    code = f'00000000-0000-4000-8000-{number:012d}'
    doc = {'uuid': code, 'filename': filename, 'type': 'Documento', 'stage': 'Convocatoria',
           'published_at': '06/10/2026 12:00'}
    path = folder / (code + Path(filename).suffix.lower())
    content = pdf_bytes(text)
    path.write_bytes(content)
    return doc, {'original': path, 'manifest': {'document_uuid': code,
        'sha256': hashlib.sha256(content).hexdigest(), 'format': 'pdf'}}


def build(folder, pairs, extra=None):
    return subject.prepare_direct_document_set(folder, [doc for doc, _ in pairs] + (extra or []),
        {doc['uuid']: download for doc, download in pairs}, inventory_complete=True)


def test_all_loose_files_are_read_despite_names_and_roles_but_archives_never_open(tmp_path):
    first = attachment(tmp_path, 1, 'nombre desconocido.pdf', 'TECHNICAL REQUIREMENT')
    second = attachment(tmp_path, 2, 'Acta Buena Pro.pdf', 'AWARD HISTORY')
    archive = {'uuid': '00000000-0000-4000-8000-000000000003', 'filename': 'bases.zip',
        'type': 'Bases integradas', 'stage': 'Convocatoria', 'published_at': '06/10/2026 12:00'}
    result = build(tmp_path, [second, first], [archive])
    reader = PdfReader(result['pdf_path'])
    assert [page.extract_text() for page in reader.pages] == ['TECHNICAL REQUIREMENT', 'AWARD HISTORY']
    assert result['page_count'] == 2
    assert [row['document_uuid'] for row in result['document_set']] == [first[0]['uuid'], second[0]['uuid']]
    assert result['excluded_documents'][0]['reason'] == 'archive_deferred'
    assert result['source_document_manifest'][1]['start_page'] == 2
    assert result['source_document_manifest'][1]['source_url'].endswith(second[0]['uuid'])
    assert not list(tmp_path.glob('*.zip'))


def test_pdf_and_both_word_formats_are_converted_without_entering_archive_branch(monkeypatch, tmp_path):
    pairs = [attachment(tmp_path, n, name, f'CONTENT {n}') for n, name in
             enumerate(['TDR.pdf', 'anexo.doc', 'especificacion.docx'], 1)]
    calls = []
    def convert(path, extension, **kwargs):
        assert extension in {'pdf', 'doc', 'docx'}
        calls.append(extension)
        return path
    monkeypatch.setattr(subject, 'prepare_pdf', convert)
    result = build(tmp_path, pairs)
    assert calls == ['pdf', 'doc', 'docx']
    assert len(PdfReader(result['pdf_path']).pages) == 3


def test_exact_replay_reuses_pinned_bytes_and_inventory_order_does_not_repay_conversion(monkeypatch, tmp_path):
    pairs = [attachment(tmp_path, 1, 'uno.pdf', 'ONE'), attachment(tmp_path, 2, 'dos.pdf', 'TWO')]
    first = build(tmp_path, pairs)
    def forbidden(*args, **kwargs):
        pytest.fail('Exact document-set replay must not reconvert')
    monkeypatch.setattr(subject, 'prepare_pdf', forbidden)
    replay = build(tmp_path, list(reversed(pairs)))
    assert replay['pdf_path'] == first['pdf_path']
    assert replay['analysis_sha256'] == first['analysis_sha256']


def test_identical_pdf_bytes_are_not_duplicated_but_all_original_uuids_remain_attributable(tmp_path):
    pairs = [attachment(tmp_path, 1, 'uno.pdf'), attachment(tmp_path, 2, 'otro.pdf')]
    result = build(tmp_path, pairs)
    assert result['page_count'] == 1 and len(result['document_set']) == 2
    first, duplicate = result['source_document_manifest']
    assert duplicate['duplicate_of'] == first['document_uuid']
    assert duplicate['start_page'] == first['start_page'] == 1
    assert duplicate['end_page'] == first['end_page'] == 1


def test_new_original_revision_or_metadata_never_reuses_old_set(tmp_path):
    first = attachment(tmp_path, 1, 'uno.pdf', 'OLD REQUIREMENT')
    old = build(tmp_path, [first])
    changed = attachment(tmp_path, 1, 'uno.pdf', 'NEW REQUIREMENT')
    new = build(tmp_path, [changed])
    assert new['document_set_hash'] != old['document_set_hash']
    assert PdfReader(new['pdf_path']).pages[0].extract_text() == 'NEW REQUIREMENT'
    changed[0]['published_at'] = '07/10/2026 12:00'
    assert build(tmp_path, [changed])['document_set_hash'] != new['document_set_hash']


def test_missing_download_or_incomplete_inventory_never_produces_partial_analysis(tmp_path):
    doc, download = attachment(tmp_path, 1, 'unknown.pdf')
    with pytest.raises(subject.DocumentSetError, match='verified download'):
        subject.prepare_direct_document_set(tmp_path, [doc], {}, inventory_complete=True)
    with pytest.raises(subject.DocumentSetError, match='complete'):
        subject.prepare_direct_document_set(tmp_path, [doc], {doc['uuid']: download}, inventory_complete=False)
    assert not list(tmp_path.glob('direct-analysis-*'))


def test_corrupt_or_unsupported_loose_attachment_blocks_analysis(tmp_path):
    pair = attachment(tmp_path, 1, 'unknown.pdf')
    pair[1]['original'].write_bytes(b'changed')
    with pytest.raises(subject.DocumentSetError, match='does not match'):
        build(tmp_path, [pair])
    other = {**pair[0], 'filename': 'requirements.xlsx'}
    with pytest.raises(subject.DocumentSetError, match='Unsupported'):
        subject.document_inventory([other], inventory_complete=True)


def test_tampered_combined_pdf_and_excess_pages_cannot_be_paid(monkeypatch, tmp_path):
    pair = attachment(tmp_path, 1, 'uno.pdf')
    result = build(tmp_path, [pair])
    result['pdf_path'].write_bytes(b'tampered')
    with pytest.raises(subject.DocumentSetError, match='Pinned'):
        build(tmp_path, [pair])
    monkeypatch.setattr(subject, 'MAX_ANALYSIS_PAGES', 1)
    second = attachment(tmp_path, 2, 'dos.pdf', 'DIFFERENT')
    with pytest.raises(subject.DocumentSetError, match='page policy'):
        build(tmp_path, [pair, second])


def test_archive_only_inventory_cannot_claim_a_read_or_invoke_conversion(monkeypatch):
    archive = {'uuid': '00000000-0000-4000-8000-000000000003', 'filename': 'bases.rar',
        'type': 'Bases', 'stage': 'Convocatoria', 'published_at': '06/10/2026 12:00'}
    with pytest.raises(subject.DocumentSetError, match='No loose'):
        subject.document_inventory([archive], inventory_complete=True)
