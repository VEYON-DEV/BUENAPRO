"""Offline map-only contracts. PostgreSQL execution has a separate opt-in suite."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

import pytest

from buenapro_worker.prod2 import discovery_registry as subject


QUERY = {"year": 2026, "keyword": "software", "object_type": "service",
         "published_from": "2026-04-06", "published_to": "2026-10-19", "seace_version": "3"}
RUN = "map-only-test-20261020"
OBSERVED = "2026-10-19T12:00:00+00:00"


def entry(convocation=20, *, keyword="software", observed=OBSERVED, description="Software de gestión"):
    return {"query": {**QUERY, "keyword": keyword}, "observed_at": observed, "page_number": 1,
        "row": {"convocation_id": convocation, "procedure_id": convocation + 100,
            "nomenclature": f"TEST-{convocation}", "buyer_name": "ENTIDAD SINTÉTICA",
            "description": description, "object_type": "service", "object_label": "Servicio",
            "published_at": "2026-10-18T10:00:00-05:00", "reference_amount": "100.00",
            "currency": "PEN", "seace_version": "3"}}


def manifest(run=RUN, queries=None):
    return {"run_id": run, "queries": queries or [deepcopy(QUERY)],
            "published_from": QUERY["published_from"], "published_to": QUERY["published_to"],
            "workers": 10, "request_interval": 1.0, "keywords_hash": "a" * 64}


def time(value):
    return datetime.fromisoformat(value)


class Database:
    """Atomic in-memory public metadata model; records all SQL, never opens a socket."""
    autocommit = True

    def __init__(self):
        self.runs, self.queries, self.registry, self.observations = {}, {}, {}, {}
        self.members, self.associations = {}, {}
        self.baseline = {name: {} for name in (
            "prod2_discoveries", "prod2_accepted_run_members", "prod2_jev_decision_cache")}
        self.calls = []
        self.catalog_columns = [{"table_name": table, "column_name": column,
            "data_type": kind, "not_null": True}
            for table, columns in subject.TABLE_COLUMNS.items() for column, kind in columns.items()]
        self.catalog_columns += [{"table_name": table, "column_name": column,
            "data_type": kind, "not_null": True}
            for table, columns in subject.BASELINE_COLUMNS.items() for column, kind in columns.items()]
        self.catalog_constraints = [{"conname": name, "contype": kind, "table_name": table,
            "columns": columns, "target_table": target, "target_columns": target_columns,
            "convalidated": True, "local_target": True, "definition": ""}
            for name, (kind, table, columns, target, target_columns) in subject.KEY_CONSTRAINTS.items()]
        self.catalog_constraints += [{"conname": name, "contype": "c", "table_name": table,
            "convalidated": True, "definition": "CHECK (" + " AND ".join(fragments) + ")"}
            for name, (table, fragments) in subject.CHECK_CONSTRAINTS.items()]
        self.catalog_indexes = [{"index_name": name, "table_name": table,
            "local_index": True, "local_table": True, "access_method": "btree",
            "key_count": 1, "attribute_count": 1, "columns": ["convocation_id"],
            "indisvalid": True, "indisready": True, "indisunique": False,
            "plain_index": True, "unfiltered": True} for name, table in subject.BASELINE_INDEXES.items()]

    @contextmanager
    def transaction(self):
        names = ("runs", "queries", "registry", "observations", "members", "associations")
        saved = {name: deepcopy(getattr(self, name)) for name in names}
        try:
            yield
        except BaseException:
            for name, value in saved.items():
                setattr(self, name, value)
            raise

    @contextmanager
    def cursor(self, **kwargs):
        yield Cursor(self)


class Cursor:
    def __init__(self, db):
        self.db, self.result = db, []

    def execute(self, statement, params=()):
        sql = " ".join(statement.split())
        db = self.db
        db.calls.append((sql, deepcopy(params)))
        self.result = []
        if "FROM pg_attribute a JOIN pg_class" in sql:
            self.result = deepcopy(db.catalog_columns)
        elif "FROM pg_constraint c JOIN pg_class" in sql:
            self.result = deepcopy(db.catalog_constraints)
        elif "FROM pg_index i JOIN pg_class" in sql:
            self.result = deepcopy(db.catalog_indexes)
        elif sql.startswith(("SELECT pg_advisory_xact_lock", "SET TRANSACTION")):
            pass
        elif sql.startswith("INSERT INTO prod2_map_runs"):
            run, digest, value = params
            db.runs.setdefault(run, {"run_id": run, "manifest_hash": digest, "manifest_json": json.loads(value)})
        elif sql.startswith("SELECT") and "FROM prod2_map_runs" in sql:
            self.result = [deepcopy(db.runs[params[0]])] if params[0] in db.runs else []
        elif sql.startswith("INSERT INTO prod2_map_run_queries"):
            run, digest, value = params
            db.queries.setdefault((run, digest), json.loads(value))
        elif sql.startswith("SELECT query_hash,query_manifest"):
            self.result = [{"query_hash": digest, "query_manifest": deepcopy(value)}
                           for (run, digest), value in db.queries.items() if run == params[0]]
        elif sql.startswith("SELECT * FROM prod2_listing_registry"):
            self.result = [deepcopy(db.registry[identity]) for identity in params[0] if identity in db.registry]
        elif sql.startswith("SELECT convocation_id,max(procedure_id)"):
            ids = params[0]
            self.result = [{"convocation_id": identity,
                "procedure_id": db.baseline["prod2_discoveries"].get(identity)}
                for identity in ids if any(identity in rows for rows in db.baseline.values())]
        elif sql.startswith("SELECT * FROM prod2_map_run_members"):
            run = params[0]
            self.result = [deepcopy(value) for (owner, identity), value in sorted(db.members.items())
                if owner == run and (len(params) == 1 or identity in params[1])]
        elif sql.startswith("SELECT o.* FROM prod2_listing_observations"):
            pairs = json.loads(params[0])
            self.result = [deepcopy(db.observations[(row["convocation_id"], time(row["observed_at"]))])
                for row in pairs if (row["convocation_id"], time(row["observed_at"])) in db.observations]
        elif sql.startswith("INSERT INTO prod2_listing_registry"):
            for item in json.loads(params[0]):
                item["observed_at"], item["first_observed_at"] = time(item["observed_at"]), time(item["first_observed_at"])
                prior = db.registry.get(item["convocation_id"])
                if prior is None:
                    db.registry[item["convocation_id"]] = item
                else:
                    assert prior["procedure_id"] == item["procedure_id"]
                    if item["observed_at"] > prior["observed_at"]:
                        prior.update({key: deepcopy(item[key]) for key in ("content_hash", "listing_json", "observed_at")})
                    prior["first_observed_at"] = min(prior["first_observed_at"], item["first_observed_at"])
        elif sql.startswith("INSERT INTO prod2_listing_observations"):
            for item in json.loads(params[0]):
                item["observed_at"] = time(item["observed_at"])
                db.observations.setdefault((item["convocation_id"], item["observed_at"]), item)
        elif sql.startswith("INSERT INTO prod2_map_run_members"):
            for item in json.loads(params[0]):
                key = item["run_id"], item["convocation_id"]
                if key not in db.members:
                    item["first_observed_at"] = time(item["first_observed_at"])
                    db.members[key] = item
                    self.result.append({"convocation_id": item["convocation_id"]})
        elif sql.startswith("INSERT INTO prod2_map_query_observations"):
            for item in json.loads(params[0]):
                item["observed_at"] = time(item["observed_at"])
                key = tuple(item[key] for key in ("run_id", "convocation_id", "query_hash", "observed_query_hash", "observed_at"))
                db.associations.setdefault(key, item)
        elif sql.startswith("SELECT o.*,m.first_classification"):
            for key, item in sorted(db.associations.items()):
                if key[0] != params[0]:
                    continue
                saved = deepcopy(item)
                saved["first_classification"] = db.members[key[:2]]["first_classification"]
                saved["listing_json"] = deepcopy(db.observations[(item["convocation_id"], item["observed_at"])]["listing_json"])
                saved["root_query"] = deepcopy(db.queries[(key[0], item["query_hash"])])
                latest = db.registry[item["convocation_id"]]["observed_at"]
                saved.update(registry_observed_at=latest, older_than_registry=item["observed_at"] < latest)
                self.result.append(saved)
        else:
            raise AssertionError(f"Unexpected SQL contract: {sql[:100]}")
        return self

    def executemany(self, statement, parameters):
        for values in parameters:
            self.execute(statement, values)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


@pytest.fixture
def db():
    conn = Database()
    subject.freeze_run(conn, RUN, manifest())
    return conn


def add_run(db, run, queries=None):
    subject.freeze_run(db, run, manifest(run, queries))


def test_new_then_replay_keeps_membership_and_observation_idempotent(db):
    source = entry()
    assert subject.observe_many(db, RUN, [source]) == [True]
    first = deepcopy(db.members[(RUN, 20)])
    assert subject.observe_many(db, RUN, [source]) == [False]
    assert db.members[(RUN, 20)] == first and first["first_classification"] == "new_id"
    report = subject.read_report(db, RUN)
    assert report["counts"] == dict(new_id=1, unchanged=0, changed=0, known_baseline_missing=0)
    assert report["ids"]["new_id"] == [20] and report["observation_count"] == 1
    assert not report["source_coverage_complete"] and report["provider_calls"] == 0
    json.dumps(report)  # Public report remains serializable without a custom encoder.


def test_two_queries_same_run_return_one_first_membership_and_both_associations():
    db = Database()
    add_run(db, RUN, [QUERY, {**QUERY, "keyword": "licencia"}])
    assert subject.observe_many(db, RUN, [entry(), entry(keyword="licencia")]) == [True, False]
    report = subject.read_report(db, RUN)
    assert report["total_ids"] == 1 and report["observation_count"] == 2
    assert report["duplicate_associations"] == 1
    assert {row["first_classification"] for row in report["observations"]} == {"new_id"}


def test_exact_duplicate_same_batch_keeps_first_association_not_a_fabricated_duplicate(db):
    assert subject.observe_many(db, RUN, [entry(), entry()]) == [True, False]
    report = subject.read_report(db, RUN)
    assert report["observation_count"] == 1 and report["duplicate_associations"] == 0


def test_unchanged_and_changed_cross_run_compare_registry_hashes(db):
    subject.observe_many(db, RUN, [entry()])
    add_run(db, "unchanged")
    assert subject.observe_many(db, "unchanged", [entry(observed="2026-10-19T12:01:00Z")]) == [True]
    assert subject.read_report(db, "unchanged")["ids"]["unchanged"] == [20]
    add_run(db, "changed")
    changed = entry(observed="2026-10-19T12:02:00Z", description="Objeto modificado")
    assert subject.observe_many(db, "changed", [changed]) == [True]
    assert subject.read_report(db, "changed")["ids"]["changed"] == [20]
    assert db.registry[20]["listing_json"]["description"] == "Objeto modificado"


def test_same_run_change_retains_first_classification(db):
    subject.observe_many(db, RUN, [entry()])
    assert subject.observe_many(db, RUN, [entry(observed="2026-10-19T12:01:00Z", description="Objeto actualizado")]) == [False]
    report = subject.read_report(db, RUN)
    assert report["counts"]["new_id"] == 1 and report["counts"]["changed"] == 0
    assert db.registry[20]["listing_json"]["description"] == "Objeto actualizado"
    assert report["duplicate_associations"] == 1


def test_first_unchanged_then_changed_is_exported_with_both_versions_for_human_go(db):
    subject.observe_many(db, RUN, [entry()])
    add_run(db, "in-flight-change", [QUERY, {**QUERY, "keyword": "licencia"}])
    first = entry(observed="2026-10-19T12:01:00Z")
    second = entry(keyword="licencia", observed="2026-10-19T12:02:00Z", description="Objeto ampliado")
    assert subject.observe_many(db, "in-flight-change", [first, second]) == [True, False]
    report = subject.read_report(db, "in-flight-change")
    assert report["counts"]["unchanged"] == 1 and report["counts"]["changed"] == 0
    assert report["changed_during_run_ids"] == report["candidate_ids"] == [20]
    assert report["requires_human_go"] and not report["automatic_paid_authorization"]
    member = report["members"][0]
    assert member["observed_hash_count"] == 2
    assert member["latest_listing_json"]["description"] == "Objeto ampliado"
    assert member["latest_content_hash"] == subject.normalize_entry(second)["content_hash"]
    assert {row["listing_json"]["description"] for row in report["observations"]} == {
        "Software de gestión", "Objeto ampliado"}
    assert db.observations[(20, time(first["observed_at"]))]["listing_json"]["description"] == "Software de gestión"


@pytest.mark.parametrize("table", ["prod2_discoveries", "prod2_accepted_run_members", "prod2_jev_decision_cache"])
def test_historical_prod2_id_without_registry_hash_is_explicitly_known_not_new(db, table):
    db.baseline[table][20] = 120 if table == "prod2_discoveries" else None
    assert subject.observe_many(db, RUN, [entry()]) == [True]
    report = subject.read_report(db, RUN)
    assert report["ids"]["known_baseline_missing"] == [20]
    assert report["counts"]["new_id"] == report["counts"]["unchanged"] == 0
    assert report["candidate_ids"] == report["needs_review_ids"] == [20]
    assert not report["automatic_paid_authorization"]
    baseline_reads = [sql for sql, params in db.calls if "AS historical GROUP BY" in sql]
    assert len(baseline_reads) == 1
    assert all(name in baseline_reads[0] for name in db.baseline)
    assert "opportunities" not in baseline_reads[0] and "prod6" not in baseline_reads[0]


def test_historical_procedure_mismatch_blocks_atomic_page(db):
    db.baseline["prod2_discoveries"][20] = 999
    with pytest.raises(subject.RegistryError, match="convocation_procedure_conflict"):
        subject.observe_many(db, RUN, [entry(19), entry(20)])
    assert not db.registry and not db.members and not db.observations


@pytest.mark.parametrize("different_run", [False, True])
def test_same_convocation_different_procedure_blocks_before_new_metadata(db, different_run):
    subject.observe_many(db, RUN, [entry()])
    before = deepcopy(db.registry)
    run = "other" if different_run else RUN
    if different_run:
        add_run(db, run)
    conflicting = entry(observed="2026-10-19T12:01:00Z")
    conflicting["row"]["procedure_id"] = 999
    with pytest.raises(subject.RegistryError, match="convocation_procedure_conflict"):
        subject.observe_many(db, run, [entry(19), conflicting])
    assert db.registry == before and (run, 19) not in db.members


def test_same_batch_procedure_disagreement_is_atomic(db):
    conflicting = entry(observed="2026-10-19T12:01:00Z")
    conflicting["row"]["procedure_id"] = 999
    with pytest.raises(subject.RegistryError, match="convocation_procedure_conflict"):
        subject.observe_many(db, RUN, [entry(), conflicting])
    assert not db.registry and not db.members


def test_older_changed_capture_preserves_latest_metadata_and_surfaces_staleness(db):
    newest = entry(observed="2026-10-19T12:10:00Z", description="Objeto nuevo")
    subject.observe_many(db, RUN, [newest])
    add_run(db, "older")
    subject.observe_many(db, "older", [entry()])
    assert db.registry[20]["listing_json"]["description"] == "Objeto nuevo"
    assert db.registry[20]["observed_at"] == time(newest["observed_at"])
    report = subject.read_report(db, "older")
    assert report["counts"]["changed"] == 1 and report["older_observations"] == 1


def test_same_instant_conflict_is_preserved_even_when_registry_has_a_later_version(db):
    subject.observe_many(db, RUN, [entry()])
    subject.observe_many(db, RUN, [entry(observed="2026-10-19T12:10:00Z", description="Más nuevo")])
    before = deepcopy(db.registry)
    add_run(db, "contradiction")
    with pytest.raises(subject.RegistryError, match="observation_timestamp_conflict"):
        subject.observe_many(db, "contradiction", [entry(19), entry(description="Contradicción antigua")])
    assert db.registry == before and ("contradiction", 19) not in db.members


def test_same_batch_equal_timestamp_different_hash_blocks(db):
    with pytest.raises(subject.RegistryError, match="observation_timestamp_conflict"):
        subject.observe_many(db, RUN, [entry(), entry(description="Contradicción")])
    assert not db.registry and not db.members


def test_hash_ignores_query_page_time_and_equivalent_timezone_or_amount_format():
    first, second = entry(), entry(keyword="licencia", observed="2026-10-19T13:00:00Z")
    second["page_number"] = 99
    second["row"]["published_at"] = "2026-10-18T15:00:00Z"
    second["row"]["reference_amount"] = "100"
    del second["row"]["object_label"]
    assert subject.normalize_entry(first)["content_hash"] == subject.normalize_entry(second)["content_hash"]
    second["row"]["description"] += " ampliado"
    assert subject.normalize_entry(first)["content_hash"] != subject.normalize_entry(second)["content_hash"]


def test_reserved_amount_stays_null_and_never_becomes_zero(db):
    source = entry()
    source["row"]["reference_amount"] = None
    subject.observe_many(db, RUN, [source])
    assert db.registry[20]["listing_json"]["reference_amount"] is None


def test_root_query_and_split_leaf_are_both_bound_and_preserved(db):
    source = entry()
    source["query"].update(published_from="2026-10-18", published_to="2026-10-19")
    subject.observe_many(db, RUN, [source])
    observed = subject.read_report(db, RUN)["observations"][0]
    assert observed["root_query"] == QUERY and observed["observed_query"] == source["query"]
    assert observed["query_hash"] != observed["observed_query_hash"]


def test_unknown_or_ambiguous_root_query_cannot_create_membership(db):
    with pytest.raises(subject.RegistryError, match="query_not_frozen_or_ambiguous"):
        subject.observe_many(db, RUN, [entry(keyword="fuera del plan")])
    overlap = {**QUERY, "published_from": "2026-10-01"}
    add_run(db, "overlap", [QUERY, overlap])
    source = entry()
    source["query"].update(published_from="2026-10-18", published_to="2026-10-19")
    with pytest.raises(subject.RegistryError, match="query_not_frozen_or_ambiguous"):
        subject.observe_many(db, "overlap", [source])
    assert not db.registry


def test_sorted_complete_id_locks_precede_writes_and_reads_are_bounded(db):
    assert subject.observe_many(db, RUN, [entry(30), entry(10), entry(30), entry(20)]) == [True, True, False, True]
    lock_index, (sql, params) = next((index, call) for index, call in enumerate(db.calls)
                                   if "AS ordered_locks" in call[0])
    assert params == ([10, 20, 30],) and "ORDER BY lock_key" in sql
    writes = [(index, sql, params) for index, (sql, params) in enumerate(db.calls)
              if sql.startswith("INSERT INTO prod2_listing") or sql.startswith("INSERT INTO prod2_map_run_members")]
    assert all(index > lock_index for index, _, _ in writes)
    # JSON batch writes replace a network round trip per row, with <=100 records each.
    assert all(len(json.loads(params[0])) <= 100 for _, _, params in writes)
    assert len([sql for sql, _ in db.calls if "AS historical GROUP BY" in sql]) == 1


def test_page_limit_and_invalid_input_fail_before_database_access(db):
    before = len(db.calls)
    with pytest.raises(subject.RegistryError, match="batch_requires_at_most_100_entries"):
        subject.observe_many(db, RUN, [entry(i) for i in range(101)])
    invalid = entry()
    invalid["row"]["convocation_id"] = True
    with pytest.raises(subject.RegistryError):
        subject.observe_many(db, RUN, [invalid])
    assert len(db.calls) == before
    assert subject.observe_many(db, RUN, []) == []


@pytest.mark.parametrize("change", ["naive_time", "object_mismatch", "unknown_field", "future_observation"])
def test_invalid_canonical_listing_is_not_silently_repaired(db, change):
    source = entry()
    if change == "naive_time":
        source["row"]["published_at"] = "2026-10-18T10:00:00"
    elif change == "object_mismatch":
        source["row"]["object_type"] = "good"
    elif change == "unknown_field":
        source["row"]["cookie"] = "SYNTHETIC-PRIVATE"
    else:
        source["observed_at"] = "2099-10-19T12:00:00Z"
    with pytest.raises(subject.RegistryError) as caught:
        subject.observe_many(db, RUN, [source])
    assert "SYNTHETIC-PRIVATE" not in str(caught.value) and not db.registry


def test_freeze_replay_is_immutable_even_if_non_query_settings_change(db):
    before = deepcopy(db.runs)
    subject.freeze_run(db, RUN, manifest())
    changed = manifest()
    changed["workers"] = 9
    with pytest.raises(subject.RegistryError, match="frozen_manifest_changed"):
        subject.freeze_run(db, RUN, changed)
    assert db.runs == before


@pytest.mark.parametrize("change", ["duplicate_queries", "run_mismatch", "noncanonical", "nan"])
def test_bad_manifest_fails_before_database_access(change):
    db = Database()
    frozen = manifest()
    if change == "duplicate_queries":
        frozen["queries"] *= 2
    elif change == "run_mismatch":
        frozen["run_id"] = "different"
    elif change == "noncanonical":
        frozen["queries"][0]["keyword"] = " software "
    else:
        frozen["request_interval"] = float("nan")
    with pytest.raises(subject.RegistryError):
        subject.freeze_run(db, RUN, frozen)
    assert not db.calls


def test_unfrozen_or_tampered_run_never_observes(db):
    with pytest.raises(subject.RegistryError, match="run_not_frozen_or_corrupt"):
        subject.observe_many(db, "missing", [entry()])
    db.runs[RUN]["manifest_json"]["workers"] = 99
    with pytest.raises(subject.RegistryError, match="run_not_frozen_or_corrupt"):
        subject.observe_many(db, RUN, [entry()])
    assert not db.registry


def test_caller_must_use_its_own_autocommit_connection(db):
    db.autocommit = False
    with pytest.raises(subject.RegistryError, match="autocommit_connection_required"):
        subject.observe_many(db, RUN, [entry()])
    with pytest.raises(subject.RegistryError, match="autocommit_connection_required"):
        subject.freeze_run(db, "another", manifest("another"))
    with pytest.raises(subject.RegistryError, match="autocommit_connection_required"):
        subject.read_report(db, RUN)


def test_require_schema_is_read_only_and_checks_all_contracts(db):
    db.calls.clear()
    assert subject.require_schema(db)["status"] == "discovery_registry_schema_ready"
    assert len(db.calls) == 3 and all(sql.startswith("SELECT") for sql, _ in db.calls)
    assert set(db.calls[0][1][0]) == set(subject.TABLE_COLUMNS) | set(subject.BASELINE_COLUMNS)


@pytest.mark.parametrize("table", list(subject.BASELINE_COLUMNS))
def test_missing_historical_baseline_blocks_schema_preflight_before_any_mutation(db, table):
    db.catalog_columns = [row for row in db.catalog_columns if row["table_name"] != table]
    db.calls.clear()
    with pytest.raises(subject.RegistryError, match="historical_baseline_schema_missing_or_incompatible"):
        subject.require_schema(db)
    assert len(db.calls) == 1 and db.calls[0][0].startswith("SELECT")


def test_historical_baseline_wrong_id_type_is_not_treated_as_an_empty_baseline(db):
    next(row for row in db.catalog_columns if row["table_name"] == "prod2_discoveries")["data_type"] = "text"
    with pytest.raises(subject.RegistryError, match="historical_baseline_schema_missing_or_incompatible"):
        subject.require_schema(db)


@pytest.mark.parametrize("field,value", [
    ("table_name", "different_table"), ("local_index", False), ("local_table", False),
    ("access_method", "hash"), ("key_count", 2), ("attribute_count", 2),
    ("columns", ["different_column"]), ("columns", []), ("indisvalid", False),
    ("indisready", False), ("indisunique", True), ("plain_index", False), ("unfiltered", False),
])
def test_named_baseline_index_must_have_the_verified_plain_lookup_shape(db, field, value):
    db.catalog_indexes[0][field] = value
    db.calls.clear()
    with pytest.raises(subject.RegistryError, match="historical_baseline_index_missing_or_incompatible"):
        subject.require_schema(db)
    assert len(db.calls) == 3 and all(sql.startswith("SELECT") for sql, _ in db.calls)


@pytest.mark.parametrize("index_name", list(subject.BASELINE_INDEXES))
def test_absent_named_baseline_index_is_not_silently_accepted_or_repaired(db, index_name):
    db.catalog_indexes = [row for row in db.catalog_indexes if row["index_name"] != index_name]
    db.calls.clear()
    with pytest.raises(subject.RegistryError, match="historical_baseline_index_missing_or_incompatible"):
        subject.require_schema(db)
    assert all(sql.startswith("SELECT") for sql, _ in db.calls)


@pytest.mark.parametrize("change", ["missing_column", "nullable", "wrong_type", "missing_key", "wrong_target", "wrong_order", "unvalidated", "missing_class"])
def test_require_schema_blocks_incompatible_schema_without_repair(db, change):
    if change == "missing_column":
        db.catalog_columns.pop()
    elif change == "nullable":
        db.catalog_columns[0]["not_null"] = False
    elif change == "wrong_type":
        db.catalog_columns[0]["data_type"] = "integer"
    elif change == "missing_key":
        db.catalog_constraints.pop(0)
    elif change == "wrong_target":
        next(row for row in db.catalog_constraints if row["contype"] == "f")["local_target"] = False
    elif change == "wrong_order":
        next(row for row in db.catalog_constraints if row["conname"] == "prod2_map_run_members_identity_fk")["target_columns"] = ["procedure_id", "convocation_id"]
    elif change == "unvalidated":
        db.catalog_constraints[0]["convalidated"] = False
    else:
        next(row for row in db.catalog_constraints if row["conname"] == "prod2_map_run_members_class_ck")["definition"] = "CHECK (new_id)"
    db.calls.clear()
    with pytest.raises(subject.RegistryError):
        subject.require_schema(db)
    assert all(sql.startswith("SELECT") for sql, _ in db.calls)


def test_migration_has_all_named_constraints_and_no_publication_or_payment_tables():
    migration = Path(__file__).resolve().parents[3] / "db/migrations/0045_prod2_discovery_registry.sql"
    text = migration.read_text()
    assert all(name in text for name in (*subject.KEY_CONSTRAINTS, *subject.CHECK_CONSTRAINTS))
    tables = re.findall(r"CREATE TABLE (\w+)", text)
    assert set(tables) == set(subject.TABLE_COLUMNS)
    for index_name, table in subject.BASELINE_INDEXES.items():
        assert re.search(r"CREATE INDEX IF NOT EXISTS " + index_name + r"\s+ON " + table + r"\(convocation_id\)", text)
    assert not re.search(r"(?:REFERENCES|INSERT INTO|UPDATE|DELETE FROM)\s+(?:opportunities|worker_jobs|matches|prod6_\w+)", text, re.I)
