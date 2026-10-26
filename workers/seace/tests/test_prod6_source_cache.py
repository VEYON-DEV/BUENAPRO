"""A PROD6 document is downloaded once: download_file leaves a private temporary copy."""
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs import download_file as download_module
from buenapro_worker.jobs import extract_tdr as extract_module
from buenapro_worker.jobs.download_file import download_file_job
from buenapro_worker.jobs.extract_tdr import extract_tdr_job
from buenapro_worker.prod6 import runtime
from buenapro_worker.prod6.reading import ReadingBlockedError
from buenapro_worker.prod6.source_cache import SourceCache
from buenapro_worker.queue.repository import Job

PDF = b"%PDF-1.7\n synthetic requirement document"
SHA = hashlib.sha256(PDF).hexdigest()


def test_copy_is_private_named_by_hash_and_read_back_verified(tmp_path):
    cache = SourceCache(tmp_path / "source-cache")
    assert cache.store(PDF) == SHA
    path = tmp_path / "source-cache" / SHA
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "source-cache").stat().st_mode & 0o777 == 0o700
    assert [entry.name for entry in (tmp_path / "source-cache").iterdir()] == [SHA]  # No temp left.
    assert cache.load(SHA) == PDF
    cache.discard(SHA)
    cache.discard(SHA)  # Idempotent.
    assert cache.load(SHA) is None and not path.exists()


def test_nothing_is_created_until_a_copy_is_stored(tmp_path):
    cache = SourceCache(tmp_path / "source-cache")
    assert cache.load(SHA) is None
    cache.discard(SHA)
    assert not (tmp_path / "source-cache").exists()


def test_corrupt_or_swapped_copy_is_rejected_and_removed(tmp_path):
    cache = SourceCache(tmp_path)
    cache.store(PDF)
    (tmp_path / SHA).write_bytes(PDF + b"tampered")
    assert cache.load(SHA) is None
    assert not (tmp_path / SHA).exists()


@pytest.mark.parametrize("value", [None, "", "pending:31", "../" + SHA, SHA.upper(), SHA[:-1], 31, MagicMock()])
def test_only_a_real_sha256_names_a_copy(tmp_path, value):
    cache = SourceCache(tmp_path / "source-cache")
    (tmp_path / "outside").write_bytes(b"x")
    assert cache.load(value) is None
    cache.discard(value)
    assert (tmp_path / "outside").exists()


def test_symlinked_copy_or_folder_is_not_trusted(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "other").write_bytes(PDF)
    link = real / SHA
    link.symlink_to(tmp_path / "other")
    assert SourceCache(real).load(SHA) is None
    folder = tmp_path / "linked"
    folder.symlink_to(real)
    assert SourceCache(folder).store(b"new") is None  # The cache is only an optimization.
    assert not (real / hashlib.sha256(b"new").hexdigest()).exists()


def test_a_filesystem_problem_never_fails_the_job(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file where the folder should be")
    assert SourceCache(blocker / "source-cache").store(PDF) is None


# --- download_file / extract_tdr -----------------------------------------------------

class CountingSource:
    """Replaces SeaceClient in both jobs and counts how often the source is asked."""

    def __init__(self, content=PDF):
        self.downloads, self.content = [], content

    def __call__(self, _settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def download_file(self, archive_id):
        self.downloads.append(archive_id)
        return self.content


def download_repo():
    repo = MagicMock()
    repo.conn.execute.return_value.fetchone.return_value = {
        "filename": "TDR.pdf", "categoria": 1, "objeto_codigo": 2}
    return repo


def extraction_result():
    return SimpleNamespace(model="m", prompt_version="p", schema_version="s", input_tokens=1,
                           output_tokens=1, cost_usd=0, raw_json={"a": 1}, summary_json={"b": 2},
                           requires_human_review=False)


def extract_repo(sha=SHA, mime="application/pdf"):
    repo = MagicMock()

    def execute(sql, params=()):
        cursor = MagicMock()
        if "SELECT d.id" in sql:
            cursor.fetchone.return_value = {"id": 7, "mime": mime,
                                            "sha256_original": sha, "objeto_codigo": 2}
        elif "INSERT INTO tdr_extractions" in sql:
            cursor.fetchone.return_value = {"id": 88}
        else:
            cursor.fetchone.return_value = None
        return cursor

    repo.conn.execute.side_effect = execute
    return repo


@pytest.fixture
def jobs(monkeypatch):
    source = CountingSource()
    monkeypatch.setattr(download_module, "SeaceClient", source)
    monkeypatch.setattr(extract_module, "SeaceClient", source)
    monkeypatch.setattr(extract_module, "find_reusable_extraction", MagicMock(return_value=None))
    return source


def test_download_leaves_a_copy_that_the_reading_uses_without_asking_the_source_again(jobs, tmp_path):
    cache = SourceCache(tmp_path / "source-cache")
    downloaded = download_file_job(MagicMock(), download_repo(), id_contrato=10,
                                   id_contrato_archivo=20, source_cache=cache)
    assert downloaded["sha256_original"] == SHA and downloaded["source_cached"] is True
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    assert extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                           extractor=extractor, source_cache=cache) == 88
    assert jobs.downloads == [20]  # One request for the whole life of the document.
    assert extractor.extract.call_args.args[0] == PDF


def test_without_a_copy_the_reading_downloads_once(jobs, tmp_path):
    cache = SourceCache(tmp_path / "source-cache")
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                    extractor=extractor, source_cache=cache)
    assert jobs.downloads == [20] and extractor.extract.call_args.args[0] == PDF


@pytest.mark.parametrize("stored", ["application/octet-stream", None])
def test_a_pdf_whose_stored_type_was_overwritten_is_still_read_as_a_pdf(jobs, tmp_path, stored):
    """Production 26/10: a listing refresh stored "application/octet-stream" for a downloaded PDF
    and the guarded reading refused it, which stopped the whole PROD6 service."""
    cache = SourceCache(tmp_path / "source-cache")
    cache.store(PDF)
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    extract_tdr_job(MagicMock(), extract_repo(mime=stored), id_contrato=10, id_contrato_archivo=20,
                    extractor=extractor, source_cache=cache)
    assert extractor.extract.call_args.kwargs["mime"] == "application/pdf"


def test_content_that_is_not_a_pdf_keeps_its_stored_type(jobs, tmp_path):
    jobs.content = b"PK\x03\x04 not a pdf"
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    extract_tdr_job(MagicMock(), extract_repo(sha=hashlib.sha256(jobs.content).hexdigest(),
                                              mime="application/zip"),
                    id_contrato=10, id_contrato_archivo=20, extractor=extractor)
    assert extractor.extract.call_args.kwargs["mime"] == "application/zip"


def test_corrupt_copy_is_replaced_by_a_download(jobs, tmp_path):
    cache = SourceCache(tmp_path)
    cache.store(PDF)
    (tmp_path / SHA).write_bytes(b"%PDF-1.7 truncated")
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                    extractor=extractor, source_cache=cache)
    assert jobs.downloads == [20] and extractor.extract.call_args.args[0] == PDF


def test_a_copy_of_another_document_is_never_used(jobs, tmp_path):
    cache = SourceCache(tmp_path)
    other = b"%PDF-1.7 a different document"
    cache.store(other)
    extractor = SimpleNamespace(extract=MagicMock(return_value=extraction_result()))
    extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                    extractor=extractor, source_cache=cache)
    assert jobs.downloads == [20] and extractor.extract.call_args.args[0] == PDF


@pytest.mark.parametrize("kind,kwargs", [
    ("anexo", {"categoria": 2}), ("docx", {"content": b"PK\x03\x04 not a pdf"})])
def test_only_documents_that_will_be_read_leave_a_copy(jobs, tmp_path, kind, kwargs):
    repo = download_repo()
    row = repo.conn.execute.return_value.fetchone.return_value
    row["categoria"] = kwargs.get("categoria", 1)
    if kind == "anexo":
        row["filename"] = "Anexo.pdf"
    jobs.content = kwargs.get("content", PDF)
    cache = SourceCache(tmp_path / "source-cache")
    result = download_file_job(MagicMock(), repo, id_contrato=10, id_contrato_archivo=20, source_cache=cache)
    assert "source_cached" not in result and not (tmp_path / "source-cache").exists()


def test_jobs_behave_as_before_without_a_cache(jobs):
    result = download_file_job(MagicMock(), download_repo(), id_contrato=10, id_contrato_archivo=20)
    assert result == {"sha256_original": SHA, "doc_class": "tdr", "size_original_bytes": len(PDF)}


class GuardedLike:
    """Shape of GuardedGeminiExtractor.recover_before_download for a retried paid job."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.extract = MagicMock(return_value=extraction_result())

    def recover_before_download(self, sha256, **_kwargs):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_the_copy_never_bypasses_the_paid_attempt_guard(jobs, tmp_path):
    cache = MagicMock(spec=SourceCache)
    extractor = GuardedLike(ReadingBlockedError("Prior attempt has no durable response"))
    with pytest.raises(ReadingBlockedError):
        extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                        extractor=extractor, source_cache=cache)
    cache.load.assert_not_called()  # The guard decides before any bytes are looked up.
    assert jobs.downloads == [] and extractor.extract.call_count == 0


def test_a_known_response_is_replayed_without_the_copy_or_the_source(jobs):
    cache = MagicMock(spec=SourceCache)
    known = extraction_result()
    assert extract_tdr_job(MagicMock(), extract_repo(), id_contrato=10, id_contrato_archivo=20,
                           extractor=GuardedLike(known), source_cache=cache) == 88
    cache.load.assert_not_called()
    assert jobs.downloads == []


# --- runtime dispatch: the copy lives until the reading is committed ---------------

@pytest.fixture
def dispatch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=True))
    monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value=None))
    conn = MagicMock()

    def execute(sql, params=()):
        cursor = MagicMock()
        cursor.fetchone.return_value = ({"sha256_original": SHA} if "FROM contract_documents" in sql
                                        else None)
        return cursor

    conn.execute.side_effect = execute
    repo = runtime.ScopedRepository(conn, profile_id="veyon", batch_id="b")
    cache = SourceCache(tmp_path / "source-cache")
    cache.store(PDF)
    return SimpleNamespace(repo=repo, cache=cache, folder=tmp_path, conn=conn)


def extract_job():
    return Job(1, "extract_tdr", "llm", {"id_contrato": 42, "id_contrato_archivo": 31}, 1)


def test_reading_gets_the_cache_and_the_copy_is_dropped_only_after_commit(dispatch_env, monkeypatch):
    env = dispatch_env
    extract = MagicMock(return_value=99)
    monkeypatch.setattr(runtime, "extract_tdr_job", extract)
    result = runtime.dispatch(MagicMock(), env.repo, extract_job(), env.folder)
    assert result["outcome"] == "read_or_reused"
    assert extract.call_args.kwargs["source_cache"].directory == env.folder / "source-cache"
    assert env.cache.load(SHA) == PDF  # Still there: the transaction has not committed.
    env.repo.run_after_commit()
    assert env.cache.load(SHA) is None and not (env.folder / "source-cache" / SHA).exists()


def test_a_failed_reading_keeps_its_copy_for_the_retry(dispatch_env, monkeypatch):
    env = dispatch_env
    monkeypatch.setattr(runtime, "extract_tdr_job", MagicMock(side_effect=ValueError("not saved")))
    with pytest.raises(ValueError):
        runtime.dispatch(MagicMock(), env.repo, extract_job(), env.folder)
    env.repo.run_after_commit()
    assert env.cache.load(SHA) == PDF


@pytest.mark.parametrize("scenario", ["closed", "current_reading"])
def test_jobs_that_end_without_reading_release_the_copy(dispatch_env, monkeypatch, scenario):
    env = dispatch_env
    if scenario == "closed":
        monkeypatch.setattr(runtime, "eligible_contract", MagicMock(return_value=False))
    else:
        monkeypatch.setattr(runtime, "current_extraction", MagicMock(return_value={"id": 5}))
        monkeypatch.setattr(runtime, "enqueue_derive_summary", MagicMock())
    runtime.dispatch(MagicMock(), env.repo, extract_job(), env.folder)
    env.repo.run_after_commit()
    assert env.cache.load(SHA) is None


def test_download_dispatch_passes_the_cache_and_retires_a_superseded_copy(dispatch_env, monkeypatch):
    env = dispatch_env
    old = "b" * 64
    (env.folder / "source-cache" / old).write_bytes(b"old")
    env.conn.execute.side_effect = lambda sql, params=(): MagicMock(
        fetchone=MagicMock(return_value={"id": 8, "sha256_original": old}))
    download = MagicMock(return_value={"sha256_original": SHA})
    monkeypatch.setattr(runtime, "download_file_job", download)
    job = Job(1, "download_file", "io", {"id_contrato": 42, "id_contrato_archivo": 31}, 1)
    runtime.dispatch(MagicMock(), env.repo, job, env.folder)
    assert download.call_args.kwargs["source_cache"].directory == env.folder / "source-cache"
    assert (env.folder / "source-cache" / old).exists()
    env.repo.run_after_commit()
    assert not (env.folder / "source-cache" / old).exists()
    assert (env.folder / "source-cache" / SHA).exists()  # The new document's copy stays for its reading.


def test_cleanup_failure_is_reported_without_failing_the_saved_job(dispatch_env, capsys):
    env = dispatch_env
    env.repo.after_commit.append(MagicMock(side_effect=OSError("disk")))
    env.repo.run_after_commit()
    assert "cleanup_failed" in capsys.readouterr().out
    assert env.repo.after_commit == []


def test_original_documents_are_not_written_next_to_receipts(dispatch_env):
    # The copy is the only place an original may exist; it must not be in the readings folder.
    assert not (dispatch_env.folder / "readings").exists()
    assert os.listdir(dispatch_env.folder) == ["source-cache"]
