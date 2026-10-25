import threading
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs import poll_prod6_profile as poll_module
from buenapro_worker.jobs.poll_prod6_profile import matches_profile_scope, poll_prod6_profile
from buenapro_worker.jobs.scheduler import enqueue_poll_search_jobs
from buenapro_worker.prod6 import screening
from buenapro_worker.seace import pacing
from buenapro_worker.seace.schemas import SearchResponse
from buenapro_worker.settings import Settings


LINES = [{"cubso_segmentos": ["39"], "keyword_phrases": ["tableros eléctricos"],
          "keyword_terms": ["ccm", "variadores"], "keywords": []}]


def settings(**kwargs):
    return Settings(database_url="postgresql://unused", gemini_api_key="unused",
                    seace_allowed_codigo_objeto="1,2", seace_allowed_segments="43,81", **kwargs)


def response(items, total=1):
    return SearchResponse.model_validate({"data": items,
        "pageable": {"pageNumber": 1, "pageSize": 100, "totalElements": total}})


def item(id, text, obj=1, state=2, year=2026):
    return {"idContrato": id, "desContratacion": f"CM-{id}-{year}",
            "idObjetoContrato": obj, "desObjetoContrato": text,
            "idEstadoContrato": state, "fecPublica": f"01/10/{year} 10:00:00"}


class DecisionStore:
    """In-memory stand-in for the PostgreSQL decision repository."""

    def __init__(self):
        self.rows = {}

    def record(self, _conn, *, item, **fields):
        key = (item.id_contrato, fields["profile_id"], fields["contract_hash"],
               fields["profile_hash"], fields["policy_version"])
        if key in self.rows:
            return False
        self.rows[key] = fields
        return True

    def by_contract(self):
        return {key[0]: row for key, row in self.rows.items()}


def fake_snapshot(conn, profile_id):
    lines = [dict(line, nombre=f"line{number}", score_umbral=70)
             for number, line in enumerate(conn.execute("lines").fetchall())]
    return {"id": profile_id, "descripcion": "", "lines": lines, "profile_hash": "profile-hash"}


@pytest.fixture(autouse=True)
def decisions(monkeypatch):
    """Keep these tests DB-free; the decision repository has its own PostgreSQL tests.

    Policy v2 never accepts in the poll, so the admission path is exercised with a
    stand-in of the old keyword rule (accept on a keyword hit) unless a test restores
    ``screening.decide``.
    """
    store = DecisionStore()
    hits = {}
    real_prefilter = poll_module.prefilter

    def prefilter(*args, **kwargs):
        result = real_prefilter(*args, **kwargs)
        hits["keyword_lines"] = result[2]
        return result

    def keyword_decide(prefilter_passed):
        if not prefilter_passed:
            return "rejected", "prefilter"
        return ("accepted" if hits["keyword_lines"] else "pending"), "keywords"

    monkeypatch.setattr(poll_module, "prefilter", prefilter)
    monkeypatch.setattr(poll_module, "decide", keyword_decide)
    monkeypatch.setattr(poll_module, "profile_snapshot", MagicMock(side_effect=fake_snapshot))
    monkeypatch.setattr(poll_module, "record_decision", store.record)
    return store


def test_scope_specific_phrase_or_two_terms_and_boundaries():
    assert matches_profile_scope("Instalación de TABLEROS ELÉCTRICOS", LINES, 39)
    assert matches_profile_scope("CCM con variadores", LINES, 39)
    assert not matches_profile_scope("Variadores solamente", LINES, 39)
    assert not matches_profile_scope("Tableros eléctricos", LINES, 43)
    assert not matches_profile_scope("xxccm xxvariadores", LINES, 39)


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_poll_only_current_relevant_vigente_goods_services(client_cls, upsert, decisions):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "tableros eléctricos"), item(2, "papelería"),
                  item(3, "tableros eléctricos", state=3), item(4, "tableros eléctricos", year=2025)]),
        response([item(5, "Mantenimiento de tableros eléctricos", obj=2)])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile")
    assert stats["candidates"] == stats["changed"] == stats["enqueued"] == 2
    assert stats["skipped"] == 3
    # Only candidates are upserted/enqueued; rejected and pending ones just get a decision.
    assert [call.args[1].id_contrato for call in upsert.call_args_list] == [1, 5]
    assert repo.enqueue.call_count == 2
    assert {id: (row["decision"], row["decided_by"]) for id, row in decisions.by_contract().items()} == {
        1: ("accepted", "keywords"), 2: ("pending", "keywords"),
        3: ("rejected", "prefilter"), 5: ("accepted", "keywords")}
    assert decisions.by_contract()[3]["prefilter_reasons"] == ["not_vigente"]
    assert 4 not in decisions.by_contract()  # wrong year: neither stored nor decided
    assert (stats["decisions_recorded"], stats["decisions_reused"], stats["prefilter_rejected"]) == (4, 0, 1)
    calls = client_cls.return_value.__enter__.return_value.search_contracts.call_args_list
    assert [(call.kwargs["objeto"], call.kwargs["segmento"]) for call in calls] == [(1,39),(2,39)]


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_unchanged_or_filtered_page_does_not_stop_pagination(client_cls, upsert):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [None]
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "papelería")], total=201),
        response([item(2, "tableros eléctricos")], total=201),
        response([item(3, "tableros eléctricos")], total=201), response([])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile", max_pages_per_segment=2)
    assert stats["candidates"] == 1
    assert stats["capped_segments"] == 1
    assert stats["enqueued"] == 1


def test_scheduler_profile_discovery_requires_opt_in():
    repo = MagicMock()
    assert "poll_prod6_profiles" not in enqueue_poll_search_jobs(settings(), repo, year=2026)
    assert "poll_prod6_profiles" in enqueue_poll_search_jobs(settings(prod6_profile_poll_enabled=True), repo, year=2026)


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_completed_extraction_not_requeued_and_expired_not_saved(client_cls, upsert, decisions):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.side_effect = [{"exists": 1}]
    repo.conn.execute.return_value.fetchall.return_value = LINES
    expired = item(2, "tableros eléctricos") | {"fecFinCotizacion": "01/01/2020 10:00:00"}
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "tableros eléctricos"), expired]), response([])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile")
    assert stats["candidates"] == 1
    assert stats["enqueued"] == 0
    assert stats["skipped"] == 1
    repo.enqueue.assert_not_called()
    assert decisions.by_contract()[2]["prefilter_reasons"] == ["closed"]
    assert decisions.by_contract()[2]["decision"] == "rejected"
    upsert.assert_called_once()


def test_rejects_unbounded_or_inactive_profile():
    with pytest.raises(ValueError):
        poll_prod6_profile(settings(), MagicMock(), profile_id="profile", max_candidates=0)
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = None
    poll_module.profile_snapshot.side_effect = screening.profile_snapshot
    with pytest.raises(ValueError, match="Active company"):
        poll_prod6_profile(settings(), repo, profile_id="missing")


def parallel_lines():
    return [dict(LINES[0], cubso_segmentos=["39", "43"])]


def parallel_repo(*, extracted=None):
    coordinator = threading.get_ident()
    repo = MagicMock()

    def execute(sql, *_args):
        assert threading.get_ident() == coordinator, "DB connection reached an HTTP thread"
        cursor = MagicMock()
        cursor.fetchone.return_value = extracted
        cursor.fetchall.return_value = parallel_lines()
        return cursor

    def enqueue(*_args, **_kwargs):
        assert threading.get_ident() == coordinator, "Queue write reached an HTTP thread"
        return 1

    repo.conn.execute.side_effect = execute
    repo.enqueue.side_effect = enqueue
    return repo


@pytest.mark.parametrize("parallelism", [2, 4])
def test_parallel_queries_overlap_with_separate_clients_and_sequential_pagination(parallelism):
    coordinator = threading.get_ident()
    barrier = threading.Barrier(parallelism)
    lock = threading.Lock()
    clients = []
    queries = []
    active = 0
    maximum = 0

    class Client:
        def __init__(self, _settings):
            self.owner = threading.get_ident()
            self.pages = []
            self.stream = None
            self.closed = False
            with lock:
                clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def search_contracts(self, **query):
            nonlocal active, maximum
            assert threading.get_ident() == self.owner != coordinator
            stream = (query["objeto"], query["segmento"])
            assert self.stream in (None, stream), "A client was shared between streams"
            self.stream = stream
            assert query["page"] == len(self.pages) + 1
            self.pages.append(query["page"])
            with lock:
                queries.append(query)
                active += 1
                maximum = max(maximum, active)
            try:
                if query["page"] == 1:
                    barrier.wait(timeout=5)
                # A filtered page must not prevent page 2. Empty page 3 ends
                # only this stream, independent of other clients' progress.
                text = "papelería" if query["page"] == 1 else "tableros eléctricos"
                items = [] if query["page"] == 3 else [item(
                    query["objeto"] * 1000 + query["segmento"] * 10 + query["page"],
                    text, obj=query["objeto"])]
                return response(items, total=201)
            finally:
                with lock:
                    active -= 1

    def upsert(*_args, **_kwargs):
        assert threading.get_ident() == coordinator
        return False

    repo = parallel_repo()
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", Client), \
            patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", side_effect=upsert):
        stats = poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026,
                                  max_pages_per_segment=100, search_parallelism=parallelism)
    assert maximum == parallelism
    assert len(clients) == 4
    assert all(client.closed and client.pages == [1, 2, 3] for client in clients)
    assert {client.stream for client in clients} == {(1, 39), (1, 43), (2, 39), (2, 43)}
    assert all(query["anio"] == 2026 and query["estado"] == 2 and query["page_size"] == 100
               for query in queries)
    assert stats["http_requests"] == stats["pages"] == 12
    assert stats["http_rows"] == stats["seen"] == 8
    assert stats["candidates"] == stats["enqueued"] == 4
    assert stats["capped_segments"] == stats["limit_reached"] == 0


def test_parallel_candidate_cap_reports_all_actual_queries_and_stream_caps():
    class Client:
        def __init__(self, _settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def search_contracts(self, **query):
            return response([item(
                query["objeto"] * 1000 + query["segmento"] * 10 + query["page"],
                "papelería" if query["page"] == 1 else "tableros eléctricos",
                obj=query["objeto"])], total=301)

    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", Client), \
            patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True) as upsert:
        stats = poll_prod6_profile(settings(), parallel_repo(), profile_id="profile", anio=2026,
            max_pages_per_segment=2, max_candidates=1, search_parallelism=4)
    assert stats["http_requests"] == stats["http_rows"] == 8
    assert stats["pages"] == stats["seen"] == 2
    assert stats["capped_segments"] == 4  # Includes streams queried but not applied.
    assert stats["limit_reached"] == stats["candidates"] == stats["enqueued"] == 1
    upsert.assert_called_once()


def test_parallel_scope_and_dedup_preserve_completed_readings_without_new_jobs(decisions):
    class Client:
        def __init__(self, _settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def search_contracts(self, **query):
            obj = query["objeto"]
            expired = item(2, "tableros eléctricos", obj=obj) | {"fecFinCotizacion": "01/01/2020 10:00:00"}
            no_publication = item(3, "tableros eléctricos", obj=obj) | {"fecPublica": None}
            return response([item(1, "tableros eléctricos", obj=obj), expired, no_publication,
                item(4, "tableros eléctricos", obj=obj, year=2025),
                item(5, "tableros eléctricos", obj=obj, state=3),
                item(6, "tableros eléctricos", obj=3), item(7, "variadores", obj=obj)])

    repo = parallel_repo(extracted={"exists": 1})
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", Client), \
            patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False) as upsert:
        stats = poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026,
                                  search_parallelism=2)
    assert stats["http_rows"] == stats["seen"] == 28
    assert stats["skipped"] == 24
    assert stats["candidates"] == 1
    assert stats["enqueued"] == 0
    upsert.assert_called_once()
    # Expired, not vigente and one-term rows only get a decision.
    assert set(decisions.by_contract()) == {1, 2, 5, 7}
    repo.enqueue.assert_not_called()


def test_preserved_candidates_still_obey_bound_without_requeuing():
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient") as client, \
            patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False):
        client.return_value.__enter__.return_value.search_contracts.return_value = response([
            item(1, "tableros eléctricos"), item(2, "tableros eléctricos")])
        repo = parallel_repo(extracted={"exists": 1})
        stats = poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026,
                                  max_candidates=1)
    assert stats["candidates"] == stats["limit_reached"] == 1
    assert stats["seen"] == stats["http_requests"] == 1
    assert stats["http_rows"] == 2
    repo.enqueue.assert_not_called()


@pytest.mark.parametrize("parallelism", [0, -1, 5, True, "2"])
def test_search_parallelism_is_bounded_before_db_or_network(parallelism):
    repo = MagicMock()
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient") as client:
        with pytest.raises(ValueError, match="Search parallelism"):
            poll_prod6_profile(settings(), repo, profile_id="profile", search_parallelism=parallelism)
    repo.conn.execute.assert_not_called()
    client.assert_not_called()


def test_parallel_http_failure_cannot_commit_partial_discovery():
    class Client:
        def __init__(self, _settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def search_contracts(self, **query):
            if query["objeto"] == 2:
                raise RuntimeError("offline source failure")
            return response([item(1, "tableros eléctricos")])

    repo = parallel_repo()
    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", Client), \
            patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item") as upsert:
        with pytest.raises(RuntimeError, match="offline source failure"):
            poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026, search_parallelism=2)
    upsert.assert_not_called()
    repo.enqueue.assert_not_called()


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_policy_v2_poll_leaves_every_prefilter_pass_pending(client_cls, upsert, decisions, monkeypatch):
    monkeypatch.setattr(poll_module, "decide", screening.decide)
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "tableros eléctricos"), item(2, "papelería"),
                  item(3, "tableros eléctricos", state=3)]), response([])]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile")
    assert {id: (row["decision"], row["decided_by"]) for id, row in decisions.by_contract().items()} == {
        1: ("pending", "jev"), 2: ("pending", "jev"), 3: ("rejected", "prefilter")}
    assert (stats["candidates"], stats["enqueued"], stats["skipped"]) == (0, 0, 3)
    upsert.assert_not_called()
    repo.enqueue.assert_not_called()


# --- A 403/429 or a spent request budget ends the sweep but keeps the pages already read ---

def denial(status):
    import httpx
    request = httpx.Request("GET", "https://example.invalid/search")
    return httpx.HTTPStatusError("denied", request=request,
                                 response=httpx.Response(status, request=request))


@pytest.mark.parametrize("stop,flag", [
    (denial(403), "source_blocked"), (denial(429), "source_blocked"),
    (pacing.SourceBlocked(403), "source_blocked"), (pacing.SourceBudgetReached(), "budget_reached")])
@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=False)
@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_serial_sweep_keeps_decisions_of_pages_read_before_the_stop(client_cls, upsert, decisions, stop, flag):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = [
        response([item(1, "papelería"), item(2, "tableros eléctricos")], total=201), stop]
    stats = poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026)
    assert stats[flag] == 1 and stats["http_requests"] == 1
    assert set(decisions.by_contract()) == {1, 2}  # Not rolled back, not requested again.
    assert client_cls.return_value.__enter__.return_value.search_contracts.call_count == 2


@patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient")
def test_other_failures_still_abort_the_sweep(client_cls):
    repo = MagicMock()
    repo.conn.execute.return_value.fetchall.return_value = LINES
    client_cls.return_value.__enter__.return_value.search_contracts.side_effect = denial(404)
    with pytest.raises(Exception, match="denied"):
        poll_prod6_profile(settings(), repo, profile_id="profile", anio=2026)


@pytest.mark.parametrize("parallelism", [1, 3])
def test_blocked_stream_does_not_discard_other_streams_and_ends_the_sweep(parallelism, decisions):
    class Client:
        def __init__(self, _settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def search_contracts(self, **query):
            if (query["objeto"], query["segmento"]) == (1, 43):
                raise denial(403)
            return response([item(query["objeto"] * 100 + query["segmento"], "papelería",
                                  obj=query["objeto"])])

    with patch("buenapro_worker.jobs.poll_prod6_profile.SeaceClient", Client):
        stats = poll_prod6_profile(settings(), parallel_repo(), profile_id="profile", anio=2026,
                                  search_parallelism=parallelism)
    assert stats["source_blocked"] == 1
    assert 139 in decisions.by_contract()  # Stream (1, 39) was read before the denial.
    if parallelism == 1:
        assert set(decisions.by_contract()) == {139}  # Nothing is asked after the denial.


# --- One search sweep for several companies, discovery limit and rotation ----------------

def segment_snapshots(**segments_by_profile):
    def snapshot(_conn, profile_id):
        return {"id": profile_id, "descripcion": "", "profile_hash": f"hash-{profile_id}",
                "lines": [{"nombre": "line", "cubso_segmentos": [str(value) for value in
                                                                 segments_by_profile[profile_id]],
                           "keyword_phrases": ["tableros eléctricos"], "keyword_terms": [],
                           "keywords": [], "score_umbral": 70}]}
    return snapshot


class RecordingSource:
    """Replaces SeaceClient: one result row per stream, every query recorded."""

    def __init__(self, pages=1, text="papelería"):
        self.queries, self.pages, self.text = [], pages, text
        self.lock = threading.Lock()

    def __call__(self, _settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def search_contracts(self, **query):
        with self.lock:
            self.queries.append((query["objeto"], query["segmento"], query["page"]))
        row = item(query["objeto"] * 100000 + int(query["segmento"]) * 10 + query["page"], self.text,
                   obj=query["objeto"])
        return response([row], total=100 * self.pages)

    def streams(self):
        return [(objeto, segment) for objeto, segment, page in self.queries if page == 1]


def poll_many(source, profile_ids, snapshots, monkeypatch, **kwargs):
    from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profiles
    monkeypatch.setattr(poll_module, "profile_snapshot", MagicMock(side_effect=snapshots))
    monkeypatch.setattr(poll_module, "SeaceClient", source)
    return poll_prod6_profiles(settings(), MagicMock(), profile_ids=profile_ids, anio=2026, **kwargs)


def test_one_page_feeds_the_decisions_of_every_profile_with_that_segment(decisions, monkeypatch):
    source = RecordingSource()
    result = poll_many(source, ["a", "b"], segment_snapshots(a=[43, 81], b=[43]), monkeypatch)
    # The union of segments is searched once: 2 objects x {43, 81}, not 4 + 2 streams.
    assert source.streams() == [(1, 43), (1, 81), (2, 43), (2, 81)]
    assert result["http_requests"] == result["pages"] == 4
    rows = {(row_id, profile) for (row_id, profile, *_rest) in decisions.rows}
    segment_of = lambda row_id: (row_id // 10) % 10000  # noqa: E731
    assert {(segment_of(row_id), profile) for row_id, profile in rows} == {
        (43, "a"), (43, "b"), (81, "a")}  # Profile b has no segment 81: no row for it.
    assert len(rows) == 6
    assert result["by_profile"]["a"]["seen"] == 4 and result["by_profile"]["b"]["seen"] == 2
    assert result["by_profile"]["a"]["decisions_recorded"] == 4 and result["by_profile"]["b"]["decisions_recorded"] == 2
    assert result["seen"] == 6 and result["decisions_recorded"] == 6
    assert result["cursor"] == {"next_stream_index": 0, "next_stream": [1, 43], "stream_count": 4,
                                "complete": True}


def test_each_profile_keeps_its_own_prefilter_and_snapshot(decisions, monkeypatch):
    source = RecordingSource()
    poll_many(source, ["a", "b"], segment_snapshots(a=[43], b=[43]), monkeypatch)
    hashes = {(key[1], key[3]) for key in decisions.rows}
    assert hashes == {("a", "hash-a"), ("b", "hash-b")}


@patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True)
def test_a_contract_accepted_by_two_profiles_is_admitted_once_and_counted_for_each(upsert, decisions, monkeypatch):
    from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profiles
    source = RecordingSource(text="Mantenimiento de tableros eléctricos")
    monkeypatch.setattr(poll_module, "profile_snapshot", MagicMock(side_effect=segment_snapshots(a=[43], b=[43])))
    monkeypatch.setattr(poll_module, "SeaceClient", source)
    repo = MagicMock()
    result = poll_prod6_profiles(settings(), repo, profile_ids=["a", "b"], anio=2026)
    assert upsert.call_count == 2  # Two streams, two contracts: not four.
    assert result["by_profile"]["a"]["candidates"] == result["by_profile"]["b"]["candidates"] == 2
    assert repo.enqueue.call_count == 2


def test_a_profile_at_its_candidate_limit_stops_while_the_other_continues(decisions, monkeypatch):
    from buenapro_worker.jobs.poll_prod6_profile import poll_prod6_profiles
    source = RecordingSource(text="Mantenimiento de tableros eléctricos")
    monkeypatch.setattr(poll_module, "profile_snapshot",
                        MagicMock(side_effect=segment_snapshots(a=[43], b=[43, 81])))
    monkeypatch.setattr(poll_module, "SeaceClient", source)
    with patch("buenapro_worker.jobs.poll_prod6_profile.upsert_search_item", return_value=True):
        result = poll_prod6_profiles(settings(), MagicMock(), profile_ids=["a", "b"], anio=2026,
                                    max_candidates=1)
    assert result["by_profile"]["a"]["limit_reached"] == 1 and result["by_profile"]["a"]["candidates"] == 1
    assert result["by_profile"]["b"]["limit_reached"] == 1  # Both are done: the sweep ends early.
    assert len(source.queries) == 1


def test_limit_cuts_the_sweep_and_the_next_one_resumes_there_until_a_full_pass(decisions, monkeypatch):
    snapshots = segment_snapshots(a=[43, 81])
    first = RecordingSource()
    result = poll_many(first, ["a"], snapshots, monkeypatch, max_requests=3)
    assert first.streams() == [(1, 43), (1, 81), (2, 43)]
    assert result["discovery_limit_reached"] == 1 and result["http_requests"] == 3
    assert result["cursor"] == {"next_stream_index": 3, "next_stream": [2, 81], "stream_count": 4,
                                "complete": False}
    second = RecordingSource()
    result = poll_many(second, ["a"], snapshots, monkeypatch, max_requests=3,
                       start_stream=result["cursor"]["next_stream_index"])
    assert second.streams() == [(2, 81), (1, 43), (1, 81)]  # Wraps around: nothing starves.
    assert result["cursor"]["next_stream_index"] == 2
    third = RecordingSource()
    result = poll_many(third, ["a"], snapshots, monkeypatch, max_requests=4, start_stream=2)
    assert third.streams() == [(2, 43), (2, 81), (1, 43), (1, 81)]
    assert "discovery_limit_reached" not in result
    assert result["cursor"]["next_stream_index"] == 0 and result["cursor"]["complete"]


def test_a_resumed_lap_searches_only_the_streams_left_and_then_counts_as_complete(decisions, monkeypatch):
    snapshots = segment_snapshots(a=[43, 81])
    source = RecordingSource()
    # A lap began at stream 0 and covered two streams; two are left, ending where it began.
    result = poll_many(source, ["a"], snapshots, monkeypatch, start_stream=2, max_streams=2)
    assert source.streams() == [(2, 43), (2, 81)]
    assert result["streams_searched"] == 2 and result["http_requests"] == 2
    assert result["cursor"] == {"next_stream_index": 0, "next_stream": [1, 43], "stream_count": 4,
                                "complete": True}
    source = RecordingSource()
    result = poll_many(source, ["a"], snapshots, monkeypatch, start_stream=1, max_streams=2)
    assert source.streams() == [(1, 81), (2, 43)]
    assert result["cursor"]["next_stream_index"] == 3 and result["cursor"]["complete"]  # After the last one.
    source = RecordingSource()
    result = poll_many(source, ["a"], snapshots, monkeypatch, start_stream=1, max_streams=3, max_requests=2)
    assert source.streams() == [(1, 81), (2, 43)]
    assert result["cursor"]["next_stream_index"] == 3 and not result["cursor"]["complete"]
    source = RecordingSource()
    result = poll_many(source, ["a"], snapshots, monkeypatch, start_stream=1, max_streams=9)
    assert len(source.streams()) == 4 and result["cursor"]["next_stream_index"] == 0  # Never more than a lap.
    with pytest.raises(ValueError):
        poll_many(RecordingSource(), ["a"], snapshots, monkeypatch, max_streams=0)


def test_a_stream_cut_mid_pagination_is_resumed_from_its_first_page(decisions, monkeypatch):
    source = RecordingSource(pages=3)
    result = poll_many(source, ["a"], segment_snapshots(a=[43]), monkeypatch, max_requests=4,
                       max_pages_per_segment=3)
    assert source.queries == [(1, 43, 1), (1, 43, 2), (1, 43, 3), (2, 43, 1)]
    assert result["cursor"]["next_stream_index"] == 1  # (2, 43) did not finish.
    assert result["http_requests"] == 4 and result["discovery_limit_reached"] == 1


def test_start_index_wraps_and_must_not_be_negative(decisions, monkeypatch):
    source = RecordingSource()
    poll_many(source, ["a"], segment_snapshots(a=[43]), monkeypatch, start_stream=5)
    assert source.streams() == [(2, 43), (1, 43)]
    with pytest.raises(ValueError):
        poll_many(RecordingSource(), ["a"], segment_snapshots(a=[43]), monkeypatch, start_stream=-1)
    with pytest.raises(ValueError):
        poll_many(RecordingSource(), ["a"], segment_snapshots(a=[43]), monkeypatch, max_requests=0)


def test_parallel_sweep_respects_the_discovery_limit(decisions, monkeypatch):
    source = RecordingSource()
    result = poll_many(source, ["a"], segment_snapshots(a=[43, 81]), monkeypatch,
                       search_parallelism=3, max_requests=2)
    assert len(source.queries) == result["http_requests"] == 2
    assert result["discovery_limit_reached"] == 1 and not result["cursor"]["complete"]


def test_blocked_stream_leaves_the_cursor_on_that_stream(decisions, monkeypatch):
    class Blocked(RecordingSource):
        def search_contracts(self, **query):
            if (query["objeto"], query["segmento"]) == (2, 43):
                raise denial(403)
            return super().search_contracts(**query)

    result = poll_many(Blocked(), ["a"], segment_snapshots(a=[43, 81]), monkeypatch)
    assert result["source_blocked"] == 1 and result["cursor"]["next_stream_index"] == 2


def test_single_profile_wrapper_returns_only_flat_counters(decisions, monkeypatch):
    monkeypatch.setattr(poll_module, "profile_snapshot", MagicMock(side_effect=segment_snapshots(a=[43])))
    monkeypatch.setattr(poll_module, "SeaceClient", RecordingSource())
    stats = poll_prod6_profile(settings(), MagicMock(), profile_id="a", anio=2026)
    assert not {"by_profile", "cursor", "streams", "streams_searched"} & set(stats)
    assert stats["seen"] == 2 and stats["http_requests"] == 2
