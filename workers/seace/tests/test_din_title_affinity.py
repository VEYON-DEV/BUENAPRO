"""Real scratch ledgers with fake JEV; no source, model, match or private-doc I/O."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import threading
import uuid

import httpx
from psycopg.types.json import Jsonb
import pytest

from buenapro_worker.prod2 import din_title_affinity as affinity
from buenapro_worker.prod2 import selected_metadata_publication as publication
from test_selected_metadata_publication import cases, pg, seed, conn_for, plan_for, ROOT


@pytest.fixture
def prepared(pg, cases, monkeypatch):
    monkeypatch.setattr(affinity, "SELECTION_SHA", publication.SELECTION_SHA)
    monkeypatch.setattr(affinity, "committed_bindings", lambda root: {})
    with conn_for(pg) as conn:
        seed(conn, cases[0])
        plan = plan_for(conn, cases)
        publication.apply_schema(conn, ROOT / "db/migrations/0047_prod2_feed_publications.sql")
        publication.publish(conn, plan, publication.digest(plan))
        conn.execute("""CREATE TABLE company_profiles(id UUID PRIMARY KEY,tenant_id UUID NOT NULL,
            profile_hash TEXT NOT NULL,identity_json JSONB NOT NULL,is_active BOOLEAN NOT NULL);
            CREATE TABLE business_lines(id BIGSERIAL PRIMARY KEY,profile_id UUID REFERENCES company_profiles(id),
            nombre TEXT NOT NULL,is_active BOOLEAN NOT NULL)""")
        conn.execute("INSERT INTO company_profiles VALUES(%s,%s,'profile-v1',%s,true)",
            (affinity.PROFILE_ID, affinity.TENANT_ID, Jsonb({"descripcion": "DIN  tableros\n eléctricos e IA", "ruc_private": "not-sent"})))
        for line in ["Industrial", "Agentes IA", "  Industrial "]:
            conn.execute("INSERT INTO business_lines(profile_id,nombre,is_active) VALUES(%s,%s,true)", (affinity.PROFILE_ID, line))
        migration = (ROOT / "db/migrations/0036_prod2_pilot_enrichment.sql").read_text()
        start, end = migration.index("CREATE TABLE IF NOT EXISTS prod2_evaluations"), migration.index("CREATE INDEX IF NOT EXISTS ix_prod2_evaluations_candidate")
        conn.execute(migration[start:end])
        conn.execute((ROOT / "db/migrations/0038_prod2_accepted_jev_cache.sql").read_text())
        selection = next(Path(ref["path"]) for ref in cases[1] if ref["sha256"] == publication.SELECTION_SHA)
        plan = affinity.freeze(conn, selection, root=ROOT, run_id="din-title-test")
    return pg, plan


def data(payload):
    return {"model": affinity.JEV_MODEL, "answers": {
        "dominio": {"type": "choice", "choice": "industrial_electrica", "confidence": .95,
                    "probabilities": {"industrial_electrica": .95, "informatica": .05}},
        "afinidad": {"type": "score", "score": 4, "confidence": .94, "probabilities": {"4": .94, "3": .06}},
        "informacion": {"type": "choice", "choice": "claro", "confidence": 1,
                        "probabilities": {"claro": 1}}}, "usage": {"input_tokens": 1000, "output_tokens": 20}}


class Clients:
    def __init__(self, behavior=None):
        self.behavior, self.calls = behavior, []
    def __call__(self):
        owner = self
        class Client:
            def post(self, url, *, headers, json):
                owner.calls.append(deepcopy(json))
                assert len(json["state"]["empresas"]) == 1
                assert json["state"]["empresas"][0] == {
                    "descripcion": "DIN tableros eléctricos e IA", "lineas": ["Agentes IA", "Industrial"]}
                assert "ruc_private" not in str(json)
                return owner.behavior(json) if owner.behavior else httpx.Response(200, json=data(json))
            def close(self):
                pass
        return Client()


def execute(prepared, folder, clients, limit=1, concurrency=1):
    dsn, plan = prepared
    return affinity.execute(dsn, plan, folder, affinity.digest(plan), "fake-only", limit=limit,
                            concurrency=concurrency, client_factory=clients)


def test_commercial_binding_is_profile_and_row_specific_but_payment_hash_is_semantic(prepared):
    _, plan = prepared
    case = plan["cases"][0]
    profile = deepcopy(plan["profile"])
    profile["profile_hash"] = "profile-v2"
    changed = affinity.canonical_binding(case, profile)
    assert changed["provider_request_hash"] == case["provider_request_hash"]
    assert changed["canonical_binding_hash"] != case["canonical_binding_hash"]
    assert len(profile["commercial_profiles"]) == 1
    assert set(profile["commercial_profiles"][0]) == {"descripcion", "lineas"}


def test_pg_known_affinity_is_canonical_only_and_cached_without_second_send(prepared, tmp_path):
    clients = Clients()
    report = execute(prepared, tmp_path, clients)
    assert report["status"] == "pilot_complete" and report["canonical_statuses"] == {"done": 1}
    assert report["sent_provider_requests_local"] == 1 and report["unknown_sent_attempts_local"] == 0
    assert report["known_cost_usd_local"] == "0.000042"
    with conn_for(prepared[0]) as conn:
        row = conn.execute("SELECT * FROM prod2_evaluations").fetchone()
        result = row["result_json"]
        assert row["kind"] == "jev_affinity" and row["policy_version"] == affinity.POLICY
        assert str(row["profile_id"]) == affinity.PROFILE_ID and result["affinity_index"] == 100
        assert result["meta"]["analysis_basis"] == "title"
        assert result["meta"]["canonical_binding_hash"] == row["request_hash"]
        assert result["meta"]["provider_request_hash"] != row["request_hash"]
        assert result["meta"]["commercial_profile_hash"] == prepared[1]["profile"]["commercial_profile_hash"]
        before = publication.frozen(row)
    again = execute(prepared, tmp_path, clients)
    assert again["canonical_statuses"] == {"done": 1} and len(clients.calls) == 1
    assert again["new_known_cost_usd"] == "0"
    with conn_for(prepared[0]) as conn:
        assert publication.frozen(conn.execute("SELECT * FROM prod2_evaluations").fetchone()) == before


@pytest.mark.parametrize("kind", ["transport", "429", "malformed"])
def test_pg_first_sent_error_stops_and_preserves_original_no_retry(prepared, tmp_path, kind):
    def behavior(payload):
        if kind == "transport":
            raise httpx.ReadTimeout("fake-only")
        if kind == "429":
            return httpx.Response(429, json={"error": "synthetic"})
        body = data(payload)
        body["model"] = "wrong"
        return httpx.Response(200, json=body)
    clients = Clients(behavior)
    result = execute(prepared, tmp_path, clients)
    assert result["status"] == "attention_required" and len(clients.calls) == 1
    assert result["canonical_statuses"] == {"uncertain": 1}
    assert len(list((tmp_path / "provider").glob("raw-*.json"))) == 1
    with pytest.raises((affinity.AffinityStop, ValueError)):
        # Malformed historical HTTP 200 must stop the entire preflight before a
        # second not-yet-sent group can be submitted beside its failed replay.
        execute(prepared, tmp_path, clients, limit=2, concurrency=2)
    assert len(clients.calls) == 1
    if kind == "malformed":
        assert result["known_cost_usd_local"] == "0.000042" and result["unknown_sent_attempts_local"] == 0
    else:
        assert result["unknown_sent_attempts_local"] == 1


def test_pg_known_response_in_flight_is_completed_when_peer_fails(prepared, tmp_path):
    barrier = threading.Barrier(2)
    first_title = prepared[1]["cases"][0]["description"]
    def behavior(payload):
        barrier.wait(timeout=5)
        return httpx.Response(429, json={"error": "synthetic"}) if payload["state"]["descripcion"] == first_title else httpx.Response(200, json=data(payload))
    clients = Clients(behavior)
    report = execute(prepared, tmp_path, clients, limit=2, concurrency=2)
    assert len(clients.calls) == 2 and report["status"] == "attention_required"
    assert report["canonical_statuses"] == {"done": 1, "uncertain": 1}
    assert report["known_cost_usd_local"] == "0.000042" and report["unknown_sent_attempts_local"] == 1


@pytest.mark.parametrize("change", ["profile", "metadata", "selection"])
def test_pg_frozen_binding_change_prevents_any_claim_or_http(prepared, tmp_path, change):
    dsn, plan = prepared
    if change == "selection":
        Path(plan["selection_path"]).write_text('{"changed":true}')
    else:
        with conn_for(dsn) as conn:
            if change == "profile":
                conn.execute("UPDATE company_profiles SET profile_hash='changed'")
            else:
                conn.execute("UPDATE prod2_discoveries SET row_hash=%s WHERE id_convocatoria=1", ("f"*64,))
    clients = Clients()
    with pytest.raises((affinity.AffinityStop, publication.PublicationError)):
        execute(prepared, tmp_path, clients)
    assert not clients.calls
    with conn_for(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM prod2_evaluations").fetchone()["n"] == 0


def test_pg_profile_reprojection_reuses_exact_paid_global_response(prepared, tmp_path):
    clients = Clients()
    execute(prepared, tmp_path / "first", clients)
    dsn, plan = prepared
    with conn_for(dsn) as conn:
        conn.execute("UPDATE company_profiles SET profile_hash='profile-v2'")
        second = affinity.freeze(conn, plan["selection_path"], root=ROOT, run_id="new-binding")
    report = execute((dsn, second), tmp_path / "second", clients)
    assert len(clients.calls) == 1 and report["sent_provider_requests_local"] == 0
    with conn_for(dsn) as conn:
        rows = conn.execute("SELECT * FROM prod2_evaluations ORDER BY id").fetchall()
        assert len(rows) == 2 and all(row["status"] == "done" for row in rows)
        assert rows[0]["request_hash"] != rows[1]["request_hash"]
        assert rows[1]["result_json"]["meta"]["provider_cache_reused"]
        assert rows[1]["cost_usd"] == 0 and rows[0]["cost_usd"] > 0


def test_pg_saved_canonical_response_completes_original_owner_without_repayment(prepared, tmp_path, monkeypatch):
    original = affinity.complete_evaluation
    def fail(*args, **kwargs):
        raise OSError("local write failure after exact response was saved")
    monkeypatch.setattr(affinity, "complete_evaluation", fail)
    clients = Clients()
    assert execute(prepared, tmp_path, clients)["status"] == "attention_required"
    assert len(clients.calls) == 1
    saved_path = next((tmp_path / "canonical-responses").glob("*.json"))
    saved = saved_path.read_bytes()
    monkeypatch.setattr(affinity, "complete_evaluation", original)
    recovered = execute(prepared, tmp_path, clients)
    assert recovered["canonical_statuses"] == {"done": 1} and len(clients.calls) == 1
    assert saved_path.read_bytes() == saved
    assert recovered["known_cost_usd_local"] == "0.000042" and recovered["unknown_sent_attempts_local"] == 0


def test_pg_identical_title_groups_reuse_one_provider_payment_for_two_canonical_ids(prepared, tmp_path):
    dsn, plan = prepared
    first = plan["cases"][0]
    with conn_for(dsn) as conn:
        conn.execute("UPDATE prod2_discoveries SET description=%s,row_hash=%s WHERE id_convocatoria=2",
                     (first["description"], "c"*64))
        conn.execute("UPDATE prod2_feed_publications SET source_row_hash=%s WHERE id_convocatoria=2", ("c"*64,))
        grouped = affinity.freeze(conn, plan["selection_path"], root=ROOT, run_id="grouped")
    clients = Clients()
    report = execute((dsn, grouped), tmp_path, clients)
    assert len(clients.calls) == 1 and report["canonical_statuses"] == {"done": 2}
    with conn_for(dsn) as conn:
        rows = conn.execute("SELECT id_convocatoria,cost_usd,result_json FROM prod2_evaluations ORDER BY id_convocatoria").fetchall()
        assert [row["id_convocatoria"] for row in rows] == [1, 2]
        assert sum(row["cost_usd"] > 0 for row in rows) == 1
        assert rows[0]["result_json"]["meta"]["provider_request_hash"] == rows[1]["result_json"]["meta"]["provider_request_hash"]


def test_runtime_commit_guard_rejects_local_uncommitted_changes(tmp_path, monkeypatch):
    relative = "runtime.py"
    path = tmp_path / relative
    path.write_text("committed bytes")
    monkeypatch.setattr(affinity, "RUNTIME_FILES", (relative,))
    monkeypatch.setattr(affinity.subprocess, "check_output", lambda *a, **k: b"committed bytes")
    assert relative in affinity.committed_bindings(tmp_path)
    path.write_text("local uncommitted bytes")
    with pytest.raises(affinity.AffinityStop, match="uncommitted"):
        affinity.committed_bindings(tmp_path)


@pytest.mark.parametrize("change", ["owner_disconnect", "profile_lock_lost", "backend_identity", "provider_lease",
    "provider_token", "canonical_lease", "canonical_token", "profile", "source"])
def test_pg_immediate_presend_guard_blocks_live_ownership_or_input_changes_without_spend(prepared, tmp_path, monkeypatch, change):
    original = affinity.check_dispatch_ownership
    def changed(owner, owner_pid, worker, claims, provider_hash, provider_token, profile, stop, owner_guard):
        if change == "owner_disconnect":
            owner.close()
        elif change == "profile_lock_lost":
            owner.execute("SELECT pg_advisory_unlock(hashtext('dinaut_three_dot_llm2'),hashtext(%s))", (affinity.PROFILE_ID,))
        elif change == "backend_identity":
            owner_pid += 1
        elif change == "provider_lease":
            worker.execute("UPDATE prod2_jev_decision_cache SET lease_until=clock_timestamp()-interval '1 second' WHERE cache_key=%s", (provider_hash,))
        elif change == "provider_token":
            worker.execute("UPDATE prod2_jev_decision_cache SET claim_token=%s WHERE cache_key=%s", (str(uuid.uuid4()), provider_hash))
        elif change == "canonical_lease":
            worker.execute("UPDATE prod2_evaluations SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (claims[0][1]["id"],))
        elif change == "canonical_token":
            worker.execute("UPDATE prod2_evaluations SET claim_token=%s WHERE id=%s", (str(uuid.uuid4()), claims[0][1]["id"]))
        elif change == "profile":
            worker.execute("UPDATE company_profiles SET profile_hash='changed-during-pacing'")
        elif change == "source":
            worker.execute("UPDATE prod2_discoveries SET row_hash=%s WHERE id_convocatoria=%s", ("f"*64, claims[0][0]["convocation_id"]))
        return original(owner, owner_pid, worker, claims, provider_hash, provider_token, profile, stop, owner_guard)
    monkeypatch.setattr(affinity, "check_dispatch_ownership", changed)
    clients = Clients()
    report = execute(prepared, tmp_path, clients)
    assert report["status"] == "attention_required" and clients.calls == []
    assert report["sent_provider_requests_local"] == report["unknown_sent_attempts_local"] == 0
    assert Decimal(report["known_cost_usd_local"]) == 0
    denied = list((tmp_path / "provider").glob("guard-denied-*.json"))
    assert len(denied) == 1 and affinity.read_bound(denied[0])["sent"] is False
    assert list((tmp_path / "provider").glob("started-*.json")) == []


def test_pg_existing_historical_provider_uncertainty_blocks_before_new_claim(prepared, tmp_path):
    dsn, plan = prepared
    case = plan["cases"][0]
    with conn_for(dsn) as conn:
        conn.execute("INSERT INTO prod2_jev_decision_cache(cache_key,convocation_id,model,policy_version,status) "
            "VALUES(%s,%s,%s,%s,'uncertain')", (case["provider_request_hash"], case["convocation_id"], affinity.JEV_MODEL, affinity.JEV_POLICY_VERSION))
    clients = Clients()
    with pytest.raises(affinity.AffinityStop, match="historical provider"):
        execute(prepared, tmp_path, clients)
    assert not clients.calls


def test_pg_din_llm2_lock_prevents_simultaneous_commercial_scoring(prepared, tmp_path):
    with conn_for(prepared[0]) as owner:
        owner.execute("SELECT pg_advisory_lock(hashtext('dinaut_three_dot_llm2'),hashtext(%s))", (affinity.PROFILE_ID,))
        try:
            with pytest.raises(affinity.AffinityStop, match="coordinator"):
                execute(prepared, tmp_path, Clients())
        finally:
            owner.execute("SELECT pg_advisory_unlock(hashtext('dinaut_three_dot_llm2'),hashtext(%s))", (affinity.PROFILE_ID,))
