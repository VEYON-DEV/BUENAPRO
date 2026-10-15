"""Offline publication contract; no database, provider, queue or network."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import dinaut_three_dot_persistence as subject


PROFILE = "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"
TENANT = "46ba7840-ae84-4de0-ac98-16fab9e5d5bb"


def case(source="seace_prod2"):
    return {"source_key": source, "source_id": 19, "opportunity_id": "opportunity",
        "fit_level": 3, "extraction_id": 27, "document_sha256": "a" * 64,
        "quality": "auto", "requires_human_review": True, "is_current": True,
        "raw_extraction_json": {"read": True}, "summary_json": {"object": "Software"},
        "facets": [{"extraction_id": 27, "is_current": True, "facet": "specific_experience",
                    "label": "Experiencia software", "required": True, "details_json": {}}]}


class Connection:
    def __init__(self, snapshot, *, previous=None, provenance=None):
        self.case = snapshot
        self.profile = {"razon_social": "DIN", "experience_json": [{"real": True}]}
        self.previous, self.provenance = previous, provenance
        self.calls = []
        self.done = {"id_convocatoria": 19, "kind": "gemini_match", "status": "done",
            "profile_id": PROFILE, "policy_version": "jev_gemma_test",
            "model": "jev-1.13.0+gemma4:31b", "result_json": {
                "extraction_id": 27, "profile_hash": subject.profile_fingerprint(self.profile)}}

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        self.result = {"id": 99}
        if sql.startswith("SELECT * FROM company_profiles"):
            self.result = self.profile
        elif sql.startswith("SELECT * FROM opportunity_matches") or sql.startswith("SELECT * FROM matches"):
            self.result = self.previous
        elif sql.startswith("SELECT * FROM prod2_match_provenance"):
            self.result = self.provenance
        elif sql.startswith("SELECT * FROM prod2_evaluations"):
            self.result = self.done
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return [self.result]


def publish(conn, **overrides):
    options = dict(profile_id=PROFILE, tenant_id=TENANT,
        expected_profile_fingerprint=subject.profile_fingerprint(conn.profile),
        score=74, verdict="ambar", breakdown={"requisitos": [], "meta": {
            "score_validated": False, "source_review_required": True}}, missing_actions=[],
        source_reader=lambda _conn, _case: deepcopy(conn.case),
        backup_callback=lambda snapshot: conn.calls.append(("BACKUP", snapshot)),
        evaluation_id=88)
    options.update(overrides)
    return subject.publish_case(conn, deepcopy(conn.case), **options)


@pytest.mark.parametrize("source,table", [("seace_prod2", "opportunity_matches"), ("seace_prod6", "matches")])
def test_publication_preserves_provisional_source_and_backs_up_before_write(source, table):
    conn = Connection(case(source))
    result = publish(conn)
    assert result["match_id"] == 99 and result["score_validated"] is False
    backup = next(i for i, (sql, _) in enumerate(conn.calls) if sql == "BACKUP")
    insert = next(i for i, (sql, _) in enumerate(conn.calls) if sql.startswith(f"INSERT INTO {table}("))
    assert backup < insert
    sql, args = conn.calls[insert]
    assert "DO NOTHING" in sql
    assert '"score_validated": false' in args[-2]
    assert '"source_review_required": true' in args[-2]
    assert not any("UPDATE prod2_document_extractions" in sql or "worker_jobs" in sql for sql, _ in conn.calls)
    assert any("prod2_match_provenance" in sql for sql, _ in conn.calls) == (source == "seace_prod2")
    assert any("INSERT INTO match_events" in sql for sql, _ in conn.calls) == (source == "seace_prod6")


@pytest.mark.parametrize("change", ["profile", "source", "reading", "published", "reservation"])
def test_drift_or_unbound_payment_never_writes_match(change):
    conn = Connection(case())
    options = {}
    if change == "profile":
        options["expected_profile_fingerprint"] = "f" * 64
    elif change == "source":
        options["source_reader"] = lambda _conn, frozen: {**frozen, "document_sha256": "b" * 64}
    elif change == "reading":
        options["source_reader"] = lambda _conn, frozen: {**frozen, "requires_human_review": False}
    elif change == "published":
        conn.previous = {"id": 129, "score": 50}
    else:
        conn.done["status"] = "uncertain"
    with pytest.raises(subject.PublicationConflict):
        publish(conn, **options)
    assert not any(sql.startswith("INSERT INTO opportunity_matches") or sql.startswith("UPDATE opportunity_matches") for sql, _ in conn.calls)


def test_replacement_is_explicit_full_row_cas_and_keeps_user_fields():
    old = {"id": 911, "score": 30, "notas": "private user note", "user_state": "interesada"}
    conn = Connection(case("seace_prod6"), previous=deepcopy(old))
    publish(conn, expected_previous=old, allowed_replacements=frozenset({("seace_prod6", 911)}))
    sql, _ = next((sql, args) for sql, args in conn.calls if sql.startswith("UPDATE matches"))
    assert "notas=" not in sql and "user_state=" not in sql
    saved = next(args for sql, args in conn.calls if sql == "BACKUP")
    assert saved["previous_match"] == old


def test_replacement_id_on_other_source_is_not_authorized():
    old = {"id": 129, "score": 30}
    conn = Connection(case("seace_prod6"), previous=old)
    with pytest.raises(subject.PublicationConflict, match="allowlist"):
        publish(conn, expected_previous=old, allowed_replacements=frozenset({("seace_prod2", 129)}))


def test_backup_failure_prevents_mutation():
    conn = Connection(case())
    def fail(_snapshot):
        raise OSError("disk full")
    with pytest.raises(OSError):
        publish(conn, backup_callback=fail)
    assert not any(sql.startswith("INSERT") or sql.startswith("UPDATE") for sql, _ in conn.calls)


@pytest.mark.parametrize("meta", [{"score_validated": True, "source_review_required": True},
    {"score_validated": False, "source_review_required": False}, {"score_validated": False}])
def test_cannot_silently_approve_score_or_source(meta):
    conn = Connection(case())
    with pytest.raises(subject.PublicationConflict):
        publish(conn, breakdown={"meta": meta})
    assert not conn.calls


def test_prod6_cannot_be_reserved_under_fake_prod2_identity():
    conn = Connection(case("seace_prod6"))
    assert subject.create_evaluation_before_dispatch(conn, conn.case, profile_id=PROFILE,
        request_hash="a" * 64, model="combined", policy_version="test") is None
    assert not conn.calls


def test_profile_fingerprint_uses_raw_model_fields_and_not_native_md5():
    one = {"razon_social": "DIN", "experience_json": [{"amount": 10}], "profile_hash": "a" * 32}
    two = {**one, "profile_hash": "b" * 32}
    assert subject.profile_fingerprint(one) == subject.profile_fingerprint(two)
    assert subject.profile_fingerprint(one) != subject.profile_fingerprint({**one, "experience_json": [{"amount": 11}]})


def test_preexisting_transaction_is_rejected_before_publication():
    conn = Connection(case())
    conn.info = SimpleNamespace(transaction_status=subject.TransactionStatus.INTRANS)
    with pytest.raises(subject.PublicationConflict, match="preexisting transaction"):
        publish(conn)
    assert not conn.calls


def test_same_id_with_changed_full_row_and_provenance_is_rejected():
    old = {"id": 129, "score": 30}
    conn = Connection(case(), previous={**old, "score": 31})
    with pytest.raises(subject.PublicationConflict, match="Previous match changed"):
        publish(conn, expected_previous=old, allowed_replacements=frozenset({("seace_prod2", 129)}))
    conn.previous = old
    conn.provenance = {"match_id": 129, "evaluation_id": 999}
    with pytest.raises(subject.PublicationConflict, match="provenance changed"):
        publish(conn, expected_previous=old, expected_provenance={"match_id": 129, "evaluation_id": 998},
            allowed_replacements=frozenset({("seace_prod2", 129)}))
    assert not any(sql.startswith("INSERT") or sql.startswith("UPDATE") for sql, _ in conn.calls)
