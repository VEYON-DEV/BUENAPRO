from __future__ import annotations

import json
import logging
import re
import time as time_module
import unicodedata
from collections import defaultdict
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from buenapro_worker.prod4.client import Prod4Client
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)
LIMA = ZoneInfo("America/Lima")
PROD4_PORTAL = "https://prod4.seace.gob.pe/openegocio/#/georeferenciacion/"
DOCUMENT_URL = "https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode={}"
OBJECT_CODES = {62: "good", 65: "service"}


def _id(row: dict[str, Any]) -> int | None:
    try:
        value = int(row.get("idProcedimiento"))
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def _date(value: Any, *, hour: str | None = None, end_of_day: bool = False) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if hour and len(raw) == 10:
        raw = f"{raw} {hour}"
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%d/%m/%Y" and end_of_day:
                parsed = datetime.combine(parsed.date(), time(23, 59))
            return parsed.replace(tzinfo=LIMA)
        except ValueError:
            continue
    return None


def _money(value: Any) -> Decimal | None:
    if value is None or str(value).strip() in ("", "---", "-", "0.00 reservado"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
        return amount if amount >= 0 else None
    except InvalidOperation:
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _slug(value: Any) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in ascii_text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def select_technology_processes(
    goods_rows: list[dict[str, Any]],
    services_rows: list[dict[str, Any]],
    segment_rows: dict[int, list[dict[str, Any]]],
    service_prefixes: list[str],
    technology_terms: list[str],
) -> dict[int, tuple[str, list[dict[str, Any]], list[str]]]:
    """Use native object listings for type, segment rows only for tech relevance.

    The segment endpoint's `codObjeto` is often an item identifier, not 62/65.
    The 81 segment contains engineering and works consulting, so an explicit
    versioned CUBSO family allowlist is required for services.
    """
    rows_by_id: dict[int, tuple[str, list[dict[str, Any]]]] = {}
    for code, rows in ((62, goods_rows), (65, services_rows)):
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            procedure_id = _id(row)
            if procedure_id and str(row.get("codObjeto")) == str(code):
                grouped[procedure_id].append(row)
        for procedure_id, items in grouped.items():
            # A procedure must not silently change its native object category.
            if procedure_id in rows_by_id:
                raise ValueError(f"PROD4 procedure {procedure_id} appeared as both goods and services")
            rows_by_id[procedure_id] = (OBJECT_CODES[code], items)

    reasons: dict[int, set[str]] = defaultdict(set)
    normalized_terms = [_slug(term) for term in technology_terms]
    for segment, rows in segment_rows.items():
        for row in rows:
            procedure_id = _id(row)
            if procedure_id not in rows_by_id:
                continue
            object_type = rows_by_id[procedure_id][0]
            cubso = str(row.get("codCubso") or "").strip()
            if segment == 43 and object_type == "good" and cubso.startswith("43"):
                reasons[procedure_id].add("cubso:43")
            elif segment == 81 and object_type == "service":
                match = next((prefix for prefix in service_prefixes if cubso.startswith(prefix)), None)
                listing_text = _slug(" ".join(
                    str(part or "")
                    for item in rows_by_id[procedure_id][1]
                    for part in (item.get("sintesisProceso"), item.get("detItem"))
                ))
                if match and any(term in listing_text for term in normalized_terms):
                    reasons[procedure_id].add(f"cubso:{match}")
                    reasons[procedure_id].add("text:technology")

    return {
        procedure_id: (rows_by_id[procedure_id][0], rows_by_id[procedure_id][1], sorted(match_reasons))
        for procedure_id, match_reasons in reasons.items()
    }


def _existing(repo: JobRepository, procedure_id: int) -> dict[str, Any] | None:
    return repo.conn.execute(
        "SELECT opportunity_id, raw_listing, raw_detail, detail_fetched_at FROM prod4_processes WHERE id_procedimiento = %s",
        (procedure_id,),
    ).fetchone()


def _ensure_identity(repo: JobRepository, procedure_id: int, object_type: str) -> UUID:
    external_id = str(procedure_id)
    repo.conn.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod4'), hashtext(%s))", (external_id,))
    source = repo.conn.execute(
        """SELECT opportunity_id FROM opportunity_sources
           WHERE source_key = 'seace_prod4' AND id_kind = 'id_procedimiento' AND external_id = %s""",
        (external_id,),
    ).fetchone()
    if source:
        opportunity_id = source["opportunity_id"]
        repo.conn.execute(
            """UPDATE opportunities SET object_type = %s,
                 lifecycle_stage = CASE WHEN lifecycle_stage IN ('awarded', 'contracted', 'closed')
                   THEN lifecycle_stage ELSE 'open' END,
                 updated_at = now()
               WHERE id = %s AND (object_type IS DISTINCT FROM %s
                 OR lifecycle_stage NOT IN ('open', 'awarded', 'contracted', 'closed'))""",
            (object_type, opportunity_id, object_type),
        )
        return opportunity_id

    row = repo.conn.execute(
        """INSERT INTO opportunities
           (record_kind, object_type, procurement_method, lifecycle_stage,
            participation_access, actionability)
           VALUES ('opportunity', %s, 'selection_procedure', 'open',
                   'registration_required', 'unknown') RETURNING id""",
        (object_type,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed to allocate PROD4 canonical identity")
    opportunity_id = row["id"]
    repo.conn.execute(
        """INSERT INTO opportunity_sources
           (opportunity_id, source_key, id_kind, external_id, native_values)
           VALUES (%s, 'seace_prod4', 'id_procedimiento', %s, %s::jsonb)""",
        (opportunity_id, external_id, _json({"codObjeto": 62 if object_type == "good" else 65})),
    )
    return opportunity_id


def _upsert_listing(
    repo: JobRepository,
    procedure_id: int,
    object_type: str,
    rows: list[dict[str, Any]],
    reasons: list[str],
    scan_at: datetime,
) -> bool:
    from buenapro_worker.jobs.refresh_schedule import schedule_instant
    rows = sorted(rows, key=lambda row: (str(row.get("nroItem") or ""), _json(row)))
    first = rows[0]
    existing = _existing(repo, procedure_id)
    listing_changed = existing is None or existing["raw_listing"] != rows
    # Documents and dates can change without a listing-text change. Revisit
    # each current ficha at most daily to discover updated bases.
    detail_stale = (
        existing is not None
        and (
            existing["detail_fetched_at"] is None
            or (scan_at - existing["detail_fetched_at"]).total_seconds() >= 24 * 3600
        )
    )
    needs_detail = listing_changed or existing["raw_detail"] is None or detail_stale
    opportunity_id = _ensure_identity(repo, procedure_id, object_type)
    repo.conn.execute(
        """INSERT INTO prod4_processes (
             id_procedimiento, opportunity_id, nomenclatura, title, description,
             object_type, procedure_type, buyer_name, published_at,
             registration_closes_at, proposals_start_at, reference_amount,
             currency, source_url, technology_relevant, technology_match_reason,
             last_seen_at, missing_since, raw_listing, updated_at
           ) VALUES (
             %s, %s, %s, %s, %s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s, true, %s::jsonb,
             %s, NULL, %s::jsonb, now()
           ) ON CONFLICT (id_procedimiento) DO UPDATE SET
             nomenclatura = EXCLUDED.nomenclatura,
             title = CASE WHEN prod4_processes.raw_detail IS NULL
               THEN EXCLUDED.title ELSE prod4_processes.title END,
             description = CASE WHEN prod4_processes.raw_detail IS NULL
               THEN EXCLUDED.description ELSE prod4_processes.description END,
             object_type = EXCLUDED.object_type,
             procedure_type = EXCLUDED.procedure_type,
             buyer_name = EXCLUDED.buyer_name,
             published_at = EXCLUDED.published_at,
             registration_closes_at = CASE WHEN prod4_processes.raw_detail IS NULL
               THEN EXCLUDED.registration_closes_at ELSE prod4_processes.registration_closes_at END,
             proposals_start_at = CASE WHEN prod4_processes.raw_detail IS NULL
               THEN EXCLUDED.proposals_start_at ELSE prod4_processes.proposals_start_at END,
             reference_amount = COALESCE(EXCLUDED.reference_amount, prod4_processes.reference_amount),
             currency = EXCLUDED.currency,
             technology_relevant = true,
             technology_match_reason = EXCLUDED.technology_match_reason,
             last_seen_at = EXCLUDED.last_seen_at,
             missing_since = NULL,
             raw_listing = EXCLUDED.raw_listing,
             updated_at = CASE
               WHEN prod4_processes.raw_listing IS DISTINCT FROM EXCLUDED.raw_listing
                 OR prod4_processes.missing_since IS NOT NULL
               THEN now() ELSE prod4_processes.updated_at END""",
        (
            procedure_id, opportunity_id, first.get("nomenclatura"), first.get("sintesisProceso") or first.get("nomenclatura"),
            first.get("sintesisProceso"), object_type, first.get("detTipoProceso"),
            first.get("detEntidad"), _date(first.get("fechaConvocatoria")),
            schedule_instant(first.get("fecFinParticipantes") or first.get("fechaFin")),
            schedule_instant(first.get("fechaPresentacionPropuestas")), _money(first.get("valorReferencial")),
            "PEN" if first.get("monedaProceso") == "Soles" else first.get("monedaProceso"),
            PROD4_PORTAL, _json(reasons), scan_at, _json(rows),
        ),
    )
    if not listing_changed:
        return needs_detail

    repo.conn.execute("DELETE FROM prod4_items WHERE id_procedimiento = %s", (procedure_id,))
    for index, item in enumerate(rows, start=1):
        try:
            nro_item = int(item.get("nroItem"))
            if nro_item < 1:
                raise ValueError
        except (ValueError, TypeError):
            nro_item = index
        repo.conn.execute(
            """INSERT INTO prod4_items
               (id_procedimiento, nro_item, cubso_code, description, quantity, unit, raw_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (id_procedimiento, nro_item) DO UPDATE SET
                 cubso_code = EXCLUDED.cubso_code,
                 description = EXCLUDED.description,
                 quantity = EXCLUDED.quantity,
                 unit = EXCLUDED.unit,
                 raw_json = EXCLUDED.raw_json""",
            (
                procedure_id, nro_item, item.get("codCubso"), item.get("detItem"),
                _money(item.get("cantItem")), item.get("detUnidadMedida"), _json(item),
            ),
        )
    return needs_detail


def _upsert_detail(repo: JobRepository, procedure_id: int, detail: dict[str, Any]) -> None:
    from buenapro_worker.jobs.refresh_schedule import schedule_instant
    previous_analysis = repo.conn.execute(
        """SELECT opportunity_id, analyzed_document_code
           FROM prod4_processes WHERE id_procedimiento = %s""",
        (procedure_id,),
    ).fetchone()
    schedule = detail.get("listaCronograma") or []
    if not isinstance(schedule, list):
        raise TypeError("PROD4 detail schedule changed shape")
    proposal_start: datetime | None = None
    proposal_close: datetime | None = None
    registration_close: datetime | None = None
    for stage in schedule:
        if not isinstance(stage, dict):
            continue
        key = _slug(stage.get("nombreEtapa") or stage.get("descripcionEtapa"))
        if "registro" in key and "particip" in key:
            registration_close = schedule_instant(stage.get("fechaFin"), stage.get("horaFin"))
            break
    for stage in schedule:
        if not isinstance(stage, dict):
            continue
        stage_name = str(stage.get("nombreEtapa") or stage.get("descripcionEtapa") or "")
        key = _slug(stage_name)
        if "presentacion" in key and ("propuestas" in key or "ofertas" in key):
            proposal_start = schedule_instant(stage.get("fechaInicio"), stage.get("horaInicio"))
            proposal_close = schedule_instant(stage.get("fechaFin"), stage.get("horaFin"))
            break
    buyer = detail.get("entidadConvocante") or {}
    if not isinstance(buyer, dict):
        buyer = {}
    detail_items = detail.get("listaItems") or []
    region = next((item.get("departamento") for item in detail_items if isinstance(item, dict) and item.get("departamento")), None)
    repo.conn.execute(
        """UPDATE prod4_processes SET
             id_convocatoria_pub = %s, numero_procedimiento = %s,
             nomenclatura = %s, title = %s,
             description = COALESCE(%s, description),
             buyer_name = COALESCE(%s, buyer_name), buyer_id = %s,
             region = %s, published_at = COALESCE(%s, published_at),
             registration_closes_at = %s,
             proposals_start_at = %s,
             proposals_closes_at = %s, reference_amount = COALESCE(%s, reference_amount),
             raw_detail = %s::jsonb, detail_fetched_at = now(), schedule_fetched_at = now(), updated_at = now()
           WHERE id_procedimiento = %s""",
        (
            detail.get("idConvocatoriaPub"), detail.get("numeroProcedimiento"),
            detail.get("nomenclatura"),
            detail.get("descripcionObjeto") or detail.get("descripcionObjetoResumen") or detail.get("nomenclatura"),
            detail.get("descripcionObjeto") or detail.get("descripcionObjetoResumen"),
            buyer.get("nombreOrganismo"), str(buyer["idOrganismo"]) if buyer.get("idOrganismo") else None,
            region, _date(detail.get("fechaPublicacion")), registration_close, proposal_start, proposal_close,
            _money(detail.get("valorReferencial")), _json(detail), procedure_id,
        ),
    )
    repo.conn.execute("DELETE FROM prod4_schedule WHERE id_procedimiento = %s", (procedure_id,))
    for position, stage in enumerate(schedule):
        if not isinstance(stage, dict):
            continue
        name = stage.get("nombreEtapa") or stage.get("descripcionEtapa")
        repo.conn.execute(
            """INSERT INTO prod4_schedule
               (id_procedimiento, position, stage_key, stage_name, starts_at, ends_at, raw_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (
                procedure_id, position, _slug(name), name,
                schedule_instant(stage.get("fechaInicio"), stage.get("horaInicio")),
                schedule_instant(stage.get("fechaFin"), stage.get("horaFin")),
                _json(stage),
            ),
        )
    documents = detail.get("listaDocumentos") or []
    if not isinstance(documents, list):
        raise TypeError("PROD4 detail documents changed shape")
    repo.conn.execute("DELETE FROM prod4_documents WHERE id_procedimiento = %s", (procedure_id,))
    for document in documents:
        if not isinstance(document, dict):
            continue
        try:
            alfresco = UUID(str(document.get("codigoAlfresco")))
        except (TypeError, ValueError):
            continue
        repo.conn.execute(
            """INSERT INTO prod4_documents
               (id_procedimiento, codigo_alfresco, name, document_type,
                extension, published_at, source_url, raw_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (id_procedimiento, codigo_alfresco) DO NOTHING""",
            (
                procedure_id, alfresco, document.get("nombreArchivo"), document.get("tipoDocumento"),
                document.get("extension"), _date(document.get("fechaPublicacion")),
                DOCUMENT_URL.format(alfresco), _json(document),
            ),
        )
    # A new or removed bases PDF must invalidate the old final verdict even
    # when the opt-in LLM pipeline is disabled. Preliminary fit remains intact.
    if previous_analysis and previous_analysis["analyzed_document_code"] is not None:
        from buenapro_worker.jobs.prod4_documents import select_official_requirements_pdf

        current_documents = repo.conn.execute(
            """SELECT codigo_alfresco, name, document_type, extension, published_at
               FROM prod4_documents WHERE id_procedimiento = %s""",
            (procedure_id,),
        ).fetchall()
        selected = select_official_requirements_pdf([dict(row) for row in current_documents])
        selected_code = selected["codigo_alfresco"] if selected else None
        if selected_code != previous_analysis["analyzed_document_code"]:
            repo.conn.execute(
                "DELETE FROM opportunity_matches WHERE opportunity_id = %s",
                (previous_analysis["opportunity_id"],),
            )
            repo.conn.execute(
                """UPDATE prod4_document_extractions SET is_current = false
                   WHERE id_procedimiento = %s AND is_current = true""",
                (procedure_id,),
            )
            repo.conn.execute(
                """UPDATE prod4_requirement_facets SET is_current = false
                   WHERE id_procedimiento = %s AND is_current = true""",
                (procedure_id,),
            )
            repo.conn.execute(
                """UPDATE prod4_processes SET document_analysis_status = NULL,
                     document_analysis_reason = NULL, analyzed_document_code = NULL,
                     document_analysis_checked_at = NULL WHERE id_procedimiento = %s""",
                (procedure_id,),
            )
    repo.conn.execute(
        """UPDATE opportunity_sources
           SET native_values = native_values || %s::jsonb
           WHERE source_key = 'seace_prod4' AND id_kind = 'id_procedimiento'
             AND external_id = %s""",
        (_json({"idConvocatoriaPub": detail.get("idConvocatoriaPub"), "nomenclatura": detail.get("nomenclatura")}), str(procedure_id)),
    )


def poll_prod4(settings: Settings, repo: JobRepository, client: Prod4Client | None = None) -> dict[str, int]:
    """Poll one complete public snapshot; never infer an award from disappearance."""
    if not settings.prod4_enabled:
        return {"selected": 0, "details": 0, "missing": 0}
    if settings.prod4_detail_limit < 0 or settings.prod4_document_enqueue_limit_per_poll < 0:
        raise ValueError("PROD4 detail and document limits cannot be negative")

    def run(source: Prod4Client) -> dict[str, int]:
        # Fetch every configured slice before changing snapshot presence state.
        goods = source.by_object(62)
        services = source.by_object(65)
        segments = {segment: source.by_segment(segment) for segment in settings.prod4_segments}
        if not goods or not services or not any(segments.values()):
            raise ValueError("PROD4 returned an unexpectedly empty source slice; snapshot was not applied")
        selected = select_technology_processes(goods, services, segments, settings.prod4_service_prefixes, settings.prod4_terms)
        previous_count = repo.conn.execute(
            "SELECT count(*) AS total FROM prod4_processes WHERE missing_since IS NULL"
        ).fetchone()["total"]
        if previous_count >= 20 and len(selected) * 2 < previous_count:
            raise ValueError("PROD4 snapshot shrank by more than half; refusing to mark opportunities missing")
        scan_at = datetime.now(timezone.utc)
        changed_ids: list[int] = []
        for procedure_id in sorted(selected):
            object_type, rows, reasons = selected[procedure_id]
            if _upsert_listing(repo, procedure_id, object_type, rows, reasons, scan_at):
                changed_ids.append(procedure_id)

        # Detail refresh is bounded. PDF/LLM analysis stays opt-in and has its
        # own smaller enqueue cap to avoid a sudden historical fanout.
        details = 0
        for procedure_id in changed_ids[:settings.prod4_detail_limit]:
            try:
                detail = source.detail(procedure_id)
                # Caller holds the snapshot transaction. A nested transaction
                # creates a SAVEPOINT, so a bad ficha cannot abort the batch.
                with repo.conn.transaction():
                    _upsert_detail(repo, procedure_id, detail)
                details += 1
            except Exception:
                # One stale/temporarily unavailable ficha must not invalidate a
                # successfully fetched complete listing snapshot.
                logger.exception("prod4_detail_unavailable", extra={"id_procedimiento": procedure_id})
            time_module.sleep(0.12)

        if settings.prod4_document_analysis_enabled:
            from buenapro_worker.jobs.prod4_documents import enqueue_current_prod4_document_sweep

            # The sweep scans past already-analyzed rows. It also advances the
            # initial 72-row backfill without requiring a manual CLI command.
            enqueue_current_prod4_document_sweep(
                settings, repo, limit=settings.prod4_document_enqueue_limit_per_poll
            )

        # Because all four slices returned successfully, absence is meaningful
        # only as a departure from this source snapshot, not as an adjudication.
        missing = repo.conn.execute(
            """UPDATE prod4_processes SET missing_since = %s, updated_at = now()
               WHERE missing_since IS NULL AND last_seen_at < %s
               RETURNING opportunity_id""",
            (scan_at, scan_at),
        ).fetchall()
        if missing:
            repo.conn.execute(
                """UPDATE opportunities o SET lifecycle_stage = 'unknown',
                     actionability = 'unknown', updated_at = now()
                   FROM prod4_processes p
                   WHERE p.opportunity_id = o.id AND p.missing_since = %s
                     AND o.lifecycle_stage NOT IN ('awarded', 'contracted', 'closed')""",
                (scan_at,),
            )
        return {"selected": len(selected), "details": details, "missing": len(missing)}

    if client is not None:
        return run(client)
    with Prod4Client(settings) as source:
        return run(source)
