"""Which listed file is the requirement: classification by name and the fallback to an attachment."""
import json
import os
import random
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.documents.pdf import (
    classify_document, classify_requirement_file, detect_mime, is_archive_name,
)
from buenapro_worker.jobs import download_file as download_job
from buenapro_worker.jobs import process_contract as process
from buenapro_worker.prod6 import document_selection as selection
from buenapro_worker.prod6.runtime import enqueue_unread_documents
from buenapro_worker.settings import Settings

# The two approved contracts that were never read (listing as SEACE returned it).
CM177_PRIMARY = [{"nombre": "REQUERIMIENTO.rar", "tamanio": "552005", "idTipoArchivo": 1, "idContratoArchivo": 416858,
                  "descripcionMime": "application/x-compressed", "descripcionExtension": ".rar", "_categoria": 1,
                  "nombreTipoArchivo": "Anexo de la contratación"}]
CM177_ATTACHMENTS = [
    {"nombre": "ANEXO N 04 y 06.docx", "idTipoArchivo": 2, "idContratoArchivo": 416861, "_categoria": 2,
     "descripcionMime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     "descripcionExtension": ".docx", "nombreTipoArchivo": "Declaración jurada de cumplimiento de requerimientos técnicos mínimo - RTM"},
    {"nombre": "ANEXO N 05 CCI.docx", "idTipoArchivo": 3, "idContratoArchivo": 416860, "_categoria": 2,
     "descripcionMime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     "descripcionExtension": ".docx", "nombreTipoArchivo": "Carta CCI"},
    {"nombre": "EETT - SERVIDOR  UE008 v2 F .pdf", "tamanio": "552554", "idTipoArchivo": 4, "idContratoArchivo": 416841,
     "_categoria": 2, "descripcionMime": "application/pdf", "descripcionExtension": ".pdf",
     "nombreTipoArchivo": "Documentos que acreditan el cumplimiento de TDR o EETT"},
    {"nombre": "FORMATO COTIZACION  - BIENES.pdf", "idTipoArchivo": 5, "idContratoArchivo": 416862, "_categoria": 2,
     "descripcionMime": "application/pdf", "descripcionExtension": ".pdf", "nombreTipoArchivo": "Oferta económica"},
]
CM5_PRIMARY = [{"nombre": "TDR -ADQUISICIÓN DE DOS (02) VARIADORES DE VELOCIDAD PARA TABLERO ELÉCTRICO DE POZO DE AGUA.pdf",
                "tamanio": "1630203", "idTipoArchivo": 1, "idContratoArchivo": 416787, "_categoria": 1,
                "descripcionMime": "application/pdf", "descripcionExtension": ".pdf"}]
CM5_ATTACHMENTS = [{"nombre": "ANEXOS CONTRATOS MENORES EPSEL S.A...pdf", "idTipoArchivo": 6, "idContratoArchivo": 416847,
                    "_categoria": 2, "descripcionMime": "application/pdf", "descripcionExtension": ".pdf",
                    "nombreTipoArchivo": "Otros documentos"}]


def names(files):
    return [item["nombre"] for item in files]


# --- classification by name --------------------------------------------------------------


@pytest.mark.parametrize("filename,expected", [
    ("TDR1_SISTEMA COMERCIAL.pdf", "tdr"),
    ("TDR.PS. 1321.pdf", "tdr"),
    ("TDR -ADQUISICIÓN DE DOS (02) VARIADORES DE VELOCIDAD PARA TABLERO ELÉCTRICO DE POZO DE AGUA.pdf", "tdr"),
    ("TERMINO DE REFERENCIA Nº TDR-012965-2026-MIDAGRI-AGROMERCADO.pdf", "tdr"),
    ("Términos de Referencia Soporte.pdf", "tdr"),
    ("Requerimiento_Final.pdf", "tdr"),
    ("REQUERIMIENTO.rar", "tdr"),
    ("TDR Servicio.docx", "tdr"),
    ("RTM NVA DIRECTIVA 6.docx", "tdr"),
    ("EETT - SERVIDOR  UE008 v2 F .pdf", "eett"),
    ("EE.TT MEDIDORES_-.pdf", "eett"),
    ("ESPECIFICACIONES-TECNICAS-00025-2026-IGP.pdf", "eett"),
    ("14. ESPECIFICACIONES TÉCNICAS V5 FINAL 21.10.2026.pdf", "eett"),
    ("Anexos_2026_OTROS_1790263468.pdf", "otro"),
    ("contratoArchivo.pdf", "otro"),
    ("Bases administrativas.pdf", "bases"),
    ("ACTA DE VISITA TECNICA.docx", "acta"),
    ("database.pdf", "otro"),
    ("Términos y condiciones.pdf", "otro"),
    ("", "otro"),
])
def test_names_decide_the_class_for_any_format(filename, expected):
    assert classify_document(filename, "application/pdf") == expected
    assert classify_document(filename, "application/octet-stream") == expected


@pytest.mark.parametrize("filename,expected", [
    ("EETT - SERVIDOR  UE008 v2 F .pdf", "eett"),
    ("EETT.pdf", "eett"),
    ("13.1. v2.0 Especificaciones Técnicas_.pdf", "eett"),
    ("TDR Mantto correctivo 4 impresoras OACGD-ACTUALIZADO.pdf", "tdr"),
    ("10.2.    v2.0 Terminos de Referencia_AP1_REV_v2.pdf", "tdr"),
    ("TDR.ps. 988.pdf", "tdr"),
    # Papers the supplier signs mention the requirement without being it.
    ("DECLARACION JURADA CUMPLIMIENTO RTM.pdf", None),
    ("2. DDJJ del Proveedor - Cumplimiento de EETT o TDR y no tener impedimento.docx", None),
    ("2-DD JJ CUMPLIR EE-TT  Y DD JJ  DE PROVEEDOR - CONTRATOS MENORES.docx", None),
    ("Documentos que acreditan el cumplimiento de los EETT.docx", None),
    ("ADJUNTAR REQUISITOS DEL PROVEEDOR SEGÚN EETT.docx", None),
    ("DOCUMENTOS SUSTENTATORIOS SEGÚN EETT.pdf", None),
    ("ANEXO 04-2026 DJ-TDR. EE-TT.docx", None),
    ("FORMATO COTIZACION  - BIENES.pdf", None),
    ("ANEXOS CONTRATOS MENORES EPSEL S.A...pdf", None),
    ("Bases administrativas.pdf", None),
])
def test_attachments_are_the_requirement_only_when_the_name_says_so(filename, expected):
    assert classify_requirement_file(filename) == expected


def test_archive_names():
    assert all(is_archive_name(name) for name in ("REQUERIMIENTO.rar", "A.ZIP", "x.7z"))
    assert not any(is_archive_name(name) for name in ("TDR.pdf", "rar.pdf", "", None))


def test_attachment_class_recognises_the_eett_the_entity_attached():
    classes = {item["nombre"]: process._attachment_class(item) for item in CM177_ATTACHMENTS}
    assert classes == {"ANEXO N 04 y 06.docx": "anexo", "ANEXO N 05 CCI.docx": "anexo",
                       "EETT - SERVIDOR  UE008 v2 F .pdf": "eett", "FORMATO COTIZACION  - BIENES.pdf": "cotizacion"}
    assert process._attachment_class({"nombre": "DECLARACION JURADA CUMPLIMIENTO RTM.pdf"}) == "otro"


def test_primary_class_follows_the_native_object_and_never_calls_an_archive_a_requirement():
    pdf = CM5_PRIMARY[0]
    assert process._primary_class(pdf, 2) == "tdr" and process._primary_class(pdf, 1) == "eett"
    assert process._primary_class(pdf, None) == "tdr"          # The name is a hint when the object is unknown.
    assert process._primary_class({"nombre": "contratoArchivo.pdf"}, None) is None
    assert process._primary_class(CM177_PRIMARY[0], 1) == "otro"


# --- selection ---------------------------------------------------------------------------


def test_cm5_reads_its_primary_pdf():
    result = selection.select_reading_files(CM5_PRIMARY, CM5_ATTACHMENTS, 1)
    assert names(result.files) == [CM5_PRIMARY[0]["nombre"]]
    assert result.fallback is None and result.manual_review is None


def test_cm177_reads_the_eett_attachment_because_the_primary_file_is_a_rar():
    result = selection.select_reading_files(CM177_PRIMARY, CM177_ATTACHMENTS, 1)
    assert result.fallback["nombre"] == "EETT - SERVIDOR  UE008 v2 F .pdf"
    assert names(result.files) == ["REQUERIMIENTO.rar", "EETT - SERVIDOR  UE008 v2 F .pdf"]
    assert result.manual_review is None


def test_a_primary_pdf_is_never_replaced_by_an_attachment():
    attachments = [{"nombre": "TDR otro.pdf", "descripcionMime": "application/pdf", "_categoria": 2}]
    assert selection.select_reading_files(CM5_PRIMARY, attachments, 2).fallback is None


def test_archive_without_a_requirement_pdf_is_an_explicit_manual_review():
    result = selection.select_reading_files(CM177_PRIMARY, CM177_ATTACHMENTS[:2], 1)
    assert result.fallback is None
    assert result.manual_review["reason"] == selection.REVIEW_ARCHIVE
    assert result.manual_review["files"] == ["REQUERIMIENTO.rar"]
    assert "comprimido" in result.manual_review["message"]


def test_word_primary_unlisted_primary_and_ambiguous_attachments_are_manual_reviews():
    word = [{"nombre": "TDR.docx", "descripcionExtension": ".docx", "_categoria": 1}]
    assert selection.select_reading_files(word, [], 2).manual_review["reason"] == selection.REVIEW_NOT_PDF
    assert selection.select_reading_files([], [], 2).manual_review["reason"] == selection.REVIEW_NO_PRIMARY
    two = [{"nombre": "EETT v1.pdf", "descripcionMime": "application/pdf"},
           {"nombre": "EETT v2.pdf", "descripcionMime": "application/pdf"}]
    ambiguous = selection.select_reading_files(CM177_PRIMARY, two, 1)
    assert ambiguous.manual_review["reason"] == selection.REVIEW_AMBIGUOUS and ambiguous.fallback is None


def test_the_native_object_breaks_the_tie_between_a_tdr_and_an_eett_attachment():
    both = [{"nombre": "TDR servicio.pdf", "descripcionMime": "application/pdf"},
            {"nombre": "EETT equipo.pdf", "descripcionMime": "application/pdf"}]
    assert selection.select_reading_files(CM177_PRIMARY, both, 1).fallback["nombre"] == "EETT equipo.pdf"
    assert selection.select_reading_files(CM177_PRIMARY, both, 2).fallback["nombre"] == "TDR servicio.pdf"
    assert selection.select_reading_files(CM177_PRIMARY, both, None).manual_review["reason"] == selection.REVIEW_AMBIGUOUS


# --- process_contract --------------------------------------------------------------------


def run_process_contract(primary, attachments, *, objeto, files_changed=True):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {"objeto_codigo": objeto, "missing_detail": False}
    saved = {}
    with patch.object(process, "SeaceClient") as client_class, \
            patch.object(process, "update_contract_detail", return_value=False), \
            patch.object(process, "upsert_files", side_effect=lambda r, s, c, files: saved.update(
                files=files) or files_changed):
        client = client_class.return_value.__enter__.return_value
        client.list_files.side_effect = [[dict(item) for item in primary], [dict(item) for item in attachments]]
        result = process.process_contract(MagicMock(), repo, 1, force_detail=False)
    downloads = [call.args[1]["id_contrato_archivo"] for call in repo.enqueue.call_args_list
                 if call.args[0] == "download_file"]
    return result, saved["files"], downloads


def test_process_contract_queues_the_eett_attachment_when_the_primary_file_is_a_rar():
    result, files, downloads = run_process_contract(CM177_PRIMARY, CM177_ATTACHMENTS, objeto=1)
    assert downloads == [416858, 416841]
    assert result["reading_fallback"] == "EETT - SERVIDOR  UE008 v2 F .pdf" and "manual_review" not in result
    marked = {item["nombre"]: item.get("_reading") for item in files}
    assert marked["EETT - SERVIDOR  UE008 v2 F .pdf"] == "attachment_is_requirement"
    assert marked["REQUERIMIENTO.rar"] is None


def test_process_contract_records_the_reason_when_nothing_can_be_read():
    result, files, downloads = run_process_contract(CM177_PRIMARY, CM177_ATTACHMENTS[:2], objeto=1)
    assert result["manual_review"]["reason"] == selection.REVIEW_ARCHIVE
    rar = next(item for item in files if item["nombre"] == "REQUERIMIENTO.rar")
    assert rar["_reading_review"]["reason"] == selection.REVIEW_ARCHIVE
    assert downloads == [416858]


def test_process_contract_is_unchanged_for_an_ordinary_contract():
    result, files, downloads = run_process_contract(CM5_PRIMARY, CM5_ATTACHMENTS, objeto=1)
    assert downloads == [416787] and result == {"detail_changed": False, "files_changed": True}
    assert not any("_reading" in item or "_reading_review" in item for item in files)
    _, _, none = run_process_contract(CM5_PRIMARY, CM5_ATTACHMENTS, objeto=1, files_changed=False)
    assert none == []


# --- download_file ------------------------------------------------------------------------


def run_download(filename, content, *, categoria=1, objeto=1):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {
        "filename": filename, "categoria": categoria, "objeto_codigo": objeto}
    with patch.object(download_job, "SeaceClient") as client_class:
        client_class.return_value.__enter__.return_value.download_file.return_value = content
        result = download_job.download_file_job(MagicMock(), repo, id_contrato=1, id_contrato_archivo=2)
    return result, repo


def test_a_rar_is_not_called_an_eett_and_is_never_sent_to_reading():
    result, repo = run_download("REQUERIMIENTO.rar", b"Rar!\x1a\x07\x01\x00 archive", objeto=1)
    assert result["doc_class"] == "otro"
    assert not [call for call in repo.enqueue.call_args_list if call.args[0] == "extract_tdr"]


def test_a_primary_pdf_keeps_the_class_of_the_native_object_and_is_read():
    result, repo = run_download("contratoArchivo.pdf", b"%PDF-1.7 body", objeto=1)
    assert result["doc_class"] == "eett"
    assert [call.args[0] for call in repo.enqueue.call_args_list] == ["extract_tdr"]
    result, _ = run_download("EETT - SERVIDOR.pdf", b"%PDF-1.7 body", categoria=2)
    assert result["doc_class"] == "eett"
    assert detect_mime(b"%PDF-1.7") == "application/pdf"


# --- the runtime queues what can be read -------------------------------------------------


def rows(*documents):
    return [{"id_contrato_archivo": item["idContratoArchivo"], "categoria": item["_categoria"],
             "filename": item["nombre"], "mime": item["descripcionMime"], "raw_file_json": item,
             "sha256_original": "pending:x", "objeto_codigo": 1, "read": False} for item in documents]


def queued(listing):
    repo = SimpleNamespace(conn=MagicMock(), batch_id="batch", enqueue=MagicMock(return_value=1))
    repo.conn.execute.return_value.fetchall.return_value = listing
    count = enqueue_unread_documents(repo, 42)
    return count, [call.args[1]["id_contrato_archivo"] for call in repo.enqueue.call_args_list]


def test_unread_cm177_queues_the_rar_download_and_the_eett_attachment():
    assert queued(rows(*CM177_PRIMARY, *CM177_ATTACHMENTS)) == (2, [416858, 416841])


def test_a_downloaded_archive_is_not_downloaded_again_every_cycle():
    listing = rows(*CM177_PRIMARY, *CM177_ATTACHMENTS)
    listing[0].update(sha256_original="a" * 64, mime="application/octet-stream")
    assert queued(listing) == (1, [416841])
    listing[3]["read"] = True
    assert queued(listing) == (0, [])


def test_a_read_primary_pdf_and_a_pending_one():
    listing = rows(*CM5_PRIMARY, *CM5_ATTACHMENTS)
    assert queued(listing) == (1, [416787])
    listing[0]["read"] = True
    assert queued(listing) == (0, [])


# --- PostgreSQL: listing keeps NULLs out and upgrades 'otro' (always rolled back) ---------

DATABASE_URL = os.environ.get("BUENAPRO_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="BUENAPRO_TEST_DATABASE_URL not set")


@pytest.fixture
def db():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@needs_db
def test_listing_classifies_primary_and_attachments_and_upgrades_an_earlier_otro(db):
    from buenapro_worker.queue.repository import JobRepository
    contract = random.randint(9_000_000_000, 9_900_000_000)
    db.execute("""INSERT INTO seace_contracts (id_contrato, codigo, anio, objeto_codigo, estado_codigo, descripcion,
                    hash_search, raw_search_json) VALUES (%s, %s, 2026, 1, 2, 'x', 'h', '{}'::jsonb)""",
               (contract, f"TEST-{contract}"))
    settings = Settings(database_url="postgresql://unused", gemini_api_key="unused")
    repo = JobRepository(db)
    # An older listing stored the EETT attachment as 'otro'.
    db.execute("""INSERT INTO contract_documents (id_contrato, id_contrato_archivo, categoria, filename, doc_class,
                    sha256_original, seace_download_url) VALUES (%s, 416841, 2, 'EETT.pdf', 'otro', 'pending:416841', 'u')""",
               (contract,))
    files = [dict(CM177_PRIMARY[0]), *[dict(item) for item in CM177_ATTACHMENTS], dict(CM5_PRIMARY[0])]
    process.upsert_files(repo, settings, contract, files)
    classes = {row["id_contrato_archivo"]: row["doc_class"] for row in db.execute(
        "SELECT id_contrato_archivo, doc_class FROM contract_documents WHERE id_contrato = %s", (contract,))}
    assert classes[416858] == "otro"        # the RAR is not an EETT
    assert classes[416787] == "eett"        # a listed primary PDF is never NULL (native object: goods)
    assert classes[416841] == "eett"        # 'otro' is upgraded when the name says it is the EETT
    assert classes[416862] == "cotizacion" and classes[416861] == "anexo"
    # A class a person or download_file already set is not overwritten.
    db.execute("UPDATE contract_documents SET doc_class = 'bases' WHERE id_contrato_archivo = 416841 AND id_contrato = %s",
               (contract,))
    process.upsert_files(repo, settings, contract, files)
    assert db.execute("SELECT doc_class FROM contract_documents WHERE id_contrato_archivo = 416841 AND id_contrato = %s",
                      (contract,)).fetchone()["doc_class"] == "bases"


@needs_db
def test_a_listing_refresh_never_overwrites_the_type_of_a_downloaded_document(db):
    """Production 26/10: SEACE lists some PDFs as application/octet-stream; the refresh replaced
    the type download_file had detected and the reading of that PDF stopped the service."""
    from buenapro_worker.queue.repository import JobRepository
    contract = random.randint(9_000_000_000, 9_900_000_000)
    db.execute("""INSERT INTO seace_contracts (id_contrato, codigo, anio, objeto_codigo, estado_codigo, descripcion,
                    hash_search, raw_search_json) VALUES (%s, %s, 2026, 2, 2, 'x', 'h', '{}'::jsonb)""",
               (contract, f"TEST-{contract}"))
    settings = Settings(database_url="postgresql://unused", gemini_api_key="unused")
    listed = [{"nombre": "contratoArchivo.pdf", "idTipoArchivo": 1, "idContratoArchivo": 420167, "_categoria": 1,
               "descripcionMime": "application/octet-stream", "descripcionExtension": ".pdf"},
              {"nombre": "ANEXOS.docx", "idTipoArchivo": 2, "idContratoArchivo": 420168, "_categoria": 2,
               "descripcionMime": "application/msword", "descripcionExtension": ".docx"}]
    process.upsert_files(JobRepository(db), settings, contract, listed)
    db.execute("""UPDATE contract_documents SET sha256_original = %s, mime = 'application/pdf'
                  WHERE id_contrato = %s AND id_contrato_archivo = 420167""", ("a" * 64, contract))
    listed[1]["descripcionMime"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    process.upsert_files(JobRepository(db), settings, contract, listed)
    mimes = {row["id_contrato_archivo"]: row["mime"] for row in db.execute(
        "SELECT id_contrato_archivo, mime FROM contract_documents WHERE id_contrato = %s", (contract,))}
    assert mimes[420167] == "application/pdf"  # Downloaded: the content's type stays.
    assert mimes[420168] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"  # Pending: listing.


@needs_db
def test_enqueue_unread_documents_reads_the_real_rows(db):
    from buenapro_worker.queue.repository import JobRepository
    contract = random.randint(9_000_000_000, 9_900_000_000)
    db.execute("""INSERT INTO seace_contracts (id_contrato, codigo, anio, objeto_codigo, estado_codigo, descripcion,
                    hash_search, raw_search_json) VALUES (%s, %s, 2026, 1, 2, 'x', 'h', '{}'::jsonb)""",
               (contract, f"TEST-{contract}"))
    settings = Settings(database_url="postgresql://unused", gemini_api_key="unused")
    process.upsert_files(JobRepository(db), settings, contract, [dict(CM177_PRIMARY[0]),
                                                                  *[dict(item) for item in CM177_ATTACHMENTS]])
    repo = JobRepository(db)
    repo.batch_id = "batch"
    with patch.object(JobRepository, "enqueue", return_value=1) as enqueue:
        assert enqueue_unread_documents(repo, contract) == 2
    assert sorted(call.args[1]["id_contrato_archivo"] for call in enqueue.call_args_list) == [416841, 416858]
