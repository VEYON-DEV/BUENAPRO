"""Conservative, complete local PDF-to-text readiness; never OCR or remote services.

The extracted text is a private derivative, not a replacement for its PDF. A
candidate route says the local coverage checks passed, not that meaning or table
relations have been certified. Ambiguity keeps the complete PDF route.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

from buenapro_worker.local_document_conversion import bounded_command

LOCAL_TEXT_POLICY = "prod2_local_complete_text_v1"
DEFAULT_POLICY = {
    "policy_version": LOCAL_TEXT_POLICY,
    "max_text_bytes": 8 * 1024 * 1024,
    "max_diagnostic_bytes": 16 * 1024 * 1024,
    "max_pages": 1000,
    "relevant_image_area_ratio": 0.08,
    "relevant_vector_area_ratio": 0.08,
    "minimum_character_coverage": 0.90,
}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MAX_PDF_BYTES = 300 * 1024 * 1024


class LocalTextError(ValueError):
    """Preparation identity or pinned private derivative cannot be trusted."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe(path):
    value = Path(path).absolute()
    if ".." in value.parts or any(p.is_symlink() for p in (value, *value.parents)):
        raise LocalTextError("Local text paths must be literal and cannot contain symlinks")
    return value.resolve()


def _file_sha(path, maximum):
    target = _safe(path)
    if not target.is_file() or not 0 < target.stat().st_size <= maximum:
        raise LocalTextError("Local text evidence is missing or exceeds its byte policy")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), target.stat().st_size


def _policy(value):
    if value is None:
        return deepcopy(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise LocalTextError("Local text policy must contain the exact versioned fields")
    result = deepcopy(value)
    if result["policy_version"] != LOCAL_TEXT_POLICY:
        raise LocalTextError("Unsupported local text readiness policy")
    for field in ("max_text_bytes", "max_diagnostic_bytes", "max_pages"):
        if type(result[field]) is not int or not 0 < result[field] <= DEFAULT_POLICY[field]:
            raise LocalTextError("Local text policy exceeds its bounded local limits")
    for field in ("relevant_image_area_ratio", "relevant_vector_area_ratio",
                  "minimum_character_coverage"):
        number = result[field]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not 0 < number <= 1):
            raise LocalTextError("Local text geometry policy is invalid")
    # Overrides may make a check more conservative, never permit more ambiguity.
    if (result["relevant_image_area_ratio"] > DEFAULT_POLICY["relevant_image_area_ratio"]
            or result["relevant_vector_area_ratio"] > DEFAULT_POLICY["relevant_vector_area_ratio"]
            or result["minimum_character_coverage"] < DEFAULT_POLICY["minimum_character_coverage"]):
        raise LocalTextError("Local text overrides cannot relax visual coverage checks")
    return result


def _source_binding(prepared, policy):
    if not isinstance(prepared, dict):
        raise LocalTextError("Prepared PDF identity must be structured")
    count = prepared.get("page_count")
    if type(count) is not int or not 0 < count <= policy["max_pages"]:
        raise LocalTextError("Prepared PDF page count exceeds local policy")
    sha = prepared.get("analysis_sha256")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise LocalTextError("Prepared PDF hash is invalid")
    pdf = _safe(prepared.get("pdf_path", ""))
    actual, size = _file_sha(pdf, _MAX_PDF_BYTES)
    if actual != sha or type(prepared.get("analysis_bytes")) is not int or size != prepared["analysis_bytes"]:
        raise LocalTextError("Prepared PDF no longer matches its pinned hash and size")
    manifest = prepared.get("source_document_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise LocalTextError("Prepared PDF lacks its complete source page mapping")
    if len(_canonical(manifest).encode("utf-8")) > policy["max_diagnostic_bytes"] // 2:
        raise LocalTextError("Source page mapping exceeds the bounded diagnostic policy")
    primaries, mapping, identifiers = {}, {number: [] for number in range(1, count + 1)}, set()
    mapping_bytes = 0
    for row in manifest:
        if not isinstance(row, dict):
            raise LocalTextError("Source page mapping is invalid")
        start, end, offset = (row.get(key) for key in ("start_page", "end_page", "page_offset"))
        identifier = row.get("candidate_id")
        if (any(type(number) is not int for number in (start, end, offset))
                or not 1 <= start <= end <= count or offset != start - 1
                or not isinstance(identifier, str) or not identifier or identifier in identifiers
                or len(identifier) > 4000 or not isinstance(row.get("original_sha256"), str)
                or not _SHA.fullmatch(row["original_sha256"])
                or not isinstance(row.get("analysis_sha256"), str)
                or not _SHA.fullmatch(row["analysis_sha256"])):
            raise LocalTextError("Source page mapping is incomplete or inconsistent")
        identifiers.add(identifier)
        if not row.get("duplicate_of"):
            for number in range(start, end + 1):
                if number in primaries:
                    raise LocalTextError("Primary source page ranges overlap")
                primaries[number] = identifier
        for number in range(start, end + 1):
            record = {
                "candidate_id": identifier,
                "document_uuid": row.get("document_uuid"),
                "source_page": number - offset,
                "original_sha256": row["original_sha256"],
                "analysis_sha256": row["analysis_sha256"],
                "duplicate_of": row.get("duplicate_of"),
            }
            mapping_bytes += len(_canonical(record).encode("utf-8"))
            if mapping_bytes > policy["max_diagnostic_bytes"] // 2:
                raise LocalTextError("Complete page alias mapping exceeds its local byte policy")
            mapping[number].append(record)
    by_id = {row["candidate_id"]: row for row in manifest}
    for row in manifest:
        if row.get("duplicate_of"):
            parent = by_id.get(row["duplicate_of"])
            if (not parent or parent.get("duplicate_of")
                    or any(row[key] != parent[key] for key in
                           ("start_page", "end_page", "page_offset", "analysis_sha256"))):
                raise LocalTextError("Duplicate source page mapping differs from its primary")
    if set(primaries) != set(mapping):
        raise LocalTextError("Prepared PDF source mapping does not cover every page")
    return pdf, {"analysis_sha256": sha, "analysis_bytes": size, "page_count": count,
                 "source_document_manifest": deepcopy(manifest)}, mapping


def _versions(binary, timeout):
    result = {"python": sys.version.split()[0], "pdftotext": "unavailable"}
    for package in ("pdfplumber", "pdfminer.six"):
        try:
            result[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            result[package] = "unavailable"
    if binary:
        # Homebrew intentionally exposes tools through symlinks. Bind the actual
        # executable bytes; the no-symlink rule applies to source/cache evidence.
        target = _safe(Path(binary).resolve())
        result["pdftotext_binary_sha256"] = _file_sha(target, 64 * 1024 * 1024)[0]
        response = subprocess.run([str(target), "-v"], capture_output=True, timeout=timeout,
                                  check=False)
        version = response.stderr + response.stdout
        if response.returncode == 0 and 0 < len(version) <= 8192:
            result["pdftotext"] = version.decode("utf-8", errors="replace").splitlines()[0]
    return result


def _write(path, content):
    target = _safe(path)
    descriptor, name = tempfile.mkstemp(prefix=".local-text-writing-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(folder, timeout_seconds):
    descriptor = os.open(folder / ".local-text.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LocalTextError("Local text private cache lock timed out") from None
                time.sleep(min(.01, max(.001, deadline - time.monotonic())))
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_json(path, maximum):
    target = _safe(path)
    _file_sha(target, maximum)
    try:
        return json.loads(target.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise LocalTextError("Pinned local text metadata is invalid") from error


def _bad_unicode(text):
    return ("\ufffd" in text or bool(re.search(r"\(cid:\d+\)", text))
            or any(unicodedata.category(char) == "Co"
                   or unicodedata.category(char) == "Cc" and char not in "\n\r\t"
                   for char in text))


def _area(item, width, height):
    try:
        values = [float(item[key]) for key in ("x0", "x1", "top", "bottom")]
        if not all(math.isfinite(value) for value in values):
            return 1.0
        x0, x1, top, bottom = values
        return max(0, min(width, x1) - max(0, x0)) * max(0, min(height, bottom) - max(0, top)) / (width * height)
    except (ValueError, TypeError, KeyError):
        return 1.0


def _inspect_pdf(path, policy):
    """Child-only PDF parser. Parent bounds elapsed time and stdout bytes."""
    import pdfplumber

    rows = []
    with pdfplumber.open(path) as pdf:
        if not 0 < len(pdf.pages) <= policy["max_pages"]:
            raise LocalTextError("PDF geometry page count exceeds local policy")
        for number, page in enumerate(pdf.pages, 1):
            width, height = float(page.width), float(page.height)
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise LocalTextError("PDF page geometry is invalid")
            image_areas = [_area(item, width, height) for item in page.images]
            # A small image in the body may still contain a requirement. Only
            # small header/footer images avoid an automatic visual-content flag.
            body_images = sum(float(item.get("top", 0)) > height * .15
                              and float(item.get("bottom", height)) < height * .85
                              for item in page.images)
            vectors = [*page.rects, *page.curves, *page.lines]
            vector_areas = [_area(item, width, height) for item in vectors]
            # Coordinates only leave the worker; source text never appears in diagnostics.
            lines = {}
            for word in page.extract_words():
                bucket = round(float(word["top"]) / 3)
                lines.setdefault(bucket, []).append((float(word["x0"]), float(word["x1"])))
            gap_rows = 0
            for words in lines.values():
                words.sort()
                if any(right[0] - left[1] > width * .15
                       for left, right in zip(words, words[1:])):
                    gap_rows += 1
            native = "".join(char.get("text", "") for char in page.chars)
            annotations = page.annots or []
            meaningful_annotations = sum(
                bool(item.get("data", {}).get("Contents"))
                or str(item.get("data", {}).get("Subtype", "")) not in {"Link", "/Link"}
                for item in annotations)
            rows.append({
                "global_page": number,
                "native_nonspace_characters": sum(not char.isspace() for char in native),
                "native_unicode_corrupt": _bad_unicode(native),
                "image_count": len(image_areas),
                "image_area_ratio": min(1.0, sum(image_areas)),
                "largest_image_area_ratio": max(image_areas, default=0),
                "body_image_count": body_images,
                "vector_count": len(vectors),
                "largest_vector_area_ratio": max(vector_areas, default=0),
                "detected_tables": len(page.find_tables()),
                "column_gap_rows": gap_rows,
                "meaningful_annotation_count": meaningful_annotations,
                "unknown_object_types": sorted(set(page.objects) - {"char", "image", "rect", "curve", "line"}),
            })
            page.close()
    return rows


def _inspect_pdf_cli():
    print(_canonical(_inspect_pdf(sys.argv[1], json.loads(sys.argv[2]))))


def _page_assessment(text, geometry, source_mappings, policy):
    row = deepcopy(geometry)
    row.update(source_mappings=source_mappings,
               extracted_nonspace_characters=sum(not char.isspace() for char in text),
               extracted_utf8_bytes=len(text.encode("utf-8")),
               text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    reasons = []
    native, extracted = row["native_nonspace_characters"], row["extracted_nonspace_characters"]
    if _bad_unicode(text) or row["native_unicode_corrupt"]:
        reasons.append("unicode_or_font_mapping_ambiguous")
    if row["image_area_ratio"] >= policy["relevant_image_area_ratio"]:
        reasons.append("relevant_image_or_ocr_layer")
    if row.get("body_image_count", 0):
        reasons.append("body_image_content_ambiguous")
    if row["detected_tables"]:
        reasons.append("table_relations_require_visual_review")
    if row["column_gap_rows"] >= 3:
        reasons.append("column_reading_order_ambiguous")
    if (row["largest_vector_area_ratio"] >= policy["relevant_vector_area_ratio"]
            or row["vector_count"] >= 20):
        reasons.append("vector_content_requires_visual_review")
    if row["meaningful_annotation_count"] or row["unknown_object_types"]:
        reasons.append("nontext_page_objects_ambiguous")
    if native and extracted < native * policy["minimum_character_coverage"]:
        reasons.append("native_text_coverage_mismatch")
    if not extracted:
        if native or row["image_count"] or row["vector_count"] or row["meaningful_annotation_count"] or row["unknown_object_types"]:
            reasons.append("empty_extraction_with_visible_or_unknown_content")
        else:
            row["confirmed_empty_page"] = True
    elif not native:
        reasons.append("extraction_without_native_character_coverage")
    row["reasons"] = sorted(set(reasons))
    row["acceptable"] = not row["reasons"]
    return row


def _marked_text(pages, mapping):
    chunks = []
    for number, text in enumerate(pages, 1):
        # File names are deliberately excluded. JSON escapes any source identifier
        # delimiters/newlines; identifiers are metadata, never instructions.
        source = _canonical(mapping[number])
        chunks.append(f"[DOCUMENTO FUENTES={source}; PAGINA GLOBAL={number}]\n{text}\n"
                      f"[FIN PAGINA GLOBAL={number}]\n")
    return "\n".join(chunks).encode("utf-8")


def _result(private, pinned, *, cache_reused):
    result = deepcopy(pinned)
    result.update(manifest_path=private / "manifest.private.json", cache_reused=cache_reused,
                  text_path=private / "complete.private.txt" if pinned.get("text_sha256") else None)
    result["metadata_sha256"] = _hash(pinned)
    return result


def _validate_pinned(pinned, binding, policy, versions, key):
    if (not isinstance(pinned, dict) or pinned.get("source_binding") != binding
            or pinned.get("policy") != policy or pinned.get("versions") != versions
            or pinned.get("cache_key") != key or pinned.get("policy_version") != LOCAL_TEXT_POLICY
            or pinned.get("pdf_sha256_before") != binding["analysis_sha256"]
            or pinned.get("pdf_sha256_after") != binding["analysis_sha256"]
            or pinned.get("page_count") != binding["page_count"]
            or pinned.get("route") not in {"candidate_text", "pdf_fallback"}):
        raise LocalTextError("Pinned local text cache differs from its exact identity")
    pages = pinned.get("pages")
    if pinned["route"] == "candidate_text":
        if (pinned.get("reasons") or not isinstance(pages, list)
                or [row.get("global_page") for row in pages] != list(range(1, binding["page_count"] + 1))
                or any(row.get("acceptable") is not True or row.get("reasons") for row in pages)
                or not any(row.get("extracted_nonspace_characters", 0) for row in pages)
                or pinned.get("coverage_complete") is not True
                or not _SHA.fullmatch(pinned.get("text_sha256", ""))
                or type(pinned.get("text_bytes")) is not int
                or not 0 < pinned["text_bytes"] <= policy["max_text_bytes"]):
            raise LocalTextError("Pinned text candidate lacks complete acceptable page coverage")
    elif not pinned.get("reasons"):
        raise LocalTextError("Pinned PDF fallback lacks its diagnostic reason")


def prepare_local_text(prepared: dict, folder: Path, *, timeout_seconds: float = 90,
                       policy: dict | None = None) -> dict:
    """Extract all pages locally, or retain PDF fallback without partial input.

    Data identity/cache tampering raises LocalTextError; missing local tools,
    parse errors and bounded timeouts produce an audited PDF fallback. No text
    is discarded to make a candidate fit: byte-limit failures keep the PDF.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
        raise LocalTextError("Local text timeout must be bounded to 300 seconds")
    limits = _policy(policy)
    pdf, binding, mapping = _source_binding(prepared, limits)
    base = _safe(folder)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    started, binary = time.monotonic(), shutil.which("pdftotext")
    try:
        versions = _versions(binary, min(10, timeout_seconds))
    except (OSError, subprocess.TimeoutExpired):
        versions = {"python": sys.version.split()[0], "local_tool_versions": "unavailable"}
    key = _hash({"source_binding": binding, "policy": limits, "versions": versions})
    private = _safe(base / ("local-text-" + key))
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    with _lock(private, max(.001, timeout_seconds - (time.monotonic() - started))):
        marker = private / "manifest.private.json"
        if marker.exists() or marker.is_symlink():
            pinned = _read_json(marker, limits["max_diagnostic_bytes"])
            _validate_pinned(pinned, binding, limits, versions, key)
            result = _result(private, pinned, cache_reused=True)
            if pinned.get("text_sha256"):
                _verify_text_file(result, pinned)
            if _file_sha(pdf, _MAX_PDF_BYTES) != (binding["analysis_sha256"], binding["analysis_bytes"]):
                raise LocalTextError("Source PDF changed during local text cache verification")
            return result
        pinned = {"policy_version": LOCAL_TEXT_POLICY, "policy": limits, "versions": versions,
                  "cache_key": key, "source_binding": binding, "page_count": binding["page_count"],
                  "pdf_sha256_before": binding["analysis_sha256"], "route": "pdf_fallback",
                  "reasons": [], "pages": [], "coverage_complete": False,
                  "text_sha256": None, "text_bytes": None}
        try:
            if not binary or any(versions.get(package) == "unavailable" for package in
                                 ("pdftotext", "pdfplumber", "pdfminer.six")):
                raise RuntimeError("missing_local_tools")

            def remaining():
                left = timeout_seconds - (time.monotonic() - started)
                if left <= 0:
                    raise TimeoutError("Local text diagnostic time budget exhausted")
                return left

            raw = bounded_command([binary, "-f", "1", "-l", str(binding["page_count"]),
                                   "-layout", "-enc", "UTF-8", "-eol", "unix", str(pdf), "-"],
                                  limit=limits["max_text_bytes"], timeout=remaining())
            extracted = raw.decode("utf-8", errors="strict").split("\f")
            if extracted and not extracted[-1].strip():
                extracted.pop()  # Poppler's final page separator, not real blank pages.
            if len(extracted) != binding["page_count"]:
                raise ValueError("page_separator_coverage_mismatch")
            command = [sys.executable, "-c",
                       "from buenapro_worker.prod2.local_text import _inspect_pdf_cli; _inspect_pdf_cli()",
                       str(pdf), _canonical(limits)]
            geometry = json.loads(bounded_command(command, limit=limits["max_diagnostic_bytes"],
                                                  timeout=remaining()))
            if (not isinstance(geometry, list) or len(geometry) != binding["page_count"]
                    or [row.get("global_page") for row in geometry] != list(range(1, binding["page_count"] + 1))):
                raise ValueError("geometry_page_coverage_mismatch")
            pages = [_page_assessment(text, row, mapping[number], limits)
                     for number, (text, row) in enumerate(zip(extracted, geometry), 1)]
            pinned.update(pages=pages, coverage_complete=True,
                          reasons=sorted({reason for row in pages for reason in row["reasons"]}))
            if not any(row["extracted_nonspace_characters"] for row in pages):
                pinned["reasons"].append("document_has_no_usable_text")
            marked = _marked_text(extracted, mapping)
            if len(marked) > limits["max_text_bytes"]:
                raise ValueError("marked_text_exceeds_byte_policy")
            _write(private / "complete.private.txt", marked)
            pinned.update(text_sha256=hashlib.sha256(marked).hexdigest(), text_bytes=len(marked))
            if not pinned["reasons"]:
                pinned["route"] = "candidate_text"
        except Exception as error:
            # No raw error strings or extracted text are logged or persisted.
            # The complete PDF remains the only usable model input on failure.
            reason = "local_text_timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else (
                "local_tools_unavailable" if isinstance(error, RuntimeError) else "local_text_extraction_failure")
            pinned.update(route="pdf_fallback", reasons=sorted(set(pinned["reasons"] + [reason])))
        after, after_bytes = _file_sha(pdf, _MAX_PDF_BYTES)
        if after != binding["analysis_sha256"] or after_bytes != binding["analysis_bytes"]:
            raise LocalTextError("Source PDF changed during local text extraction")
        pinned["pdf_sha256_after"] = after
        _validate_pinned(pinned, binding, limits, versions, key)
        encoded = _canonical(pinned).encode("utf-8")
        if len(encoded) > limits["max_diagnostic_bytes"]:
            raise LocalTextError("Complete local text metadata exceeds its byte policy")
        _write(marker, encoded)
        return _result(private, pinned, cache_reused=False)


def _verify_text_file(result, pinned):
    marker = _safe(result["manifest_path"])
    target = _safe(result.get("text_path", ""))
    if target != marker.parent / "complete.private.txt":
        raise LocalTextError("Local text path escaped its private cache")
    sha, size = _file_sha(target, pinned["policy"]["max_text_bytes"])
    if sha != pinned.get("text_sha256") or size != pinned.get("text_bytes"):
        raise LocalTextError("Pinned complete local text has changed")
    return target


def read_verified_text(result: dict, prepared: dict) -> str:
    """Recheck private metadata, source SHA and full text immediately before use."""
    if not isinstance(result, dict) or result.get("route") != "candidate_text":
        raise LocalTextError("Only complete locally accepted text can be transported")
    policy = _policy(result.get("policy"))
    pdf, binding, mapping = _source_binding(prepared, policy)
    marker = _safe(result.get("manifest_path", ""))
    pinned = _read_json(marker, policy["max_diagnostic_bytes"])
    key = _hash({"source_binding": binding, "policy": policy, "versions": result.get("versions")})
    _validate_pinned(pinned, binding, policy, result.get("versions"), key)
    if (marker.parent.name != "local-text-" + key or marker.name != "manifest.private.json"
            or result.get("metadata_sha256") != _hash(pinned)
            or any(result.get(field) != pinned.get(field) for field in pinned)
            or any(row.get("source_mappings") != mapping[row["global_page"]] for row in pinned["pages"])):
        raise LocalTextError("Local text result differs from its exact pinned metadata")
    target = _verify_text_file(result, pinned)
    try:
        content = target.read_bytes().decode("utf-8", errors="strict")
    except (UnicodeError, OSError) as error:
        raise LocalTextError("Pinned complete local text is unreadable") from error
    # Recheck after reading too: no mutable-file transport between checks.
    if (hashlib.sha256(content.encode("utf-8")).hexdigest() != pinned["text_sha256"]
            or _file_sha(pdf, _MAX_PDF_BYTES)[0] != binding["analysis_sha256"]):
        raise LocalTextError("Local text or source changed during transport verification")
    return content
