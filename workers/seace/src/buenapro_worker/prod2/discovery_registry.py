"""Map-only PostgreSQL identity registry; no source, model, Redis or publication I/O.

Each parallel collector must supply its own autocommit connection. A transaction
locks the complete batch of IDs in deterministic advisory-key order, including
IDs not inserted yet. First run membership and query observations are durable;
True means first membership in this run, never permission for a paid call.

Listing hashes intentionally exclude query/page/observation time and the
redundant object_label. Historical IDs are read only from the three PROD2
ledgers below; their old, differently defined hashes are not comparison hashes.
Older captures remain observations but cannot replace newer listing metadata.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from psycopg.rows import dict_row

from .discovery import CaptureError, LIMA, validate_capture

CLASSES = ("new_id", "unchanged", "changed", "known_baseline_missing")
CONTENT_FIELDS = {"convocation_id", "procedure_id", "nomenclature", "buyer_name", "description",
                  "object_type", "published_at", "reference_amount", "currency", "seace_version"}
TABLE_COLUMNS = {
    "prod2_map_runs": {"run_id": "text", "manifest_hash": "text", "manifest_json": "jsonb",
                       "created_at": "timestamp with time zone"},
    "prod2_map_run_queries": {"run_id": "text", "query_hash": "text", "query_manifest": "jsonb"},
    "prod2_listing_registry": {"convocation_id": "bigint", "procedure_id": "bigint",
        "content_hash": "text", "listing_json": "jsonb", "first_observed_at": "timestamp with time zone",
        "observed_at": "timestamp with time zone"},
    "prod2_listing_observations": {"convocation_id": "bigint", "observed_at": "timestamp with time zone",
                                   "content_hash": "text", "listing_json": "jsonb"},
    "prod2_map_run_members": {"run_id": "text", "convocation_id": "bigint", "procedure_id": "bigint",
        "first_classification": "text", "first_content_hash": "text", "first_observed_at": "timestamp with time zone"},
    "prod2_map_query_observations": {"run_id": "text", "convocation_id": "bigint", "query_hash": "text",
        "observed_query_hash": "text", "observed_query": "jsonb", "observed_at": "timestamp with time zone",
        "content_hash": "text", "page_number": "integer", "is_duplicate": "boolean"},
}
BASELINE_COLUMNS = {
    "prod2_discoveries": {"id_convocatoria": "bigint", "id_procedimiento": "bigint"},
    "prod2_accepted_run_members": {"convocation_id": "bigint"},
    "prod2_jev_decision_cache": {"convocation_id": "bigint"},
}
BASELINE_INDEXES = {
    "prod2_jev_decision_cache_convocation_idx": "prod2_jev_decision_cache",
    "prod2_accepted_run_members_convocation_idx": "prod2_accepted_run_members",
}
# Named structural constraints are also checked by their actual key/target columns.
KEY_CONSTRAINTS = {
    "prod2_map_runs_pk": ("p", "prod2_map_runs", ["run_id"], None, []),
    "prod2_map_run_queries_pk": ("p", "prod2_map_run_queries", ["run_id", "query_hash"], None, []),
    "prod2_map_run_queries_run_fk": ("f", "prod2_map_run_queries", ["run_id"], "prod2_map_runs", ["run_id"]),
    "prod2_listing_registry_pk": ("p", "prod2_listing_registry", ["convocation_id"], None, []),
    "prod2_listing_registry_identity_uk": ("u", "prod2_listing_registry", ["convocation_id", "procedure_id"], None, []),
    "prod2_listing_observations_pk": ("p", "prod2_listing_observations", ["convocation_id", "observed_at"], None, []),
    "prod2_listing_observations_hash_uk": ("u", "prod2_listing_observations", ["convocation_id", "observed_at", "content_hash"], None, []),
    "prod2_listing_observations_id_fk": ("f", "prod2_listing_observations", ["convocation_id"], "prod2_listing_registry", ["convocation_id"]),
    "prod2_map_run_members_pk": ("p", "prod2_map_run_members", ["run_id", "convocation_id"], None, []),
    "prod2_map_run_members_run_fk": ("f", "prod2_map_run_members", ["run_id"], "prod2_map_runs", ["run_id"]),
    "prod2_map_run_members_identity_fk": ("f", "prod2_map_run_members", ["convocation_id", "procedure_id"], "prod2_listing_registry", ["convocation_id", "procedure_id"]),
    "prod2_map_run_members_observation_fk": ("f", "prod2_map_run_members", ["convocation_id", "first_observed_at", "first_content_hash"], "prod2_listing_observations", ["convocation_id", "observed_at", "content_hash"]),
    "prod2_map_query_observations_pk": ("p", "prod2_map_query_observations", ["run_id", "convocation_id", "query_hash", "observed_query_hash", "observed_at"], None, []),
    "prod2_map_query_observations_member_fk": ("f", "prod2_map_query_observations", ["run_id", "convocation_id"], "prod2_map_run_members", ["run_id", "convocation_id"]),
    "prod2_map_query_observations_query_fk": ("f", "prod2_map_query_observations", ["run_id", "query_hash"], "prod2_map_run_queries", ["run_id", "query_hash"]),
    "prod2_map_query_observations_observation_fk": ("f", "prod2_map_query_observations", ["convocation_id", "observed_at", "content_hash"], "prod2_listing_observations", ["convocation_id", "observed_at", "content_hash"]),
}
CHECK_CONSTRAINTS = {
    "prod2_map_runs_id_ck": ("prod2_map_runs", ["run_id", "119"]),
    "prod2_map_runs_hash_ck": ("prod2_map_runs", ["manifest_hash", "[0-9a-f]{64}"]),
    "prod2_map_runs_json_ck": ("prod2_map_runs", ["jsonb_typeof", "manifest_json", "object"]),
    "prod2_map_run_queries_hash_ck": ("prod2_map_run_queries", ["query_hash", "[0-9a-f]{64}"]),
    "prod2_map_run_queries_json_ck": ("prod2_map_run_queries", ["jsonb_typeof", "query_manifest", "object"]),
    "prod2_listing_registry_ids_ck": ("prod2_listing_registry", ["convocation_id > 0", "procedure_id > 0"]),
    "prod2_listing_registry_hash_ck": ("prod2_listing_registry", ["content_hash", "[0-9a-f]{64}"]),
    "prod2_listing_registry_json_ck": ("prod2_listing_registry", ["jsonb_typeof", "listing_json", "object"]),
    "prod2_listing_registry_time_ck": ("prod2_listing_registry", ["first_observed_at <= observed_at"]),
    "prod2_listing_observations_hash_ck": ("prod2_listing_observations", ["content_hash", "[0-9a-f]{64}"]),
    "prod2_listing_observations_json_ck": ("prod2_listing_observations", ["jsonb_typeof", "listing_json", "object"]),
    "prod2_map_run_members_class_ck": ("prod2_map_run_members", list(CLASSES)),
    "prod2_map_query_observations_hash_ck": ("prod2_map_query_observations", ["observed_query_hash", "[0-9a-f]{64}"]),
    "prod2_map_query_observations_json_ck": ("prod2_map_query_observations", ["jsonb_typeof", "observed_query", "object"]),
    "prod2_map_query_observations_page_ck": ("prod2_map_query_observations", ["page_number > 0"]),
}


class RegistryError(ValueError):
    """Sanitized integrity/input code; callers must retain the rejected source capture."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise RegistryError("invalid_json") from None


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _run_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value):
        raise RegistryError("invalid_run_id")
    return value


def _connection(conn) -> None:
    if conn.autocommit is not True:
        raise RegistryError("autocommit_connection_required")


def _query(value: Any) -> dict:
    try:
        return validate_capture({"query": value, "observed_at": datetime.now(timezone.utc).isoformat(),
            "page": {"number": 1, "total_pages": 1, "total_results": 0, "page_size": 1}, "rows": []}).query
    except (CaptureError, TypeError, ValueError):
        raise RegistryError("invalid_query") from None


def _manifest(run_id: str, value: Any) -> dict:
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise RegistryError("manifest_run_mismatch")
    manifest = json.loads(_json(value))
    queries = manifest.get("queries")
    if not isinstance(queries, list) or not queries:
        raise RegistryError("manifest_queries_required")
    normalized = [_query(query) for query in queries]
    if normalized != queries or len({_hash(query) for query in normalized}) != len(queries):
        raise RegistryError("manifest_queries_not_unique_canonical")
    return manifest


def normalize_entry(value: Any) -> dict:
    """Revalidate the canonical row emitted by keyword_sweep; drop no source content."""
    if not isinstance(value, dict) or set(value) != {"row", "query", "page_number", "observed_at"}:
        raise RegistryError("invalid_entry_fields")
    row, query = value["row"], _query(value["query"])
    if not isinstance(row, dict) or set(row) not in (CONTENT_FIELDS, CONTENT_FIELDS | {"object_label"}):
        raise RegistryError("invalid_listing_fields")
    if row["object_type"] != query["object_type"]:
        raise RegistryError("listing_object_mismatch")
    if type(value["page_number"]) is not int or not 1 <= value["page_number"] <= 2**31 - 1:
        raise RegistryError("invalid_page_number")
    try:
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        if published.tzinfo is None or published.utcoffset() is None or published.second or published.microsecond:
            raise ValueError
        label = "Bien" if query["object_type"] == "good" else "Servicio"
        if row.get("object_label", label) != label:
            raise ValueError
        raw = {key: item for key, item in row.items() if key != "object_type"}
        raw.update(object_label=label, published_at=published.astimezone(LIMA).strftime("%d/%m/%Y %H:%M"),
                   reference_amount="Reservado" if row["reference_amount"] is None else row["reference_amount"])
        capture = validate_capture({"query": query, "observed_at": value["observed_at"],
            "page": {"number": 1, "total_pages": 1, "total_results": 1, "page_size": 1}, "rows": [raw]})
    except (CaptureError, TypeError, ValueError, AttributeError):
        raise RegistryError("invalid_canonical_listing") from None
    canonical = {key: item for key, item in capture.rows[0].items() if key in CONTENT_FIELDS}
    canonical["published_at"] = published.astimezone(timezone.utc).isoformat()
    return {"row": canonical, "content_hash": _hash(canonical), "query": query,
        "observed_query_hash": _hash(query), "page_number": value["page_number"],
        "observed_at": capture.observed_at}


def classify(prior: dict | None, content_hash: str, *, baseline_known: bool) -> str:
    if prior is not None:
        return "unchanged" if prior["content_hash"] == content_hash else "changed"
    return "known_baseline_missing" if baseline_known else "new_id"


def _root_query(query: dict, roots: list[dict]) -> dict:
    identity = ("keyword", "year", "object_type", "seace_version")
    candidates = [root for root in roots if all(root[key] == query[key] for key in identity)
        and root["published_from"] <= query["published_from"] <= query["published_to"] <= root["published_to"]]
    if len(candidates) != 1:
        raise RegistryError("query_not_frozen_or_ambiguous")
    return candidates[0]


def require_schema(conn) -> dict:
    """Read-only catalog check in current_schema; never execute or prepare DDL."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT c.relname AS table_name,a.attname AS column_name,
            format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull AS not_null
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace=current_schema()::regnamespace AND c.relname=ANY(%s)
              AND a.attnum>0 AND NOT a.attisdropped""", (list(TABLE_COLUMNS) + list(BASELINE_COLUMNS),))
        columns = cursor.fetchall()
        actual = {name: {} for name in TABLE_COLUMNS}
        baseline = {name: {} for name in BASELINE_COLUMNS}
        for row in columns:
            if row["table_name"] in BASELINE_COLUMNS:
                if row["column_name"] in BASELINE_COLUMNS[row["table_name"]]:
                    baseline[row["table_name"]][row["column_name"]] = row["data_type"]
                continue
            if not row["not_null"]:
                raise RegistryError("registry_schema_nullable_column")
            actual[row["table_name"]][row["column_name"]] = row["data_type"]
        if actual != TABLE_COLUMNS:
            raise RegistryError("registry_schema_columns_missing_or_incompatible")
        if baseline != BASELINE_COLUMNS:
            raise RegistryError("historical_baseline_schema_missing_or_incompatible")
        cursor.execute("""SELECT c.conname,c.contype,own.relname AS table_name,
            target.relname AS target_table,c.convalidated,
            ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n,pos)
              JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.n ORDER BY k.pos) AS columns,
            ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(n,pos)
              JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.n ORDER BY k.pos) AS target_columns,
            pg_get_constraintdef(c.oid) AS definition,
            (c.contype<>'f' OR target.relnamespace=own.relnamespace) AS local_target
            FROM pg_constraint c JOIN pg_class own ON own.oid=c.conrelid
            LEFT JOIN pg_class target ON target.oid=c.confrelid
            WHERE own.relnamespace=current_schema()::regnamespace AND own.relname=ANY(%s)""",
            (list(TABLE_COLUMNS),))
        constraints = {row["conname"]: row for row in cursor.fetchall()}
        for name, expected in KEY_CONSTRAINTS.items():
            row = constraints.get(name)
            if (not row or not row["convalidated"] or not row["local_target"] or
                    (row["contype"], row["table_name"], row["columns"], row["target_table"], row["target_columns"]) != expected):
                raise RegistryError("registry_schema_identity_constraint_incompatible")
        for name, (table, fragments) in CHECK_CONSTRAINTS.items():
            row = constraints.get(name)
            if (not row or row["contype"] != "c" or row["table_name"] != table
                    or not row["convalidated"] or any(fragment not in row["definition"] for fragment in fragments)):
                raise RegistryError("registry_schema_check_constraint_incompatible")
        # IF NOT EXISTS only proves a name is occupied, not that it is this index.
        cursor.execute("""SELECT idx.relname AS index_name,own.relname AS table_name,
            (idx.relnamespace=current_schema()::regnamespace) AS local_index,
            (own.relnamespace=current_schema()::regnamespace) AS local_table,
            am.amname AS access_method,i.indnkeyatts AS key_count,i.indnatts AS attribute_count,
            i.indisvalid,i.indisready,i.indisunique,
            (i.indexprs IS NULL) AS plain_index,(i.indpred IS NULL) AS unfiltered,
            ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(n,pos)
              JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.n ORDER BY k.pos) AS columns
            FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
            JOIN pg_class own ON own.oid=i.indrelid JOIN pg_am am ON am.oid=idx.relam
            WHERE idx.relnamespace=current_schema()::regnamespace AND idx.relname=ANY(%s)""",
            (list(BASELINE_INDEXES),))
        indexes = {row["index_name"]: row for row in cursor.fetchall()}
        for name, table in BASELINE_INDEXES.items():
            row = indexes.get(name)
            if (not row or row["table_name"] != table or not row["local_index"] or not row["local_table"]
                    or row["access_method"] != "btree" or row["columns"] != ["convocation_id"]
                    or row["key_count"] != 1 or row["attribute_count"] != 1 or row["indisunique"]
                    or not all(row[key] for key in ("indisvalid", "indisready", "plain_index", "unfiltered"))):
                raise RegistryError("historical_baseline_index_missing_or_incompatible")
    return {"status": "discovery_registry_schema_ready", "tables": sorted(TABLE_COLUMNS),
            "historical_baseline_tables": sorted(BASELINE_COLUMNS)}


def freeze_run(conn, run_id: str, manifest: dict) -> None:
    run_id = _run_id(run_id)
    manifest = _manifest(run_id, manifest)
    _connection(conn)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_map_manifest'),hashtext(%s))", (run_id,))
        cursor.execute("""INSERT INTO prod2_map_runs(run_id,manifest_hash,manifest_json)
            VALUES (%s,%s,%s::jsonb) ON CONFLICT (run_id) DO NOTHING""", (run_id, _hash(manifest), _json(manifest)))
        cursor.execute("SELECT * FROM prod2_map_runs WHERE run_id=%s", (run_id,))
        saved = cursor.fetchone()
        if saved["manifest_hash"] != _hash(manifest) or saved["manifest_json"] != manifest:
            raise RegistryError("frozen_manifest_changed")
        cursor.executemany("""INSERT INTO prod2_map_run_queries(run_id,query_hash,query_manifest)
            VALUES (%s,%s,%s::jsonb) ON CONFLICT (run_id,query_hash) DO NOTHING""",
            [(run_id, _hash(query), _json(query)) for query in manifest["queries"]])
        cursor.execute("SELECT query_hash,query_manifest FROM prod2_map_run_queries WHERE run_id=%s", (run_id,))
        if {row["query_hash"]: row["query_manifest"] for row in cursor.fetchall()} != {
                _hash(query): query for query in manifest["queries"]}:
            raise RegistryError("frozen_queries_changed")


def observe_many(conn, run_id: str, entries: list[dict]) -> list[bool]:
    run_id = _run_id(run_id)
    if not isinstance(entries, list) or len(entries) > 100:
        raise RegistryError("batch_requires_at_most_100_entries")
    prepared = [normalize_entry(entry) for entry in entries]
    _connection(conn)
    if not prepared:
        return []
    ids = sorted({item["row"]["convocation_id"] for item in prepared})
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT manifest_hash,manifest_json FROM prod2_map_runs WHERE run_id=%s", (run_id,))
        saved = cursor.fetchone()
        if not saved or saved["manifest_hash"] != _hash(saved["manifest_json"]):
            raise RegistryError("run_not_frozen_or_corrupt")
        manifest = _manifest(run_id, saved["manifest_json"])
        for item in prepared:
            item["query_hash"] = _hash(_root_query(item["query"], manifest["queries"]))
        # Hash collisions serialize safely: sort actual lock keys, not only ID input.
        cursor.execute("""SELECT pg_advisory_xact_lock(hashtext('prod2_discovery_registry'),lock_key)
            FROM (SELECT DISTINCT hashtext(id::text) AS lock_key FROM unnest(%s::bigint[]) AS ids(id)
                  ORDER BY lock_key) AS ordered_locks""", (ids,))
        cursor.execute("SELECT * FROM prod2_listing_registry WHERE convocation_id=ANY(%s) ORDER BY convocation_id FOR UPDATE", (ids,))
        prior = {row["convocation_id"]: row for row in cursor.fetchall()}
        cursor.execute("""SELECT convocation_id,max(procedure_id) AS procedure_id FROM (
            SELECT id_convocatoria AS convocation_id,id_procedimiento AS procedure_id
              FROM prod2_discoveries WHERE id_convocatoria=ANY(%s)
            UNION ALL SELECT convocation_id,NULL::bigint FROM prod2_accepted_run_members WHERE convocation_id=ANY(%s)
            UNION ALL SELECT convocation_id,NULL::bigint FROM prod2_jev_decision_cache WHERE convocation_id=ANY(%s)
            ) AS historical GROUP BY convocation_id""", (ids, ids, ids))
        baseline = cursor.fetchall()
        known_ids = {row["convocation_id"] for row in baseline}
        procedures = {row["convocation_id"]: row["procedure_id"] for row in baseline if row["procedure_id"] is not None}
        cursor.execute("SELECT * FROM prod2_map_run_members WHERE run_id=%s AND convocation_id=ANY(%s)", (run_id, ids))
        members = {row["convocation_id"]: row for row in cursor.fetchall()}
        instants = {(item["row"]["convocation_id"], item["observed_at"]) for item in prepared}
        cursor.execute("""SELECT o.* FROM prod2_listing_observations o
            JOIN jsonb_to_recordset(%s::jsonb) AS x(convocation_id bigint,observed_at timestamptz)
              USING(convocation_id,observed_at)""", (_json([{"convocation_id": convocation,
                  "observed_at": observed.isoformat()} for convocation, observed in sorted(instants)]),))
        observations = {(row["convocation_id"], row["observed_at"]): row["content_hash"] for row in cursor.fetchall()}
        registry_rows, observation_rows, membership_rows, first_indices = {}, {}, {}, {}
        for index, item in enumerate(prepared):
            row, digest, observed = item["row"], item["content_hash"], item["observed_at"]
            convocation, procedure = row["convocation_id"], row["procedure_id"]
            for previous in (prior.get(convocation), members.get(convocation), registry_rows.get(convocation)):
                if previous and previous["procedure_id"] != procedure:
                    raise RegistryError("convocation_procedure_conflict")
            if convocation in procedures and procedures[convocation] != procedure:
                raise RegistryError("convocation_procedure_conflict")
            identity = (convocation, observed)
            if identity in observations and observations[identity] != digest:
                raise RegistryError("observation_timestamp_conflict")
            previous = prior.get(convocation)
            if previous and previous["observed_at"] == observed and previous["content_hash"] != digest:
                raise RegistryError("observation_timestamp_conflict")
            observations[identity] = digest
            observation_rows[identity] = {"convocation_id": convocation, "observed_at": observed.isoformat(),
                                          "content_hash": digest, "listing_json": row}
            if convocation not in members and convocation not in membership_rows:
                first_indices[convocation] = index
                membership_rows[convocation] = {"run_id": run_id, "convocation_id": convocation,
                    "procedure_id": procedure, "first_classification": classify(previous, digest, baseline_known=convocation in known_ids),
                    "first_content_hash": digest, "first_observed_at": observed.isoformat()}
            current = registry_rows.get(convocation)
            earliest = min(observed, datetime.fromisoformat(current["first_observed_at"])) if current else observed
            if current is None or observed > datetime.fromisoformat(current["observed_at"]):
                registry_rows[convocation] = {"convocation_id": convocation, "procedure_id": procedure,
                    "content_hash": digest, "listing_json": row, "first_observed_at": earliest.isoformat(), "observed_at": observed.isoformat()}
            else:
                current["first_observed_at"] = earliest.isoformat()
        # Four bounded bulk writes; each ID conflict is checked before mutation.
        cursor.execute("""INSERT INTO prod2_listing_registry AS existing
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(convocation_id bigint,procedure_id bigint,
              content_hash text,listing_json jsonb,first_observed_at timestamptz,observed_at timestamptz)
            ORDER BY convocation_id ON CONFLICT (convocation_id) DO UPDATE SET
              content_hash=CASE WHEN EXCLUDED.observed_at>existing.observed_at THEN EXCLUDED.content_hash ELSE existing.content_hash END,
              listing_json=CASE WHEN EXCLUDED.observed_at>existing.observed_at THEN EXCLUDED.listing_json ELSE existing.listing_json END,
              observed_at=GREATEST(existing.observed_at,EXCLUDED.observed_at),
              first_observed_at=LEAST(existing.first_observed_at,EXCLUDED.first_observed_at)
            WHERE existing.procedure_id=EXCLUDED.procedure_id""", (_json([registry_rows[key] for key in ids]),))
        cursor.execute("""INSERT INTO prod2_listing_observations
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(convocation_id bigint,observed_at timestamptz,
              content_hash text,listing_json jsonb)
            ORDER BY convocation_id,observed_at ON CONFLICT (convocation_id,observed_at) DO NOTHING""",
            (_json(list(observation_rows.values())),))
        cursor.execute("""INSERT INTO prod2_map_run_members
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(run_id text,convocation_id bigint,procedure_id bigint,
              first_classification text,first_content_hash text,first_observed_at timestamptz)
            ORDER BY convocation_id ON CONFLICT (run_id,convocation_id) DO NOTHING RETURNING convocation_id""",
            (_json([membership_rows[key] for key in sorted(membership_rows)]),))
        inserted = {row["convocation_id"] for row in cursor.fetchall()}
        flags = [item["row"]["convocation_id"] in inserted and first_indices[item["row"]["convocation_id"]] == index
                 for index, item in enumerate(prepared)]
        associations = {}
        for index, item in enumerate(prepared):
            association = {"run_id": run_id, "convocation_id": item["row"]["convocation_id"],
                "query_hash": item["query_hash"], "observed_query_hash": item["observed_query_hash"],
                "observed_query": item["query"], "observed_at": item["observed_at"].isoformat(),
                "content_hash": item["content_hash"], "page_number": item["page_number"], "is_duplicate": not flags[index]}
            identity = tuple(association[key] for key in (
                "run_id", "convocation_id", "query_hash", "observed_query_hash", "observed_at"))
            associations.setdefault(identity, association)
        cursor.execute("""INSERT INTO prod2_map_query_observations
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(run_id text,convocation_id bigint,query_hash text,
              observed_query_hash text,observed_query jsonb,observed_at timestamptz,content_hash text,page_number integer,is_duplicate boolean)
            ORDER BY convocation_id,observed_at ON CONFLICT DO NOTHING""", (_json(list(associations.values())),))
    return flags


def read_report(conn, run_id: str) -> dict:
    """Read a consistent map-only snapshot; not proof that source coverage is complete."""
    run_id = _run_id(run_id)
    _connection(conn)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SELECT manifest_hash,manifest_json FROM prod2_map_runs WHERE run_id=%s", (run_id,))
        saved = cursor.fetchone()
        if not saved or saved["manifest_hash"] != _hash(saved["manifest_json"]):
            raise RegistryError("run_not_frozen_or_corrupt")
        cursor.execute("SELECT * FROM prod2_map_run_members WHERE run_id=%s ORDER BY convocation_id", (run_id,))
        members = cursor.fetchall()
        cursor.execute("""SELECT o.*,m.first_classification,h.listing_json,q.query_manifest AS root_query,
            r.observed_at AS registry_observed_at,(o.observed_at<r.observed_at) AS older_than_registry
            FROM prod2_map_query_observations o JOIN prod2_map_run_members m USING(run_id,convocation_id)
            JOIN prod2_map_run_queries q USING(run_id,query_hash)
            JOIN prod2_listing_observations h USING(convocation_id,observed_at,content_hash)
            JOIN prod2_listing_registry r USING(convocation_id)
            WHERE o.run_id=%s ORDER BY o.convocation_id,o.query_hash,o.observed_at,o.observed_query_hash""", (run_id,))
        observations = cursor.fetchall()
    by_id = {}
    for observation in observations:
        by_id.setdefault(observation["convocation_id"], []).append(observation)
    for member in members:
        observed = by_id.get(member["convocation_id"], [])
        if not observed:
            raise RegistryError("membership_observation_missing")
        latest = max(observed, key=lambda row: row["observed_at"])
        member.update(observed_hash_count=len({row["content_hash"] for row in observed}),
                      latest_listing_json=latest["listing_json"], latest_content_hash=latest["content_hash"],
                      latest_observed_at=latest["observed_at"],
                      needs_review=member["first_classification"] == "known_baseline_missing")
    changed_during_run = sorted(member["convocation_id"] for member in members if member["observed_hash_count"] > 1)
    candidate_ids = sorted({member["convocation_id"] for member in members
        if member["first_classification"] != "unchanged" or member["observed_hash_count"] > 1})
    members = [{key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
               for row in members]
    observations = [{key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
                    for row in observations]
    counts = Counter(row["first_classification"] for row in members)
    return {"run_id": run_id, "manifest_hash": saved["manifest_hash"], "manifest": saved["manifest_json"],
        "counts": {kind: counts[kind] for kind in CLASSES}, "total_ids": len(members),
        "ids": {kind: [row["convocation_id"] for row in members if row["first_classification"] == kind] for kind in CLASSES},
        "members": members, "observations": observations, "observation_count": len(observations),
        "changed_during_run_ids": changed_during_run, "candidate_ids": candidate_ids,
        "needs_review_ids": sorted(row["convocation_id"] for row in members if row["needs_review"]),
        "duplicate_associations": sum(row["is_duplicate"] for row in observations),
        "older_observations": sum(row["older_than_registry"] for row in observations),
        "source_coverage_complete": False, "published_opportunities": 0, "provider_calls": 0,
        "requires_human_go": True, "automatic_paid_authorization": False}
