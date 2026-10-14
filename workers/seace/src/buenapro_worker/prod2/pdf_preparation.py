"""Prepare selected local PDFs/Word files; archive selection happens upstream.

Original bytes remain immutable. The complete PDF and every transport part use
global, one-based page ranges; splitting never removes pages or image detail.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from uuid import UUID

from pypdf import PdfReader, PdfWriter

from buenapro_worker.local_document_conversion import prepare_pdf, safe_member, WORD_FOOTNOTES_AT_END_POLICY
from buenapro_worker.local_pdf_transport import partition_pdf, PdfTransportError
from buenapro_worker.normalization.facets import canonical_hash
from .archive_inventory import POLICY_7Z, inventory_policy


SELECTED_DOCUMENTS_POLICY = "prod2_selected_documents_v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_ANALYSIS_BYTES = 45_000_000
MAX_ANALYSIS_PAGES = 1000
_SHA = re.compile(r"[a-f0-9]{64}")


class PdfPreparationError(ValueError):
    """A complete selected document set cannot safely be prepared."""


def _snapshot(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise PdfPreparationError("Selection metadata must be finite JSON") from error


def _candidates(candidates: list[dict]) -> list[dict]:
    if not isinstance(candidates, list) or not 0 < len(candidates) <= 2000:
        raise PdfPreparationError("A bounded nonempty selected candidate list is required")
    rows, seen = _snapshot(candidates), set()
    for row in rows:
        if not isinstance(row, dict):
            raise PdfPreparationError("Selected candidate metadata is invalid")
        identity = row.get("candidate_id")
        if not isinstance(identity, str) or not identity or len(identity) > 256 or identity in seen:
            raise PdfPreparationError("Selected candidate IDs must be unique nonempty strings")
        seen.add(identity)
        try:
            if str(UUID(row["document_uuid"])) != row["document_uuid"]:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise PdfPreparationError("Selected candidate parent UUID is invalid") from error
        if row.get("source_kind") not in {"loose", "archive_member"}:
            raise PdfPreparationError("Selected candidate provenance is invalid")
        if not isinstance(row.get("original_sha256"), str) or not _SHA.fullmatch(row["original_sha256"]):
            raise PdfPreparationError("Selected candidate source hash is invalid")
        for field in ("filename", "type", "stage", "published_at", "source_url"):
            if not isinstance(row.get(field), str):
                raise PdfPreparationError("Selected candidate metadata is incomplete")
        filename = row["filename"]
        if (not filename or PurePosixPath(filename).name != filename or "\\" in filename
                or Path(filename).suffix.lower() not in {".pdf", ".doc", ".docx"}):
            raise PdfPreparationError("Selected candidate format is unsupported")
        try:
            safe_member(filename)
            if row["source_kind"] == "archive_member":
                member = row["member_path"]
                parent = row["parent_filename"]
                if not isinstance(member, str) or not isinstance(parent, str):
                    raise ValueError()
                safe_member(member)
                safe_member(parent)
                inventory_policy(row)
                supported_parent = Path(parent).suffix.lower() in {".zip", ".rar"} or (
                    Path(parent).suffix.lower() == ".7z" and row.get("archive_inventory_policy") == POLICY_7Z)
                if (PurePosixPath(member.replace("\\", "/")).name != filename
                        or PurePosixPath(parent).name != parent or "\\" in parent
                        or not supported_parent
                        or not isinstance(row.get("archive_sha256"), str)
                        or not _SHA.fullmatch(row["archive_sha256"])
                        or row.get("member_sha256") != row["original_sha256"]):
                    raise ValueError()
        except (KeyError, ValueError, TypeError) as error:
            raise PdfPreparationError("Selected archive member provenance is invalid") from error
    return rows


def _source(path: Path, folder: Path, expected_sha: str, maximum: int) -> bytes:
    if path.is_symlink() or not path.resolve().is_relative_to(folder):
        raise PdfPreparationError("Selected source must remain inside its private folder")
    try:
        if not 0 < path.stat().st_size <= maximum:
            raise PdfPreparationError("Selected source exceeds original byte policy")
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
    except OSError as error:
        raise PdfPreparationError("Selected source is missing or unreadable") from error
    if not 0 < len(content) <= maximum:
        raise PdfPreparationError("Selected source exceeds original byte policy")
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise PdfPreparationError("Selected source hash changed")
    return content


def _mkdir(path: Path) -> None:
    if path.is_symlink():
        raise PdfPreparationError("Private preparation directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)


def _write(path: Path, content: bytes) -> None:
    """Publish complete derivative bytes atomically, never a partial PDF."""
    if path.is_symlink():
        raise PdfPreparationError("Private PDF must not be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=".pdf-writing-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _reader(content: bytes) -> PdfReader:
    try:
        if not content.startswith(b"%PDF-"):
            raise PdfPreparationError("Selected candidate did not produce a PDF")
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise PdfPreparationError("Encrypted selected PDF requires review")
        if not 0 < len(reader.pages) <= MAX_ANALYSIS_PAGES:
            raise PdfPreparationError("Selected PDF exceeds page policy or has no pages")
        return reader
    except PdfPreparationError:
        raise
    except Exception as error:
        raise PdfPreparationError("Selected PDF could not be parsed") from error


def _serialize(writer: PdfWriter) -> bytes:
    content = io.BytesIO()
    writer.write(content)
    return content.getvalue()


def _lossless_optimization(content: bytes) -> bytes:
    """Flate content streams/object dedup only; retain baseline on any doubt."""
    try:
        before = _reader(content)
        writer = PdfWriter(clone_from=io.BytesIO(content))
        for page in writer.pages:
            page.compress_content_streams()
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        optimized = _serialize(writer)
        if len(optimized) >= len(content):
            return content
        after = _reader(optimized)
        if len(before.pages) != len(after.pages):
            return content
        for original, derivative in zip(before.pages, after.pages):
            if (list(original.mediabox) != list(derivative.mediabox)
                    or list(original.cropbox) != list(derivative.cropbox)
                    or original.rotation != derivative.rotation):
                return content
            old = original.get_contents()
            new = derivative.get_contents()
            if (old.get_data() if old else b"") != (new.get_data() if new else b""):
                return content
        return optimized
    except Exception:
        return content


def _result(private: Path, manifest: dict, maximum: int) -> dict:
    """Verify exact pinned derivatives before exposing paths to the caller."""
    try:
        pdf = private / "analysis.pdf"
        if pdf.is_symlink():
            raise ValueError()
        content = pdf.read_bytes()
        if (hashlib.sha256(content).hexdigest() != manifest["analysis_sha256"]
                or len(content) != manifest["analysis_bytes"]
                or len(_reader(content).pages) != manifest["page_count"]):
            raise ValueError()
        chunks, expected_start = [], 1
        for number, part in enumerate(manifest["chunks"], 1):
            if part["file_name"] != f"part-{number:04d}.pdf":
                raise ValueError()
            target = private / part["file_name"]
            if target.is_symlink():
                raise ValueError()
            data = target.read_bytes()
            pages = part["end_page"] - part["start_page"] + 1
            if (not 0 < len(data) <= maximum or len(data) != part["bytes"]
                    or hashlib.sha256(data).hexdigest() != part["sha256"]
                    or part["start_page"] != expected_start or part["page_count"] != pages
                    or part["page_offset"] != expected_start - 1
                    or len(_reader(data).pages) != pages):
                raise ValueError()
            expected_start = part["end_page"] + 1
            chunks.append({key: value for key, value in part.items() if key != "file_name"}
                          | {"pdf_path": target})
        if not chunks or expected_start != manifest["page_count"] + 1:
            raise ValueError()
        return {**manifest, "pdf_path": pdf, "chunks": chunks}
    except Exception as error:
        raise PdfPreparationError("Pinned preparation does not match its exact PDF parts") from error


def selected_preparation_identity(
    candidates: list[dict], selection_context: dict, *,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_analysis_bytes: int = MAX_ANALYSIS_BYTES, optimize_lossless: bool = True,
    word_conversion_policy=None,
) -> dict:
    """Pure, exact preparation identity, shared by conversion and paid reuse."""
    if (type(max_source_bytes) is not int or max_source_bytes <= 0
            or type(max_analysis_bytes) is not int or not 0 < max_analysis_bytes < 50_000_000
            or type(optimize_lossless) is not bool):
        raise PdfPreparationError("Invalid source or provider transport byte limits")
    if not isinstance(selection_context, dict):
        raise PdfPreparationError("Explicit selection context is required")
    if word_conversion_policy is not None and word_conversion_policy != WORD_FOOTNOTES_AT_END_POLICY:
        raise PdfPreparationError("Unsupported explicit Word conversion policy")
    return {"document_scope": SELECTED_DOCUMENTS_POLICY, "document_set": _candidates(candidates),
            "selection_context": _snapshot(selection_context), "max_source_bytes": max_source_bytes,
            "max_analysis_bytes": max_analysis_bytes, "max_pages": MAX_ANALYSIS_PAGES,
            "optimize_lossless": optimize_lossless,
            **({"word_conversion_policy": word_conversion_policy} if word_conversion_policy is not None else {})}


def prepare_selected_document_set(
    folder: Path, candidates: list[dict], files: dict[str, Path], selection_context: dict,
    *, max_source_bytes: int = MAX_SOURCE_BYTES,
    max_analysis_bytes: int = MAX_ANALYSIS_BYTES, optimize_lossless: bool = True,
    cache_folder: Path | None = None, word_conversion_policy=None,
) -> dict:
    """Preserve caller selection order and all attributable PDF pages.

    Source policy bounds each candidate and the sum of unique original hashes.
    Analysis policy bounds each transport part, not the retained complete PDF.
    ``files`` contains selected local member/loose paths; no archive is opened.
    ``cache_folder`` is operational only, excluded from the selection identity;
    exact Word derivatives can be reused across independent source run folders.
    """
    if (type(max_source_bytes) is not int or max_source_bytes <= 0
            or type(max_analysis_bytes) is not int or not 0 < max_analysis_bytes < 50_000_000
            or type(optimize_lossless) is not bool):
        raise PdfPreparationError("Invalid source or provider transport byte limits")
    if not isinstance(selection_context, dict) or not isinstance(files, dict):
        raise PdfPreparationError("Explicit selection context and selected local files are required")
    if word_conversion_policy is not None and word_conversion_policy != WORD_FOOTNOTES_AT_END_POLICY:
        raise PdfPreparationError("Unsupported explicit Word conversion policy")
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise PdfPreparationError("Private source folder is invalid")
    folder = folder.resolve()
    rows, context = _candidates(candidates), _snapshot(selection_context)
    try:
        archive_policy = inventory_policy(context)
        if any("archive_inventory_policy" in row and row["archive_inventory_policy"] != archive_policy for row in rows):
            raise ValueError()
    except ValueError as error:
        raise PdfPreparationError("Selected archive policy differs from its frozen context") from error
    sources, by_hash, paths = {}, {}, {}
    for row in rows:
        identity = row["candidate_id"]
        if identity not in files:
            raise PdfPreparationError("A selected candidate has no local verified file")
        try:
            path = Path(files[identity])
        except TypeError as error:
            raise PdfPreparationError("Selected local file path is invalid") from error
        content = _source(path, folder, row["original_sha256"], max_source_bytes)
        by_hash.setdefault(row["original_sha256"], content)
        if sum(map(len, by_hash.values())) > max_source_bytes:
            raise PdfPreparationError("Selected originals exceed aggregate source byte policy")
        sources[identity], paths[identity] = by_hash[row["original_sha256"]], path
    identity = selected_preparation_identity(rows, context, max_source_bytes=max_source_bytes,
        max_analysis_bytes=max_analysis_bytes, optimize_lossless=optimize_lossless,
        word_conversion_policy=word_conversion_policy)
    selection_hash = canonical_hash(identity)
    common = {**identity, "document_set_identity": identity,
              "selection_hash": selection_hash, "document_set_hash": selection_hash}
    cache = folder if cache_folder is None else Path(cache_folder)
    if cache.is_symlink():
        raise PdfPreparationError("Preparation cache folder must not be a symlink")
    if cache_folder is not None:
        _mkdir(cache)
        cache.chmod(0o700)
    cache = cache.resolve()
    private = cache / ("selected-analysis-" + selection_hash)
    _mkdir(private)
    with _preparation_lock(private):
        return _materialize(folder, rows, sources, paths, private, common,
            max_source_bytes=max_source_bytes, max_analysis_bytes=max_analysis_bytes,
            optimize_lossless=optimize_lossless, word_conversion_policy=word_conversion_policy)


@contextmanager
def _preparation_lock(private: Path):
    """One converter owns a hash; the OS releases the lock after interruption."""
    path = private / ".preparation.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise PdfPreparationError("Private preparation lock is unavailable") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _materialize(folder, rows, sources, paths, private, common, *,
                 max_source_bytes, max_analysis_bytes, optimize_lossless, word_conversion_policy=None):
    from .batch_runner import private_write, read_private

    marker = private / "manifest.private.json"
    if marker.exists():
        try:
            pinned = read_private(marker)
        except (ValueError, OSError) as error:
            raise PdfPreparationError("Pinned preparation manifest is invalid") from error
        if not isinstance(pinned, dict) or any(pinned.get(key) != value for key, value in common.items()):
            raise PdfPreparationError("Pinned selection context no longer matches")
        result = _result(private, pinned, max_analysis_bytes)
    else:
        writer, provenance, rendered, pages = PdfWriter(), [], {}, 0
        for row in rows:
            candidate_id = row["candidate_id"]
            parent = private / row["document_uuid"]
            _mkdir(parent)
            work = parent / hashlib.sha256(candidate_id.encode()).hexdigest()
            _mkdir(work)
            source = work / ("source" + Path(row["filename"]).suffix.lower())
            if source.exists():
                _source(source, private, row["original_sha256"], max_source_bytes)
            else:
                _write(source, sources[candidate_id])
            try:
                converted = prepare_pdf(source, source.suffix.lstrip("."), max_bytes=max_source_bytes,
                    **({"word_conversion_policy": word_conversion_policy} if word_conversion_policy is not None else {}))
                if converted.is_symlink() or not converted.resolve().is_relative_to(work):
                    raise PdfPreparationError("Converted PDF escaped its private candidate directory")
                if not 0 < converted.stat().st_size <= max_source_bytes:
                    raise PdfPreparationError("Converted PDF exceeds source preparation byte policy")
                pdf = converted.read_bytes()
                _source(source, private, row["original_sha256"], max_source_bytes)
                conversion_provenance = {}
                if word_conversion_policy is not None and source.suffix in {".doc", ".docx"}:
                    receipt = read_private(converted.parent.parent / "word-conversion.private.json")
                    if (not isinstance(receipt, dict) or receipt.get("policy") != word_conversion_policy
                            or receipt.get("pdf_sha256") != hashlib.sha256(pdf).hexdigest()
                            or receipt.get("pdf_bytes") != len(pdf)
                            or receipt.get("only_placement_attribute_changed") is not True
                            or receipt.get("derived_placement") != "document"
                            or receipt.get("original_placement") not in {"page", "document"}
                            or type(receipt.get("footnote_count")) is not int
                            or receipt["footnote_count"] < 0
                            or any(not isinstance(receipt.get(field), str) or not _SHA.fullmatch(receipt[field])
                                   for field in ("native_fodt_sha256", "derived_fodt_sha256", "conversion_source_sha256"))):
                        raise PdfPreparationError("Explicit Word conversion provenance does not match its PDF")
                    conversion_provenance = {"word_conversion": receipt}
            except PdfPreparationError:
                raise
            except Exception as error:
                raise PdfPreparationError("Selected candidate could not be converted to a readable PDF") from error
            converted_sha = hashlib.sha256(pdf).hexdigest()
            if converted_sha in rendered:
                previous = rendered[converted_sha]
                provenance.append({**row, **conversion_provenance, "analysis_sha256": converted_sha,
                    "start_page": previous["start_page"], "end_page": previous["end_page"],
                    "page_offset": previous["start_page"] - 1, "duplicate_of": previous["candidate_id"]})
                continue
            reader = _reader(pdf)
            if pages + len(reader.pages) > MAX_ANALYSIS_PAGES:
                raise PdfPreparationError("Selected document set exceeds total page policy")
            record = {**row, **conversion_provenance, "analysis_sha256": converted_sha,
                      "start_page": pages + 1, "end_page": pages + len(reader.pages), "page_offset": pages}
            for page in reader.pages:
                writer.add_page(page)
            writer.add_outline_item(row["filename"], pages)
            pages += len(reader.pages)
            provenance.append(record)
            rendered[converted_sha] = record
        baseline = _serialize(writer)
        pdf = _lossless_optimization(baseline) if optimize_lossless else baseline
        try:
            parts = partition_pdf(pdf, max_chunk_bytes=max_analysis_bytes, max_pages=MAX_ANALYSIS_PAGES)
        except PdfTransportError as error:
            raise PdfPreparationError("Complete selected PDF cannot fit lossless transport parts") from error
        chunks = []
        for number, part in enumerate(parts, 1):
            name = f"part-{number:04d}.pdf"
            _write(private / name, part.content)
            chunks.append({"file_name": name, "sha256": hashlib.sha256(part.content).hexdigest(),
                "start_page": part.start_page, "end_page": part.end_page,
                "page_offset": part.page_offset, "page_count": part.page_count, "bytes": len(part.content)})
        _write(private / "analysis.pdf", pdf)
        pinned = {**common, "analysis_sha256": hashlib.sha256(pdf).hexdigest(),
                  "analysis_bytes": len(pdf), "page_count": pages, "chunks": chunks,
                  "source_document_manifest": provenance, "unoptimized_analysis_bytes": len(baseline),
                  "optimization_applied": len(pdf) < len(baseline)}
        # Sources are checked again before a complete derivative can be pinned.
        for row in rows:
            _source(paths[row["candidate_id"]], folder, row["original_sha256"], max_source_bytes)
        private_write(marker, pinned)
        result = _result(private, pinned, max_analysis_bytes)
    for row in rows:
        _source(paths[row["candidate_id"]], folder, row["original_sha256"], max_source_bytes)
    return result
