from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from buenapro_worker.documents.pdf import classify_document, classify_requirement_file, is_archive_name
from buenapro_worker.jobs.poll_search import canonical_hash, parse_lima_datetime
from buenapro_worker.jobs.opportunity_identity import refresh_prod6_classification
from buenapro_worker.prod6.document_selection import select_reading_files
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.pacing import source_stop
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def _file_download_url(settings: Settings, id_contrato_archivo: int) -> str:
    return (
        f"{settings.seace_base_url}"
        f"/archivo/archivos-publico/descargar-archivo-contrato/{id_contrato_archivo}"
    )


def _cronograma_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {"etapas": detail.get("uitContratoEtapaProjectionList") or []}


def _first_item(detail: dict[str, Any]) -> dict[str, Any]:
    items = detail.get("uitContratoItemProjectionList") or []
    return items[0] if items else {}


def quotation_window_from_detail(detail: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    from buenapro_worker.jobs.refresh_schedule import schedule_instant
    stages = detail.get("uitContratoEtapaProjectionList") or []
    quotation = next(
        (
            stage
            for stage in stages
            if _parse_int(stage.get("idEtapaContrato")) == 2
            or "COTIZ" in str(stage.get("nomEtapaContrato") or "").upper()
        ),
        None,
    )
    if not quotation:
        return None, None
    return (
        schedule_instant(quotation.get("fecIni")),
        schedule_instant(quotation.get("fecFin")),
    )


def update_contract_detail(repo: JobRepository, id_contrato: int, detail: dict[str, Any]) -> bool:
    projection = detail.get("uitContratoCompletoProjection") or {}
    item = _first_item(detail)
    hash_detail = canonical_hash(detail)
    cronograma = _cronograma_from_detail(detail)
    fec_ini_cotizacion, fec_fin_cotizacion = quotation_window_from_detail(detail)

    row = repo.conn.execute(
        """
        UPDATE seace_contracts
        SET estado_codigo = COALESCE(%s, estado_codigo),
            objeto_codigo = COALESCE(%s, objeto_codigo),
            entidad_nombre = COALESCE(%s, entidad_nombre),
            descripcion = COALESCE(%s, descripcion),
            cubso_item = COALESCE(%s, cubso_item),
            ubigeo = COALESCE(%s, ubigeo),
            departamento = COALESCE(%s, departamento),
            provincia = COALESCE(%s, provincia),
            distrito = COALESCE(%s, distrito),
            fec_publica = COALESCE(%s, fec_publica),
            fec_ini_cotizacion = %s,
            fec_fin_cotizacion = %s,
            cronograma = %s::jsonb,
            resultado = COALESCE(NULLIF(%s::jsonb, '{}'::jsonb), resultado),
            hash_detail = %s,
            raw_detail_json = %s::jsonb,
            detail_fetched_at = now(),
            schedule_fetched_at = now(),
            pipeline_state = 'detail_fetched',
            updated_at = now()
        WHERE id_contrato = %s
          AND hash_detail IS DISTINCT FROM %s
        RETURNING id_contrato
        """,
        (
            projection.get("idEstadoContrato"),
            projection.get("idObjetoContrato"),
            projection.get("nomEntidad"),
            projection.get("desObjetoContrato"),
            item.get("codCubso"),
            item.get("ubigeo"),
            _location_part(item.get("nomDistritoExt"), 0),
            _location_part(item.get("nomDistritoExt"), 1),
            _location_part(item.get("nomDistritoExt"), 2),
            parse_lima_datetime(projection.get("fecPublica")),
            fec_ini_cotizacion,
            fec_fin_cotizacion,
            json.dumps(cronograma, ensure_ascii=False),
            json.dumps(_resultado_from_detail(detail), ensure_ascii=False),
            hash_detail,
            json.dumps(detail, ensure_ascii=False),
            id_contrato,
            hash_detail,
        ),
    ).fetchone()
    if row is None:
        # Sin cambios de contenido: igual registrar que el detalle esta fresco.
        repo.conn.execute(
            """UPDATE seace_contracts SET detail_fetched_at = now(), schedule_fetched_at = now(),
                 fec_ini_cotizacion = %s, fec_fin_cotizacion = %s WHERE id_contrato = %s""",
            (fec_ini_cotizacion, fec_fin_cotizacion, id_contrato),
        )
    else:
        refresh_prod6_classification(repo, id_contrato)
    return row is not None


def _location_part(value: str | None, index: int) -> str | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split("/") if part.strip()]
    return parts[index] if index < len(parts) else None


def _resultado_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    items = detail.get("uitContratoItemProjectionList") or []
    adjudicados = [
        {
            "ruc": item.get("codRuc"),
            "razon_social": item.get("nomRazonSocial"),
            "precio_total": item.get("precioTotal"),
            "estado_cotiza": item.get("nomEstadoCotiza"),
        }
        for item in items
        if item.get("codRuc") or item.get("nomRazonSocial") or item.get("precioTotal")
    ]
    return {"adjudicados": adjudicados} if adjudicados else {}


def _attachment_class(file_item: dict[str, Any]) -> str:
    """Clase para archivos de categoria 2 (no se descargan; solo listado/descarga directa)."""
    # SEACE usa normalmente idTipoArchivo=5 para Oferta económica. Algunas
    # entidades lo etiquetan como tipo 4/6 y lo incluyen en un paquete, por eso
    # el nombre sigue siendo un fallback necesario.
    if _parse_int(file_item.get("idTipoArchivo")) == 5:
        return "cotizacion"
    name = " ".join(
        str(file_item.get(key) or "") for key in ("nombre", "nombreTipoArchivo")
    ).lower()
    if any(term in name for term in ("cotiz", "oferta economica", "oferta económica", "propuesta economica", "propuesta económica", "estructura de costos")):
        return "cotizacion"
    if "anexo" in name or "formato" in name:
        return "anexo"
    # A PDF that is itself the TDR/EETT (the entity attached it here instead of as the main
    # file) is read like one; papers that merely cite the TDR ("declaracion de cumplimiento") are not.
    return classify_requirement_file(str(file_item.get("nombre") or ""),
                                     str(file_item.get("descripcionMime") or "")) or "otro"


def _primary_class(file_item: dict[str, Any], objeto_codigo: int | None) -> str | None:
    """Class of a category-1 file before it is downloaded (download_file confirms it).

    Same rule as download_file: the contract's native object decides, and an archive is not
    a readable requirement. Without it the name still gives a hint, so a listed TDR is never NULL.
    """
    name = str(file_item.get("nombre") or "")
    if is_archive_name(name):
        return "otro"
    if objeto_codigo == 1:
        return "eett"
    if objeto_codigo == 2:
        return "tdr"
    role = classify_document(name, str(file_item.get("descripcionMime") or ""))
    return role if role in {"tdr", "eett"} else None


def upsert_files(repo: JobRepository, settings: Settings, id_contrato: int, files: list[dict[str, Any]]) -> bool:
    hash_files = canonical_hash(files)
    previous = repo.conn.execute(
        "SELECT hash_files, objeto_codigo FROM seace_contracts WHERE id_contrato = %s",
        (id_contrato,),
    ).fetchone()
    changed = previous is None or previous["hash_files"] != hash_files
    objeto_codigo = _parse_int(previous["objeto_codigo"]) if previous is not None else None

    for file_item in files:
        id_archivo = file_item.get("idContratoArchivo")
        if id_archivo is None:
            continue
        categoria = int(file_item.get("_categoria") or 1)
        doc_class = _primary_class(file_item, objeto_codigo) if categoria == 1 else _attachment_class(file_item)
        repo.conn.execute(
            """
            INSERT INTO contract_documents (
              id_contrato,
              id_contrato_archivo,
              categoria,
              filename,
              mime,
              size_original_bytes,
              sha256_original,
              seace_download_url,
              doc_class,
              raw_file_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id_contrato, id_contrato_archivo)
            DO UPDATE SET
              filename = EXCLUDED.filename,
              -- Once downloaded, the type comes from the content (download_file); the listing's
              -- descripcionMime ("application/octet-stream" for some PDFs) never overwrites it.
              mime = CASE WHEN contract_documents.sha256_original LIKE 'pending:%%'
                          THEN EXCLUDED.mime ELSE contract_documents.mime END,
              size_original_bytes = EXCLUDED.size_original_bytes,
              seace_download_url = EXCLUDED.seace_download_url,
              doc_class = CASE
                WHEN EXCLUDED.doc_class = 'cotizacion' THEN 'cotizacion'
                WHEN contract_documents.doc_class = 'otro' AND EXCLUDED.doc_class IN ('tdr', 'eett')
                  THEN EXCLUDED.doc_class
                ELSE COALESCE(contract_documents.doc_class, EXCLUDED.doc_class)
              END,
              raw_file_json = EXCLUDED.raw_file_json,
              updated_at = now()
            """,
            (
                id_contrato,
                id_archivo,
                categoria,
                file_item.get("nombre") or f"{id_archivo}.pdf",
                file_item.get("descripcionMime"),
                _parse_int(file_item.get("tamanio")),
                f"pending:{id_archivo}",
                _file_download_url(settings, int(id_archivo)),
                doc_class,
                json.dumps(file_item, ensure_ascii=False),
            ),
        )

    repo.conn.execute(
        """
        UPDATE seace_contracts
        SET hash_files = %s,
            pipeline_state = 'files_listed',
            updated_at = now()
        WHERE id_contrato = %s
        """,
        (hash_files, id_contrato),
    )
    return changed


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def process_contract(
    settings: Settings,
    repo: JobRepository,
    id_contrato: int,
    *,
    batch_id: str | None = None,
    bucket: str | None = None,
    segment: int | str | None = None,
    force_detail: bool = False,
) -> dict[str, Any]:
    # La ingesta normal reutiliza el detalle. Lifecycle puede forzar el GET para
    # detectar cambios de estado y cronograma con una rotacion controlada.
    existing = repo.conn.execute(
        "SELECT raw_detail_json IS NULL AS missing_detail FROM seace_contracts WHERE id_contrato = %s",
        (id_contrato,),
    ).fetchone()
    needs_detail = force_detail or existing is None or bool(existing["missing_detail"])

    with SeaceClient(settings) as client:
        detail = client.contract_detail(id_contrato) if needs_detail else None
        primary_files = client.list_files(id_contrato, category=1)
        try:
            attachments = client.list_files(id_contrato, category=2)
        except Exception as error:
            # A 403/429, open breaker or spent budget is not "no attachments": the
            # contract must be retried, not stored with an incomplete file list.
            if source_stop(error) is not None:
                raise
            attachments = []

    for file_item in primary_files:
        file_item["_categoria"] = 1
    for file_item in attachments:
        file_item["_categoria"] = 2

    detail_changed = update_contract_detail(repo, id_contrato, detail) if detail is not None else False

    # El requerimiento es el PDF de categoria 1. Si no hay ninguno (RAR/ZIP, Word) y un anexo PDF
    # es el TDR/EETT, ese anexo se lee; si no, queda como revision manual con motivo. Las marcas
    # quedan en raw_file_json (consultable) y en el resultado del job.
    selection = select_reading_files(primary_files, attachments, _contract_object(repo, id_contrato))
    if selection.fallback is not None:
        selection.fallback["_reading"] = "attachment_is_requirement"
    if selection.manual_review is not None:
        for file_item in primary_files:
            file_item["_reading_review"] = {"reason": selection.manual_review["reason"],
                                            "message": selection.manual_review["message"]}
    files_changed = upsert_files(repo, settings, id_contrato, primary_files + attachments)

    if files_changed:
        # Los archivos de categoria 1 (TDR/requerimiento) se descargan y pasan a extraccion,
        # mas el anexo elegido cuando no hay PDF de categoria 1; el resto queda listado.
        for file_item in selection.files:
            id_archivo = file_item.get("idContratoArchivo")
            if id_archivo is None:
                continue
            payload: dict[str, object] = {"id_contrato": id_contrato, "id_contrato_archivo": id_archivo}
            if batch_id:
                payload["batch_id"] = batch_id
            if bucket:
                payload["bucket"] = bucket
            if segment is not None:
                payload["segment"] = segment
            repo.enqueue(
                "download_file",
                payload,
                queue_name="io",
                dedup_key=f"download_file:{batch_id or 'default'}:{id_archivo}",
                priority=2,
            )

    logger.info(
        "process_contract_done",
        extra={"id_contrato": id_contrato, "detail_changed": detail_changed, "files_changed": files_changed,
               **({"reading_review": selection.manual_review["reason"]} if selection.manual_review else {})},
    )
    result: dict[str, Any] = {"detail_changed": detail_changed, "files_changed": files_changed}
    if selection.manual_review is not None:
        result["manual_review"] = selection.manual_review
    if selection.fallback is not None:
        result["reading_fallback"] = str(selection.fallback.get("nombre") or "")
    return result


def _contract_object(repo: JobRepository, id_contrato: int) -> int | None:
    row = repo.conn.execute(
        "SELECT objeto_codigo FROM seace_contracts WHERE id_contrato = %s", (id_contrato,)
    ).fetchone()
    try:
        return _parse_int(row["objeto_codigo"]) if row is not None else None
    except (KeyError, TypeError):
        return None
