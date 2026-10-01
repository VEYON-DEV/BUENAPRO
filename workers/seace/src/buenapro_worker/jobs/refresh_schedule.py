"""Bounded refresh of stored technology schedules; never downloads documents."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from buenapro_worker.jobs.poll_prod4 import _slug
from buenapro_worker.jobs.poll_search import canonical_hash
from buenapro_worker.prod4.client import Prod4Client
from buenapro_worker.seace.client import SeaceClient


def schedule_instant(value: Any, hour: Any = None) -> datetime | None:
    """Only materialize a timestamp if the source supplied a clock."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if hour and len(raw) == 10:
        raw += " " + str(hour).strip()
    if not re.search(r"[ T]\d{2}:\d{2}", raw):
        return None
    parsed = None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            pass
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Lima"))
    return parsed.astimezone(timezone.utc)


def validate_schedule(detail: dict, source: str, native_id: int) -> list[dict]:
    key = "listaCronograma" if source == "prod4" else "uitContratoEtapaProjectionList"
    if not isinstance(detail, dict) or key not in detail:
        raise ValueError("Missing official schedule: refusing to clear stored dates")
    if source == "prod6":
        projection = detail.get("uitContratoCompletoProjection") or {}
        returned_id = projection.get("idContrato")
        if returned_id is not None and int(returned_id) != native_id:
            raise ValueError("PROD6 detail identity mismatch")
    stages = detail[key]
    if not isinstance(stages, list) or not all(isinstance(stage, dict) for stage in stages):
        raise ValueError("Official schedule changed shape")
    # Empty arrays may be an upstream transient failure. Keep existing data.
    if not stages:
        raise ValueError("Empty official schedule: refusing destructive refresh")
    return stages


def persist_schedule(conn: Any, source: str, native_id: int, stages: list[dict]) -> None:
    """Caller owns the per-record transaction. Locks avoid overwriting ingestion."""
    if source == "prod6":
        row = conn.execute(
            "SELECT raw_detail_json FROM seace_contracts WHERE id_contrato = %s FOR UPDATE",
            (native_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Contract disappeared during refresh")
        raw = dict(row["raw_detail_json"] or {})
        raw["uitContratoEtapaProjectionList"] = stages
        quote = next((s for s in stages if str(s.get("idEtapaContrato")) == "2"
                      or "cotiz" in _slug(s.get("nomEtapaContrato"))), {})
        conn.execute(
            """UPDATE seace_contracts SET cronograma = %s::jsonb,
                 raw_detail_json = %s::jsonb, hash_detail = %s,
                 fec_ini_cotizacion = %s, fec_fin_cotizacion = %s,
                 schedule_fetched_at = now(), updated_at = now()
               WHERE id_contrato = %s""",
            (json.dumps({"etapas": stages}), json.dumps(raw), canonical_hash(raw),
             schedule_instant(quote.get("fecIni")), schedule_instant(quote.get("fecFin")), native_id),
        )
        return
    conn.execute("SELECT id_procedimiento FROM prod4_processes WHERE id_procedimiento = %s FOR UPDATE", (native_id,))
    proposals = next((s for s in stages if "presentacion" in _slug(s.get("nombreEtapa") or s.get("descripcionEtapa"))
                      and any(k in _slug(s.get("nombreEtapa") or s.get("descripcionEtapa")) for k in ("ofertas", "propuestas"))), {})
    registration = next((s for s in stages if "registro" in _slug(s.get("nombreEtapa") or s.get("descripcionEtapa"))
                         and "particip" in _slug(s.get("nombreEtapa") or s.get("descripcionEtapa"))), {})
    conn.execute(
        """UPDATE prod4_processes SET
             raw_detail = COALESCE(raw_detail, '{}'::jsonb) || %s::jsonb,
             proposals_start_at = %s, proposals_closes_at = %s,
             registration_closes_at = %s, schedule_fetched_at = now(), updated_at = now()
           WHERE id_procedimiento = %s""",
        (json.dumps({"listaCronograma": stages}),
         schedule_instant(proposals.get("fechaInicio"), proposals.get("horaInicio")),
         schedule_instant(proposals.get("fechaFin"), proposals.get("horaFin")),
         schedule_instant(registration.get("fechaFin"), registration.get("horaFin")), native_id),
    )
    conn.execute("DELETE FROM prod4_schedule WHERE id_procedimiento = %s", (native_id,))
    for position, stage in enumerate(stages):
        name = stage.get("nombreEtapa") or stage.get("descripcionEtapa")
        conn.execute(
            """INSERT INTO prod4_schedule
               (id_procedimiento, position, stage_key, stage_name, starts_at, ends_at, raw_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (native_id, position, _slug(name), name,
             schedule_instant(stage.get("fechaInicio"), stage.get("horaInicio")),
             schedule_instant(stage.get("fechaFin"), stage.get("horaFin")), json.dumps(stage)),
        )


def refresh_schedules(settings: Any, conn: Any, *, source: str = "both", limit: int = 100,
                      ids: list[int] | None = None, apply: bool = False,
                      include_inactive: bool = False,
                      prod4_client: Any = None, prod6_client: Any = None) -> dict:
    if source not in ("prod4", "prod6", "both") or not 1 <= limit <= 5000:
        raise ValueError("Use a valid source and a limit between 1 and 5000")
    if ids is not None and (source == "both" or not ids or any(i <= 0 for i in ids)):
        raise ValueError("Explicit positive IDs require a single source")
    if not conn.autocommit:
        raise ValueError("Schedule refresh requires autocommit; each record is atomic")
    stats = {"mode": "apply" if apply else "dry-run", "sources": {}}
    for current in (("prod4", "prod6") if source == "both" else (source,)):
        if current == "prod4":
            sql = """SELECT id_procedimiento AS id FROM prod4_processes
                     WHERE technology_relevant = true AND object_type IN ('good', 'service')
                     AND (%s OR missing_since IS NULL)
                     AND (%s::bigint[] IS NULL OR id_procedimiento = ANY(%s::bigint[]))
                     ORDER BY schedule_fetched_at ASC NULLS FIRST, id_procedimiento LIMIT %s"""
            params = (include_inactive, ids, ids, limit)
        else:
            sql = """SELECT id_contrato AS id FROM seace_contracts
                     WHERE objeto_codigo IN (1, 2) AND cubso_segmento = ANY(%s::text[])
                     AND (%s OR estado_codigo = 2)
                     AND (%s::bigint[] IS NULL OR id_contrato = ANY(%s::bigint[]))
                     ORDER BY schedule_fetched_at ASC NULLS FIRST, id_contrato LIMIT %s"""
            params = ([str(segment) for segment in settings.prod4_segments], include_inactive, ids, ids, limit)
        rows = conn.execute(sql, params).fetchall()
        result = {"selected": len(rows), "validated": 0, "updated": 0, "failed": [], "stages": 0}
        stats["sources"][current] = result
        supplied = prod4_client if current == "prod4" else prod6_client
        client_class = Prod4Client if current == "prod4" else SeaceClient
        with client_class(settings) if supplied is None else _ProvidedClient(supplied) as client:
            for row in rows:
                native_id = int(row["id"])
                try:
                    detail = client.detail(native_id) if current == "prod4" else client.contract_detail(native_id)
                    stages = validate_schedule(detail, current, native_id)
                    result["validated"] += 1
                    result["stages"] += len(stages)
                    if apply:
                        with conn.transaction():
                            persist_schedule(conn, current, native_id, stages)
                        result["updated"] += 1
                except Exception as exc:
                    # Avoid URLs/tokens or personal source content in output.
                    result["failed"].append({"id": native_id, "error": type(exc).__name__})
    return stats


class _ProvidedClient:
    def __init__(self, client: Any):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_args):
        pass
