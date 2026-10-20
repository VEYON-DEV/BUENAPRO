"""Publish exactly a frozen selected metadata scope, without source/model calls.

The read-only prepare phase binds original captures, all case outcomes, and
existing canonical identities. Publication is one PostgreSQL transaction using
the same identity advisory locks as the source reader. Existing discoveries,
details, schedules, extractions, matches, lifecycle and source-native JSON are
never overwritten. A separate sidecar explicitly opts metadata into the feed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .handoff_selection import load_selection

VERSION = "prod2_selected_metadata_publication_v1"
SCOPE = "prod2-din-ia-industrial-20261020"
SELECTION_SHA = "5a3fd548248ee719d616f9bc1994dcdc1edebade84c234911d3c14d170e6f3db"
HANDOFF_SHA = "45e6ee68c1b3cdb2350a77840a9ba6dd2c5c71d28c7009df3fdaa95849ad5575"
COUNT = 157
LOCK_KEY = 7820659504701
MIGRATION = "0047_prod2_feed_publications.sql"
STATUSES = {"closed_without_reading", "rejected_below_60", "opening_not_confirmed", "needs_review",
            "selection_needs_review", "selection_complete", "llm1_complete", "documents_missing",
            "failed", "retained_verification_failed", "unknown"}


class PublicationError(ValueError):
    """Controlled integrity failure; no credentials or opaque source data."""


def frozen(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {key: frozen(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [frozen(item) for item in value]
    return value


def encoded(value):
    return json.dumps(frozen(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PublicationError("Missing or unsafe frozen publication input")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bound(path, expected_sha=None):
    actual = file_sha(path)
    if expected_sha is not None and actual != expected_sha:
        raise PublicationError("Frozen publication input hash changed")
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise PublicationError("Expected a publication JSON object")
    return value


def immutable_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    data = encoded(value)
    if path.exists():
        if path.read_bytes() != data:
            raise PublicationError("Immutable publication evidence conflicts")
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def source_inputs(root, handoff, selection, reader_folder):
    selected = load_selection(handoff, selection, SELECTION_SHA, root=root)
    binding = selected["binding"]
    if (binding["scope_id"] != SCOPE or binding["handoff_sha256"] != HANDOFF_SHA
            or len(selected["items"]) != COUNT):
        raise PublicationError("Publication is restricted to the exact authorized 157-ID selection")
    inputs, cases = [{"path": str(Path(selection).resolve()), "sha256": SELECTION_SHA},
                     {"path": str(Path(handoff).resolve()), "sha256": HANDOFF_SHA}], []
    for item in selected["items"]:
        row, provenance = item["row"], item["handoff_provenance"]
        conv = row["convocation_id"]
        path = Path(reader_folder).resolve() / str(conv) / "outcome.private.json"
        checksum = file_sha(path)
        outcome = read_bound(path, checksum)
        observed = outcome.get("handoff_provenance", {})
        status = outcome.get("status")
        if (outcome.get("convocation_id") != conv or outcome.get("procedure_id") != row["procedure_id"]
                or outcome.get("description") != row["description"] or outcome.get("object_type") != row["object_type"]
                or observed.get("selection_sha256") != SELECTION_SHA
                or observed.get("listing_content_hash") != provenance["listing_content_hash"]
                or status not in STATUSES or type(outcome.get("accepted")) is not bool):
            raise PublicationError("Reader outcome does not bind the selected source identity")
        if status in {"closed_without_reading", "rejected_below_60", "opening_not_confirmed"} and outcome["accepted"]:
            raise PublicationError("Rejected or closed source cannot be marked accepted")
        inputs.append({"path": str(path), "sha256": checksum})
        # The actual outcome remains a hashed input; no synthetic eligibility is inferred.
        cases.append({"listing": row, "observed_at": item["observed_at"],
            "registry_content_hash": provenance["listing_content_hash"], "reader_outcome_sha256": checksum,
            "reader_status": status, "accepted": outcome["accepted"]})
    return cases, inputs


def snapshot(conn, cases, *, lock=False):
    ids = [case["listing"]["convocation_id"] for case in cases]
    procedures = [case["listing"]["procedure_id"] for case in cases]
    tail = " ORDER BY id_convocatoria FOR UPDATE" if lock else ""
    discoveries = conn.execute("SELECT * FROM prod2_discoveries WHERE id_convocatoria=ANY(%s) OR id_procedimiento=ANY(%s)" + tail,
                               (ids, procedures)).fetchall()
    sources = conn.execute("SELECT * FROM opportunity_sources WHERE "
        "(source_key IN ('seace_prod2','seace_prod4') AND id_kind='id_procedimiento' AND external_id=ANY(%s)) OR "
        "(source_key='seace_prod2' AND id_kind='id_convocatoria' AND external_id=ANY(%s))",
        ([str(value) for value in procedures], [str(value) for value in ids])).fetchall()
    legacy = conn.execute("SELECT id_procedimiento,id_convocatoria_pub,opportunity_id,object_type "
                          "FROM prod4_processes WHERE id_procedimiento=ANY(%s)", (procedures,)).fetchall()
    canonical_ids = list({str(row["opportunity_id"]) for row in discoveries + sources + legacy})
    opportunities = conn.execute("SELECT * FROM opportunities WHERE id=ANY(%s::uuid[])", (canonical_ids,)).fetchall()
    registry = conn.execute("SELECT * FROM prod2_listing_registry WHERE convocation_id=ANY(%s)" +
        (" ORDER BY convocation_id FOR SHARE" if lock else ""), (ids,)).fetchall()
    ready = conn.execute("SELECT to_regclass('prod2_feed_publications') AS name").fetchone()["name"]
    publications = conn.execute("SELECT * FROM prod2_feed_publications WHERE id_convocatoria=ANY(%s)",
                                (ids,)).fetchall() if ready else []
    return frozen({"discoveries": discoveries, "sources": sources, "legacy": legacy,
                   "opportunities": opportunities, "registry": registry, "publications": publications})


def identity(case, state):
    row = case["listing"]
    conv, procedure, obj = row["convocation_id"], row["procedure_id"], row["object_type"]
    identities = set()
    for prior in state["discoveries"]:
        if prior["id_convocatoria"] == conv or prior["id_procedimiento"] == procedure:
            if prior["id_procedimiento"] != procedure or prior["object_type"] != obj:
                raise PublicationError("Discovery identity contradicts selected procedure/object")
            identities.add(prior["opportunity_id"])
    for prior in state["sources"]:
        relevant = ((prior["id_kind"] == "id_procedimiento" and prior["external_id"] == str(procedure))
                    or (prior["source_key"] == "seace_prod2" and prior["id_kind"] == "id_convocatoria"
                        and prior["external_id"] == str(conv)))
        if relevant:
            identities.add(prior["opportunity_id"])
    for prior in state["legacy"]:
        if prior["id_procedimiento"] == procedure:
            if prior["object_type"] != obj or prior["id_convocatoria_pub"] not in (None, conv):
                raise PublicationError("Legacy source contradicts selected procedure/object")
            identities.add(prior["opportunity_id"])
    if len(identities) > 1:
        raise PublicationError("Selected source has conflicting canonical UUID identities")
    value = next(iter(identities)) if identities else None
    if value:
        matches = [prior for prior in state["opportunities"] if prior["id"] == value]
        if len(matches) != 1 or matches[0]["object_type"] not in (obj, "unknown"):
            raise PublicationError("Canonical UUID object identity is missing or contradictory")
    return value


def discovery_row(case, opportunity_id):
    row = case["listing"]
    return {"id_convocatoria": row["convocation_id"], "id_procedimiento": row["procedure_id"],
        "opportunity_id": opportunity_id, "nomenclature": row["nomenclature"], "buyer_name": row["buyer_name"],
        "description": row["description"], "object_type": row["object_type"], "object_label": row["object_label"],
        "published_at": frozen(datetime.fromisoformat(row["published_at"])),
        "reference_amount": row["reference_amount"], "currency": row["currency"],
        "seace_version": "3", "row_hash": digest(row),
        "first_seen_at": frozen(datetime.fromisoformat(case["observed_at"])),
        "last_seen_at": frozen(datetime.fromisoformat(case["observed_at"]))}


def registry_row(row):
    # Registry hashes deliberately remove object_label and normalize time to UTC.
    normalized = {key: value for key, value in row.items() if key != "object_label"}
    normalized["published_at"] = frozen(datetime.fromisoformat(row["published_at"]))
    return normalized


def build_plan(cases, state, inputs, run_id, *, runtime_bindings=None):
    if len(cases) != COUNT or len({case["listing"]["convocation_id"] for case in cases}) != COUNT:
        raise PublicationError("Publication requires the exact 157 distinct selected IDs")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", run_id):
        raise PublicationError("Invalid publication run ID")
    registry = {row["convocation_id"]: row for row in state["registry"]}
    old = {row["id_convocatoria"]: row for row in state["discoveries"]}
    publications = {row["id_convocatoria"]: row for row in state["publications"]}
    planned = []
    for case in cases:
        row = case["listing"]
        normalized = registry_row(row)
        saved = registry.get(row["convocation_id"])
        if (saved is None or saved["procedure_id"] != row["procedure_id"]
                or saved["content_hash"] != case["registry_content_hash"]
                or saved["listing_json"] != normalized or digest(normalized) != case["registry_content_hash"]):
            raise PublicationError("Registry no longer equals the exact frozen selected listing")
        existing = old.get(row["convocation_id"])
        known = identity(case, state)
        opportunity_id = known or str(uuid4())
        expected = existing or discovery_row(case, opportunity_id)
        planned.append({**case, "opportunity_id": opportunity_id, "canonical_preexisting": known is not None,
            "existing_discovery": existing, "expected_discovery": expected,
            "prior_publication": publications.get(row["convocation_id"]),
            "source_row_hash": expected["row_hash"]})
    return {"version": VERSION, "scope_id": SCOPE, "selection_sha256": SELECTION_SHA,
        "handoff_sha256": HANDOFF_SHA, "publication_run_id": run_id,
        "prepared_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs,
        "runtime_bindings": runtime_bindings or {}, "cases": planned,
        "new_discoveries": sum(case["existing_discovery"] is None for case in planned),
        "preserved_discoveries": sum(case["existing_discovery"] is not None for case in planned),
        "model_calls": 0, "source_calls": 0, "no_ficha_or_reading_fabricated": True}


def validate_plan(plan, expected_sha, *, root=None):
    if (digest(plan) != expected_sha or plan.get("version") != VERSION or plan.get("scope_id") != SCOPE
            or plan.get("selection_sha256") != SELECTION_SHA or plan.get("handoff_sha256") != HANDOFF_SHA
            or len(plan.get("cases", [])) != COUNT):
        raise PublicationError("Frozen publication plan identity changed")
    bound_inputs = {}
    for ref in plan["inputs"]:
        if file_sha(ref["path"]) != ref["sha256"]:
            raise PublicationError("Reader ledger or frozen handoff input changed")
        bound_inputs[ref["sha256"]] = read_bound(ref["path"], ref["sha256"])
    selection = bound_inputs.get(SELECTION_SHA, {})
    selected = {row["convocation_id"]: row for row in selection.get("candidates", [])}
    if len(selected) != COUNT or selection.get("scope_id") != SCOPE or HANDOFF_SHA not in bound_inputs:
        raise PublicationError("Publication input bindings omit the exact selected scope")
    ids = set()
    for case in plan["cases"]:
        row = case["listing"]
        if row["convocation_id"] in ids or case["reader_status"] not in STATUSES:
            raise PublicationError("Publication contains duplicate IDs or unknown outcome status")
        ids.add(row["convocation_id"])
        chosen = selected.get(row["convocation_id"], {})
        outcome = bound_inputs.get(case["reader_outcome_sha256"], {})
        if (chosen.get("procedure_id") != row["procedure_id"]
                or chosen.get("listing_content_hash") != case["registry_content_hash"]
                or chosen.get("title_sha256") != hashlib.sha256(row["description"].encode()).hexdigest()
                or outcome.get("convocation_id") != row["convocation_id"]
                or outcome.get("procedure_id") != row["procedure_id"]
                or outcome.get("description") != row["description"]
                or outcome.get("status") != case["reader_status"]
                or outcome.get("accepted") is not case["accepted"]):
            raise PublicationError("Publication row/status differs from the exact frozen selection/reader ledger")
        expected = case["existing_discovery"] or discovery_row(case, case["opportunity_id"])
        if expected != case["expected_discovery"] or expected["row_hash"] != case["source_row_hash"]:
            raise PublicationError("Publication discovery snapshot or row hash changed")
        UUID(case["opportunity_id"])
    if ids != set(selected):
        raise PublicationError("Publication case IDs differ from the selected scope")
    if root is not None:
        for relative, checksum in plan["runtime_bindings"].items():
            if file_sha(Path(root) / relative) != checksum:
                raise PublicationError("Publication-specific runtime binding changed")


def apply_schema(conn, migration):
    migration = Path(migration)
    if migration.name != MIGRATION:
        raise PublicationError("Only dedicated migration 0047 is permitted")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        # Existing production installations already own the migration ledger.
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = conn.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,)).fetchone()
        ready = conn.execute("SELECT to_regclass('prod2_feed_publications') AS name").fetchone()["name"]
        if bool(applied) != bool(ready):
            raise PublicationError("Publication schema and migration ledger disagree")
        if not applied:
            conn.execute(migration.read_text())
            conn.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (MIGRATION,))
        conn.execute("SELECT id_convocatoria,publication_run_id,source_row_hash,selection_sha256,"
                     "reader_outcome_sha256,registry_content_hash,reader_status,accepted,published_at "
                     "FROM prod2_feed_publications LIMIT 0")
    return {"migration": MIGRATION, "migration_sha256": file_sha(migration), "applied": not bool(applied)}


def marker(case, plan):
    return {"id_convocatoria": case["listing"]["convocation_id"], "publication_run_id": plan["publication_run_id"],
        "source_row_hash": case["source_row_hash"], "selection_sha256": SELECTION_SHA,
        "reader_outcome_sha256": case["reader_outcome_sha256"], "registry_content_hash": case["registry_content_hash"],
        "reader_status": case["reader_status"], "accepted": case["accepted"]}


def publish(conn, plan, expected_sha, *, root=None):
    validate_plan(plan, expected_sha, root=root)
    with conn.transaction():
        executor = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext('prod2_accepted_executor'),0) AS locked").fetchone()
        if not executor["locked"]:
            raise PublicationError("A selected reader owns the executor lock; metadata publication must wait")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        for namespace, values in (("seace_prod4", [str(c["listing"]["procedure_id"]) for c in plan["cases"]]),
                                  ("seace_prod2_convocation", [str(c["listing"]["convocation_id"]) for c in plan["cases"]])):
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s),hashtext(v)) "
                "FROM (SELECT unnest(%s::text[]) AS v ORDER BY 1) ordered", (namespace, values))
        state = snapshot(conn, plan["cases"], lock=True)
        current = {row["id_convocatoria"]: row for row in state["discoveries"]}
        registries = {row["convocation_id"]: row for row in state["registry"]}
        publications = {row["id_convocatoria"]: row for row in state["publications"]}
        creates, new_opportunities, aliases, markers = [], [], [], []
        replayed = 0
        for case in plan["cases"]:
            row = case["listing"]
            conv = row["convocation_id"]
            registry = registries.get(conv)
            if (registry is None or registry["content_hash"] != case["registry_content_hash"]
                    or registry["listing_json"] != registry_row(row)):
                raise PublicationError("Registry changed before metadata publication")
            existing = current.get(conv)
            if existing is not None and existing != case["expected_discovery"]:
                raise PublicationError("Existing discovery changed; metadata publication never overwrites it")
            if existing is None and case["existing_discovery"] is not None:
                raise PublicationError("Preserved discovery disappeared before publication")
            known = identity(case, state)
            if known is not None and known != case["opportunity_id"]:
                raise PublicationError("Canonical identity changed before publication")
            if known is None:
                if case["canonical_preexisting"]:
                    raise PublicationError("Previously existing canonical identity disappeared")
                new_opportunities.append({"id": case["opportunity_id"], "object_type": row["object_type"]})
            desired = marker(case, plan)
            previous = publications.get(conv)
            if previous is not None and {k: v for k, v in previous.items() if k != "published_at"} == desired:
                if existing is None:
                    raise PublicationError("Publication marker exists without its discovery")
                replayed += 1
            elif previous != case["prior_publication"]:
                raise PublicationError("Opt-in publication marker changed before compare-and-swap")
            else:
                markers.append(desired)
            if existing is None:
                creates.append(case["expected_discovery"])
            for kind, external_id in (("id_procedimiento", row["procedure_id"]), ("id_convocatoria", conv)):
                aliases.append({"opportunity_id": case["opportunity_id"], "id_kind": kind, "external_id": str(external_id),
                    "native_values": {"id_procedimiento": row["procedure_id"], "id_convocatoria": conv, "seace_version": "3"}})
        if new_opportunities:
            conn.execute("INSERT INTO opportunities(id,record_kind,object_type,procurement_method,lifecycle_stage,participation_access,actionability) "
                "SELECT x.id,'opportunity',x.object_type,'selection_procedure','unknown','unknown','unknown' "
                "FROM jsonb_to_recordset(%s) AS x(id UUID,object_type TEXT)", (Jsonb(new_opportunities),))
        conn.execute("INSERT INTO opportunity_sources(opportunity_id,source_key,id_kind,external_id,native_values) "
            "SELECT x.opportunity_id,'seace_prod2',x.id_kind,x.external_id,x.native_values FROM jsonb_to_recordset(%s) "
            "AS x(opportunity_id UUID,id_kind TEXT,external_id TEXT,native_values JSONB) "
            "ON CONFLICT(source_key,id_kind,external_id) DO NOTHING", (Jsonb(aliases),))
        if creates:
            conn.execute("""INSERT INTO prod2_discoveries(id_convocatoria,id_procedimiento,opportunity_id,
                nomenclature,buyer_name,description,object_type,object_label,published_at,reference_amount,
                currency,seace_version,row_hash,first_seen_at,last_seen_at)
                SELECT x.id_convocatoria,x.id_procedimiento,x.opportunity_id,x.nomenclature,x.buyer_name,
                    x.description,x.object_type,x.object_label,x.published_at,x.reference_amount,x.currency,
                    x.seace_version,x.row_hash,x.first_seen_at,x.last_seen_at
                FROM jsonb_to_recordset(%s) AS x(id_convocatoria BIGINT,id_procedimiento BIGINT,opportunity_id UUID,
                    nomenclature TEXT,buyer_name TEXT,description TEXT,object_type TEXT,object_label TEXT,
                    published_at TIMESTAMPTZ,reference_amount NUMERIC(18,2),currency TEXT,seace_version TEXT,
                    row_hash TEXT,first_seen_at TIMESTAMPTZ,last_seen_at TIMESTAMPTZ)""", (Jsonb(creates),))
        if markers:
            conn.execute("""INSERT INTO prod2_feed_publications(id_convocatoria,publication_run_id,source_row_hash,
                selection_sha256,reader_outcome_sha256,registry_content_hash,reader_status,accepted)
                SELECT x.id_convocatoria,x.publication_run_id,x.source_row_hash,x.selection_sha256,
                    x.reader_outcome_sha256,x.registry_content_hash,x.reader_status,x.accepted
                FROM jsonb_to_recordset(%s) AS x(id_convocatoria BIGINT,publication_run_id TEXT,source_row_hash TEXT,
                    selection_sha256 TEXT,reader_outcome_sha256 TEXT,registry_content_hash TEXT,reader_status TEXT,accepted BOOLEAN)
                ON CONFLICT(id_convocatoria) DO UPDATE SET publication_run_id=excluded.publication_run_id,
                    source_row_hash=excluded.source_row_hash,selection_sha256=excluded.selection_sha256,
                    reader_outcome_sha256=excluded.reader_outcome_sha256,registry_content_hash=excluded.registry_content_hash,
                    reader_status=excluded.reader_status,accepted=excluded.accepted""", (Jsonb(markers),))
        # One bounded fresh readback within the same transaction checks all UUID aliases and markers.
        final = snapshot(conn, plan["cases"])
        final_discoveries = {row["id_convocatoria"]: row for row in final["discoveries"]}
        final_markers = {row["id_convocatoria"]: row for row in final["publications"]}
        for case in plan["cases"]:
            conv = case["listing"]["convocation_id"]
            if final_discoveries.get(conv) != case["expected_discovery"] or identity(case, final) != case["opportunity_id"]:
                raise PublicationError("Metadata or canonical UUID readback disagrees; rollback whole publication")
            if {k: v for k, v in final_markers.get(conv, {}).items() if k != "published_at"} != marker(case, plan):
                raise PublicationError("Publication sidecar readback disagrees; rollback whole publication")
    return {"version": VERSION, "scope_id": SCOPE, "publication_run_id": plan["publication_run_id"],
        "plan_sha256": expected_sha, "selected": COUNT, "created_discoveries": len(creates),
        "preserved_discoveries": COUNT-len(creates), "new_canonical_uuids": len(new_opportunities),
        "new_publication_markers": len(markers), "replayed_markers": replayed,
        "reader_statuses": dict(Counter(case["reader_status"] for case in plan["cases"])),
        "source_calls": 0, "model_calls": 0, "details_created": 0, "schedules_created": 0,
        "matches_created": 0, "existing_rows_overwritten": 0, "semantic_qa_approved": False}
