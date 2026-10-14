"""Remove verified, case-owned PROD2 working documents without a cloud copy.

The caller supplies a PostgreSQL readback proof only after publishing the final
outcome. This module has no network/database/provider clients. Payment receipts,
JSON preparation pins and responses always survive. Shared preparation caches
outside the case directory never belong to this cleanup policy.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from buenapro_worker.normalization.facets import canonical_hash
from .batch_runner import private_write, read_private


POLICY = "prod2_completed_case_temporary_cleanup_v1"
_SHA = re.compile(r"[a-f0-9]{64}")
_UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
_DOCUMENT = {".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".fodt"}
_RENDER = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".txt"}
_MAX_FILES = 20000
_MAX_REFERENCE_FILES = 100000
_MAX_JSON_BYTES = 32 * 1024**2
_MAX_CORPUS_JSON_BYTES = 96 * 1024**2
_MAX_REFERENCE_BYTES = 512 * 1024**2
_MAX_ARTIFACT_BYTES = 2 * 1024**3


class TemporaryCleanupError(ValueError):
    """Safe local cleanup cannot be verified; remaining evidence is retained."""


def _safe(path, *, directory=False):
    value = Path(path).absolute()
    if ".." in value.parts:
        raise TemporaryCleanupError("Unsafe temporary cleanup path")
    for part in (value, *value.parents):
        if part.is_symlink():
            raise TemporaryCleanupError("Temporary cleanup does not follow symlinks")
    if directory and not value.is_dir():
        raise TemporaryCleanupError("Temporary cleanup directory is missing")
    return value


def _json(path, *, maximum=_MAX_JSON_BYTES):
    path = _safe(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise TemporaryCleanupError("Temporary cleanup JSON evidence is unavailable")
    try:
        return read_private(path)
    except (OSError, ValueError, UnicodeError) as error:
        raise TemporaryCleanupError("Temporary cleanup JSON evidence is invalid") from error


def _files(root):
    """Inventory one explicitly owned namespace, never follow linked directories."""
    count = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            if path.is_symlink():
                raise TemporaryCleanupError("Temporary cleanup namespace contains a symlink")
        for name in sorted(files):
            count += 1
            if count > _MAX_FILES:
                raise TemporaryCleanupError("Temporary cleanup exceeds its file count limit")
            path = Path(parent) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise TemporaryCleanupError("Temporary cleanup requires regular files")
            yield path


def _facts(path, case):
    path = _safe(path)
    if not path.is_relative_to(case):
        raise TemporaryCleanupError("Temporary artifact escaped its case")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_ARTIFACT_BYTES:
        raise TemporaryCleanupError("Temporary artifact exceeds its regular-file byte limit")
    sha, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > _MAX_ARTIFACT_BYTES:
                raise TemporaryCleanupError("Temporary artifact exceeds its streaming byte limit")
            sha.update(chunk)
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if any(getattr(before, k) != getattr(after, k) for k in fields) or total != after.st_size:
        raise TemporaryCleanupError("Temporary artifact changed during inventory")
    return {"path": path.relative_to(case).as_posix(), "sha256": sha.hexdigest(),
            "bytes": total, "links": after.st_nlink}


def _proof(case, proof):
    outcome = _json(case / "outcome.private.json")
    if (not isinstance(proof, dict) or proof.get("pg_verified") is not True
            or type(proof.get("convocation_id")) is not int
            or str(proof["convocation_id"]) != case.name
            or proof.get("status") != "llm1_complete"
            or not isinstance(outcome, dict) or outcome.get("accepted") is not True
            or outcome.get("status") != "llm1_complete"
            or outcome.get("convocation_id") != proof["convocation_id"]
            or not isinstance(proof.get("document_set_hash"), str)
            or not _SHA.fullmatch(proof["document_set_hash"])
            or outcome.get("document_set_hash") != proof["document_set_hash"]
            or any(type(proof.get(k)) is not int or proof[k] != 0 for k in
                   ("active_payments", "usage_unknown_attempts", "remote_cleanup_pending"))
            or proof.get("remote_cleanup_unknown") is not False):
        raise TemporaryCleanupError("Temporary cleanup requires a completed, reconciled PostgreSQL readback")
    for key in ("extraction_id", "evaluation_id", "request_hash"):
        if key in outcome and key in proof and proof[key] != outcome[key]:
            raise TemporaryCleanupError("Temporary cleanup proof differs from its durable outcome")
    sent_input = outcome.get("llm1_input")
    if isinstance(sent_input, dict) and sent_input.get("policy_version") == "prod2_embedding_retrieved_text_v1":
        durable = proof.get("embedding_durable_proof")
        stable = {key: value for key, value in (durable or {}).items() if key != "verified_at"}
        if (not isinstance(durable, dict) or stable != sent_input.get("durable_proof")
                or durable.get("pg_verified") is not True or durable.get("blocks_persisted") is not True
                or durable.get("vectors_persisted") is not True
                or durable.get("corpus_hash") != sent_input.get("corpus_hash")
                or durable.get("retrieval_hash") != sent_input.get("retrieval_hash")
                or durable.get("context_sha256") != sent_input.get("context_sha256")):
            raise TemporaryCleanupError("Retrieved cleanup requires a fresh matching corpus and vector readback")
    return outcome


def _payment_uncertain(case, files):
    for path in files:
        name = path.name
        if name.startswith("embedding-") and name.endswith(".started.private.json"):
            final = path.with_name(name.replace(".started.private.json", ".outcome.private.json"))
            if not final.is_file() or final.is_symlink():
                return True
            value = _json(final)
            attempt = name.removeprefix("embedding-").removesuffix(".started.private.json")
            if value.get("attempt_id") != attempt or value.get("provider_usage_known") is not True:
                return True
            if value.get("status") == "failed_pre_send" and value.get("request_sent") is False:
                continue
            response = path.with_name(name.replace(".started.private.json", ".response.private.json"))
            claim = path.with_name(name.replace(".started.private.json", ".claim.private.json"))
            if (value.get("status") != "done" or value.get("complete") is not True
                    or not response.is_file() or response.is_symlink()
                    or not claim.is_file() or claim.is_symlink()):
                return True
            received, owner = _json(response), _json(claim)
            if (received.get("attempt_id") != attempt or owner.get("attempt_id") != attempt
                    or received.get("request_hash") != owner.get("request_hash")
                    or received.get("claim_token") != owner.get("claim_token")
                    or received.get("response", {}).get("response_sha256") != value.get("response_sha256")
                    or received.get("response", {}).get("complete") is not True
                    or received.get("response", {}).get("provider_usage_known") is not True):
                return True
        if name.startswith("embedding-") and name.endswith(".claim.private.json"):
            started = path.with_name(name.replace(".claim.private.json", ".started.private.json"))
            if not started.is_file() or started.is_symlink():
                return True
        if name.endswith(".claim.private.json") and name.startswith(("selector-", "jev-", "gemini_extraction-")):
            # Cached PostgreSQL responses do not create a local claim. A local
            # ownership claim requires its local response before discarding input.
            response = path.with_name(name.replace(".claim.private.json", ".response.private.json"))
            if not response.is_file() or response.is_symlink():
                failure = path.with_name(name.replace(".claim.private.json", ".failure.private.json"))
                if name.startswith("gemini_extraction-") and failure.is_file() and not failure.is_symlink():
                    from .luna_fallback import validated_failure_receipt
                    if validated_failure_receipt(_json(path), _json(failure)):
                        continue
                    from .manual_reconciliation import validated_manual_failure_receipt
                    if validated_manual_failure_receipt(_json(path), _json(failure)):
                        continue
                return True
            if not isinstance(_json(response), dict):
                return True
        if name.endswith(".started.private.json") and name.startswith(("jev-attempt-", "gemini-selected-", "openai-selected-")):
            final = path.with_name(name.replace(".started.private.json", ".outcome.private.json"))
            if not final.is_file():
                return True
            value = _json(final)
            if name.startswith("jev-attempt-") and value.get("usage_known") is not True:
                return True
            if name.startswith(("gemini-selected-", "openai-selected-")):
                remote = value
                cleanup = path.with_name(name.replace(".started.private.json", ".cleanup.private.json"))
                if cleanup.exists():
                    remote = _json(cleanup)
                    attempt = name.removesuffix(".started.private.json")
                    if remote.get("attempt_id") != attempt or value.get("attempt_id") != attempt:
                        return True
                if (value.get("provider_usage_known") is not True
                        or type(remote.get("remote_cleanup_pending")) is not int
                        or remote["remote_cleanup_pending"] != 0
                        or remote.get("remote_cleanup_unknown") is not False):
                    return True
    return False


def _inventory(case, outcome, files, *, verification=None):
    """Prove ownership using downloads, member sidecars and preparation pins."""
    owned, held = {}, {}
    def add(path, expected_sha=None, expected_bytes=None, *, artifact_kind=None):
        if not path.exists():
            return
        facts = _facts(path, case)
        if ((expected_sha is not None and facts["sha256"] != expected_sha)
                or (expected_bytes is not None and facts["bytes"] != expected_bytes)):
            raise TemporaryCleanupError("Temporary artifact differs from its pinned evidence")
        if facts["links"] != 1:
            held[facts["path"]] = "hardlinked_file"
        else:
            owned[facts["path"]] = {k: v for k, v in facts.items() if k != "links"}
            if artifact_kind is not None:
                owned[facts["path"]]["artifact_kind"] = artifact_kind

    for path in files:
        if path.parent == case and path.name.endswith(".download.private.json"):
            code = path.name.removesuffix(".download.private.json")
            receipt = _json(path)
            if not _UUID.fullmatch(code) or receipt.get("document_uuid") != code:
                raise TemporaryCleanupError("Downloaded temporary document UUID is unbound")
            sha, size = receipt.get("sha256"), receipt.get("bytes")
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or type(size) is not int or size <= 0:
                raise TemporaryCleanupError("Downloaded temporary document facts are invalid")
            for extension in (".pdf", ".doc", ".docx", ".zip", ".rar", ".7z"):
                add(case / (code + extension), sha, size)
        # Archive extraction never names outputs from an untrusted archive path.
        if len(path.relative_to(case).parts) == 4 and path.suffix == ".json":
            root, parent, archive_sha, filename = path.relative_to(case).parts
            if root == parent + ".members" and _UUID.fullmatch(parent) and _SHA.fullmatch(archive_sha):
                member = _json(path)
                if (member.get("parent_uuid") != parent or member.get("parent_sha256") != archive_sha
                        or member.get("candidate_id") != "archive:" + path.stem
                        or member.get("format") not in {"pdf", "doc", "docx"}
                        or not _SHA.fullmatch(str(member.get("original_sha256", "")))):
                    raise TemporaryCleanupError("Temporary archive member has no exact ownership binding")
                add(path.with_suffix("." + member["format"]), member["original_sha256"], member["size"])

    # Only the new per-case cache is owned here. Legacy cache siblings cannot be
    # discovered or deleted even when they contain identical preparation hashes.
    private = case / "preparation-cache" / ("selected-analysis-" + outcome["document_set_hash"])
    if private.is_dir():
        pin = _json(private / "manifest.private.json")
        if (pin.get("document_set_hash") != outcome["document_set_hash"]
                or canonical_hash(pin.get("document_set_identity")) != outcome["document_set_hash"]):
            raise TemporaryCleanupError("Case preparation pin differs from its completed document set")
        add(private / "analysis.pdf", pin["analysis_sha256"], pin["analysis_bytes"])
        for number, part in enumerate(pin["chunks"], 1):
            if part.get("file_name") != f"part-{number:04d}.pdf":
                raise TemporaryCleanupError("Temporary preparation part name is invalid")
            add(private / part["file_name"], part["sha256"], part["bytes"])
        for row in pin["document_set"]:
            code, candidate = row["document_uuid"], row["candidate_id"]
            if not _UUID.fullmatch(code):
                raise TemporaryCleanupError("Temporary preparation candidate UUID is invalid")
            work = private / code / hashlib.sha256(candidate.encode()).hexdigest()
            source = work / ("source" + Path(row["filename"]).suffix.lower())
            add(source, row["original_sha256"])
            for path in files:
                if not path.is_relative_to(work) or path == source:
                    continue
                relative = path.relative_to(work)
                # These names are created by the isolated Word converter. Keep
                # arbitrary files, profile settings and JSON provenance receipts.
                if (path.name == "sanitized-bases.docx" and path.parent == work
                        or relative.parts[0].startswith("libreoffice-profile-")
                        and len(relative.parts) == 3 and relative.parts[1] == "converted"
                        and path.suffix.lower() in {".pdf", ".fodt"}):
                    add(path)

    verified_corpora = {}
    for path in files:
        relative = path.relative_to(case)
        sent_input = outcome.get("llm1_input")
        durable = (verification or {}).get("embedding_durable_proof")
        if (len(relative.parts) == 2 and relative.parts[0].startswith("embedding-corpus-")
                and path.name == "corpus.private.json"):
            if not isinstance(sent_input, dict) or not isinstance(durable, dict):
                held[relative.as_posix()] = "embedding_persistence_unverified"
                continue
            from .embedding_corpus import validate_corpus
            try:
                corpus = validate_corpus(_json(path, maximum=_MAX_CORPUS_JSON_BYTES))
            except (ValueError, TypeError, KeyError) as error:
                raise TemporaryCleanupError("Complete corpus derivative is no longer verifiable") from error
            if (relative.parts[0] != "embedding-corpus-" + canonical_hash(corpus["identity"])
                    or corpus["corpus_hash"] != sent_input.get("corpus_hash")
                    or corpus["analysis_sha256"] != sent_input.get("analysis_sha256")
                    or corpus["source_manifest_hash"] != sent_input.get("source_manifest_hash")
                    or canonical_hash(corpus) != durable.get("corpus_payload_sha256")):
                raise TemporaryCleanupError("Corpus derivative differs from its durable paid input")
            add(path, artifact_kind="embedding_corpus_derivative")
            verified_corpora[path.parent] = corpus
        if (len(relative.parts) == 2 and relative.parts[0].startswith("embedding-corpus-")
                and re.fullmatch(r"ocr-page-\d{4}\.private\.json", path.name)):
            corpus = verified_corpora.get(path.parent)
            if corpus is None:
                held[relative.as_posix()] = "ocr_completed_corpus_provenance_unavailable"
                continue
            pin = _json(path, maximum=16 * 1024**2)
            page = int(path.name[len("ocr-page-"):len("ocr-page-") + 4])
            expected = next((row for row in corpus["pages"] if row["global_page"] == page), None)
            if (not expected or expected.get("ocr_cache_file_sha256") != canonical_hash(pin)
                    or pin.get("identity", {}).get("version") != "prod2_local_page_ocr_cache_v1"
                    or pin.get("identity", {}).get("global_page") != page
                    or any(pin.get("identity", {}).get(key) != corpus["identity"].get(key)
                           for key in ("source_binding", "config", "versions"))
                    or pin.get("identity_hash") != canonical_hash(pin.get("identity"))
                    or pin.get("cache_hash") != canonical_hash({key: value for key, value in pin.items()
                                                              if key != "cache_hash"})):
                raise TemporaryCleanupError("OCR text derivative differs from its complete durable corpus")
            add(path, artifact_kind="embedding_ocr_text_derivative")
        if (len(relative.parts) == 2 and relative.parts[0].startswith("embedding-context-")
                and path.name == "context.private.txt"):
            if not isinstance(sent_input, dict) or not isinstance(durable, dict):
                held[relative.as_posix()] = "embedding_persistence_unverified"
                continue
            pin = _json(path.parent / "manifest.private.json")
            stored_pin = {key: value for key, value in pin.get("durable_proof", {}).items() if key != "verified_at"}
            expected_pin = {key: value for key, value in durable.items() if key != "verified_at"}
            if (relative.parts[0] != "embedding-context-" + str(sent_input.get("retrieval_hash"))
                    or any(pin.get(key) != sent_input.get(key) for key in (
                        "analysis_sha256", "corpus_hash", "retrieval_hash", "context_sha256", "context_bytes"))
                    or stored_pin != expected_pin):
                raise TemporaryCleanupError("Context derivative manifest differs from its durable paid input")
            add(path, sent_input.get("context_sha256"), sent_input.get("context_bytes"))
        if (len(relative.parts) == 2 and relative.parts[0].startswith("local-text-")
                and path.name == "complete.private.txt"):
            pin = _json(path.parent / "manifest.private.json")
            # Local-text source binding carries full provenance rather than a
            # document_set_hash; bind to the exact completed primary PDF.
            prep = private / "manifest.private.json"
            if (path.parent.name != "local-text-" + str(pin.get("cache_key"))
                    or not _SHA.fullmatch(str(pin.get("cache_key", "")))
                    or not _SHA.fullmatch(str(pin.get("text_sha256", "")))
                    or type(pin.get("text_bytes")) is not int or pin["text_bytes"] <= 0
                    or not prep.exists()
                    or pin.get("source_binding", {}).get("analysis_sha256") != _json(prep).get("analysis_sha256")):
                held[relative.as_posix()] = "unbound_local_text"
                continue
            add(path, pin.get("text_sha256"), pin.get("text_bytes"))

    # Unrecognized large artifacts must be visible in the report, never silently
    # broadened into an extension wildcard deletion.
    for path in files:
        relative = path.relative_to(case).as_posix()
        corpus_derivative = (path.parent.name.startswith("embedding-corpus-") and (
            path.name == "corpus.private.json" or re.fullmatch(r"ocr-page-\d{4}\.private\.json", path.name)))
        if (path.suffix.lower() in (_DOCUMENT | _RENDER) or corpus_derivative) and relative not in owned and relative not in held:
            held[relative] = "ownership_unproven"
    return list(owned.values()), held


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def _references(case, roots):
    """Resolve bounded external JSON path references; values never enter logs."""
    if not roots:
        raise TemporaryCleanupError("Temporary cleanup reference scope was not supplied")
    protected, count, total = set(), 0, 0
    for root in dict.fromkeys(_safe(p, directory=True) for p in roots):
        if not case.is_relative_to(root):
            raise TemporaryCleanupError("Temporary cleanup reference root does not cover its case")
        for parent, directories, files in os.walk(root, followlinks=False):
            base = Path(parent)
            if base.is_relative_to(case):
                directories[:] = []
                continue
            # A link is itself a reference, not a namespace to walk. Producer
            # metadata cannot legitimately be written through linked directories;
            # aliases of test/dependency folders must not block unrelated cases.
            linked = set()
            for name in [*directories, *files]:
                path = base / name
                if path.is_symlink():
                    linked.add(name)
                    try:
                        target = path.resolve(strict=False)
                    except (OSError, RuntimeError) as error:
                        raise TemporaryCleanupError("Reference link cannot be safely resolved") from error
                    if target == case or target.is_relative_to(case):
                        protected.add(target)
                    elif case.is_relative_to(target):
                        protected.add(case)
            directories[:] = [name for name in directories if base / name != case and name not in linked]
            for name in files:
                if name in linked or not name.endswith(".json"):
                    continue
                path = base / name
                count += 1
                total += path.stat().st_size
                if count > _MAX_REFERENCE_FILES or total > _MAX_REFERENCE_BYTES:
                    raise TemporaryCleanupError("Temporary cleanup reference inventory exceeds its bounds")
                for value in _strings(_json(path)):
                    # Only explicit filesystem references, not snippets or URLs.
                    if value.startswith(("/", "tmp/", "./", "../")):
                        candidate = Path(value)
                        if not candidate.is_absolute():
                            candidate = root.parent / candidate if value.startswith("tmp/") else base / candidate
                        candidate = Path(os.path.normpath(candidate))
                        if candidate == case or candidate.is_relative_to(case):
                            protected.add(candidate)
    return protected


@contextmanager
def _lock(case):
    fd = os.open(case / ".temporary-cleanup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_report(path, value):
    temporary = path.parent / (".cleanup-report-" + os.urandom(12).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise TemporaryCleanupError("Temporary cleanup report cannot replace a symlink")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _remove(case, entry):
    """Use directory descriptors so nested path swaps cannot escape the case."""
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise TemporaryCleanupError("Temporary cleanup plan contains an unsafe relative path")
    descriptor = os.open(case, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            nested = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        facts = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1:
            raise TemporaryCleanupError("Temporary cleanup target is linked or not regular")
        source = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            digest, total = hashlib.sha256(), 0
            with os.fdopen(source, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_ARTIFACT_BYTES:
                        raise TemporaryCleanupError("Temporary cleanup target exceeds its byte limit")
                    digest.update(chunk)
            current = os.fstat(source)
            final = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
            if (total != entry["bytes"] or digest.hexdigest() != entry["sha256"]
                    or any(getattr(current, k) != getattr(final, k) or getattr(current, k) != getattr(facts, k)
                           for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink"))):
                raise TemporaryCleanupError("Temporary cleanup target changed after its verified plan")
            os.unlink(relative.name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(source)
    finally:
        os.close(descriptor)


def _validate_plan(plan, *, outcome=None, verification=None):
    entries = plan.get("files")
    if not isinstance(entries, list) or len(entries) > _MAX_FILES:
        raise TemporaryCleanupError("Temporary cleanup plan exceeds its exact file bound")
    seen, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise TemporaryCleanupError("Temporary cleanup plan artifact is invalid")
        path = Path(entry["path"])
        corpus_derivative = (len(path.parts) == 2 and path.parts[0].startswith("embedding-corpus-")
            and _SHA.fullmatch(path.parts[0].removeprefix("embedding-corpus-"))
            and path.name == "corpus.private.json" and entry.get("artifact_kind") == "embedding_corpus_derivative")
        ocr_derivative = (len(path.parts) == 2 and path.parts[0].startswith("embedding-corpus-")
            and _SHA.fullmatch(path.parts[0].removeprefix("embedding-corpus-"))
            and re.fullmatch(r"ocr-page-\d{4}\.private\.json", path.name)
            and entry.get("artifact_kind") == "embedding_ocr_text_derivative")
        if corpus_derivative or ocr_derivative:
            sent_input = (outcome or {}).get("llm1_input")
            durable = (verification or {}).get("embedding_durable_proof")
            if (not isinstance(sent_input, dict) or not isinstance(durable, dict)
                    or sent_input.get("policy_version") != "prod2_embedding_retrieved_text_v1"
                    or durable.get("corpus_hash") != sent_input.get("corpus_hash")
                    or durable.get("pg_verified") is not True):
                raise TemporaryCleanupError("Corpus JSON removal requires its retrieved-input PostgreSQL proof")
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != entry["path"]
                or (path.suffix.lower() not in (_DOCUMENT | _RENDER) and not (corpus_derivative or ocr_derivative)) or path.name.startswith(".env")
                or entry["path"] in seen or not _SHA.fullmatch(str(entry.get("sha256", "")))
                or type(entry.get("bytes")) is not int or not 0 <= entry["bytes"] <= _MAX_ARTIFACT_BYTES):
            raise TemporaryCleanupError("Temporary cleanup plan cannot target receipts or unsafe artifacts")
        seen.add(entry["path"])
        total += entry["bytes"]
        if total > _MAX_ARTIFACT_BYTES:
            raise TemporaryCleanupError("Temporary cleanup plan exceeds its aggregate byte bound")


def cleanup_completed_case(case_folder, *, verification: dict, reference_roots=(), execute=True):
    """Return a compact idempotent cleanup report; only verified completed cases.

    ``reference_roots`` must cover the local run/recovery universe (normally the
    repository's tmp folder). An unavailable/malformed reference scan conserves
    the case. The immutable plan is published before removal; replays cannot add
    new targets. Missing artifacts are reported without rebuilding paid input.
    """
    if type(execute) is not bool:
        raise TemporaryCleanupError("Temporary cleanup execution mode must be boolean")
    case = _safe(case_folder, directory=True)
    with _lock(case):
        outcome = _proof(case, verification)
        files = list(_files(case))
        common = {"policy_version": POLICY, "convocation_id": verification["convocation_id"],
                  "outcome_hash": canonical_hash(outcome), "document_set_hash": outcome["document_set_hash"]}
        result = {**common, "status": "dry_run" if not execute else "complete", "deleted_files": 0,
                  "deleted_bytes": 0, "cumulative_deleted_bytes": 0, "already_absent_files": 0, "retained": []}
        if _payment_uncertain(case, files):
            return {**result, "status": "retained_unreconciled_payment"}
        marker, report = case / "temporary-cleanup-plan.private.json", case / "temporary-cleanup.private.json"
        if marker.exists():
            plan = _json(marker)
            if any(plan.get(k) != v for k, v in common.items()) or not isinstance(plan.get("files"), list):
                raise TemporaryCleanupError("Temporary cleanup plan differs from its completed case")
        else:
            entries, held = _inventory(case, outcome, files, verification=verification)
            plan = {**common, "files": entries, "retained": [{"path": p, "reason": r} for p, r in sorted(held.items())]}
        _validate_plan(plan, outcome=outcome, verification=verification)
        try:
            references = _references(case, reference_roots)
        except (TemporaryCleanupError, OSError):
            return {**result, "status": "retained_reference_scope_unverified", "retained": plan.get("retained", [])}
        held, entries = list(plan.get("retained", [])), []
        for entry in plan["files"]:
            target = case / entry["path"]
            if any(target == path or target.is_relative_to(path) for path in references):
                held.append({"path": entry["path"], "reason": "external_reference"})
            else:
                entries.append(entry)
        result["retained"] = held
        # Validate every remaining target before the first deletion, so a stale
        # known source blocks the whole pass rather than leaving partial cleanup.
        for entry in entries:
            target = case / entry["path"]
            if target.exists():
                facts = _facts(target, case)
                if facts["sha256"] != entry["sha256"] or facts["bytes"] != entry["bytes"] or facts["links"] != 1:
                    raise TemporaryCleanupError("Temporary cleanup target differs from its immutable inventory")
        if not execute:
            return {**result, "planned_files": len(entries), "planned_bytes": sum(e["bytes"] for e in entries)}
        if not marker.exists():
            private_write(marker, plan)
        if report.exists():
            previous = _json(report)
            if any(previous.get(k) != v for k, v in common.items()):
                raise TemporaryCleanupError("Temporary cleanup report differs from its exact case")
            result["cumulative_deleted_bytes"] = previous.get("cumulative_deleted_bytes", 0)
        _write_report(report, {**result, "status": "running"})
        for entry in entries:
            target = case / entry["path"]
            if not target.exists():
                result["already_absent_files"] += 1
                continue
            _remove(case, entry)
            result["deleted_files"] += 1
            result["deleted_bytes"] += entry["bytes"]
            result["cumulative_deleted_bytes"] += entry["bytes"]
            _write_report(report, {**result, "status": "running"})
        if held:
            result["status"] = "complete_with_retained"
        _write_report(report, result)
        return result
