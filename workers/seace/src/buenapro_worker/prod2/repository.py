"""Canonical PROD2 evidence and durable reservations; no transport or model calls.

Requires migrations 0035/0036/0041. Every operation has its own transaction;
an enclosing transaction still belongs to the caller. COMMIT a reservation
before calling the provider. Expired claims are uncertain and are not retried.
Official schedule dates never establish a winner or canonical lifecycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from buenapro_worker.normalization.facets import derive_facets, derive_summary, facet_hash
from buenapro_worker.prod2.discovery import _identity, validate_capture
from buenapro_worker.prod2.documents import METADATA_URL
from buenapro_worker.prod2.document_events import event_hash

LIMA = ZoneInfo("America/Lima")
KINDS = {"jev_affinity", "gemini_extraction", "gemini_match"}
DETAIL_FIELDS = {"procedure_id", "convocatoria_id", "nomenclature", "convocatoria_number",
                 "selection_type", "regulation", "seace_version", "entity", "object",
                 "description", "amount_text", "published_at", "schedule", "documents",
                 "document_inventory_total", "document_inventory_complete", "item_states",
                 "document_events"}


class Prod2PersistenceError(ValueError):
    """Sanitized validation/identity/claim failure without opaque source values."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise Prod2PersistenceError("Expected finite JSON data") from None


def hash_request(value: Any) -> str:
    """Hash the COMPLETE actual provider input, excluding volatile observation dates."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Prod2PersistenceError("Expected a lowercase SHA256")
    return value


def _text(value: Any, *, limit: int = 20000, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value.strip() and not empty):
        raise Prod2PersistenceError("Invalid structured text")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise Prod2PersistenceError("Unexpected control characters")
    return " ".join(value.split())


def _id(value: Any) -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise Prod2PersistenceError("Invalid official numeric identity")
    return value


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise Prod2PersistenceError("Invalid UUID") from None


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Prod2PersistenceError("Observation timestamp requires timezone")
    return value.astimezone(timezone.utc)


def _official_time(text: str) -> str | None:
    # Date-only and unrecognized official formats retain their original text.
    for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=LIMA).isoformat()
        except ValueError:
            pass
    return None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _deadline(schedule: list[dict], needles: tuple[str, ...], key: str) -> str | None:
    values = {row[key] for row in schedule if all(n in _fold(row["stage"]) for n in needles) and row[key]}
    return next(iter(values)) if len(values) == 1 else None


def _source_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "prod2.seace.gob.pe"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or ";" in parsed.path or parsed.fragment
            or set(parse_qs(parsed.query)) - {"idProc", "faces-redirect"}):
        raise Prod2PersistenceError("Expected a stable official ficha reference")
    return value


def prepare_detail(detail: Any) -> dict:
    """Validate only the sanitized parse_detail contract; never accept raw HTML."""
    if not isinstance(detail, dict) or set(detail) - DETAIL_FIELDS:
        raise Prod2PersistenceError("Unexpected fields in structured detail")
    required = {"procedure_id", "convocatoria_id", "nomenclature", "entity", "object",
                "description", "schedule", "documents", "document_inventory_total",
                "document_inventory_complete", "item_states"}
    if not required <= set(detail):
        raise Prod2PersistenceError("Missing structured detail fields")
    result = {key: _text(value, empty=True) for key, value in detail.items()
              if (key not in required and key != "document_events")
              or key in {"nomenclature", "entity", "object", "description"}}
    for key in ("nomenclature", "entity", "object", "description"):
        result[key] = _text(detail[key])
    if result["object"] not in {"Bien", "Servicio"}:
        raise Prod2PersistenceError("Unsupported PROD2 object")
    if result.get("seace_version", "3") not in {"3", "SEACE 3.0", "SEACE 3"}:
        raise Prod2PersistenceError("Unsupported PROD2 version")
    result.update(procedure_id=_id(detail["procedure_id"]), convocatoria_id=_id(detail["convocatoria_id"]))
    if not isinstance(detail["schedule"], list) or not isinstance(detail["documents"], list):
        raise Prod2PersistenceError("Expected schedule and document arrays")
    schedule = []
    for row in detail["schedule"]:
        if not isinstance(row, dict) or set(row) != {"stage", "starts_at_text", "ends_at_text"}:
            raise Prod2PersistenceError("Invalid schedule row")
        stage = _text(row["stage"], limit=1000)
        start, end = (_text(row[key], limit=100, empty=True) for key in ("starts_at_text", "ends_at_text"))
        schedule.append({"stage": stage, "starts_at_text": start, "ends_at_text": end,
                         "starts_at": _official_time(start), "ends_at": _official_time(end)})
    def document_row(row):
        if not isinstance(row, dict) or set(row) != {"uuid", "seace_version", "filename", "stage", "type", "published_at"}:
            raise Prod2PersistenceError("Invalid document inventory row")
        code = _uuid(row["uuid"])
        if row["seace_version"] != "3":
            raise Prod2PersistenceError("Duplicate document UUID or unsupported version")
        published = _text(row["published_at"], limit=100, empty=True)
        return {"uuid": code, "filename": _text(row["filename"], limit=1000),
                "stage": _text(row["stage"], limit=1000, empty=True),
                "type": _text(row["type"], limit=1000, empty=True),
                "published_at_text": published, "published_at": _official_time(published)}

    documents, codes = [], set()
    for row in detail["documents"]:
        document = document_row(row)
        code = document["uuid"]
        if code in codes:
            raise Prod2PersistenceError("Duplicate document UUID or unsupported version")
        codes.add(code)
        documents.append(document)
    # Compatibility: old captures keep their exact normalized hash. New native
    # captures distinguish unique downloadable resources from publication rows.
    events = documents
    if "document_events" in detail:
        if not isinstance(detail["document_events"], list):
            raise Prod2PersistenceError("Expected document publication array")
        events, first_by_code, event_keys = [], {}, set()
        for row in detail["document_events"]:
            document = document_row(row)
            key = event_hash(document)
            if key in event_keys:
                raise Prod2PersistenceError("Duplicate exact document publication")
            event_keys.add(key)
            first = first_by_code.setdefault(document["uuid"], document)
            if first["filename"] != document["filename"]:
                raise Prod2PersistenceError("Conflicting document UUID filename")
            events.append(document)
        if list(first_by_code.values()) != documents:
            raise Prod2PersistenceError("Document resources do not match first publication representatives")
        result["document_events"] = events
    total, complete = detail["document_inventory_total"], detail["document_inventory_complete"]
    if (type(complete) is not bool or (total is not None and (type(total) is not int or total < len(events)))
            or (complete and total != len(events))):
        raise Prod2PersistenceError("Document completeness contradicts inventory")
    if not isinstance(detail["item_states"], list):
        raise Prod2PersistenceError("Expected observed item states")
    states = list(dict.fromkeys(_text(state, limit=200) for state in detail["item_states"]))
    result.update(schedule=schedule, documents=documents, document_inventory_total=total,
                  document_inventory_complete=complete, item_states=states)
    return result


def _one(cursor: Any, sql: str, params: tuple = ()) -> dict | None:
    cursor.execute(sql, params)
    return cursor.fetchone()


def upsert_pilot_discovery(conn: Any, row: dict, query_manifest: dict, *, batch_id: str,
                           observed_at: datetime) -> dict:
    """Persist ONE selected parse_search row, never full-query coverage.

    The caller independently verifies the native query and two action IDs and
    selects eligible candidates before this call. Each durable batch admits at
    most ten distinct convocatorias. No discovery run/page is manufactured.
    """
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", batch_id):
        raise Prod2PersistenceError("Invalid durable pilot batch identifier")
    if not isinstance(row, dict) or set(row) != {"procedure_id", "convocatoria_id", "entity",
            "published_at", "nomenclature", "object", "description", "amount_text", "currency", "seace_version"}:
        raise Prod2PersistenceError("Expected sanitized native search row")
    observed = _aware(observed_at)
    # Reuse the existing value validator only; its synthetic validation envelope
    # is never stored as an observed page or a server result count.
    capture = validate_capture({"query": query_manifest, "observed_at": observed.isoformat(),
        "page": {"number": 1, "total_pages": 1, "total_results": 1, "page_size": 1},
        "rows": [{"procedure_id": row["procedure_id"], "convocation_id": row["convocatoria_id"],
                  "nomenclature": row["nomenclature"], "buyer_name": row["entity"],
                  "published_at": row["published_at"], "description": row["description"],
                  "object_label": row["object"], "reference_amount": row["amount_text"],
                  "currency": row["currency"], "seace_version": row["seace_version"]}]})
    data = capture.rows[0]
    procedure, convocation = data["procedure_id"], data["convocation_id"]
    digest = hash_request(data)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_pilot_batch'),hashtext(%s))", (batch_id,))
        cursor.execute("INSERT INTO prod2_pilot_batches(batch_id) VALUES (%s) ON CONFLICT DO NOTHING", (batch_id,))
        batch = _one(cursor, "SELECT requested_limit FROM prod2_pilot_batches WHERE batch_id=%s FOR UPDATE", (batch_id,))
        prior = _one(cursor, "SELECT * FROM prod2_pilot_observations WHERE batch_id=%s AND id_convocatoria=%s", (batch_id, convocation))
        if prior and (prior["row_hash"] != digest or prior["query_hash"] != capture.query_hash):
            raise Prod2PersistenceError("Pilot member already has a different durable observation")
        if not prior:
            count = _one(cursor, "SELECT count(*) AS total FROM prod2_pilot_observations WHERE batch_id=%s", (batch_id,))
            if count["total"] >= batch["requested_limit"]:
                raise Prod2PersistenceError("Pilot batch already reached its selected-candidate limit")
        # Cooperate with discovery.py and the retired source's identity locks.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod4'),hashtext(%s))", (str(procedure),))
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('seace_prod2_convocation'),hashtext(%s))", (str(convocation),))
        opportunity, created = _identity(cursor, data, observed)
        cursor.execute("""INSERT INTO prod2_discoveries(id_convocatoria,id_procedimiento,opportunity_id,
            nomenclature,buyer_name,description,object_type,object_label,published_at,reference_amount,
            currency,seace_version,row_hash,first_seen_at,last_seen_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id_convocatoria) DO UPDATE SET nomenclature=EXCLUDED.nomenclature,
              buyer_name=EXCLUDED.buyer_name,description=EXCLUDED.description,published_at=EXCLUDED.published_at,
              reference_amount=EXCLUDED.reference_amount,currency=EXCLUDED.currency,
              row_hash=EXCLUDED.row_hash,last_seen_at=EXCLUDED.last_seen_at""",
            (convocation, procedure, opportunity, data["nomenclature"], data["buyer_name"], data["description"],
             data["object_type"], data["object_label"], data["published_at"], data["reference_amount"],
             data["currency"], "3", digest, observed, observed))
        cursor.execute("""INSERT INTO prod2_pilot_observations(batch_id,id_convocatoria,query_hash,
            query_manifest,row_hash,row_manifest,observed_at) VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)
            ON CONFLICT(batch_id,id_convocatoria) DO NOTHING""",
            (batch_id, convocation, capture.query_hash, _json(capture.query), digest, _json(data), observed))
    return {"id_convocatoria": convocation, "opportunity_id": str(opportunity), "created": created,
            "coverage": "pilot_selection", "batch_id": batch_id}


def persist_detail(conn: Any, detail: dict, *, observed_at: datetime, source_url: str,
                   source_session_dependent: bool = True) -> dict:
    data = prepare_detail(detail)
    observed = _aware(observed_at)
    if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise Prod2PersistenceError("Future ficha observation")
    source = _source_url(source_url)
    if type(source_session_dependent) is not bool:
        raise Prod2PersistenceError("Expected explicit session dependency")
    convocation = data["convocatoria_id"]
    digest = hash_request(data)
    schedule = data["schedule"]
    award_rows = [row for row in schedule if "buena pro" in _fold(row["stage"])]
    events = data.get("document_events", data["documents"])
    award_docs = list(dict.fromkeys(doc["uuid"] for doc in events if "buena pro" in _fold(doc["stage"])))
    award = {"schedule_stage_present": bool(award_rows), "scheduled_stages": award_rows,
             "stage_documents_listed": award_docs, "award_result": "unknown", "winner": None,
             "awarded_amount": None, "effective_award_date": None}
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        discovery = _one(cursor, "SELECT * FROM prod2_discoveries WHERE id_convocatoria = %s FOR UPDATE", (convocation,))
        if not discovery:
            raise Prod2PersistenceError("Import verified discovery before its ficha")
        if (discovery["id_procedimiento"] != data["procedure_id"]
                or discovery["nomenclature"] != data["nomenclature"]
                or discovery["buyer_name"] != data["entity"]
                or discovery["description"] != data["description"]
                or discovery["object_label"] != data["object"]):
            raise Prod2PersistenceError("Ficha does not match canonical discovery")
        previous = _one(cursor, "SELECT detail_hash, detail_fetched_at FROM prod2_details WHERE id_convocatoria = %s", (convocation,))
        if previous and (previous["detail_fetched_at"] > observed or
                         (previous["detail_fetched_at"] == observed and previous["detail_hash"] != digest)):
            raise Prod2PersistenceError("Stale or contradictory ficha observation")
        opportunity = discovery["opportunity_id"]
        proposals_start = _deadline(schedule, ("presentacion", "oferta"), "starts_at") or _deadline(schedule, ("presentacion", "propuesta"), "starts_at")
        proposals_close = _deadline(schedule, ("presentacion", "oferta"), "ends_at") or _deadline(schedule, ("presentacion", "propuesta"), "ends_at")
        cursor.execute("""INSERT INTO prod2_details(id_convocatoria, opportunity_id, detail_hash,
            detail_fetched_at, source_url, source_session_dependent, registration_closes_at,
            proposals_start_at, proposals_closes_at, schedule_json, item_states, buena_pro_json,
            document_inventory_total, document_inventory_complete, raw_detail)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s::jsonb)
            ON CONFLICT(id_convocatoria) DO UPDATE SET detail_hash=EXCLUDED.detail_hash,
              detail_fetched_at=EXCLUDED.detail_fetched_at, source_url=EXCLUDED.source_url,
              source_session_dependent=EXCLUDED.source_session_dependent,
              registration_closes_at=EXCLUDED.registration_closes_at, proposals_start_at=EXCLUDED.proposals_start_at,
              proposals_closes_at=EXCLUDED.proposals_closes_at, schedule_json=EXCLUDED.schedule_json,
              item_states=EXCLUDED.item_states, buena_pro_json=EXCLUDED.buena_pro_json,
              document_inventory_total=EXCLUDED.document_inventory_total,
              document_inventory_complete=EXCLUDED.document_inventory_complete, raw_detail=EXCLUDED.raw_detail""",
            (convocation, opportunity, digest, observed, source, source_session_dependent,
             _deadline(schedule, ("registro", "participante"), "ends_at"), proposals_start,
             proposals_close, _json(schedule), data["item_states"], _json(award),
             data["document_inventory_total"], data["document_inventory_complete"], _json(data)))
        if data["document_inventory_complete"]:
            cursor.execute("UPDATE prod2_documents SET is_listed=false WHERE id_convocatoria=%s", (convocation,))
            cursor.execute("UPDATE prod2_document_events SET is_listed=false WHERE id_convocatoria=%s", (convocation,))
        for document in data["documents"]:
            cursor.execute("""INSERT INTO prod2_documents(id_convocatoria,codigo_alfresco,name,document_type,
                stage_name,published_at,published_at_text,source_url,first_seen_at,last_seen_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id_convocatoria,codigo_alfresco) DO UPDATE SET name=EXCLUDED.name,
                  document_type=EXCLUDED.document_type,stage_name=EXCLUDED.stage_name,published_at=EXCLUDED.published_at,
                  published_at_text=EXCLUDED.published_at_text,is_listed=true,last_seen_at=EXCLUDED.last_seen_at""",
                (convocation, document["uuid"], document["filename"], document["type"], document["stage"],
                 document["published_at"], document["published_at_text"], METADATA_URL + "?id=" + document["uuid"], observed, observed))
        for document in events:
            cursor.execute("""INSERT INTO prod2_document_events(id_convocatoria,codigo_alfresco,event_hash,
                filename,seace_version,document_type,stage_name,published_at,published_at_text,first_seen_at,last_seen_at)
                VALUES (%s,%s,%s,%s,'3',%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id_convocatoria,codigo_alfresco,event_hash) DO UPDATE SET
                  is_listed=true,last_seen_at=EXCLUDED.last_seen_at""",
                (convocation, document["uuid"], event_hash(document), document["filename"], document["type"],
                 document["stage"], document["published_at"], document["published_at_text"], observed, observed))
    return {"id_convocatoria": convocation, "opportunity_id": str(opportunity), "detail_hash": digest,
            "documents_count": len(data["documents"]), "document_events_count": len(events),
            "document_inventory_complete": data["document_inventory_complete"]}


def record_document_download(conn: Any, convocation_id: int, manifest: dict, *, storage_key: str | None = None) -> dict:
    convocation, code, sha = _id(convocation_id), _uuid(manifest.get("document_uuid")), _sha(manifest.get("sha256"))
    size = manifest.get("bytes")
    if type(size) is not int or size < 1 or manifest.get("format") not in {"pdf", "zip", "ole", "rar", "7z"}:
        raise Prod2PersistenceError("Invalid downloaded document facts")
    mime = _text(manifest.get("content_type"), limit=200, empty=True)
    if storage_key is not None:
        storage_key = _text(storage_key, limit=1000)
        if storage_key.startswith("/") or ":" in storage_key or ".." in storage_key.split("/"):
            raise Prod2PersistenceError("Expected a durable object storage key, not a local path or URL")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        document = _one(cursor, "SELECT is_listed FROM prod2_documents WHERE id_convocatoria=%s AND codigo_alfresco=%s FOR UPDATE", (convocation, code))
        if not document or not document["is_listed"]:
            raise Prod2PersistenceError("Download is not in the observed official inventory")
        saved = _one(cursor, """INSERT INTO prod2_document_downloads(id_convocatoria,codigo_alfresco,
            sha256_original,bytes,content_type,format,storage_key) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id_convocatoria,codigo_alfresco,sha256_original) DO UPDATE SET
              storage_key=COALESCE(EXCLUDED.storage_key,prod2_document_downloads.storage_key)
            RETURNING *""", (convocation, code, sha, size, mime, manifest["format"], storage_key))
        if saved["bytes"] != size or saved["format"] != manifest["format"]:
            raise Prod2PersistenceError("Same document hash has contradictory download facts")
    return saved


def claim_evaluation(conn: Any, convocation_id: int, *, kind: str, request_hash: str, model: str,
                     policy_version: str, profile_id: str | None = None, lease_seconds: int = 600) -> dict:
    convocation, request_hash = _id(convocation_id), _sha(request_hash)
    if kind not in KINDS or type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
        raise Prod2PersistenceError("Invalid evaluation kind or lease")
    model, policy = _text(model, limit=200), _text(policy_version, limit=200)
    profile = _uuid(profile_id) if profile_id is not None else None
    key = hash_request({"convocation": convocation, "kind": kind, "request_hash": request_hash,
                        "model": model, "policy_version": policy, "profile_id": profile})
    token = str(uuid4())
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""INSERT INTO prod2_evaluations(evaluation_key,id_convocatoria,kind,
            request_hash,model,policy_version,profile_id) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(evaluation_key) DO NOTHING""", (key, convocation, kind, request_hash, model, policy, profile))
        row = _one(cursor, "SELECT *, (lease_until <= now()) AS expired FROM prod2_evaluations WHERE evaluation_key=%s FOR UPDATE", (key,))
        if row["status"] == "done":
            return {"status": "cached", "id": int(row["id"]), "result_json": row["result_json"]}
        if row["status"] == "running" and row["expired"]:
            cursor.execute("UPDATE prod2_evaluations SET status='uncertain',updated_at=now() WHERE id=%s", (row["id"],))
            return {"status": "uncertain", "id": int(row["id"])}
        if row["status"] != "pending":
            return {"status": "busy" if row["status"] == "running" else row["status"], "id": int(row["id"])}
        cursor.execute("""UPDATE prod2_evaluations SET status='running',claim_token=%s,claimed_at=now(),
            lease_until=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s""", (token, lease_seconds, row["id"]))
    return {"status": "claimed", "id": int(row["id"]), "claim_token": token, "evaluation_key": key}


def _usage(input_tokens: Any, output_tokens: Any, cost_usd: Any) -> tuple:
    for value in (input_tokens, output_tokens):
        if value is not None and (type(value) is not int or not 0 <= value <= 2**31 - 1):
            raise Prod2PersistenceError("Invalid provider token usage")
    try:
        cost = None if cost_usd is None else Decimal(str(cost_usd))
        if cost is not None and (not cost.is_finite() or not 0 <= cost < 10000):
            raise InvalidOperation()
    except (InvalidOperation, ValueError):
        raise Prod2PersistenceError("Invalid provider cost") from None
    return input_tokens, output_tokens, cost


def complete_evaluation(conn: Any, evaluation_id: int, claim_token: str, result_json: dict, *,
                        input_tokens: int | None = None, output_tokens: int | None = None,
                        cost_usd: Any = None) -> dict:
    identity, token = _id(evaluation_id), _uuid(claim_token)
    if not isinstance(result_json, dict):
        raise Prod2PersistenceError("Expected structured provider result")
    payload = _json(result_json)
    usage = _usage(input_tokens, output_tokens, cost_usd)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        # A late result from the original owner can resolve an uncertain lease.
        row = _one(cursor, """UPDATE prod2_evaluations SET status='done',result_json=%s::jsonb,
            input_tokens=%s,output_tokens=%s,cost_usd=%s,completed_at=now(),updated_at=now()
            WHERE id=%s AND claim_token=%s AND status IN ('running','uncertain') RETURNING *""",
            (payload, *usage, identity, token))
        if not row:
            raise Prod2PersistenceError("Evaluation is not owned by this claim or is already completed")
    return row


def persist_extraction(conn: Any, convocation_id: int, document_uuid: str, sha256_original: str,
                       *, evaluation_id: int, result: Any,
                       allowed_models: tuple[str, ...] | None = None) -> dict:
    """Persist an LLM1 result; caller completed its durable evaluation.

    A fallback keeps the original reservation and records the effective model
    in result_json.model. Pass the configured primary/fallback allowlist for
    this case; no reservation or model invocation is repeated here.
    """
    convocation, code, sha = _id(convocation_id), _uuid(document_uuid), _sha(sha256_original)
    evaluation = _id(evaluation_id)
    raw = result.raw_json
    if not isinstance(raw, dict) or type(result.requires_human_review) is not bool:
        raise Prod2PersistenceError("Invalid structured extraction result")
    model, prompt, schema = (_text(value, limit=200) for value in (result.model, result.prompt_version, result.schema_version))
    usage = _usage(result.input_tokens, result.output_tokens, result.cost_usd)
    summary, facets = derive_summary(raw), derive_facets(raw)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        discovery = _one(cursor, "SELECT opportunity_id FROM prod2_discoveries WHERE id_convocatoria=%s FOR UPDATE", (convocation,))
        if not discovery:
            raise Prod2PersistenceError("Unknown canonical discovery")
        reservation = _one(cursor, "SELECT * FROM prod2_evaluations WHERE id=%s", (evaluation,))
        if not reservation or (reservation["id_convocatoria"], reservation["kind"], reservation["status"]) != (convocation, "gemini_extraction", "done"):
            raise Prod2PersistenceError("Extraction has no completed matching reservation")
        binding = reservation["result_json"]
        if not isinstance(binding, dict) or (binding.get("document_uuid"), binding.get("document_sha256")) != (code, sha):
            raise Prod2PersistenceError("Extraction reservation does not bind the official document revision")
        effective_model = binding.get("model", reservation["model"])
        if effective_model != model:
            raise Prod2PersistenceError("Extraction does not match its recorded effective model")
        if model == "gpt-6-luna":
            from .luna_policy import validate_luna_bundle
            try:
                validate_luna_bundle(binding)
            except (KeyError, TypeError, ValueError) as error:
                raise Prod2PersistenceError("Luna extraction provenance is invalid") from error
            if reservation["model"] != model or binding["raw_extraction_json"] != raw:
                raise Prod2PersistenceError("Luna requires its own exact provider reservation")
        if allowed_models is not None:
            if model not in allowed_models or not (model.startswith("gemini-") or model == "gpt-6-luna"):
                raise Prod2PersistenceError("Effective model is outside the configured LLM1 policy")
        elif effective_model != reservation["model"]:
            raise Prod2PersistenceError("Fallback persistence requires the configured Gemini model allowlist")
        prior = _one(cursor, "SELECT * FROM prod2_document_extractions WHERE evaluation_id=%s", (evaluation,))
        if prior:
            if (prior["id_convocatoria"], str(prior["codigo_alfresco"]), prior["sha256_original"]) != (convocation, code, sha):
                raise Prod2PersistenceError("Evaluation already belongs to another document")
            return {"extraction_id": int(prior["id"]), "opportunity_id": str(prior["opportunity_id"]), "cached": True}
        download = _one(cursor, """SELECT d.format FROM prod2_document_downloads d JOIN prod2_documents i
            USING(id_convocatoria,codigo_alfresco) WHERE d.id_convocatoria=%s AND d.codigo_alfresco=%s
            AND d.sha256_original=%s AND i.is_listed=true""", (convocation, code, sha))
        if not download:
            raise Prod2PersistenceError("Extraction has no recorded current official download")
        opportunity = discovery["opportunity_id"]
        cursor.execute("UPDATE prod2_document_extractions SET is_current=false WHERE id_convocatoria=%s AND is_current", (convocation,))
        row = _one(cursor, """INSERT INTO prod2_document_extractions(id_convocatoria,opportunity_id,
            codigo_alfresco,sha256_original,evaluation_id,model,prompt_version,schema_version,input_tokens,
            output_tokens,cost_usd,raw_extraction_json,summary_json,requires_human_review,facet_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) RETURNING id""",
            (convocation, opportunity, code, sha, evaluation, model, prompt, schema, *usage,
             _json(raw), _json(summary), result.requires_human_review, len(facets)))
        extraction_id = int(row["id"])
        cursor.execute("UPDATE prod2_requirement_facets SET is_current=false WHERE id_convocatoria=%s AND is_current", (convocation,))
        for facet in facets:
            cursor.execute("""INSERT INTO prod2_requirement_facets(id_convocatoria,opportunity_id,extraction_id,
                facet,label,required,details_json,evidence_json,facet_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)""",
                (convocation, opportunity, extraction_id, facet["facet"], facet["label"], facet["required"],
                 _json(facet["details"]), _json(facet["evidence"]), facet_hash(facet)))
    return {"extraction_id": extraction_id, "opportunity_id": str(opportunity), "cached": False,
            "facet_count": len(facets), "requires_human_review": result.requires_human_review}


def persist_match(conn: Any, convocation_id: int, *, profile_id: str, extraction_id: int,
                  evaluation_id: int, profile_hash: str, score: int, verdict: str,
                  breakdown_json: dict, missing_actions_json: list,
                  business_line_id: str | None = None, allow_source_review: bool = False) -> dict:
    """Store document-backed compliance only; preliminary Jev affinity stays separate.

    ``allow_source_review`` accepts a current reading still flagged for human review: the web then
    shows the match as a provisional estimate (``evaluated_source_review_required``).
    """
    convocation, extraction, evaluation = _id(convocation_id), _id(extraction_id), _id(evaluation_id)
    profile, profile_hash = _uuid(profile_id), _sha(profile_hash)
    line = _uuid(business_line_id) if business_line_id is not None else None
    if type(score) is not int or not 0 <= score <= 100 or verdict not in {"verde", "ambar", "rojo", "gris"}:
        raise Prod2PersistenceError("Invalid final match score or verdict")
    if not isinstance(breakdown_json, dict) or not isinstance(missing_actions_json, list):
        raise Prod2PersistenceError("Invalid final match payload")
    if breakdown_json.get("meta") is not None and not isinstance(breakdown_json["meta"], dict):
        raise Prod2PersistenceError("Invalid final match provenance")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        source = _one(cursor, """SELECT e.* FROM prod2_document_extractions e JOIN prod2_documents d
            USING(id_convocatoria,codigo_alfresco) WHERE e.id=%s AND e.id_convocatoria=%s
            AND e.is_current=true AND d.is_listed=true FOR UPDATE OF e""", (extraction, convocation))
        if (not source or (source["requires_human_review"] and allow_source_review is not True)
                or source["quality"] == "failed" or source["facet_count"] < 1):
            raise Prod2PersistenceError("Final match requires current document requirements without review blockers")
        substantive = {"economic_experience", "general_experience", "specific_experience", "key_personnel",
                       "equipment", "insurance", "license", "company_certification", "education", "training",
                       "professional_registration"}
        if not any(facet["required"] and facet["facet"] in substantive for facet in derive_facets(source["raw_extraction_json"])):
            raise Prod2PersistenceError("Generic administrative requirements cannot establish a final verdict")
        reservation = _one(cursor, "SELECT * FROM prod2_evaluations WHERE id=%s", (evaluation,))
        if not reservation or (reservation["id_convocatoria"], reservation["kind"], reservation["status"], str(reservation["profile_id"])) != (convocation, "gemini_match", "done", profile):
            raise Prod2PersistenceError("Final match has no completed profile reservation")
        binding = reservation["result_json"]
        if not isinstance(binding, dict) or (binding.get("extraction_id"), binding.get("profile_hash")) != (extraction, profile_hash):
            raise Prod2PersistenceError("Match reservation does not bind the current extraction/profile snapshot")
        if line is not None:
            valid_line = _one(cursor, "SELECT id FROM business_lines WHERE id=%s AND profile_id=%s", (line, profile))
            if not valid_line:
                raise Prod2PersistenceError("Business line belongs to a different profile")
        payload = {**breakdown_json, "meta": {**(breakdown_json.get("meta") or {}),
                   "source": "seace_prod2", "source_key": "seace_prod2", "id_convocatoria": convocation,
                   "extraction_id": extraction, "document_sha256": source["sha256_original"],
                   "prompt_version": reservation["policy_version"], "profile_hash": profile_hash,
                   "evaluation_id": evaluation}}
        row = _one(cursor, """INSERT INTO opportunity_matches(profile_id,opportunity_id,business_line_id,score,
            verdict,breakdown_json,missing_actions_json,matched_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,now(),now())
            ON CONFLICT(profile_id,opportunity_id) DO UPDATE SET business_line_id=EXCLUDED.business_line_id,
              score=EXCLUDED.score,verdict=EXCLUDED.verdict,breakdown_json=EXCLUDED.breakdown_json,
              missing_actions_json=EXCLUDED.missing_actions_json,matched_at=now(),updated_at=now() RETURNING id""",
            (profile, source["opportunity_id"], line, score, verdict, _json(payload), _json(missing_actions_json)))
        match_id = int(row["id"])
        cursor.execute("""INSERT INTO prod2_match_provenance(match_id,id_convocatoria,extraction_id,evaluation_id,profile_hash)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT(match_id) DO UPDATE SET id_convocatoria=EXCLUDED.id_convocatoria,
              extraction_id=EXCLUDED.extraction_id,evaluation_id=EXCLUDED.evaluation_id,
              profile_hash=EXCLUDED.profile_hash,matched_at=now()""", (match_id, convocation, extraction, evaluation, profile_hash))
    return {"match_id": match_id, "score": score, "verdict": verdict}
