from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from buenapro_worker.historical.outcomes import classify_outcome, parse_contract_code
from buenapro_worker.jobs.poll_search import parse_lima_datetime
from buenapro_worker.jobs.process_contract import update_contract_detail
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def _int(value: object) -> int | None:
    try:
        return int(str(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_item(detail: dict[str, Any]) -> dict[str, Any]:
    items = detail.get("uitContratoItemProjectionList") or []
    return items[0] if items else {}


def _segment_from_cubso(value: object, fallback: int | str | None) -> str | None:
    raw = _text(value)
    if raw:
        digits = "".join(character for character in raw if character.isdigit())
        if len(digits) >= 2:
            return digits[:2]
    return _text(fallback)


def _refresh_supplier_totals(repo: JobRepository, ruc: str | None) -> None:
    if not ruc:
        return
    repo.conn.execute(
        """
        UPDATE historical_suppliers supplier
        SET total_awards = aggregate.awards,
            total_awarded_amount = aggregate.amount,
            updated_at = now()
        FROM (
          SELECT count(*)::int AS awards, COALESCE(sum(precio_total), 0) AS amount
          FROM historical_contract_outcomes
          WHERE supplier_ruc = %s AND estado_resultado = 'ADJUDICADO'
        ) aggregate
        WHERE supplier.ruc = %s
        """,
        (ruc, ruc),
    )


def upsert_historical_outcome(
    repo: JobRepository,
    *,
    detail: dict[str, Any],
    search_item: dict[str, Any] | None = None,
    segment: int | str | None = None,
    source_document_url: str | None = None,
) -> str:
    search_item = search_item or {}
    projection = detail.get("uitContratoCompletoProjection") or {}
    item = _first_item(detail)
    id_contrato = _int(projection.get("idContrato") or search_item.get("idContrato"))
    if id_contrato is None:
        raise ValueError("SEACE historical detail has no idContrato")

    code = _text(
        projection.get("desContratacion")
        or projection.get("nroContratacion")
        or search_item.get("desContratacion")
    ) or str(id_contrato)
    parsed_code = parse_contract_code(code)
    outcome = classify_outcome(detail)
    entity_id = _int(projection.get("idEntidad") or search_item.get("idEntidad"))
    entity_name = _text(projection.get("nomEntidad") or search_item.get("nomEntidad"))
    sigla_id = _int(projection.get("idSigla"))
    sigla_name = _text(projection.get("nomSigla") or parsed_code["codigo_sigla"])
    area_id = _int(projection.get("idAreaUsuaria"))
    area_name = _text(projection.get("nomAreaUsuaria"))

    if entity_id is not None:
        repo.conn.execute(
            """
            INSERT INTO historical_entities (seace_entity_id, name, sigla_id, sigla_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (seace_entity_id) DO UPDATE SET
              name = EXCLUDED.name,
              sigla_id = COALESCE(EXCLUDED.sigla_id, historical_entities.sigla_id),
              sigla_name = COALESCE(EXCLUDED.sigla_name, historical_entities.sigla_name),
              updated_at = now()
            """,
            (entity_id, entity_name or f"Entidad {entity_id}", sigla_id, sigla_name),
        )
    if area_id is not None:
        repo.conn.execute(
            """
            INSERT INTO historical_user_areas (seace_area_id, seace_entity_id, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (seace_area_id) DO UPDATE SET
              seace_entity_id = COALESCE(EXCLUDED.seace_entity_id, historical_user_areas.seace_entity_id),
              name = EXCLUDED.name,
              updated_at = now()
            """,
            (area_id, entity_id, area_name or f"Área {area_id}"),
        )
    if outcome.supplier_ruc:
        repo.conn.execute(
            """
            INSERT INTO historical_suppliers (ruc, razon_social)
            VALUES (%s, %s)
            ON CONFLICT (ruc) DO UPDATE SET
              razon_social = EXCLUDED.razon_social,
              last_seen_at = now(),
              updated_at = now()
            """,
            (outcome.supplier_ruc, outcome.supplier_name or outcome.supplier_ruc),
        )

    description = _text(
        projection.get("desObjetoContrato")
        or search_item.get("desObjetoContrato")
        or item.get("desItem")
    ) or "Sin descripción"
    cubso_item = _text(item.get("codCubso") or item.get("codigoCubso"))
    previous_supplier = repo.conn.execute(
        "SELECT supplier_ruc FROM historical_contract_outcomes WHERE id_contrato = %s",
        (id_contrato,),
    ).fetchone()
    repo.conn.execute(
        """
        INSERT INTO historical_contract_outcomes (
          id_contrato, codigo_completo, codigo_tipo, codigo_correlativo, codigo_anio,
          codigo_sigla, seace_entity_id, seace_area_id, entity_name, area_name,
          cubso_segmento, cubso_item, cubso_name, descripcion, fec_publica,
          fec_ini_cotizacion, fec_fin_cotizacion, estado_resultado, supplier_ruc,
          supplier_name, precio_total, source_document_url, raw_detail_json
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
        )
        ON CONFLICT (id_contrato) DO UPDATE SET
          codigo_completo=EXCLUDED.codigo_completo, codigo_tipo=EXCLUDED.codigo_tipo,
          codigo_correlativo=EXCLUDED.codigo_correlativo, codigo_anio=EXCLUDED.codigo_anio,
          codigo_sigla=EXCLUDED.codigo_sigla, seace_entity_id=EXCLUDED.seace_entity_id,
          seace_area_id=EXCLUDED.seace_area_id, entity_name=EXCLUDED.entity_name,
          area_name=EXCLUDED.area_name, cubso_segmento=EXCLUDED.cubso_segmento,
          cubso_item=EXCLUDED.cubso_item, cubso_name=EXCLUDED.cubso_name,
          descripcion=EXCLUDED.descripcion, fec_publica=EXCLUDED.fec_publica,
          fec_ini_cotizacion=EXCLUDED.fec_ini_cotizacion,
          fec_fin_cotizacion=EXCLUDED.fec_fin_cotizacion,
          estado_resultado=EXCLUDED.estado_resultado, supplier_ruc=EXCLUDED.supplier_ruc,
          supplier_name=EXCLUDED.supplier_name, precio_total=EXCLUDED.precio_total,
          source_document_url=COALESCE(EXCLUDED.source_document_url, historical_contract_outcomes.source_document_url),
          raw_detail_json=EXCLUDED.raw_detail_json, last_checked_at=now(), updated_at=now()
        """,
        (
            id_contrato,
            parsed_code["codigo_completo"],
            parsed_code["codigo_tipo"],
            parsed_code["codigo_correlativo"],
            parsed_code["codigo_anio"],
            parsed_code["codigo_sigla"],
            entity_id,
            area_id,
            entity_name,
            area_name,
            _segment_from_cubso(cubso_item, segment),
            cubso_item,
            _text(item.get("nomCubso") or item.get("desCubso")),
            description,
            parse_lima_datetime(projection.get("fecPublica") or search_item.get("fecPublica")),
            parse_lima_datetime(projection.get("fecIniCotizacion") or search_item.get("fecIniCotizacion")),
            parse_lima_datetime(projection.get("fecFinCotizacion") or search_item.get("fecFinCotizacion")),
            outcome.state,
            outcome.supplier_ruc,
            outcome.supplier_name,
            outcome.price_total,
            source_document_url,
            json.dumps(detail, ensure_ascii=False),
        ),
    )
    _refresh_supplier_totals(repo, outcome.supplier_ruc)
    if previous_supplier and previous_supplier["supplier_ruc"] != outcome.supplier_ruc:
        _refresh_supplier_totals(repo, previous_supplier["supplier_ruc"])
    return outcome.state


def backfill_historical_outcomes(
    settings: Settings,
    repo: JobRepository,
    *,
    segment: int,
    limit: int | None = None,
    year: int | None = None,
    page_size: int = 50,
    resume: bool = True,
    include_files: bool = True,
) -> dict[str, int]:
    page_size = max(1, min(100, page_size))
    checkpoint = repo.conn.execute(
        "SELECT * FROM historical_backfill_progress WHERE cubso_segmento=%s",
        (str(segment),),
    ).fetchone()
    repo.conn.commit()
    if resume and checkpoint:
        page_size = int(checkpoint["page_size"])
    elif limit is not None:
        page_size = min(page_size, limit)
    page = int(checkpoint["next_page"]) if resume and checkpoint else 1
    stats = {
        "seen": int(checkpoint["processed"]) if resume and checkpoint else 0,
        "saved": int(checkpoint["saved"]) if resume and checkpoint else 0,
        "failed": int(checkpoint["failed"]) if resume and checkpoint else 0,
        "adjudicated": 0,
        "deserted": 0,
        "without_result": 0,
        "total": int(checkpoint["total_elements"]) if resume and checkpoint else 0,
        "next_page": page,
    }
    failed_ids = list(checkpoint["failed_ids"] or []) if resume and checkpoint else []
    saved_at_start = stats["saved"]
    finished_all = False
    repo.conn.execute(
        """
        INSERT INTO historical_backfill_progress (
          cubso_segmento, status, next_page, page_size, processed, saved, failed,
          failed_ids, started_at, heartbeat_at, completed_at, last_error
        ) VALUES (%s, 'running', %s, %s, %s, %s, %s, %s::jsonb, now(), now(), NULL, NULL)
        ON CONFLICT (cubso_segmento) DO UPDATE SET
          status='running', next_page=EXCLUDED.next_page, page_size=EXCLUDED.page_size,
          processed=EXCLUDED.processed, saved=EXCLUDED.saved, failed=EXCLUDED.failed,
          failed_ids=EXCLUDED.failed_ids,
          started_at=CASE WHEN %s THEN historical_backfill_progress.started_at ELSE now() END,
          heartbeat_at=now(), completed_at=NULL, last_error=NULL, updated_at=now()
        """,
        (
            str(segment), page, page_size, stats["seen"], stats["saved"], stats["failed"],
            json.dumps(failed_ids), resume,
        ),
    )
    repo.conn.commit()
    with SeaceClient(settings) as client:
        while limit is None or stats["saved"] - saved_at_start < limit:
            try:
                response = client.search_contracts(
                    anio=year or datetime.now(timezone.utc).year,
                    estado=4,
                    objeto=2,
                    segmento=segment,
                    page=page,
                    page_size=page_size,
                )
            except Exception as exc:
                repo.conn.execute(
                    """
                    UPDATE historical_backfill_progress
                    SET status='failed', last_error=%s, heartbeat_at=now(), updated_at=now()
                    WHERE cubso_segmento=%s
                    """,
                    (str(exc)[:2000], str(segment)),
                )
                repo.conn.commit()
                raise
            if not response.data:
                finished_all = True
                break
            stats["total"] = response.pageable.total_elements
            for search in response.data:
                stats["seen"] += 1
                try:
                    detail = client.contract_detail(search.id_contrato)
                    source_document_url = None
                    if include_files:
                        try:
                            files = client.list_files(search.id_contrato, category=1)
                            file_id = _int(files[0].get("idContratoArchivo")) if files else None
                            if file_id is not None:
                                source_document_url = (
                                    f"{settings.seace_base_url}/archivo/archivos-publico/"
                                    f"descargar-archivo-contrato/{file_id}"
                                )
                        except Exception:
                            logger.warning(
                                "historical_files_unavailable",
                                extra={"id_contrato": search.id_contrato},
                            )
                    with repo.conn.transaction():
                        state = upsert_historical_outcome(
                            repo,
                            detail=detail,
                            search_item=search.model_dump(mode="json", by_alias=True),
                            segment=segment,
                            source_document_url=source_document_url,
                        )
                    stats["saved"] += 1
                    key = {"ADJUDICADO": "adjudicated", "DESIERTO": "deserted"}.get(
                        state, "without_result"
                    )
                    stats[key] += 1
                except Exception:
                    stats["failed"] += 1
                    if search.id_contrato not in failed_ids:
                        failed_ids.append(search.id_contrato)
                    logger.exception(
                        "historical_contract_failed",
                        extra={"id_contrato": search.id_contrato, "segment": segment},
                    )
            stats["next_page"] = page + 1
            repo.conn.execute(
                """
                UPDATE historical_backfill_progress SET
                  status='running', next_page=%s, page_size=%s, total_elements=%s,
                  processed=%s, saved=%s, failed=%s, failed_ids=%s::jsonb,
                  heartbeat_at=now(), last_error=NULL, updated_at=now()
                WHERE cubso_segmento=%s
                """,
                (
                    stats["next_page"], page_size, stats["total"], stats["seen"],
                    stats["saved"], stats["failed"], json.dumps(failed_ids), str(segment),
                ),
            )
            repo.conn.commit()
            logger.info(
                "historical_backfill_progress",
                extra=stats | {"segment": segment, "page": page},
            )
            total_pages = (response.pageable.total_elements + response.pageable.page_size - 1) // response.pageable.page_size
            if page >= total_pages:
                finished_all = True
                break
            page += 1
    status = (
        "completed_with_errors" if stats["failed"] else "completed"
    ) if finished_all else "running"
    repo.conn.execute(
        """
        UPDATE historical_backfill_progress
        SET status=%s, processed=%s, saved=%s, failed=%s, failed_ids=%s::jsonb,
            heartbeat_at=now(), completed_at=CASE WHEN %s THEN now() ELSE NULL END,
            updated_at=now()
        WHERE cubso_segmento=%s
        """,
        (
            status, stats["seen"], stats["saved"], stats["failed"],
            json.dumps(failed_ids), finished_all, str(segment),
        ),
    )
    repo.conn.commit()
    logger.info("historical_backfill_done", extra=stats | {"segment": segment})
    return stats


def retry_failed_historical_outcomes(
    settings: Settings,
    repo: JobRepository,
    *,
    segment: int,
    year: int | None = None,
    include_files: bool = True,
) -> dict[str, int]:
    checkpoint = repo.conn.execute(
        "SELECT failed_ids FROM historical_backfill_progress WHERE cubso_segmento=%s",
        (str(segment),),
    ).fetchone()
    pending = {int(value) for value in (checkpoint["failed_ids"] if checkpoint else [])}
    stored = {
        int(row["id_contrato"])
        for row in repo.conn.execute(
            "SELECT id_contrato FROM historical_contract_outcomes WHERE cubso_segmento=%s",
            (str(segment),),
        ).fetchall()
    }
    repo.conn.commit()
    stats = {
        "scanned": 0,
        "missing": 0,
        "recovered": 0,
        "remaining": len(pending),
    }

    page = 1
    with SeaceClient(settings) as client:
        while True:
            response = client.search_contracts(
                anio=year or datetime.now(timezone.utc).year,
                estado=4,
                objeto=2,
                segmento=segment,
                page=page,
                page_size=100,
            )
            if not response.data:
                break
            for search in response.data:
                stats["scanned"] += 1
                if search.id_contrato in stored and search.id_contrato not in pending:
                    continue
                stats["missing"] += 1
                try:
                    detail = client.contract_detail(search.id_contrato)
                    source_document_url = None
                    if include_files:
                        files = client.list_files(search.id_contrato, category=1)
                        file_id = _int(files[0].get("idContratoArchivo")) if files else None
                        if file_id is not None:
                            source_document_url = (
                                f"{settings.seace_base_url}/archivo/archivos-publico/"
                                f"descargar-archivo-contrato/{file_id}"
                            )
                    with repo.conn.transaction():
                        upsert_historical_outcome(
                            repo,
                            detail=detail,
                            search_item=search.model_dump(mode="json", by_alias=True),
                            segment=segment,
                            source_document_url=source_document_url,
                        )
                    pending.discard(search.id_contrato)
                    stored.add(search.id_contrato)
                    stats["recovered"] += 1
                except Exception:
                    pending.add(search.id_contrato)
                    logger.exception(
                        "historical_retry_failed",
                        extra={"id_contrato": search.id_contrato, "segment": segment},
                    )
            total_pages = (
                response.pageable.total_elements + response.pageable.page_size - 1
            ) // response.pageable.page_size
            if page >= total_pages:
                break
            page += 1

    stats["remaining"] = len(pending)
    repo.conn.execute(
        """
        UPDATE historical_backfill_progress
        SET saved=(SELECT count(*) FROM historical_contract_outcomes WHERE cubso_segmento=%s),
            failed=%s, failed_ids=%s::jsonb,
            status=CASE WHEN %s = 0 AND completed_at IS NOT NULL THEN 'completed' ELSE status END,
            heartbeat_at=now(), updated_at=now()
        WHERE cubso_segmento=%s
        """,
        (str(segment), len(pending), json.dumps(sorted(pending)), len(pending), str(segment)),
    )
    repo.conn.commit()
    logger.info("historical_retry_done", extra=stats | {"segment": segment})
    return stats


def refresh_recent_closures(
    settings: Settings,
    repo: JobRepository,
    *,
    days: int = 15,
    limit: int = 300,
) -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = repo.conn.execute(
        """
        SELECT id_contrato, cubso_segmento
        FROM seace_contracts
        WHERE fec_fin_cotizacion BETWEEN %s AND now()
        ORDER BY fec_fin_cotizacion DESC
        LIMIT %s
        """,
        (since, limit),
    ).fetchall()
    stats = {"checked": 0, "updated": 0, "historical_saved": 0}
    with SeaceClient(settings) as client:
        for row in rows:
            detail = client.contract_detail(int(row["id_contrato"]))
            stats["checked"] += 1
            if update_contract_detail(repo, int(row["id_contrato"]), detail):
                stats["updated"] += 1
            projection = detail.get("uitContratoCompletoProjection") or {}
            state = _int(projection.get("idEstadoContrato"))
            outcome = classify_outcome(detail)
            if state == 4 or outcome.state != "SIN_RESULTADO":
                upsert_historical_outcome(repo, detail=detail, segment=row["cubso_segmento"])
                stats["historical_saved"] += 1
    logger.info("recent_closures_done", extra=stats | {"days": days})
    return stats


def historical_stats(repo: JobRepository, *, segment: int | str) -> dict[str, object]:
    row = repo.conn.execute(
        """
        SELECT count(*)::int AS total,
          count(*) FILTER (WHERE estado_resultado='ADJUDICADO')::int AS adjudicated,
          count(*) FILTER (WHERE estado_resultado='DESIERTO')::int AS deserted,
          count(precio_total)::int AS with_price,
          min(precio_total) AS price_min,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY precio_total) AS price_median,
          max(precio_total) AS price_max
        FROM historical_contract_outcomes WHERE cubso_segmento=%s
        """,
        (str(segment),),
    ).fetchone()
    return dict(row or {})
