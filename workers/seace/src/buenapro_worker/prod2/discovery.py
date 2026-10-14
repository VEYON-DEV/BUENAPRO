"""Persist sanitized, independently verified captures without lifecycle inference.

One run identifies one query pass with immutable totals/pages. A changed result
set requires a new run_id; identical captures are safe to replay after failure.
The producer must verify both official action IDs and the query/page data.
This module does not implement or validate an HTTP/browser transport. A valid
JSON shape cannot prove that SEACE applied the intended query on the server.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

LIMA = ZoneInfo("America/Lima")
QUERY_KEYS = {"keyword", "object_type", "year", "published_from", "published_to", "seace_version"}
PAGE_KEYS = {"number", "total_pages", "total_results", "page_size"}
ROW_KEYS = {"procedure_id", "convocation_id", "nomenclature", "buyer_name", "published_at",
            "description", "object_label", "reference_amount", "currency", "seace_version"}


class CaptureError(ValueError):
    """Invalid, stale or internally contradictory capture; writes are rolled back."""


class TruncatedQueryError(CaptureError):
    """The portal's 499-result boundary requires a narrower publication window."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CaptureError(f"{label}: expected exactly the documented fields")
    return value


def _int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CaptureError(f"{label}: invalid integer")
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CaptureError(f"{label}: invalid text")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise CaptureError(f"{label}: control characters are not supported")
    return " ".join(value.split())


def _iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CaptureError(f"{label}: expected ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CaptureError(f"{label}: invalid date") from exc


def _observed(value: Any, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError as exc:
        raise CaptureError("observed_at: invalid timestamp") from exc
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CaptureError("observed_at: timezone is required")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now.astimezone(timezone.utc) + timedelta(minutes=5):
        raise CaptureError("observed_at: future capture")
    return parsed


def _amount(value: Any) -> str | None:
    # PROD2 can explicitly withhold a reference value. Keep the source literal
    # in the raw capture while representing the undisclosed amount as NULL.
    if value in ("---", "Reservado"):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", value):
        raise CaptureError("reference_amount: invalid decimal or grouping")
    amount = Decimal(value.replace(",", ""))
    if amount >= Decimal("10000000000000000"):
        raise CaptureError("reference_amount: exceeds NUMERIC(18,2)")
    return str(amount.quantize(Decimal("0.01")))


@dataclass(frozen=True)
class Capture:
    query: dict[str, Any]
    query_hash: str
    observed_at: datetime
    page: dict[str, int]
    rows: list[dict[str, Any]]
    page_hash: str


def validate_capture(value: Any, *, now: datetime | None = None) -> Capture:
    root = _keys(value, {"query", "observed_at", "page", "rows"}, "capture")
    raw_query = _keys(root["query"], QUERY_KEYS, "query")
    year = _int(raw_query["year"], "query.year", 2000, 2100)
    start = _iso_date(raw_query["published_from"], "published_from")
    end = _iso_date(raw_query["published_to"], "published_to")
    if start > end:
        raise CaptureError("query: publication date range is reversed")
    # The native year filter can refer to the procedure/exercise. Publications
    # must lie within the date range, independently of that year filter.
    object_type = raw_query["object_type"]
    if object_type not in ("good", "service") or raw_query["seace_version"] != "3":
        raise CaptureError("query: unsupported object or SEACE version")
    query = {**raw_query, "keyword": _text(raw_query["keyword"], "keyword", 120)}
    observed_at = _observed(root["observed_at"], now or datetime.now(timezone.utc))
    page_raw = _keys(root["page"], PAGE_KEYS, "page")
    page_size = _int(page_raw["page_size"], "page_size", 1, 100)
    raw_total = page_raw["total_results"]
    if type(raw_total) is int and raw_total >= 499:
        raise TruncatedQueryError("query: result boundary reached; split publication dates")
    total = _int(raw_total, "total_results", 0, 498)
    pages = _int(page_raw["total_pages"], "total_pages", 1, 498)
    number = _int(page_raw["number"], "page.number", 1, pages)
    if pages != max(1, (total + page_size - 1) // page_size):
        raise CaptureError("page: total_pages does not reconcile with total_results")
    raw_rows = root["rows"]
    expected = min(page_size, max(0, total - (number - 1) * page_size))
    if not isinstance(raw_rows, list) or len(raw_rows) != expected:
        raise CaptureError("page: missing or truncated rows")
    rows: list[dict[str, Any]] = []
    convocation_ids: set[int] = set()
    for raw in raw_rows:
        raw = _keys(raw, ROW_KEYS, "row")
        procedure = _int(raw["procedure_id"], "procedure_id", 1, 2**63 - 1)
        convocation = _int(raw["convocation_id"], "convocation_id", 1, 2**63 - 1)
        if convocation in convocation_ids:
            raise CaptureError("page: duplicate convocation ID")
        convocation_ids.add(convocation)
        label = "Bien" if object_type == "good" else "Servicio"
        if raw["object_label"] != label or raw["seace_version"] != query["seace_version"]:
            raise CaptureError("row: object/version does not match query")
        try:
            published = datetime.strptime(raw["published_at"], "%d/%m/%Y %H:%M").replace(tzinfo=LIMA)
        except (TypeError, ValueError) as exc:
            raise CaptureError("published_at: expected dd/mm/YYYY HH:MM") from exc
        if not start <= published.date() <= end or published > observed_at:
            raise CaptureError("row: publication outside query dates or after observation")
        rows.append({"procedure_id": procedure, "convocation_id": convocation,
                     "nomenclature": _text(raw["nomenclature"], "nomenclature", 500),
                     "buyer_name": _text(raw["buyer_name"], "buyer_name", 1000),
                     "description": _text(raw["description"], "description", 20000),
                     "published_at": published.isoformat(), "object_type": object_type,
                     "object_label": label, "reference_amount": _amount(raw["reference_amount"]),
                     "currency": _text(raw["currency"], "currency", 50), "seace_version": "3"})
    page = {"number": number, "total_pages": pages, "total_results": total, "page_size": page_size}
    query_hash = _hash(query)
    return Capture(query, query_hash, observed_at, page, rows, _hash({"query": query, "page": page, "rows": rows}))


def _one(cursor: Any, sql: str, params: tuple = ()) -> dict | None:
    cursor.execute(sql, params)
    return cursor.fetchone()


def _identity(cursor: Any, row: dict, observed_at: datetime) -> tuple[Any, bool]:
    procedure, convocation = row["procedure_id"], row["convocation_id"]
    cursor.execute("""SELECT source_key, id_kind, external_id, opportunity_id FROM opportunity_sources
        WHERE (source_key IN ('seace_prod2', 'seace_prod4') AND id_kind = 'id_procedimiento' AND external_id = %s)
           OR (source_key = 'seace_prod2' AND id_kind = 'id_convocatoria' AND external_id = %s)""",
        (str(procedure), str(convocation)))
    sources = cursor.fetchall()
    prod4 = _one(cursor, """SELECT opportunity_id, id_convocatoria_pub, object_type
        FROM prod4_processes WHERE id_procedimiento = %s""", (procedure,))
    if prod4 and (prod4["object_type"] != row["object_type"]
                  or (prod4["id_convocatoria_pub"] is not None and prod4["id_convocatoria_pub"] != convocation)):
        raise CaptureError("identity: PROD4 object/convocation disagrees with verified capture")
    identities = {str(source["opportunity_id"]) for source in sources}
    if prod4:
        identities.add(str(prod4["opportunity_id"]))
    if len(identities) > 1:
        raise CaptureError("identity: incompatible canonical opportunity IDs")
    existing = _one(cursor, """SELECT id_procedimiento, opportunity_id, object_type, last_seen_at
        FROM prod2_discoveries WHERE id_convocatoria = %s""", (convocation,))
    if existing and (existing["id_procedimiento"] != procedure
                     or existing["object_type"] != row["object_type"]
                     or (identities and str(existing["opportunity_id"]) not in identities)):
        raise CaptureError("identity: convocation is already linked to a different procedure/object")
    cursor.execute("SELECT object_type, last_seen_at FROM prod2_discoveries WHERE id_procedimiento = %s", (procedure,))
    for prior in cursor.fetchall():
        if prior["object_type"] != row["object_type"] or prior["last_seen_at"] > observed_at:
            raise CaptureError("capture: stale observation or inconsistent procedure object")
    if identities:
        opportunity_id = next(iter(identities))
    else:
        opportunity = _one(cursor, """INSERT INTO opportunities
            (record_kind, object_type, procurement_method, lifecycle_stage, participation_access, actionability)
            VALUES ('opportunity', %s, 'selection_procedure', 'unknown', 'unknown', 'unknown') RETURNING id""",
            (row["object_type"],))
        opportunity_id = opportunity["id"]
    native = _json({"id_procedimiento": procedure, "id_convocatoria": convocation, "seace_version": "3"})
    for kind, external_id in (("id_procedimiento", procedure), ("id_convocatoria", convocation)):
        cursor.execute("""INSERT INTO opportunity_sources
            (opportunity_id, source_key, id_kind, external_id, native_values)
            VALUES (%s, 'seace_prod2', %s, %s, %s::jsonb) ON CONFLICT DO NOTHING""",
            (opportunity_id, kind, str(external_id), native))
    # Canonical lifecycle/actionability and existing PROD4 records are never updated.
    return opportunity_id, existing is None


def import_capture(conn: Any, value: Any, *, run_id: str) -> dict[str, Any]:
    """Atomically import one verified full page; requires 0035 and a writable transaction.

    Uses an explicit dict-row cursor, independent of the caller's row factory.
    If called inside another transaction, the caller owns the outer commit.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", run_id):
        raise CaptureError("run_id: invalid durable pass identifier")
    capture = validate_capture(value)
    page = capture.page
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_discovery_run'), hashtext(%s))", (run_id,))
        cursor.execute("""INSERT INTO prod2_discovery_queries(query_hash, query_manifest)
            VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING""", (capture.query_hash, _json(capture.query)))
        cursor.execute("""INSERT INTO prod2_discovery_runs
            (run_id, query_hash, total_results, total_pages, page_size, first_observed_at, last_observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (run_id, capture.query_hash, page["total_results"], page["total_pages"], page["page_size"], capture.observed_at, capture.observed_at))
        run = _one(cursor, "SELECT * FROM prod2_discovery_runs WHERE run_id = %s FOR UPDATE", (run_id,))
        if (run["query_hash"], run["total_results"], run["total_pages"], run["page_size"]) != (
                capture.query_hash, page["total_results"], page["total_pages"], page["page_size"]):
            raise CaptureError("run: query/totals changed; use a new run_id")
        saved = _one(cursor, "SELECT page_hash FROM prod2_discovery_pages WHERE run_id = %s AND page_number = %s", (run_id, page["number"]))
        if saved:
            if saved["page_hash"] != capture.page_hash:
                raise CaptureError("run: page contents changed; use a new run_id")
            return {"run_id": run_id, "status": run["status"], "page": page["number"], "replayed": True,
                    "captured_rows": len(capture.rows), "new_discoveries": 0}
        if capture.observed_at < run["last_observed_at"]:
            raise CaptureError("run: stale page observation")
        if capture.rows:
            cursor.execute("SELECT id_convocatoria FROM prod2_discovery_page_rows WHERE run_id = %s AND id_convocatoria = ANY(%s::bigint[])",
                           (run_id, [row["convocation_id"] for row in capture.rows]))
            if cursor.fetchone():
                raise CaptureError("run: duplicate convocation across pages")
        # Match PROD4's identity lock; global sorted acquisition avoids deadlocks
        # between different runs containing the same procedures/convocations.
        for procedure in sorted({row["procedure_id"] for row in capture.rows}):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod4'), hashtext(%s))", (str(procedure),))
        for convocation in sorted({row["convocation_id"] for row in capture.rows}):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod2_convocation'), hashtext(%s))", (str(convocation),))
        new_discoveries = 0
        for row in capture.rows:
            opportunity_id, new = _identity(cursor, row, capture.observed_at)
            new_discoveries += int(new)
            cursor.execute("""INSERT INTO prod2_discoveries
                (id_convocatoria, id_procedimiento, opportunity_id, nomenclature, buyer_name, description,
                 object_type, object_label, published_at, reference_amount, currency, seace_version,
                 row_hash, first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id_convocatoria) DO UPDATE SET
                  nomenclature = EXCLUDED.nomenclature, buyer_name = EXCLUDED.buyer_name,
                  description = EXCLUDED.description, published_at = EXCLUDED.published_at,
                  reference_amount = EXCLUDED.reference_amount, currency = EXCLUDED.currency,
                  row_hash = EXCLUDED.row_hash, last_seen_at = EXCLUDED.last_seen_at""",
                (row["convocation_id"], row["procedure_id"], opportunity_id, row["nomenclature"], row["buyer_name"],
                 row["description"], row["object_type"], row["object_label"], row["published_at"],
                 row["reference_amount"], row["currency"], row["seace_version"], _hash(row), capture.observed_at, capture.observed_at))
        cursor.execute("""INSERT INTO prod2_discovery_pages
            (run_id, page_number, observed_at, page_hash, row_count, rows_manifest) VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
            (run_id, page["number"], capture.observed_at, capture.page_hash, len(capture.rows), _json(capture.rows)))
        for position, row in enumerate(capture.rows, 1):
            cursor.execute("INSERT INTO prod2_discovery_page_rows(run_id, page_number, row_number, id_convocatoria) VALUES (%s, %s, %s, %s)",
                           (run_id, page["number"], position, row["convocation_id"]))
        counts = _one(cursor, """SELECT count(*) AS pages, coalesce(sum(row_count), 0) AS rows,
            min(page_number) AS first_page, max(page_number) AS last_page
            FROM prod2_discovery_pages WHERE run_id = %s""", (run_id,))
        ids = _one(cursor, "SELECT count(*) AS ids FROM prod2_discovery_page_rows WHERE run_id = %s", (run_id,))
        complete = (counts["pages"] == page["total_pages"] and counts["first_page"] == 1
                    and counts["last_page"] == page["total_pages"]
                    and counts["rows"] == page["total_results"] and ids["ids"] == page["total_results"])
        status = "complete" if complete else "partial"
        cursor.execute("""UPDATE prod2_discovery_runs SET status = %s, last_observed_at = %s,
            completed_at = CASE WHEN %s THEN %s ELSE NULL END WHERE run_id = %s""",
            (status, capture.observed_at, complete, capture.observed_at, run_id))
        return {"run_id": run_id, "status": status, "page": page["number"], "replayed": False,
                "captured_rows": len(capture.rows), "new_discoveries": new_discoveries,
                "completed_pages": counts["pages"], "total_pages": page["total_pages"]}
