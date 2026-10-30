"""accepted_unread: --llm1-min-affinity reads only accepted candidates at or above it.

Offline: no source, PostgreSQL, Redis, provider or embedding calls.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from buenapro_worker.prod2 import embedding_resume, input_policy, prior_reading_guard, selected_read
from buenapro_worker.prod2.keyword_chain import assess_final_report
from test_prod2_handoff_runner import options as id_options
from test_prod2_handoff_selection import package
from test_prod2_keyword_chain import RUN, report as chain_report
from test_prod2_pending_continuation import (Stage as PendingStage, bind as bind_pending, context as pending_context,
    outcome as pending_outcome, subject as pending)
from test_prod2_service import Clock, Egress, NOW, configuration, fake_children, paths, service, write as write_private

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("prod2_min_affinity_script", ROOT / "scripts/run_prod2_selected.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


@pytest.fixture(autouse=True)
def stops(monkeypatch):
    monkeypatch.setattr(subject, "STOP", threading.Event())
    monkeypatch.setattr(subject, "EMBEDDING_DISPATCH_STOP", threading.Event())
    monkeypatch.setattr(subject, "HANDOFF_GUARD", None)


def args(*values):
    return subject.parser().parse_args(["--folder", "tmp/synthetic-selected", *values])


def frozen(requested):
    return {**requested, "requested": requested, "run_id": "batch1", "commercial_profiles": {}, "gemini_model": "synthetic"}


# Planning, manifest and identity.

def test_default_threshold_adds_no_key_and_older_manifests_read_as_sixty():
    legacy = subject.plan(args("--sweep-id", "initial"))
    assert "llm1_min_affinity" not in legacy
    assert subject.plan(args("--sweep-id", "initial", "--llm1-min-affinity", "60")) == legacy
    assert subject.llm1_min_affinity(legacy) == subject.llm1_min_affinity({}) == 60
    assert subject.parser().get_default("llm1_min_affinity") == 60


def test_threshold_is_requested_and_frozen_with_its_own_queue_identity():
    old = frozen(subject.plan(args("--sweep-id", "initial")))
    new = frozen(subject.plan(args("--sweep-id", "initial", "--llm1-min-affinity", "85")))
    assert new["llm1_min_affinity"] == new["requested"]["llm1_min_affinity"] == 85
    assert type(new["llm1_min_affinity"]) is int and subject.llm1_min_affinity(new) == 85
    # A frozen folder compares ``requested`` whole, so neither direction resumes silently.
    assert new["requested"] != old["requested"]
    assert subject.staging_identity(new) != subject.staging_identity(old)
    same = frozen(subject.plan(args("--sweep-id", "initial", "--llm1-min-affinity", "85.0")))
    assert subject.staging_identity(same) == subject.staging_identity(new)
    other = frozen(subject.plan(args("--sweep-id", "initial", "--llm1-min-affinity", "90")))
    assert subject.staging_identity(other) != subject.staging_identity(new)
    assert subject.plan(args("--llm1-min-affinity", "84.5"))["llm1_min_affinity"] == 84.5
    assert subject.plan(args("--llm1-min-affinity", "100"))["llm1_min_affinity"] == 100


@pytest.mark.parametrize("value", ["59.99", "100.01", "0", "-1", "nan", "inf"])
def test_threshold_outside_sixty_to_one_hundred_is_refused(value):
    with pytest.raises(ValueError, match="between the Jev threshold"):
        subject.plan(args("--llm1-min-affinity", value))


@pytest.mark.parametrize("value", [True, 59, 100.5, "85", None, float("nan")])
def test_invalid_frozen_threshold_is_refused(value):
    with pytest.raises(ValueError, match="Invalid frozen LLM1 minimum affinity"):
        subject.llm1_min_affinity({"llm1_min_affinity": value})


def test_id_import_reads_every_accepted_candidate(package):
    parse = subject.parser().parse_args
    assert "llm1_min_affinity" not in subject.plan(parse(id_options(package)))
    with pytest.raises(ValueError, match="ID import reads every accepted candidate"):
        subject.plan(parse(id_options(package, "--llm1-min-affinity", "85")))


def test_embedding_fork_cannot_change_the_threshold_nor_its_source_queue():
    default = {"jev_threshold": 60, "selector_high": 80}
    embedding_resume._same_scope(default, dict(default))
    embedding_resume._same_scope({**default, "llm1_min_affinity": 85}, {**default, "llm1_min_affinity": 85})
    for source, new in ((default, {**default, "llm1_min_affinity": 85}), ({**default, "llm1_min_affinity": 85}, default),
                        ({**default, "llm1_min_affinity": 85}, {**default, "llm1_min_affinity": 90})):
        with pytest.raises(embedding_resume.EmbeddingResumeError, match="frozen source"):
            embedding_resume._same_scope(source, new)
    for flags in ((), ("--llm1-min-affinity", "85")):
        manifest = frozen(subject.plan(args("--sweep-id", "initial", *flags)))
        assert embedding_resume._old_queue(manifest) == subject.staging_identity(manifest)


# Completion.

def completion_report(**changes):
    return {"source_coverage_complete": True, "redis": {"queued": 0, "pending": 0},
        "accepted": 10, "llm1_complete": 3, "accepted_unread": 7, "costs": {"usage_unknown_attempts": 0},
        "records": [{"accepted": True, "status": "llm1_complete"}] * 3
            + [{"accepted": True, "status": "accepted_unread"}] * 7 + [{"accepted": False, "status": "rejected_below_60"}], **changes}


def test_accepted_unread_is_a_settled_outcome_for_exit_codes():
    bounded, unlimited = (subject.execution_policy(args(*extra)) for extra in (("--max-accepted", "10"), ("--all-results",)))
    assert subject.completion_exit_code(completion_report(), bounded) == 0
    assert subject.completion_exit_code(completion_report(), unlimited) == 0
    older = completion_report(llm1_complete=10)
    del older["accepted_unread"]
    assert subject.completion_exit_code(older, bounded) == 0
    # A missing reading, an unread count that does not add up or a review item still stops it.
    assert subject.completion_exit_code(completion_report(accepted_unread=6), bounded) == 2
    assert subject.completion_exit_code(completion_report(accepted_unread=6), unlimited) == 2
    assert subject.completion_exit_code(completion_report(accepted=11), unlimited) == 2
    review = completion_report(records=[*completion_report()["records"], {"accepted": False, "status": "needs_review"}])
    assert subject.completion_exit_code(review, unlimited) == 2


def chain_with_unread(unread=2, done=1):
    value = chain_report()
    value.update(accepted=done + unread, llm1_complete=done, accepted_unread=unread, processed=3,
        records=[{"status": "llm1_complete"}] * done + [{"status": "accepted_unread"}] * unread
            + [{"status": "rejected_below_60"}] * (3 - done - unread))
    return value


def test_keyword_chain_counts_accepted_unread_as_complete_not_review():
    result = assess_final_report(chain_with_unread(), RUN, 0)
    assert result["advance"] is True and result["state"] == "complete"
    assert result["reason"] == "source_and_queue_complete"
    assert result["summary"]["accepted_unread"] == 2
    assert result["summary"]["outcome_status_counts"] == {"llm1_complete": 1, "accepted_unread": 2}
    only_unread = assess_final_report(chain_with_unread(unread=3, done=0), RUN, 0)
    assert only_unread["state"] == "complete"
    # Reports written before the status existed carry no count and behave as before.
    older = chain_report()
    assert "accepted_unread" not in older and assess_final_report(older, RUN, 0)["state"] == "complete"


def test_keyword_chain_still_flags_what_unread_does_not_explain():
    missing = chain_with_unread(unread=1, done=1)
    missing["accepted"] = 3
    assert assess_final_report(missing, RUN, 0)["state"] == "complete_with_review"
    assert assess_final_report(chain_with_unread(), RUN, 2)["state"] == "complete_with_review"
    reviewed = chain_with_unread()
    reviewed["requires_review"] = 1
    assert assess_final_report(reviewed, RUN, 0)["state"] == "complete_with_review"
    reviewed = chain_with_unread()
    reviewed["records"][-1]["status"] = "needs_review"
    assert assess_final_report(reviewed, RUN, 0)["state"] == "complete_with_review"


@pytest.mark.parametrize("value", [3, -1, True, "2", 1.0, None])
def test_keyword_chain_refuses_inconsistent_or_malformed_unread_counts(value):
    result = assess_final_report({**chain_with_unread(), "accepted_unread": value}, RUN, 0)
    assert result["advance"] is False and result["reason"] == "invalid_reading_counts"


# The service accepts the flag, freezes it in the epoch and reports the unread cases.

@pytest.mark.parametrize("flag", [["--llm1-min-affinity", "85"], ["--llm1-min-affinity=85"]])
def test_service_accepts_the_flag_in_base_argv_and_binds_it_into_the_epoch(paths, monkeypatch, flag):
    config, state = paths
    value = configuration()
    value["base_argv"] += flag
    write_private(config, value)
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    plan = service._read(children[0].folder / "plan.private.json")
    assert plan["requested"]["llm1_min_affinity"] == 85
    assert "--llm1-min-affinity" in {part.split("=")[0] for part in plan["argv"]}
    assert service._read(children[0].folder / "manifest.private.json")["requested"] == plan["requested"]


def test_service_does_not_require_the_flag_and_its_default_changes_no_epoch_input(paths, monkeypatch):
    config, state = paths
    write_private(config, configuration())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    plan = service._read(children[0].folder / "plan.private.json")
    assert "llm1_min_affinity" not in plan["requested"] and "--llm1-min-affinity" not in plan["argv"]


@pytest.mark.parametrize("flag", [["--llm1-min-affinity", "59"], ["--llm1-min-affinity", "101"]])
def test_service_refuses_an_out_of_range_threshold_before_any_child(paths, monkeypatch, flag):
    config, state = paths
    value = configuration()
    value["base_argv"] += flag
    write_private(config, value)
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == [] and service._read(state / "state.private.json")["status"] == "attention_required"


def test_service_completes_a_cycle_with_unread_cases_and_alerts_them(paths, monkeypatch):
    config, state = paths
    value = configuration()
    value["base_argv"] += ["--llm1-min-affinity", "85"]
    write_private(config, value)

    def with_unread(report):
        report.update(accepted=3, llm1_complete=1, accepted_unread=2, processed=3,
            records=[{"status": "llm1_complete"}] + [{"status": "accepted_unread"}] * 2)
        report["redis"]["seen"] = 3

    fake_children(monkeypatch, mutate=with_unread)
    egress = Egress(monkeypatch, [])
    clock = Clock()
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert service._read(state / "state.private.json")["status"] == "complete"
    assert "3 aceptadas, 1 leídas, 2 sin leer, 0 en revisión" in egress.alerts[-1]


# Continuation of a frozen queue and business-batch attribution.

def test_continuation_inherits_the_threshold_and_lists_unread_as_finished(pending_context, monkeypatch):
    original = pending_context.original / "manifest.private.json"
    original.unlink()
    pending_context.old["llm1_min_affinity"] = 85
    pending.private_write(original, pending_context.old)
    base, manifest = bind_pending(pending_context)
    assert base["llm1_min_affinity"] == manifest["llm1_min_affinity"] == 85
    stage = PendingStage()
    monkeypatch.setattr(pending.selected, "STOP", threading.Event())
    monkeypatch.setattr(pending, "audit_summary", lambda folder: {"usage_unknown_attempts": 0, "known_cost_usd": .01})
    for conv, accepted, status in [(20, False, "rejected_below_60"), (21, True, "accepted_unread"), (22, True, "llm1_complete")]:
        identity, _ = stage.next()
        pending_outcome(pending_context.folder, conv, accepted=accepted, status=status)
        stage.ack(identity)
    value = pending.report(pending_context.folder, manifest, stage, SimpleNamespace(requests=1), began=0)
    assert value["accepted_unread_ids"] == [21] and value["accepted_unread"] == 1
    assert value["llm1_complete_ids"] == [22] and value["needs_review"] == 0 and value["accepted"] == 2
    assert value["complete"]
    other = pending_context.folder.parent / "other"
    pending_outcome(other, 21, accepted=False, status="accepted_unread")
    with pytest.raises(ValueError, match="authorized durable classification"):
        pending.read_outcomes(other, manifest)


def test_business_batch_report_counts_unread_apart_from_reads():
    from buenapro_worker.prod2.business_batches import report_business_batches
    manifest = {"business_plan": {"keyword_index": {"software": {"search_batch_id": "b1"}},
        "batches": [{"id": "b1", "search_keyword_count": 1}]}, "resolved_years": [2026], "objects": ["service"]}
    coverage = [{"query": {"keyword": "software", "year": 2026, "object_type": "service"}, "coverage_complete": True}]
    records = [{"business_batch_id": "b1", "accepted": True, "status": status}
               for status in ("llm1_complete", "accepted_unread", "accepted_unread")]
    row = report_business_batches(manifest, coverage, records)[0]
    assert (row["accepted"], row["llm1_complete"], row["accepted_unread"]) == (3, 1, 2)


# The consumer.

DETAIL = {"procedure_id": 456, "convocatoria_id": 123, "selection_type": "Licitación Pública",
    "description": "TABLERO ELÉCTRICO", "item_states": ["Convocado"], "document_inventory_complete": True,
    "documents": [{"uuid": "official", "filename": "bases.pdf", "type": "Bases", "stage": "Convocatoria",
        "published_at": "2026-10-19"}],
    "schedule": [{"stage": "Registro de participantes", "starts_at_text": "19/10/2026 09:00",
        "ends_at_text": "20/10/2026 09:00"}, {"stage": "Presentación de ofertas",
        "starts_at_text": "20/10/2026 10:00", "ends_at_text": "20/10/2026 11:00"}]}
CAPTURE = {"observed_at": "2026-10-20T15:30:00+00:00", "detail": DETAIL}
DOCUMENT_STAGES = ("download", "preview_candidates", "JevDocumentSelector", "paid_document_selection",
                   "prepare_selected_document_set", "list_archive", "extract_members",
                   "reuse_completed_read", "persist_completed_llm1", "upload_preview", "record_document_download")


class Queue:
    def __init__(self, conv=123):
        self.conv, self.acked, self.sent = conv, [], False

    def next(self):
        if self.sent:
            return None
        self.sent = True
        return str(self.conv), {"row": {"convocation_id": self.conv, "procedure_id": 456, "description": "TABLERO ELÉCTRICO",
            "object_type": "good"}, "query": {"keyword": "tablero"}, "observed_at": "2026-10-20T00:00:00+00:00",
            "page_number": 1}

    def ack(self, value):
        self.acked.append(value)


def consume(tmp_path, monkeypatch, affinity, manifest, *, prior=None, enrich=None, detail=DETAIL):
    """Run the real consumer over one accepted candidate with every outside call faked."""
    @contextmanager
    def connect(*a, **kw):
        yield None

    @contextmanager
    def permission(*a, **kw):
        yield lambda: None

    seen = SimpleNamespace(promoted=[], touched=[], persisted=[])
    monkeypatch.setattr(subject.psycopg, "connect", connect)
    monkeypatch.setattr(subject, "provider_permission", permission)
    monkeypatch.setattr(subject, "AuditedJevClient", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(subject, "JevDescriptionAnalyzer", lambda *a, **kw: object())
    monkeypatch.setattr(subject, "jev_request_hash", lambda *a, **kw: "a" * 64)
    monkeypatch.setattr(subject, "paid_jev", lambda *a: ("a" * 64, {"affinity_index": affinity, "cost_usd": 0}, True))
    monkeypatch.setattr(subject.repo, "promote_accepted", lambda *a, **kw: seen.promoted.append(kw["row"]["convocation_id"]) or {"created": True})
    monkeypatch.setattr(prior_reading_guard, "preserve_prior_reading", lambda conn, **kw: prior)
    monkeypatch.setattr(subject, "cleanup_finished_case", lambda *a: None)
    if enrich is not None:
        monkeypatch.setattr(subject, "enrich", enrich)

    def forbidden(name):
        def stop(*a, **kw):
            seen.touched.append(name)
            pytest.fail("no document or reading work expected: " + name)
        return stop
    for name in DOCUMENT_STAGES:
        monkeypatch.setattr(subject, name, forbidden(name))
    monkeypatch.setattr(selected_read, "paid_selected_read", forbidden("paid_selected_read"))
    monkeypatch.setattr(input_policy, "prepare_input", forbidden("prepare_input"))
    monkeypatch.setattr(subject, "get_detail", lambda *a, **kw: {**CAPTURE, "detail": detail})
    monkeypatch.setattr(subject, "persist_detail", lambda conn, saved, **kw: seen.persisted.append(saved) or {"detail_hash": "d" * 64})
    monkeypatch.setattr(subject, "save_participation_assessment", lambda *a, **kw: None)
    limits = subject.RunLimits(max_accepted=5, max_candidates=5, max_jev_calls=5, max_llm1_calls=5, max_embedding_calls=5)
    queue = Queue()
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = {"run_id": "synthetic", "commercial_profiles": {}, "profile_id": "profile", "gemini_model": "synthetic",
            "document_storage_policy": subject.OFFICIAL_LINKS_POLICY}
    subject.consume(SimpleNamespace(database_url="synthetic"), queue, {**base, **manifest}, tmp_path, None, limits, "synthetic")
    assert queue.acked == ["123"]
    return subject.read_private(tmp_path / "123/outcome.private.json"), limits, seen


def recording_enrich(calls):
    def enrich(conn, settings, private, item, manifest, pacer, limits, jev_key, **kwargs):
        calls.append(kwargs)
        return {"status": "llm1_complete"}
    return enrich


@pytest.mark.parametrize("affinity,manifest,deferred", [
    (84.99, {"llm1_min_affinity": 85}, True),
    (60, {"llm1_min_affinity": 85}, True),
    (85, {"llm1_min_affinity": 85}, False),
    (99.5, {"llm1_min_affinity": 85}, False),
    (60, {}, False),
    (70, {}, False),
])
def test_consumer_defers_reading_only_below_the_threshold(tmp_path, monkeypatch, affinity, manifest, deferred):
    calls = []
    outcome, limits, seen = consume(tmp_path, monkeypatch, affinity, manifest, enrich=recording_enrich(calls))
    assert calls == [{"defer_reading": True} if deferred else {}]
    assert outcome["accepted"] is True and outcome["affinity_index"] == affinity
    assert outcome["status"] == "llm1_complete"  # The fake enrich answers; deferral is the real enrich's decision.
    assert seen.promoted == [123] and limits.accepted_ids == {123}


def test_below_threshold_is_promoted_with_its_ficha_and_spends_no_reading_budget(tmp_path, monkeypatch):
    outcome, limits, seen = consume(tmp_path, monkeypatch, 84.99, {"llm1_min_affinity": 85})
    assert outcome["status"] == "accepted_unread" and outcome["accepted"] is True
    assert outcome["affinity_index"] == 84.99 and outcome["llm1_min_affinity"] == 85
    assert seen.promoted == [123] and limits.accepted_ids == {123}  # Counts against --max-accepted.
    assert seen.persisted == [DETAIL] and outcome["detail_hash"] == "d" * 64
    assert outcome["temporal_state"] and outcome["schedule"] == DETAIL["schedule"]
    assert outcome["documents"] == [{"document_uuid": "official", "filename": "bases.pdf", "type": "Bases",
        "stage": "Convocatoria", "published_at": "2026-10-19", "source_url": subject.METADATA_URL + "?id=official"}]
    assert seen.touched == [] and not outcome.get("requires_human_review")
    assert limits.calls == {"jev": 0, "llm1": 0, "embedding": 0}
    assert limits.reserved == {"jev": 0, "llm1": 0, "embedding": 0}
    assert (tmp_path / "123/metadata.private.json").exists()
    assert not list((tmp_path / "123").glob("*.download.private.json"))


def test_exactly_the_threshold_takes_the_full_reading_path(tmp_path, monkeypatch):
    # The first step after the ficha is the official inventory check: an incomplete inventory stops a
    # full read there, while a candidate just below the threshold never gets that far.
    incomplete = {**DETAIL, "document_inventory_complete": False}
    read, _, _ = consume(tmp_path / "read", monkeypatch, 85, {"llm1_min_affinity": 85}, detail=incomplete)
    assert read["status"] == "needs_review" and read["error_type"] == "RuntimeError"
    unread, _, _ = consume(tmp_path / "unread", monkeypatch, 84.99, {"llm1_min_affinity": 85}, detail=incomplete)
    assert unread["status"] == "accepted_unread" and unread["document_inventory_complete"] is False


@pytest.mark.parametrize("prior_status", ["llm1_complete", "needs_review"])
def test_a_candidate_with_a_prior_reading_keeps_todays_outcome(tmp_path, monkeypatch, prior_status):
    prior = {"status": prior_status, "llm1_prior_preserved": True, "llm1_cost_usd": 0}
    outcome, limits, seen = consume(tmp_path, monkeypatch, 70, {"llm1_min_affinity": 85,
        "prior_reading_policy": "prod2_preserve_prior_readings_v1"}, prior=prior, enrich=recording_enrich([]))
    assert outcome["status"] == prior_status and outcome["llm1_prior_preserved"] is True
    assert seen.promoted == [123] and (tmp_path / "123/prior-reading-preserved.private.json").exists()


def test_below_sixty_is_still_rejected_whatever_the_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(subject.repo, "record_run_member", lambda *a, **kw: None)
    outcome, limits, seen = consume(tmp_path, monkeypatch, 59.9, {"llm1_min_affinity": 85}, enrich=recording_enrich([]))
    assert outcome["status"] == "rejected_below_60" and outcome["accepted"] is False and seen.promoted == []


def test_enrich_defers_before_the_inventory_check_but_a_full_read_still_needs_it(tmp_path, monkeypatch):
    capture = {**CAPTURE, "detail": {**DETAIL, "document_inventory_complete": False}}
    monkeypatch.setattr(subject, "persist_detail", lambda *a, **kw: {"detail_hash": "d" * 64})
    monkeypatch.setattr(subject, "save_participation_assessment", lambda *a, **kw: None)
    monkeypatch.setattr(subject, "download", lambda *a, **kw: pytest.fail("no document download"))
    manifest = {"llm1_min_affinity": 85}
    item = {"row": {"convocation_id": 123}}
    record = subject.enrich(None, None, tmp_path, item, manifest, None, None, "offline", capture=capture, defer_reading=True)
    assert record["status"] == "accepted_unread" and record["document_inventory_complete"] is False
    with pytest.raises(RuntimeError, match="Official inventory incomplete"):
        subject.enrich(None, None, tmp_path, item, manifest, None, None, "offline", capture=capture)
    # An older manifest has no key and an unread record then reports the Jev floor.
    older = subject.enrich(None, None, tmp_path, item, {}, None, None, "offline", capture=capture, defer_reading=True)
    assert older["llm1_min_affinity"] == 60
