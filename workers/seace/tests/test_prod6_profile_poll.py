import threading
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs import poll_prod6_profile as poll_module
from buenapro_worker.jobs.poll_prod6_profile import matches_profile_scope, poll_prod6_profile
from buenapro_worker.jobs.scheduler import enqueue_poll_search_jobs
from buenapro_worker.prod6 import screening
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
    """Keep these tests DB-free; the decision repository has its own PostgreSQL tests."""
    store = DecisionStore()
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
