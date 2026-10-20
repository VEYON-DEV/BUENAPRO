"""Complete selected-PDF corpus, native text and bounded local OCR, with no providers.

Word inputs use the existing verified PDF conversion: every location is explicitly
a page of that derivative. Coverage diagnostics never imply legal completeness.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import csv
import fcntl
import hashlib
import io
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unicodedata

from buenapro_worker.local_document_conversion import bounded_command
from .local_text import DEFAULT_POLICY as _NATIVE_POLICY, _source_binding, _file_sha, _safe
from .partial_reading import PAGE_FAILURE_POLICY, is_partial_corpus

CORPUS_POLICY = "prod2_complete_embedding_corpus_v1"
DEFAULT_CONFIG = {
    "policy_version": CORPUS_POLICY, "tokenizer": "cl100k_base",
    "chunk_target_tokens": 1000, "overlap_tokens": 128, "maximum_input_tokens": 8192,
    "max_pages": 1000, "max_text_bytes": 32 * 1024 * 1024,
    "max_manifest_bytes": 96 * 1024 * 1024,
    "native_timeout_seconds": 300, "ocr_timeout_seconds": 90,
    "ocr_lock_timeout_seconds": 300, "max_ocr_pages": 1000,
    "ocr_languages": "spa+eng", "ocr_dpi": 180, "ocr_max_dimension": 2600,
    "relevant_image_area_ratio": .08, "minimum_ocr_confidence": 65.0,
    "minimum_ocr_confident_word_ratio": .85,
    "minimum_ocr_words": 30, "textual_scan_minimum_image_area_ratio": .65,
    "maximum_ocr_column_gap_rows": 2,
    "ocr_textual_scan_policy": "prod2_confident_textual_scan_v1",
}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_OCR_SEMAPHORE = threading.BoundedSemaphore(1)


class CorpusError(ValueError):
    """Source identity, corpus integrity or configuration cannot be trusted."""


class CorpusStopped(InterruptedError):
    """Local work stopped at a page boundary; verified OCR caches remain reusable."""


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def canonical_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokenizer():
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def token_count(text):
    # Source strings are untrusted text, never special tokenizer instructions.
    return len(tokenizer().encode(text, disallowed_special=()))


def _config(value):
    result = deepcopy(DEFAULT_CONFIG)
    if value is not None:
        if not isinstance(value, dict) or set(value) - (set(result) | {"page_failure_policy", "partial_ocr_psm"}):
            raise CorpusError("Unknown corpus configuration field")
        result.update(deepcopy(value))
    if result["policy_version"] != CORPUS_POLICY or result["tokenizer"] != "cl100k_base":
        raise CorpusError("Unsupported corpus policy or tokenizer")
    for key in ("chunk_target_tokens", "overlap_tokens", "maximum_input_tokens",
                "max_pages", "max_text_bytes", "max_manifest_bytes", "max_ocr_pages",
                "ocr_dpi", "ocr_max_dimension", "minimum_ocr_words", "maximum_ocr_column_gap_rows"):
        if type(result[key]) is not int or result[key] < (0 if key == "overlap_tokens" else 1):
            raise CorpusError("Invalid bounded corpus configuration")
    if not (128 <= result["chunk_target_tokens"] <= result["maximum_input_tokens"] <= 8192
            and result["overlap_tokens"] < result["chunk_target_tokens"]
            and result["max_pages"] <= 1000 and result["max_ocr_pages"] <= 1000
            and result["max_text_bytes"] <= DEFAULT_CONFIG["max_text_bytes"]
            and result["max_manifest_bytes"] <= DEFAULT_CONFIG["max_manifest_bytes"]
            and 72 <= result["ocr_dpi"] <= 300 and result["ocr_max_dimension"] <= 4000
            and result["minimum_ocr_words"] >= 30 and result["maximum_ocr_column_gap_rows"] <= 2):
        raise CorpusError("Corpus limits exceed their hard bounds")
    for key in ("native_timeout_seconds", "ocr_timeout_seconds", "ocr_lock_timeout_seconds"):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 < number <= 300:
            raise CorpusError("Local process timeout exceeds its bound")
    for key in ("relevant_image_area_ratio", "minimum_ocr_confident_word_ratio",
                "textual_scan_minimum_image_area_ratio"):
        number = result[key]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not 0 < number <= 1):
            raise CorpusError("Invalid corpus coverage threshold")
    if result["relevant_image_area_ratio"] > .08:
        raise CorpusError("Image coverage threshold cannot be relaxed")
    number = result["minimum_ocr_confidence"]
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not 65 <= number <= 100:
        raise CorpusError("OCR confidence threshold cannot be relaxed")
    if (result["minimum_ocr_confident_word_ratio"] < .85
            or not re.fullmatch(r"[a-z_]+(?:\+[a-z_]+)*", result["ocr_languages"])):
        raise CorpusError("Invalid OCR language or confidence policy")
    if (result["textual_scan_minimum_image_area_ratio"] < .65
            or result["ocr_textual_scan_policy"] not in {
                "prod2_confident_textual_scan_v1", "prod2_confident_text_layout_review_v2",
                "prod2_confident_text_layout_review_v3"}):
        raise CorpusError("Unsupported or relaxed textual-scan policy")
    if "page_failure_policy" in result and (
            result["page_failure_policy"] != PAGE_FAILURE_POLICY
            or result["ocr_textual_scan_policy"] != "prod2_confident_text_layout_review_v3"):
        raise CorpusError("Partial page failure policy requires its explicit V3 OCR contract")
    if "partial_ocr_psm" in result and (result["partial_ocr_psm"] != "1" or not is_partial_corpus(result)):
        raise CorpusError("Explicit partial OCR orientation mode requires V5 and PSM1")
    return result


def _bad_unicode(text):
    return ("\ufffd" in text or "\x00" in text or bool(re.search(r"\(cid:\d+\)", text))
            or any(unicodedata.category(char) == "Co" for char in text))


def _area(obj, width, height):
    return max(0., min(width, float(obj.get("x1", 0))) - max(0., float(obj.get("x0", 0)))) * max(
        0., min(height, float(obj.get("bottom", 0))) - max(0., float(obj.get("top", 0)))) / (width * height)


def _annotation_name(value):
    """PDF names may be PSLiteral objects whose str() is /'Link', not /Link."""
    from pdfminer.pdftypes import resolve1
    from pdfminer.psparser import PSLiteral, literal_name
    value = resolve1(value)
    if isinstance(value, PSLiteral):
        value = literal_name(value)
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    return value.removeprefix("/") if isinstance(value, str) else None


def _annotation_present(value):
    """An empty/whitespace PDF string is not an annotation's substantive text."""
    from pdfminer.pdftypes import resolve1
    from pdfminer.utils import decode_text
    value = resolve1(value)
    if isinstance(value, bytes):
        return bool(decode_text(value).strip())
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and bool(value)


def _annotation_field(data, key):
    """AcroForm field values/type can live on a parent, with a bounded traversal."""
    from pdfminer.pdftypes import resolve1
    seen = set()
    current = data
    for _ in range(8):
        current = resolve1(current)
        if not isinstance(current, dict) or id(current) in seen:
            return None
        seen.add(id(current))
        if key in current:
            return resolve1(current[key])
        current = current.get("Parent")
        if current is None:
            return None
    raise CorpusError("PDF annotation field inheritance exceeds its bound")


def _classify_annotations(annotations):
    """V3 only: distinguish navigation/cryptographic widgets from omitted content.

    A signature widget is retained as a visual-review warning, never evidence that
    a signature, authority or legal permission has been verified. Form values,
    comment/markup content and unknown objects remain unresolved rather than being
    silently dropped. No raw field values, personal names or URLs enter telemetry.
    """
    records, warnings, unresolved = [], set(), 0
    for item in annotations or []:
        data = item.get("data", {})
        if not isinstance(data, dict):
            raise CorpusError("PDF annotation dictionary is invalid")
        subtype = _annotation_name(data.get("Subtype"))
        field_type = _annotation_name(_annotation_field(data, "FT")) if subtype == "Widget" else None
        contents = _annotation_present(data.get("Contents"))
        appearance = _annotation_present(data.get("AP"))
        value = _annotation_present(_annotation_field(data, "V")) if subtype == "Widget" else False
        default_value = _annotation_present(_annotation_field(data, "DV")) if subtype == "Widget" else False
        # Off is an actual checkbox/radio state: it is still a form value whose
        # association with surrounding labels cannot be inferred from its name.
        appearance_state = _annotation_present(data.get("AS")) if subtype == "Widget" else False
        if subtype == "Link" and not contents and not appearance:
            classification = "navigation_link_without_content"
        elif subtype == "Widget" and field_type == "Sig" and not contents:
            classification = "signature_widget_visual_review"
            warnings.add("signature_widget_visual_content_not_verified")
        elif subtype == "Widget":
            classification = ("form_widget_data_not_captured" if
                contents or value or default_value or appearance or appearance_state else
                "form_widget_structure_not_verified")
            warnings.add("form_widget_values_or_relations_not_verified")
            unresolved += 1
        else:
            classification = "substantive_or_unknown_annotation_not_captured"
            warnings.add("annotation_content_or_appearance_not_captured")
            unresolved += 1
        records.append({"subtype": subtype, "field_type": field_type,
            "classification": classification, "has_contents": contents,
            "has_appearance": appearance, "has_value": value,
            "has_default_value": default_value, "has_appearance_state": appearance_state})
    return {"policy_version": "prod2_annotation_text_capture_v3", "annotations": records,
            "unresolved_count": unresolved, "visual_warnings": sorted(warnings)}


def _native_pages(path, annotation_policy=None):
    """Child-process implementation: PDF parsing does not run unbounded in the parent."""
    import pdfplumber
    result = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 1000:
            raise CorpusError("PDF page count exceeds extraction bound")
        for number, page in enumerate(pdf.pages, 1):
            width, height = float(page.width), float(page.height)
            if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                raise CorpusError("Invalid PDF page geometry")
            text = page.extract_text(layout=True) or ""
            # Preserve cells and headers separately, in addition to the complete
            # layout text. Retrieval includes complete table blocks, never a row slice.
            tables = []
            for index, table in enumerate(page.find_tables()):
                cells = table.extract()
                if cells:
                    tables.append({"ordinal": index, "bbox": list(table.bbox), "rows": [
                        ["" if cell is None else str(cell) for cell in row] for row in cells]})
            images = [{"bbox": [image.get("x0"), image.get("top"), image.get("x1"),
                                  image.get("bottom")], "area_ratio": _area(image, width, height)}
                      for image in page.images]
            vectors = [*page.rects, *page.curves, *page.lines]
            annotation_review = None
            if annotation_policy == "prod2_confident_text_layout_review_v3":
                annotation_review = _classify_annotations(page.annots)
                annotation_count = annotation_review["unresolved_count"]
            else:
                # Preserve the exact legacy classification and output/cache identity.
                annotations = [item for item in (page.annots or [])
                               if item.get("data", {}).get("Contents")
                               or str(item.get("data", {}).get("Subtype", "")) not in {"Link", "/Link"}]
                annotation_count = len(annotations)
            native_count = sum(not str(char.get("text", "")).isspace() for char in page.chars)
            result.append({"global_page": number, "text": text, "tables": tables,
                "width": width, "height": height, "images": images,
                "image_area_ratio": min(1., sum(row["area_ratio"] for row in images)),
                "native_characters": native_count,
                "native_unicode_corrupt": _bad_unicode("".join(c.get("text", "") for c in page.chars)),
                "vector_count": len(vectors), "largest_vector_area_ratio": max(
                    (_area(item, width, height) for item in vectors), default=0.),
                "meaningful_annotations": annotation_count,
                "unknown_object_types": sorted(set(page.objects) - {"char", "image", "rect", "curve", "line"})})
            if annotation_review is not None:
                result[-1]["annotation_review"] = annotation_review
            page.close()
    return result


def _extract_native(pdf, config):
    package_root = str(Path(__file__).resolve().parents[2])
    # CPU bounds complement wall-clock/output limits. Darwin rejects RLIMIT_DATA
    # despite reporting infinity: declare it unsupported instead of rejecting
    # every PDF or claiming a memory bound that was never installed.
    cpu = math.ceil(config["native_timeout_seconds"])
    annotation_argument = ("," + json.dumps(config["ocr_textual_scan_policy"]) if
        config["ocr_textual_scan_policy"] == "prod2_confident_text_layout_review_v3" else "")
    code = ("import sys,resource;"
            "resource.setrlimit(resource.RLIMIT_DATA,(1073741824,1073741824)) if sys.platform != 'darwin' else None;"
            f"resource.setrlimit(resource.RLIMIT_CPU,({cpu},{cpu + 1}));"
            "sys.path.insert(0," + json.dumps(package_root) + ");"
            "from buenapro_worker.prod2.embedding_corpus import _native_pages,canonical_json;"
            "print(canonical_json(_native_pages(sys.argv[1]" + annotation_argument + ")))")
    data = bounded_command([sys.executable, "-B", "-c", code, str(pdf)],
                           limit=config["max_manifest_bytes"], timeout=config["native_timeout_seconds"])
    return json.loads(data)


def _versions(config):
    result = {"native_data_allocation_limit_bytes": 1024**3 if sys.platform != "darwin" else None,
              "native_cpu_limit_seconds": math.ceil(config["native_timeout_seconds"])}
    for package in ("pdfplumber", "pdfminer.six", "tiktoken"):
        result[package] = metadata.version(package)
    for name in ("pdftoppm", "tesseract"):
        binary = shutil.which(name)
        if not binary:
            result[name] = None
            continue
        target = Path(binary).resolve()
        result[name] = {"binary_sha256": _file_sha(target, 64 * 1024 * 1024)[0]}
    return result


@contextmanager
def _file_lock(path, timeout):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CorpusError("Local OCR lock is not owned by this user")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Local OCR lock timed out") from None
                time.sleep(.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _ocr_slot(config):
    acquired = _OCR_SEMAPHORE.acquire(timeout=config["ocr_lock_timeout_seconds"])
    if not acquired:
        raise TimeoutError("Local OCR slot timed out")
    try:
        lock = Path(tempfile.gettempdir()).resolve() / f"buenapro-prod2-ocr-{os.getuid()}.lock"
        with _file_lock(lock, config["ocr_lock_timeout_seconds"]):
            yield
    finally:
        _OCR_SEMAPHORE.release()


def _parse_tsv(data, config):
    options = {"quoting": csv.QUOTE_NONE} if config["ocr_textual_scan_policy"] == "prod2_confident_text_layout_review_v3" else {}
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="strict")), delimiter="\t", **options))
    words, lines = [], {}
    for row in rows:
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        confidence = float(row["conf"])
        if not math.isfinite(confidence) or not -1 <= confidence <= 100:
            raise CorpusError("OCR returned an invalid confidence")
        key = tuple(int(row[name]) for name in ("page_num", "block_num", "par_num", "line_num"))
        word = {"text": row["text"], "confidence": confidence,
                "left": int(row["left"]), "top": int(row["top"]),
                "width": int(row["width"]), "height": int(row["height"])}
        lines.setdefault(key, []).append(word)
        words.append(word)
    texts, column_gap_rows = [], 0
    for key in sorted(lines):
        row = sorted(lines[key], key=lambda item: item["left"])
        # Tabs preserve large visual column gaps. OCR tables remain explicitly
        # unverified because text and confidence alone do not certify cell relations.
        cells = []
        column_gap = False
        for index, word in enumerate(row):
            if index:
                gap = word["left"] - row[index - 1]["left"] - row[index - 1]["width"]
                cells.append("\t" if gap > max(24, word["height"] * 2) else " ")
                column_gap = column_gap or gap > max(24, word["height"] * 2)
            cells.append(word["text"])
        texts.append("".join(cells))
        column_gap_rows += bool(column_gap)
    text = "\n".join(texts)
    mean = sum(word["confidence"] for word in words) / len(words) if words else None
    confident_ratio = (sum(word["confidence"] >= config["minimum_ocr_confidence"]
                           for word in words) / len(words)) if words else 0.
    return {"text": text, "word_count": len(words), "mean_confidence": mean,
            "confident_word_ratio": confident_ratio, "column_gap_rows": column_gap_rows, "words": words}


def _ocr_page_once(pdf, number, private, config, *, psm="3"):
    render, ocr = shutil.which("pdftoppm"), shutil.which("tesseract")
    if not render or not ocr:
        return {"text": "", "acceptable": False, "reasons": ["local_ocr_tool_unavailable"]}
    with _ocr_slot(config):
        with tempfile.TemporaryDirectory(prefix=f"ocr-page-{number}-", dir=private) as workspace:
            folder = Path(workspace)
            image = folder / "page.png"
            data = bounded_command([render, "-f", str(number), "-l", str(number), "-singlefile",
                "-r", str(config["ocr_dpi"]), "-scale-to", str(config["ocr_max_dimension"]),
                "-png", str(pdf)], limit=40 * 1024 * 1024, timeout=config["ocr_timeout_seconds"])
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise CorpusError("Local render did not return a PNG")
            image.write_bytes(data)
            os.chmod(image, 0o600)
            response = bounded_command(["/usr/bin/env", "OMP_THREAD_LIMIT=1", ocr, str(image),
                "stdout", "-l", config["ocr_languages"], "--psm", psm, "tsv"],
                limit=8 * 1024 * 1024, timeout=config["ocr_timeout_seconds"])
            result = _parse_tsv(response, config)
            result["cleaned_render_bytes"] = len(data)
    reasons = []
    if not result["word_count"]:
        reasons.append("ocr_empty_on_visible_page")
    if _bad_unicode(result["text"]):
        reasons.append("ocr_unicode_ambiguous")
    if result["confident_word_ratio"] < config["minimum_ocr_confident_word_ratio"]:
        reasons.append("ocr_low_confidence")
    if result["mean_confidence"] is None or result["mean_confidence"] < config["minimum_ocr_confidence"]:
        reasons.append("ocr_low_mean_confidence")
    result.update(acceptable=not reasons, reasons=reasons)
    # Full word boxes are useful during extraction, not duplicated in the corpus.
    result.pop("words", None)
    return result


def _ocr_page(pdf, number, private, config):
    if is_partial_corpus(config):
        # Catch only an engine/render failure here. Source and cache integrity
        # checks in _cached_ocr_page remain outside this boundary and still abort.
        try:
            result = _ocr_page_once(pdf, number, private, config, psm=config.get("partial_ocr_psm", "3"))
        except CorpusStopped:
            raise
        except (OSError, ValueError, TimeoutError, KeyError) as error:
            result = {"text": "", "acceptable": False,
                "reasons": ["local_ocr_" + type(error).__name__.lower()]}
        result["ocr_attempts"] = [{"dpi": config["ocr_dpi"],
            "maximum_dimension": config["ocr_max_dimension"], "psm": config.get("partial_ocr_psm", "3"),
            "word_count": result.get("word_count", 0),
            "mean_confidence": result.get("mean_confidence"),
            "confident_word_ratio": result.get("confident_word_ratio", 0),
            "text_sha256": text_hash(result.get("text", "")),
            "acceptable": _strong_ocr_text(result, config), "reasons": result.get("reasons", [])}]
        return result
    if config["ocr_textual_scan_policy"] != "prod2_confident_text_layout_review_v3":
        return _ocr_page_once(pdf, number, private, config)
    # A bounded second pass applies only to an unreadable page. PSM1 detects
    # rotated text; PSM6 helps uniform pages. No model or threshold relaxation.
    variants = [(config, "3"), *[({**config, "ocr_dpi": 300, "ocr_max_dimension": 4000}, psm)
                                for psm in ("3", "1", "6")]]
    results, attempts = [], []
    for options, psm in variants:
        result = _ocr_page_once(pdf, number, private, options, psm=psm)
        results.append(result)
        attempts.append({"dpi": options["ocr_dpi"], "maximum_dimension": options["ocr_max_dimension"],
                         "psm": psm, "word_count": result.get("word_count", 0),
                         "mean_confidence": result.get("mean_confidence"),
                         "confident_word_ratio": result.get("confident_word_ratio", 0),
                         "text_sha256": text_hash(result.get("text", "")),
                         "acceptable": _strong_ocr_text(result, config), "reasons": result.get("reasons", [])})
        if _strong_ocr_text(result, config):
            break
    def rank(value):
        return (bool(_strong_ocr_text(value, config)), value.get("confident_word_ratio", 0),
                value.get("mean_confidence") or 0, value.get("word_count", 0))
    selected = deepcopy(max(results, key=rank))
    selected["ocr_attempts"] = attempts
    selected["cleaned_render_bytes"] = sum(r.get("cleaned_render_bytes", 0) for r in results)
    return selected


def _cached_ocr_page(pdf, number, private, config, identity, *, source_folders=()):
    """V2 resumable page result, including known low-quality/empty OCR results.

    Cache results do not buy certainty: the quality thresholds and all ambiguous
    layout warnings are reassessed by the corpus builder. Errors/timeouts are not
    forged into successful OCR text. A changed PDF/config/tool/source mapping
    cannot reuse the earlier page's text.
    """
    source_sha, source_bytes = _file_sha(pdf, 300 * 1024 * 1024)
    if (source_sha, source_bytes) != (identity["source_binding"]["analysis_sha256"],
                                    identity["source_binding"]["analysis_bytes"]):
        raise CorpusError("Prepared PDF changed before cached page OCR")
    key = {"version": "prod2_local_page_ocr_cache_v1", "global_page": number,
           "source_binding": identity["source_binding"], "config": config,
           "versions": identity["versions"]}
    path = private / f"ocr-page-{number:04d}.private.json"
    if path.exists() or path.is_symlink():
        _file_sha(path, 16 * 1024 * 1024)
        try:
            pinned = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise CorpusError("Cached page OCR has invalid JSON integrity") from None
        if (not isinstance(pinned, dict) or set(pinned) != {"identity", "identity_hash", "result", "cache_hash"}
                or pinned["identity"] != key or pinned["identity_hash"] != canonical_hash(key)
                or pinned["cache_hash"] != canonical_hash({k: v for k, v in pinned.items() if k != "cache_hash"})
                or not isinstance(pinned["result"], dict) or not isinstance(pinned["result"].get("text"), str)):
            raise CorpusError("Cached page OCR differs from its exact identity or result")
        result = deepcopy(pinned["result"])
    else:
        from .embedding_ocr_recovery import adopt_page_ocr
        adopted = adopt_page_ocr(source_folders, source_binding=identity["source_binding"],
            config=config, versions=identity["versions"], number=number) if source_folders else None
        if adopted is not None:
            result, proof = adopted
            result["ocr_adoption_proof"] = proof
            result["cleaned_render_bytes"] = 0
        else:
            result = _ocr_page(pdf, number, private, config)
        after = _file_sha(pdf, 300 * 1024 * 1024)
        if after != (source_sha, source_bytes):
            raise CorpusError("Prepared PDF changed while reading page OCR")
        pinned = {"identity": key, "identity_hash": canonical_hash(key), "result": result}
        pinned["cache_hash"] = canonical_hash(pinned)
        _write_private(path, pinned, 16 * 1024 * 1024)
    return result, canonical_hash(pinned)


def _strong_ocr_text(result, config):
    if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
        return False
    mean, ratio = result.get("mean_confidence"), result.get("confident_word_ratio")
    return (result.get("acceptable") is True and not result.get("reasons")
        and not _bad_unicode(result["text"])
        and not isinstance(mean, bool) and isinstance(mean, (int, float)) and math.isfinite(mean)
        and config["minimum_ocr_confidence"] <= mean <= 100
        and not isinstance(ratio, bool) and isinstance(ratio, (int, float)) and math.isfinite(ratio)
        and config["minimum_ocr_confident_word_ratio"] <= ratio <= 1)


def _split_tokens(text, target, overlap):
    """Exact Unicode character slices; token slices can split a UTF-8 character."""
    encoding = tokenizer()
    encoded = encoding.encode(text, disallowed_special=())
    if len(encoded) <= target:
        return [(text, 0, len(text))]
    _, starts = encoding.decode_with_offsets(encoded)
    offsets = [*starts, len(text)]
    result, start = [], 0
    while start < len(encoded):
        end = min(len(encoded), start + target)
        first, last = offsets[start], offsets[end]
        while last <= first and end < len(encoded):
            end += 1
            last = offsets[end]
        piece = text[first:last]
        # Fragment re-encoding occasionally increases token count at boundaries.
        while len(encoding.encode(piece, disallowed_special=())) > target and end > start + 1:
            end -= 1
            last = offsets[end]
            piece = text[first:last]
        if not piece or last <= first:
            raise CorpusError("Tokenizer cannot form an exact bounded character fragment")
        result.append((piece, first, last))
        if end >= len(encoded):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and offsets[next_start] <= first:
            next_start += 1
        start = next_start
    return result


def _heading(text):
    stripped = text.strip()
    if not stripped or len(stripped) > 180:
        return False
    alphabetic = [char for char in stripped if char.isalpha()]
    upper = alphabetic and sum(char.isupper() for char in alphabetic) / len(alphabetic) >= .85
    return bool(upper or re.match(r"^(?:\d+(?:\.\d+)*[.)]?|[A-Z][.)])\s+\S", stripped))


def _paragraphs(text):
    # Split only empty lines, keeping table/column whitespace and line order.
    return [part.strip("\n") for part in re.split(r"\n\s*\n", text) if part.strip()]


def _block(text, number, locations, section, *, kind="paragraph", table=None, ordinal=0):
    primary = next((item for item in locations if not item.get("duplicate_of")), locations[0])
    value = {"text": text, "text_sha256": text_hash(text), "global_page": number,
        "source_page": primary["source_page"], "document_uuid": primary["document_uuid"],
        "candidate_id": primary["candidate_id"], "source_sha256": primary["original_sha256"],
        "source_analysis_sha256": primary["analysis_sha256"], "section_id": section,
        "kind": kind, "table_id": table, "ordinal": ordinal, "locations": [
            {"candidate_id": item["candidate_id"], "document_uuid": item["document_uuid"],
             "source_page": item["source_page"], "source_sha256": item["original_sha256"],
             "source_analysis_sha256": item["analysis_sha256"], "duplicate_of": item.get("duplicate_of"),
             "location_kind": "prepared_pdf_page"} for item in locations]}
    value["id"] = canonical_hash(value)
    return value


def _make_chunks(blocks, config):
    """Chunks are embedding search units; complete blocks remain retrieval evidence."""
    result = []
    target, overlap = config["chunk_target_tokens"], config["overlap_tokens"]
    # Pack paragraphs within a section before applying token overlap. Native
    # table blocks stand alone, keeping all cell/header provenance. Retrieval
    # expands embedding slices back into their whole source blocks.
    groups = []
    for block in blocks:
        if (not groups or block["kind"] == "table" or groups[-1][0]["kind"] == "table"
                or (block["candidate_id"], block["section_id"]) != (
                    groups[-1][0]["candidate_id"], groups[-1][0]["section_id"])):
            groups.append([block])
        else:
            groups[-1].append(block)
    for group_index, group in enumerate(groups):
        text, ranges = "", []
        for block in group:
            if text:
                text += "\n\n"
            first = len(text)
            text += block["text"]
            ranges.append((block, first, len(text)))
        for ordinal, (piece, first, last) in enumerate(_split_tokens(text, target, overlap)):
            # Empty table cells and token boundaries can produce whitespace-only
            # slices. Full blocks retain it; an embedding search unit needs text.
            if not piece.strip():
                continue
            touched = [(block, start, end) for block, start, end in ranges if start < last and end > first]
            if not touched:
                raise CorpusError("Embedding fragment lacks a source block")
            value = {"text": piece, "text_sha256": text_hash(piece), "token_count": token_count(piece),
                "block_ids": [block["id"] for block, _, _ in touched],
                "global_pages": sorted({block["global_page"] for block, _, _ in touched}),
                "section_id": group[0]["section_id"], "table_id": group[0]["table_id"],
                "block_ranges": [{"block_id": block["id"], "start_char": max(0, first - start),
                                  "end_char": min(end - start, last - start)}
                                 for block, start, end in touched],
                "group_ordinal": group_index, "ordinal": ordinal}
            if value["token_count"] > config["maximum_input_tokens"]:
                raise CorpusError("Embedding chunk exceeds the tokenizer bound")
            value["chunk_id"] = canonical_hash(value)
            result.append(value)
    return result


def validate_corpus(corpus):
    """Validate local/PG replay before any payment or retrieval."""
    if not isinstance(corpus, dict) or corpus.get("policy_version") != CORPUS_POLICY:
        raise CorpusError("Unsupported complete corpus")
    body = {key: value for key, value in corpus.items() if key not in {"corpus_hash", "cache_hit"}}
    if corpus.get("corpus_hash") != canonical_hash(body):
        raise CorpusError("Corpus content no longer matches its hash")
    config = _config(corpus.get("config"))
    if (corpus.get("pdf_sha256_before") != corpus.get("analysis_sha256")
            or corpus.get("pdf_sha256_after") != corpus.get("analysis_sha256")
            or not _SHA.fullmatch(corpus.get("analysis_sha256", ""))
            or corpus.get("source_manifest_hash") != canonical_hash(corpus.get("source_document_manifest"))):
        raise CorpusError("Corpus source hashes are inconsistent")
    count = corpus.get("page_count")
    if type(count) is not int or not 0 < count <= config["max_pages"]:
        raise CorpusError("Corpus page count is invalid")
    binding = corpus.get("identity", {}).get("source_binding", {})
    if (binding != {"analysis_sha256": corpus["analysis_sha256"],
                   "analysis_bytes": corpus["analysis_bytes"], "page_count": count,
                   "source_document_manifest": corpus["source_document_manifest"]}
            or corpus.get("identity", {}).get("config") != config):
        raise CorpusError("Corpus identity differs from its complete source binding")
    expected_locations, expected_mappings, primaries = {}, {page: [] for page in range(1, count + 1)}, {}
    identifiers = set()
    by_candidate = {}
    for row in corpus["source_document_manifest"]:
        start, end, offset = (row.get(key) for key in ("start_page", "end_page", "page_offset"))
        candidate = row.get("candidate_id")
        if (any(type(number) is not int for number in (start, end, offset))
                or not 1 <= start <= end <= count or offset != start - 1
                or not isinstance(candidate, str) or not candidate or candidate in identifiers
                or not _SHA.fullmatch(row.get("original_sha256", ""))
                or not _SHA.fullmatch(row.get("analysis_sha256", ""))):
            raise CorpusError("Corpus source ranges or revision hashes are invalid")
        identifiers.add(candidate)
        by_candidate[candidate] = row
        for page in range(start, end + 1):
            if not row.get("duplicate_of"):
                if page in primaries:
                    raise CorpusError("Corpus primary source page ranges overlap")
                primaries[page] = row
            expected_mappings[page].append({"candidate_id": candidate, "document_uuid": row.get("document_uuid"),
                "source_page": page - offset, "original_sha256": row["original_sha256"],
                "analysis_sha256": row["analysis_sha256"], "duplicate_of": row.get("duplicate_of")})
    if set(primaries) != set(expected_mappings):
        raise CorpusError("Corpus source mapping does not cover every page")
    for row in corpus["source_document_manifest"]:
        if row.get("duplicate_of"):
            parent = by_candidate.get(row["duplicate_of"])
            if (not parent or parent.get("duplicate_of") or any(row[key] != parent[key] for key in (
                    "start_page", "end_page", "page_offset", "analysis_sha256"))):
                raise CorpusError("Corpus duplicate mapping differs from its primary PDF")
    for page, mappings in expected_mappings.items():
        expected_locations[page] = [{"candidate_id": row["candidate_id"], "document_uuid": row["document_uuid"],
            "source_page": row["source_page"], "source_sha256": row["original_sha256"],
            "source_analysis_sha256": row["analysis_sha256"], "duplicate_of": row.get("duplicate_of"),
            "location_kind": "prepared_pdf_page"} for row in mappings]
    if [row.get("global_page") for row in corpus.get("pages", [])] != list(range(1, count + 1)):
        raise CorpusError("Corpus diagnostic page coverage is incomplete")
    if any(row.get("source_mappings") != expected_mappings[row["global_page"]] for row in corpus["pages"]):
        raise CorpusError("Corpus page diagnostics differ from official source provenance")
    unresolved = [row["global_page"] for row in corpus["pages"] if not row.get("acceptable")]
    if corpus.get("unresolved_pages") != unresolved or corpus.get("coverage_complete") is not (not unresolved):
        raise CorpusError("Corpus completeness contradicts page diagnostics")
    if config["ocr_textual_scan_policy"] in {
            "prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"}:
        if (corpus.get("text_coverage_complete") is not corpus["coverage_complete"]
                or corpus.get("claims_visual_complete") is not False
                or corpus.get("text_capture_policy") != config["ocr_textual_scan_policy"]
                or any(page.get("text_coverage_complete") is not page["acceptable"]
                       or page.get("claims_visual_complete") is not False for page in corpus["pages"])):
            raise CorpusError("Text capture coverage cannot claim verified visual relationships")
        if any(page.get("ocr_cache_file_sha256") is not None
               and not _SHA.fullmatch(page["ocr_cache_file_sha256"]) for page in corpus["pages"]):
            raise CorpusError("Page OCR cache hash is invalid")
    if config["ocr_textual_scan_policy"] == "prod2_confident_text_layout_review_v3":
        classification_counts = {
            "navigation_link_without_content": 0, "signature_widget_visual_review": 0,
            "form_widget_data_not_captured": 1, "form_widget_structure_not_verified": 1,
            "substantive_or_unknown_annotation_not_captured": 1}
        diagnostic_keys = {"subtype", "field_type", "classification", "has_contents",
            "has_appearance", "has_value", "has_default_value", "has_appearance_state"}
        for page in corpus["pages"]:
            review = page.get("annotation_review")
            if (not isinstance(review, dict) or set(review) != {
                    "policy_version", "annotations", "unresolved_count", "visual_warnings"}
                    or review["policy_version"] != "prod2_annotation_text_capture_v3"
                    or not isinstance(review["annotations"], list)
                    or type(review["unresolved_count"]) is not int
                    or not isinstance(review["visual_warnings"], list)):
                raise CorpusError("V3 annotation diagnostics are invalid")
            expected_unresolved = 0
            for item in review["annotations"]:
                if (not isinstance(item, dict) or set(item) != diagnostic_keys
                        or item["classification"] not in classification_counts
                        or any(type(item[key]) is not bool for key in diagnostic_keys if key.startswith("has_"))
                        or any(item[key] is not None and not isinstance(item[key], str)
                               for key in ("subtype", "field_type"))):
                    raise CorpusError("V3 annotation classification is invalid")
                expected_unresolved += classification_counts[item["classification"]]
            if (review["unresolved_count"] != expected_unresolved
                    or any(not isinstance(warning, str) or warning not in page["visual_warnings"]
                           for warning in review["visual_warnings"])
                    or (expected_unresolved and (page["acceptable"]
                        or "unrepresented_page_objects" not in page["reasons"]))
                    or (review["visual_warnings"] and not page["human_review_recommended"])):
                raise CorpusError("V3 annotation coverage contradicts its diagnostics")
    ids = set()
    for block in corpus.get("blocks", []):
        if (not isinstance(block, dict) or block.get("id") in ids
                or block.get("id") != canonical_hash({k: v for k, v in block.items() if k != "id"})
                or block.get("text_sha256") != text_hash(block["text"])
                or not 1 <= block["global_page"] <= count or not block.get("locations")):
            raise CorpusError("Corpus block integrity or provenance is invalid")
        if is_partial_corpus(config) and not corpus["pages"][block["global_page"] - 1]["acceptable"]:
            raise CorpusError("Partial corpus cannot index an omitted page")
        primary = primaries[block["global_page"]]
        if (block["locations"] != expected_locations[block["global_page"]]
                or block["candidate_id"] != primary["candidate_id"]
                or block["document_uuid"] != primary.get("document_uuid")
                or block["source_page"] != block["global_page"] - primary["page_offset"]
                or block["source_sha256"] != primary["original_sha256"]
                or block["source_analysis_sha256"] != primary["analysis_sha256"]):
            raise CorpusError("Corpus block source identity differs from official page mapping")
        ids.add(block["id"])
    if (corpus.get("source_tokens") != sum(token_count(block["text"]) for block in corpus["blocks"])
            or corpus.get("text_bytes") != sum(len(block["text"].encode("utf-8")) for block in corpus["blocks"])):
        raise CorpusError("Corpus token or byte counters differ from exact source text")
    chunks = corpus.get("chunks", [])
    if chunks != _make_chunks(corpus.get("blocks", []), config):
        raise CorpusError("Corpus chunks differ from exact complete blocks")
    return corpus


def _write_private(path, value, maximum):
    data = canonical_json(value).encode("utf-8")
    if len(data) > maximum:
        raise CorpusError("Complete corpus exceeds its bounded manifest size")
    descriptor, name = tempfile.mkstemp(prefix=".corpus-writing-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_corpus(prepared, folder, *, config=None, stop_requested=lambda: False, ocr_cache_sources=()):
    """Return every selected source page's text with explicit unresolved coverage.

    Only the verified analysis PDF is used. No preview, remote OCR, generation,
    embeddings or permanent source upload happens here. OCR-derived table relations
    and unrepresented figures remain reviewable, rather than triggering full-PDF LLM.
    """
    if not callable(stop_requested):
        raise CorpusError("Corpus stop callback must be callable")
    if stop_requested():
        raise CorpusStopped("Corpus stopped before local extraction")
    limits = _config(config)
    if ocr_cache_sources and limits["ocr_textual_scan_policy"] != "prod2_confident_text_layout_review_v3":
        raise CorpusError("OCR cache adoption requires a new V3 corpus")
    annotation_review = limits["ocr_textual_scan_policy"] == "prod2_confident_text_layout_review_v3"
    layout_review = limits["ocr_textual_scan_policy"] in {
        "prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"}
    native_limits = deepcopy(_NATIVE_POLICY)
    native_limits["max_pages"] = limits["max_pages"]
    pdf, binding, mapping = _source_binding(prepared, native_limits)
    versions = _versions(limits)
    identity = {"source_binding": binding, "config": limits, "versions": versions}
    base = _safe(folder)
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    private = _safe(base / ("embedding-corpus-" + canonical_hash(identity)))
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    marker = private / "corpus.private.json"
    with _file_lock(private / ".corpus.lock", 300):
        if marker.exists() or marker.is_symlink():
            _file_sha(marker, limits["max_manifest_bytes"])
            pinned = validate_corpus(json.loads(marker.read_bytes()))
            if (pinned["identity"] != identity
                    or _file_sha(pdf, 300 * 1024 * 1024) != (binding["analysis_sha256"], binding["analysis_bytes"])):
                raise CorpusError("Cached corpus no longer matches its exact source")
            return {**pinned, "cache_hit": True}
        extraction_error = None
        try:
            native = _extract_native(pdf, limits)
            if ([row.get("global_page") for row in native] != list(range(1, binding["page_count"] + 1))):
                raise CorpusError("Local extraction does not cover every prepared page")
        except (OSError, ValueError, TimeoutError) as error:
            # Do not surface paths/source text/exception bodies in telemetry.
            extraction_error = type(error).__name__
            native = [{"global_page": number, "text": "", "tables": [], "images": [],
                       "native_parse_failed": True} for number in range(1, binding["page_count"] + 1)]
        blocks, pages, cleaned, ocr_pages, total_text_bytes = [], [], 0, 0, 0
        sections = {}
        for row in native:
            if stop_requested():
                raise CorpusStopped("Corpus stopped at a page boundary")
            number = row["global_page"]
            locations = mapping[number]
            primary = next(item for item in locations if not item.get("duplicate_of"))
            candidate = primary["candidate_id"]
            section = sections.setdefault(candidate, canonical_hash({"candidate": candidate, "section": "opening"}))
            text, reasons = row["text"], []
            method, ocr_result, ocr_cache_sha = "native_layout", None, None
            visual_warnings = []
            if row.get("native_parse_failed"):
                reasons.append("native_extraction_failed")
            image_relevant = (row.get("image_area_ratio", 0) >= limits["relevant_image_area_ratio"]
                              or any(image.get("area_ratio", 0) >= .01
                                     and float((image.get("bbox") or [0, 0, 0, 0])[1] or 0) > row.get("height", 1) * .15
                                     for image in row.get("images", [])))
            native_bad = _bad_unicode(text) or row.get("native_unicode_corrupt")
            visible_without_text = (not text.strip() and (row.get("images") or row.get("vector_count")
                                                          or row.get("native_characters")))
            if not row.get("native_parse_failed") and (image_relevant or native_bad or visible_without_text):
                if ocr_pages >= limits["max_ocr_pages"]:
                    reasons.append("ocr_page_budget_exhausted")
                else:
                    ocr_pages += 1
                    try:
                        if layout_review:
                            options = {"source_folders": ocr_cache_sources} if ocr_cache_sources else {}
                            ocr_result, ocr_cache_sha = _cached_ocr_page(pdf, number, private, limits, identity, **options)
                            if stop_requested():
                                raise CorpusStopped("Corpus stopped after preserving page OCR")
                        else:
                            ocr_result = _ocr_page(pdf, number, private, limits)
                        cleaned += ocr_result.get("cleaned_render_bytes", 0)
                        reasons.extend(ocr_result.get("reasons", []))
                        if not ocr_result.get("acceptable") and not ocr_result.get("reasons"):
                            reasons.append("ocr_coverage_unresolved")
                        if layout_review and not _strong_ocr_text(ocr_result, limits):
                            reasons.append("ocr_text_quality_unresolved")
                        if ocr_result.get("text"):
                            if text.strip() and not native_bad:
                                text += "\n\n[OCR LOCAL: CONTENIDO VISUAL DE LA PAGINA]\n" + ocr_result["text"]
                                method = "native_plus_full_page_ocr"
                            else:
                                text, method = ocr_result["text"], "full_page_ocr"
                    except CorpusStopped:
                        raise
                    except CorpusError:
                        # Identity corruption is not a quality downgrade that a
                        # corpus may silently cache and carry into a later paid read.
                        if layout_review:
                            raise
                        reasons.append("local_ocr_corpuserror")
                    except (OSError, ValueError, TimeoutError) as error:
                        reasons.append("local_ocr_" + type(error).__name__.lower())
                # A confidently OCR'd full-page textual scan is usable text under
                # an explicit pilot policy, not a certified reading of diagrams.
                # Mixed small images, sparse OCR and raster multi-column tables
                # retain unresolved coverage rather than silently losing relations.
                if image_relevant:
                    largest_image = max((image.get("area_ratio", 0) for image in row.get("images", [])), default=0.)
                    textual_scan = (ocr_result is not None and ocr_result.get("acceptable") is True
                        and largest_image >= limits["textual_scan_minimum_image_area_ratio"]
                        and ocr_result.get("word_count", 0) >= limits["minimum_ocr_words"]
                        and ocr_result.get("column_gap_rows", 0) <= limits["maximum_ocr_column_gap_rows"])
                    if not textual_scan:
                        if layout_review:
                            visual_warnings.append("visual_relations_not_verified")
                        else:
                            reasons.append("visual_relations_not_verified")
                    if layout_review:
                        if largest_image < limits["textual_scan_minimum_image_area_ratio"]:
                            visual_warnings.append("mixed_image_region_structure_uncertain")
                        if ocr_result and ocr_result.get("word_count", 0) < limits["minimum_ocr_words"]:
                            visual_warnings.append("sparse_visual_ocr_text_structure_uncertain")
                        if ocr_result and ocr_result.get("column_gap_rows", 0) > limits["maximum_ocr_column_gap_rows"]:
                            visual_warnings.append("raster_table_or_columns_relations_uncertain")
            if annotation_review:
                visual_warnings.extend(row.get("annotation_review", {}).get("visual_warnings", []))
            if row.get("meaningful_annotations") or row.get("unknown_object_types"):
                reasons.append("unrepresented_page_objects")
            if (row.get("largest_vector_area_ratio", 0) >= .08
                    and not row.get("tables") and row.get("vector_count", 0) >= 20):
                if layout_review:
                    visual_warnings.append("unrepresented_vector_figure")
                else:
                    reasons.append("unrepresented_vector_figure")
            if native_bad and (ocr_result is None or not ocr_result.get("acceptable")):
                reasons.append("unicode_mapping_unresolved")
            extracted_chars = sum(not char.isspace() for char in row["text"])
            if row.get("native_characters", 0) and extracted_chars < row["native_characters"] * .9:
                reasons.append("native_character_coverage_mismatch")
            confirmed_empty = not text.strip() and not any(row.get(key) for key in (
                "native_parse_failed", "images", "vector_count", "native_characters",
                "meaningful_annotations", "unknown_object_types"))
            if not text.strip() and not confirmed_empty:
                reasons.append("empty_page_coverage_unresolved")
            paragraphs = _paragraphs(text)
            ordinal = 0
            for paragraph in paragraphs:
                lines = paragraph.splitlines()
                if lines and _heading(lines[0]):
                    section = canonical_hash({"candidate": candidate, "page": number,
                                              "heading": lines[0].strip()})
                    sections[candidate] = section
                block = _block(paragraph, number, locations, section, ordinal=ordinal,
                               kind="ocr_layout" if method == "full_page_ocr" else "paragraph")
                total_text_bytes += len(paragraph.encode("utf-8"))
                blocks.append(block)
                ordinal += 1
            for table in row.get("tables", []):
                table_text = "\n".join(" | ".join(cell.replace("\n", " / ") for cell in cells)
                                       for cells in table["rows"])
                identifier = canonical_hash({"candidate": candidate, "page": number,
                                             "table": table["ordinal"], "bbox": table["bbox"]})
                block = _block(table_text, number, locations, section, kind="table", table=identifier,
                               ordinal=ordinal)
                block["table_rows"] = table["rows"]
                block["table_bbox"] = table["bbox"]
                block["id"] = canonical_hash({k: v for k, v in block.items() if k != "id"})
                total_text_bytes += len(table_text.encode("utf-8"))
                blocks.append(block)
                ordinal += 1
            if total_text_bytes > limits["max_text_bytes"]:
                raise CorpusError("Complete corpus text exceeds its bounded size; nothing is silently truncated")
            page_record = {"global_page": number, "method": method, "acceptable": not reasons,
                "reasons": sorted(set(reasons)), "confirmed_empty": confirmed_empty,
                "text_sha256": text_hash(text), "text_bytes": len(text.encode("utf-8")),
                "native_characters": row.get("native_characters", 0),
                "image_count": len(row.get("images", [])), "image_area_ratio": row.get("image_area_ratio", 0),
                "table_count": len(row.get("tables", [])), "source_mappings": locations,
                "visual_warnings": sorted(set(visual_warnings + (["ocr_text_does_not_certify_figures_or_table_relations"] if ocr_result else []))),
                "human_review_recommended": ocr_result is not None or bool(visual_warnings),
                "ocr": {key: value for key, value in (ocr_result or {}).items()
                        if key not in {"text", "cleaned_render_bytes"}} or None}
            if layout_review:
                page_record.update(text_coverage_complete=not reasons, claims_visual_complete=False,
                                   ocr_cache_file_sha256=ocr_cache_sha)
            if annotation_review:
                page_record["annotation_review"] = row.get("annotation_review", {
                    "policy_version": "prod2_annotation_text_capture_v3", "annotations": [],
                    "unresolved_count": row.get("meaningful_annotations", 0), "visual_warnings": []})
            pages.append(page_record)
        after_sha, after_bytes = _file_sha(pdf, 300 * 1024 * 1024)
        if (after_sha, after_bytes) != (binding["analysis_sha256"], binding["analysis_bytes"]):
            raise CorpusError("Prepared PDF changed during complete corpus extraction")
        unresolved = [page["global_page"] for page in pages if not page["acceptable"]]
        if is_partial_corpus(limits):
            # Diagnostics and OCR receipts retain every page. Only confidently
            # captured pages become evidence or paid embedding inputs.
            blocks = [block for block in blocks if pages[block["global_page"] - 1]["acceptable"]]
            total_text_bytes = sum(len(block["text"].encode("utf-8")) for block in blocks)
        corpus = {"policy_version": CORPUS_POLICY, "identity": identity, "config": limits,
            "analysis_sha256": binding["analysis_sha256"], "analysis_bytes": binding["analysis_bytes"],
            "pdf_sha256_before": binding["analysis_sha256"], "pdf_sha256_after": after_sha,
            "source_document_manifest": binding["source_document_manifest"],
            "source_manifest_hash": canonical_hash(binding["source_document_manifest"]),
            "page_count": binding["page_count"], "coverage_complete": not unresolved,
            "unresolved_pages": unresolved, "native_extraction_error_type": extraction_error,
            "pages": pages, "blocks": blocks, "chunks": _make_chunks(blocks, limits),
            "source_tokens": sum(token_count(block["text"]) for block in blocks),
            "text_bytes": total_text_bytes, "ocr_page_count": ocr_pages,
            "cleaned_render_bytes": cleaned,
            "visual_warnings": sorted({warning for page in pages for warning in page["visual_warnings"]}),
            "human_review_recommended": any(page["human_review_recommended"] for page in pages),
            "authority_verified": False, "claims_exhaustive_legal_read": False}
        if layout_review:
            corpus.update(text_coverage_complete=not unresolved, claims_visual_complete=False,
                          text_capture_policy=limits["ocr_textual_scan_policy"])
        corpus["corpus_hash"] = canonical_hash(corpus)
        validate_corpus(corpus)
        _write_private(marker, corpus, limits["max_manifest_bytes"])
        return {**corpus, "cache_hit": False}
