from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.schemas import SearchItem
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)
LIMA_TZ = ZoneInfo("America/Lima")


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_lima_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.strptime(value, "%d/%m/%Y %H:%M:%S")
    return parsed.replace(tzinfo=LIMA_TZ).astimezone(timezone.utc)


def is_in_scope(item: SearchItem, settings: Settings, segment: int | None) -> bool:
    if item.estado_codigo not in settings.allowed_estado_contrato:
        return False
    if item.objeto_codigo not in settings.allowed_codigo_objeto:
        return False
    if segment is not None and segment not in settings.allowed_segments:
        return False
    return True


def upsert_search_item(repo: JobRepository, item: SearchItem, *, anio: int, segment: int | None) -> bool:
    raw = item.model_dump(mode="json", by_alias=True)
    hash_search = canonical_hash(raw)
    publication = parse_lima_datetime(item.fec_publica)
    actual_year = publication.astimezone(LIMA_TZ).year if publication else anio
    row = repo.conn.execute(
        """
        INSERT INTO seace_contracts (
          id_contrato,
          codigo,
          anio,
          entidad_nombre,
          objeto_codigo,
          estado_codigo,
          descripcion,
          cubso_segmento,
          cotizar,
          fec_publica,
          fec_ini_cotizacion,
          fec_fin_cotizacion,
          hash_search,
          raw_search_json,
          first_seen_at,
          last_seen_at,
          updated_at
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
          now(), now(), now()
        )
        ON CONFLICT (id_contrato)
        DO UPDATE SET
          codigo = EXCLUDED.codigo,
          anio = EXCLUDED.anio,
          entidad_nombre = EXCLUDED.entidad_nombre,
          objeto_codigo = EXCLUDED.objeto_codigo,
          estado_codigo = EXCLUDED.estado_codigo,
          descripcion = EXCLUDED.descripcion,
          cubso_segmento = COALESCE(EXCLUDED.cubso_segmento, seace_contracts.cubso_segmento),
          cotizar = EXCLUDED.cotizar,
          fec_publica = EXCLUDED.fec_publica,
          fec_ini_cotizacion = EXCLUDED.fec_ini_cotizacion,
          fec_fin_cotizacion = EXCLUDED.fec_fin_cotizacion,
          hash_search = EXCLUDED.hash_search,
          raw_search_json = EXCLUDED.raw_search_json,
          last_seen_at = now(),
          updated_at = now()
        WHERE seace_contracts.hash_search IS DISTINCT FROM EXCLUDED.hash_search
        RETURNING (xmax = 0) AS inserted
        """,
        (
            item.id_contrato,
            item.codigo,
            actual_year,
            item.entidad_nombre,
            item.objeto_codigo,
            item.estado_codigo,
            item.descripcion,
            str(segment) if segment is not None else None,
            item.cotizar,
            publication,
            parse_lima_datetime(item.fec_ini_cotizacion),
            parse_lima_datetime(item.fec_fin_cotizacion),
            hash_search,
            json.dumps(raw, ensure_ascii=False),
        ),
    ).fetchone()
    return row is not None


def poll_search(
    settings: Settings,
    repo: JobRepository,
    *,
    anio: int,
    max_contracts: int | None = None,
    segments: list[int] | None = None,
    objects: list[int] | None = None,
    batch_id: str | None = None,
    bucket: str | None = None,
) -> dict[str, int]:
    stats = {"seen": 0, "changed": 0, "enqueued": 0, "skipped": 0, "limit_reached": 0}
    segment_list: list[int | None] = segments or settings.allowed_segments or [None]
    object_list = objects or settings.allowed_codigo_objeto
    if not object_list or any(obj not in settings.allowed_codigo_objeto for obj in object_list):
        raise ValueError("Objects must be a nonempty subset of SEACE_ALLOWED_CODIGO_OBJETO")
    if any(segment is not None and segment not in settings.allowed_segments for segment in segment_list):
        raise ValueError("Segments must be configured in SEACE_ALLOWED_SEGMENTS")

    def finish(event: str) -> dict[str, int]:
        logger.info(
            event,
            extra=stats | {"anio": anio, "max_contracts": max_contracts, "batch_id": batch_id, "bucket": bucket},
        )
        if event != "poll_search_done":
            logger.info(
                "poll_search_done",
                extra=stats | {"anio": anio, "max_contracts": max_contracts, "batch_id": batch_id, "bucket": bucket},
            )
        return stats

    with SeaceClient(settings) as client:
        for objeto in object_list:
            for segment in segment_list:
                segment_stats = _poll_object_segment(
                    client, settings, repo, stats, anio=anio, objeto=objeto,
                    segment=segment, max_contracts=max_contracts,
                    batch_id=batch_id, bucket=bucket,
                )
                if segment_stats == "limit_reached":
                    return finish("poll_search_limit_reached")

    return finish("poll_search_done")


def _poll_object_segment(
    client: SeaceClient,
    settings: Settings,
    repo: JobRepository,
    stats: dict[str, int],
    *,
    anio: int,
    objeto: int,
    segment: int | None,
    max_contracts: int | None,
    batch_id: str | None,
    bucket: str | None,
) -> str:
    page = 1
    while True:
        if max_contracts is not None and stats["changed"] >= max_contracts:
            stats["limit_reached"] = 1
            return "limit_reached"

        response = client.search_contracts(
            anio=anio,
            estado=settings.primary_estado_contrato,
            objeto=objeto,
            segmento=segment,
            page=page,
            page_size=100,
        )

        if not response.data:
            break

        page_changed = False
        for item in response.data:
            if max_contracts is not None and stats["changed"] >= max_contracts:
                stats["limit_reached"] = 1
                return "limit_reached"

            stats["seen"] += 1
            if not is_in_scope(item, settings, segment) or item.objeto_codigo != objeto:
                stats["skipped"] += 1
                logger.info(
                    "contract_out_of_mvp_scope",
                    extra={
                        "id_contrato": item.id_contrato,
                        "estado": item.estado_codigo,
                        "objeto": item.objeto_codigo,
                        "segment": segment,
                    },
                )
                continue

            changed = upsert_search_item(repo, item, anio=anio, segment=segment)
            if changed:
                page_changed = True
                stats["changed"] += 1
                payload: dict[str, object] = {"id_contrato": item.id_contrato}
                if batch_id:
                    payload["batch_id"] = batch_id
                if bucket:
                    payload["bucket"] = bucket
                if segment is not None:
                    payload["segment"] = segment
                job_id = repo.enqueue(
                    "process_contract",
                    payload,
                    queue_name="io",
                    dedup_key=f"process_contract:{batch_id or 'default'}:{item.id_contrato}",
                    priority=2,
                )
                if job_id is not None:
                    stats["enqueued"] += 1

        total_pages = (response.pageable.total_elements + response.pageable.page_size - 1) // response.pageable.page_size
        if not page_changed or page >= total_pages:
            break
        page += 1
    return "done"
