from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from buenapro_worker.jobs.poll_search import canonical_hash, parse_lima_datetime
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
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
        parse_lima_datetime(quotation.get("fecIni")),
        parse_lima_datetime(quotation.get("fecFin")),
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
            fec_ini_cotizacion = COALESCE(%s, fec_ini_cotizacion),
            fec_fin_cotizacion = COALESCE(%s, fec_fin_cotizacion),
            cronograma = %s::jsonb,
            resultado = COALESCE(NULLIF(%s::jsonb, '{}'::jsonb), resultado),
            hash_detail = %s,
            raw_detail_json = %s::jsonb,
            detail_fetched_at = now(),
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
            "UPDATE seace_contracts SET detail_fetched_at = now() WHERE id_contrato = %s",
            (id_contrato,),
        )
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
    return "otro"


def upsert_files(repo: JobRepository, settings: Settings, id_contrato: int, files: list[dict[str, Any]]) -> bool:
    hash_files = canonical_hash(files)
    previous = repo.conn.execute(
        "SELECT hash_files FROM seace_contracts WHERE id_contrato = %s",
        (id_contrato,),
    ).fetchone()
    changed = previous is None or previous["hash_files"] != hash_files

    for file_item in files:
        id_archivo = file_item.get("idContratoArchivo")
        if id_archivo is None:
            continue
        categoria = int(file_item.get("_categoria") or 1)
        doc_class = None if categoria == 1 else _attachment_class(file_item)
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
              mime = EXCLUDED.mime,
              size_original_bytes = EXCLUDED.size_original_bytes,
              seace_download_url = EXCLUDED.seace_download_url,
              doc_class = CASE
                WHEN EXCLUDED.doc_class = 'cotizacion' THEN 'cotizacion'
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
) -> dict[str, bool]:
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
        except Exception:
            attachments = []

    for file_item in primary_files:
        file_item["_categoria"] = 1
    for file_item in attachments:
        file_item["_categoria"] = 2

    detail_changed = update_contract_detail(repo, id_contrato, detail) if detail is not None else False
    files_changed = upsert_files(repo, settings, id_contrato, primary_files + attachments)

    if files_changed:
        # Solo los archivos de categoria 1 (TDR/requerimiento) se descargan y
        # pasan a extraccion; los anexos quedan listados con descarga directa.
        for file_item in primary_files:
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
        extra={"id_contrato": id_contrato, "detail_changed": detail_changed, "files_changed": files_changed},
    )
    return {"detail_changed": detail_changed, "files_changed": files_changed}
