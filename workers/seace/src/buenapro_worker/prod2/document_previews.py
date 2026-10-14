"""Bounded local content samples for document triage, never eligibility evidence.

Original bytes are immutable. Extraction runs in a time-limited child process;
the private cache binds both bytes and sampling policy. No network/model calls.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from xml.etree import ElementTree
import zipfile

from buenapro_worker.local_document_conversion import bounded_command, prepare_pdf, zip_members
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2.document_selection import _text


PREVIEW_POLICY = "prod2_local_document_sample_v2"
ANCHORED_PREVIEW_POLICY = "prod2_local_document_anchor_sample_v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_PAGES = 1000
FULL_XML_PARSE_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_DEPTH = 256
MAX_XML_PARAGRAPHS = 200000
MAX_NATIVE_SCAN_PAGES = 40
PROCESS_TIMEOUT_SECONDS = 150
_HEADINGS = re.compile(
    r"t[eé]rminos\s+de\s+referencia|especificaciones\s+t[eé]cnicas|"
    r"requisitos\s+de\s+calificaci[oó]n|requerimiento|subcontrataci[oó]n|"
    r"experiencia\s+del\s+postor|objeto\s+de\s+la\s+contrataci[oó]n|"
    r"perfil\s+del\s+(?:personal|consultor)|alcance\s+del\s+servicio", re.I)


_PREVIEW_REASONS = {
    "Word preview contains unsupported active content": "word_active_content",
    "Word preview XML exceeds size policy": "word_xml_size_limit",
    "Word preview contains unsafe XML": "word_unsafe_xml",
    "Word preview has no document body": "word_body_missing",
    "Word preview XML is malformed": "word_malformed_xml",
    "Word preview XML exceeds structure policy": "word_xml_structure_limit",
    "Encrypted documents cannot be previewed": "pdf_encrypted",
    "Document preview exceeds the page policy": "pdf_page_limit",
    "A required local document preview tool is unavailable": "preview_tool_missing",
    "Spanish OCR language is unavailable": "ocr_language_missing",
    "Document preview image exceeds size policy": "preview_image_size_limit",
}


class DocumentPreviewError(ValueError):
    """Sanitized local preview failure; caller retains original and source URL."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code or _PREVIEW_REASONS.get(message)


def _anchor_options(source_sha, pages):
    if source_sha is None and pages is None:
        return None, None
    if (not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha)
            or not isinstance(pages, list) or not 1 <= len(pages) <= 3
            or any(type(number) is not int or not 1 <= number <= MAX_PAGES for number in pages)
            or pages != sorted(set(pages))):
        raise DocumentPreviewError("Explicit preview anchors require a source SHA and one to three ordered pages")
    return source_sha.lower(), list(pages)


def parse_preview_anchor_specs(values):
    """Parse an explicit, bounded SHA:page[,page] CLI mapping without effects."""
    if not isinstance(values, list) or len(values) > 10:
        raise DocumentPreviewError("Preview anchors are bounded to ten source SHA values")
    mapping = {}
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}:[1-9][0-9]*(?:,[1-9][0-9]*){0,2}", value):
            raise DocumentPreviewError("Use preview anchor SHA:page[,page] with up to three pages")
        source_sha, raw_pages = value.split(":")
        source_sha, pages = _anchor_options(source_sha, [int(number) for number in raw_pages.split(",")])
        if source_sha in mapping:
            raise DocumentPreviewError("Preview anchor source SHA values must not repeat")
        mapping[source_sha] = pages
    return dict(sorted(mapping.items()))


def preview_anchor_mapping(manifest):
    """Validate frozen policy before source/model effects, preserving legacy absence."""
    if "document_preview_anchors" not in manifest:
        return {}
    value = manifest["document_preview_anchors"]
    if (not isinstance(value, dict) or set(value) != {"policy_version", "pages_by_source_sha256"}
            or value.get("policy_version") != ANCHORED_PREVIEW_POLICY
            or not isinstance(value.get("pages_by_source_sha256"), dict)
            or not 1 <= len(value["pages_by_source_sha256"]) <= 10
            or manifest.get("document_previews") is not True
            or manifest.get("selector_policy_version") != "prod2_jev_document_selection_v3"):
        raise DocumentPreviewError("Invalid frozen preview anchor policy or selector")
    mapping = {}
    for sha, pages in value["pages_by_source_sha256"].items():
        normalized, canonical = _anchor_options(sha, pages)
        if normalized != sha:
            raise DocumentPreviewError("Frozen preview anchor SHA values must be canonical")
        mapping[sha] = canonical
    return dict(sorted(mapping.items()))


def _bounded_anchor_sample(parts, maximum):
    """Attribute each bounded excerpt to its explicit global source page."""
    selected, numbers = [], []
    for position, (number, value) in enumerate(parts):
        remaining = maximum - sum(len(item) + 1 for item in selected)
        share = remaining // (len(parts) - position)
        label = f"[Pagina global {number}]\n"
        excerpt = _clean(value, max(0, share - len(label)))
        if excerpt:
            selected.append(label + excerpt)
            numbers.append(number)
    return _clean("\n".join(selected), maximum), numbers


def _safe_path(path: Path, *, source: bool = False) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts:
        raise DocumentPreviewError("Document preview path is unsafe")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise DocumentPreviewError("Document preview paths cannot contain symlinks")
    if source and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
        raise DocumentPreviewError("Document preview source must be a regular file")
    return path


def _source_sha(path: Path) -> str:
    _safe_path(path, source=True)
    if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
        raise DocumentPreviewError("Document preview source exceeds the byte policy")
    result, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise DocumentPreviewError("Document preview source exceeds the byte policy")
            result.update(chunk)
    return result.hexdigest()


def _clean(value: str, maximum: int) -> str:
    value = "".join(c for c in value if ord(c) >= 32 or c in "\n\r\t")
    # Redact before truncation, so a partially cut credential is never exposed.
    return _text(value, limit=len(value))[:maximum]


def _bounded_sample(parts: list[tuple[int | None, str]], maximum: int) -> tuple[str, list[int]]:
    """Reserve space for later requirements headings as well as opening pages."""
    initial, relevant = parts[:3], []
    for position, (number, text) in enumerate(parts):
        match = _HEADINGS.search(text)
        if match:
            relevant.append((position, number, text[max(0, match.start() - 100):match.start() + 1600]))
    # Opening headings must not consume all space reserved for later requirements.
    relevant.sort(key=lambda row: row[0] < 3)
    relevant = relevant[:3]
    initial_budget = int(maximum * .6) if relevant else maximum
    selected, pages = [], []
    remaining = initial_budget
    for number, text in initial:
        if remaining <= 0:
            break
        excerpt = _clean(text, min(remaining, max(200, initial_budget // len(initial))))
        if excerpt:
            selected.append(excerpt)
            remaining -= len(excerpt) + 1
            if number is not None:
                pages.append(number)
    remaining = maximum - sum(len(item) + 1 for item in selected)
    for position, (_, number, text) in enumerate(relevant):
        if remaining <= 0:
            break
        excerpt = _clean(text, remaining // (len(relevant) - position))
        if excerpt and excerpt not in selected:
            selected.append(excerpt)
            remaining -= len(excerpt) + 1
            if number is not None and number not in pages:
                pages.append(number)
    return _clean("\n".join(selected), maximum), pages


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path and Path("/opt/homebrew/bin", name).is_file():
        path = str(Path("/opt/homebrew/bin", name))
    if not path:
        raise DocumentPreviewError("A required local document preview tool is unavailable")
    return path


def _ocr(path: Path, scratch: Path, count: int | list[int]) -> list[tuple[int, str]]:
    renderer, tesseract = _tool("pdftoppm"), _tool("tesseract")
    languages = bounded_command([tesseract, "--list-langs"], limit=32000, timeout=10).decode()
    available = set(languages.splitlines())
    if "spa" not in available:
        raise DocumentPreviewError("Spanish OCR language is unavailable")
    language = "spa+eng" if "eng" in available else "spa"
    parts = []
    numbers = range(1, count + 1) if type(count) is int else count
    for number in numbers:
        prefix = scratch / f"ocr-{number}"
        bounded_command([renderer, "-f", str(number), "-l", str(number), "-singlefile",
                         "-scale-to", "1800", "-png", str(path), str(prefix)],
                        limit=1000, timeout=30)
        image = prefix.with_suffix(".png")
        _safe_path(image, source=True)
        if image.stat().st_size > 32 * 1024 * 1024:
            raise DocumentPreviewError("Document preview image exceeds size policy")
        os.chmod(image, 0o600)
        text = bounded_command([tesseract, str(image), "stdout", "-l", language, "--psm", "3"],
                               limit=200000, timeout=30).decode("utf-8", errors="replace")
        parts.append((number, text))
        image.unlink()
    return parts


def _pdf_sample(path: Path, scratch: Path, maximum: int, ocr_pages: int, anchor_pages=None) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise DocumentPreviewError("Encrypted documents cannot be previewed")
    count = len(reader.pages)
    if not 0 < count <= MAX_PAGES:
        raise DocumentPreviewError("Document preview exceeds the page policy")
    if anchor_pages is not None:
        _, anchor_pages = _anchor_options("0" * 64, anchor_pages)
        if any(number > count for number in anchor_pages):
            raise DocumentPreviewError("Explicit preview anchor is outside the original PDF")
        parts = [(number, (reader.pages[number - 1].extract_text() or "")[:100000]) for number in anchor_pages]
        scanned = [number for number, text in parts if len(re.sub(r"\W", "", text)) < 80]
        if scanned and ocr_pages:
            if len(scanned) > ocr_pages:
                raise DocumentPreviewError("Explicit preview anchors exceed the requested OCR budget")
            replacements = dict(_ocr(path, scratch, scanned))
            parts = [(number, replacements.get(number, text)) for number, text in parts]
            method = "OCR"
        else:
            method = "native_text"
        text, pages = _bounded_anchor_sample(parts, maximum)
        return {"preview_text": text, "page_count": count, "preview_pages": pages, "method": method}
    # Inspect at most 40 pages: opening 32 plus distributed later samples.
    indexes = list(range(min(count, 32)))
    if count > 32:
        indexes += sorted({32 + (count - 33) * number // 7 for number in range(8)})
    parts = [(number + 1, (reader.pages[number].extract_text() or "")[:100000])
             for number in indexes[:MAX_NATIVE_SCAN_PAGES]]
    opening = [text for number, text in parts if number <= 3]
    if any(len(re.sub(r"\W", "", text)) < 80 for text in opening) and ocr_pages:
        # OCR only opening pages; do not claim the rest of a scanned PDF was read.
        ocr_parts = _ocr(path, scratch, min(count, ocr_pages))
        parts = [*ocr_parts, *(row for row in parts if row[0] > min(count, ocr_pages))]
        method = "OCR"
    else:
        method = "native_text"
    text, pages = _bounded_sample(parts, maximum)
    return {"preview_text": text, "page_count": count, "preview_pages": pages, "method": method}


def _check_word_xml(archive, member):
    """Reject active XML before parsing, including declarations across chunks."""
    total, tail = 0, b""
    with archive.open(member) as source:
        while chunk := source.read(65536):
            total += len(chunk)
            if total > MAX_XML_BYTES:
                raise DocumentPreviewError("Word preview XML exceeds size policy")
            # UTF-16/32 ASCII declarations contain NULs. Never let a differently
            # encoded DTD bypass the same guard used for ordinary UTF-8 XML.
            inspected = (tail + chunk).replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in inspected or b"<!ENTITY" in inspected:
                raise DocumentPreviewError("Word preview contains unsafe XML")
            tail = (tail + chunk)[-128:]


def _stream_word_body(archive, member):
    """Read larger bodies without retaining their complete XML element tree."""
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    stack, paragraphs, paragraph_positions = [], [], []
    body_depth, paragraph_depth, characters, found_body = None, 0, 0, False
    with archive.open(member) as source:
        for event, element in ElementTree.iterparse(source, events=("start", "end")):
            if event == "start":
                stack.append(element)
                if len(stack) > MAX_XML_DEPTH:
                    raise DocumentPreviewError("Word preview XML exceeds structure policy")
                if element.tag == namespace + "body" and len(stack) == 2 and not found_body:
                    body_depth, found_body = len(stack), True
                if body_depth is not None and element.tag == namespace + "p":
                    if len(paragraphs) >= MAX_XML_PARAGRAPHS:
                        raise DocumentPreviewError("Word preview XML exceeds structure policy")
                    # Word textboxes can nest paragraphs. Reserve their preorder
                    # positions rather than rearranging them by closing-tag order.
                    paragraph_positions.append(len(paragraphs))
                    paragraphs.append(None)
                    paragraph_depth += 1
                continue
            if body_depth is not None and element.tag == namespace + "p":
                text = " ".join(element.itertext())
                characters += len(text) + 1
                if characters > MAX_XML_BYTES:
                    raise DocumentPreviewError("Word preview XML exceeds structure policy")
                paragraphs[paragraph_positions.pop()] = text
                paragraph_depth -= 1
            if body_depth == len(stack):
                body_depth = None
            # Children inside a paragraph remain until its itertext is consumed.
            # Detach completed elements so millions of empty nodes cannot pile up.
            if not paragraph_depth:
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()
    if not found_body:
        raise DocumentPreviewError("Word preview has no document body")
    return "\n".join(paragraphs)


def _docx_sample(path: Path, maximum: int) -> dict:
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive, MAX_SOURCE_BYTES)
        names = [item.filename for item in members]
        if (names.count("word/document.xml") != 1
                or sum(item.file_size for item in members) > MAX_SOURCE_BYTES
                or any("vba" in name.casefold() or "/embeddings/" in name.casefold() for name in names)):
            raise DocumentPreviewError("Word preview contains unsupported active content")
        member = archive.getinfo("word/document.xml")
        if member.file_size > MAX_XML_BYTES:
            raise DocumentPreviewError("Word preview XML exceeds size policy")
        _check_word_xml(archive, member)
        try:
            if member.file_size <= FULL_XML_PARSE_BYTES:
                # Preserve the exact sampling/cache contract for previously
                # accepted files; only formerly oversized XML uses streaming.
                root = ElementTree.fromstring(archive.read(member))
                body = root.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")
                if body is None:
                    raise DocumentPreviewError("Word preview has no document body")
                paragraphs = [" ".join(node.itertext()) for node in body.findall(
                    ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")]
                joined = "\n".join(paragraphs)
            else:
                joined = _stream_word_body(archive, member)
        except ElementTree.ParseError:
            raise DocumentPreviewError("Word preview XML is malformed") from None
    # Text-only XML deliberately does not resolve relationships or embedded images.
    text, _ = _bounded_sample([(None, joined)], maximum)
    return {"preview_text": text, "page_count": None, "preview_pages": [], "method": "word_text"}


def _extract(path: Path, scratch: Path, maximum: int, ocr_pages: int, anchor_pages=None) -> dict:
    _safe_path(path, source=True)
    if anchor_pages is not None and path.suffix.lower() != ".pdf":
        raise DocumentPreviewError("Explicit preview anchors only support original PDFs")
    if path.suffix.lower() == ".docx":
        return _docx_sample(path, maximum)
    if path.suffix.lower() == ".doc":
        # Conversion writes only inside disposable private scratch.
        copy = scratch / "source.doc"
        shutil.copyfile(path, copy)
        os.chmod(copy, 0o600)
        result = _pdf_sample(prepare_pdf(copy, "doc", max_bytes=MAX_SOURCE_BYTES),
                             scratch, maximum, ocr_pages)
        if result["method"] == "native_text":
            result["method"] = "word_text"
        return result
    return _pdf_sample(path, scratch, maximum, ocr_pages, anchor_pages)


def _preview_worker() -> None:
    """Internal child entry; never prints traceback or source text on failure."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (110, 120))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
        anchors = json.loads(sys.argv[5]) if len(sys.argv) > 5 else None
        result = _extract(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), anchors)
    except DocumentPreviewError as error:
        result = {"error": "Bounded local document preview failed"}
        if error.reason_code in _PREVIEW_REASONS.values():
            result["reason_code"] = error.reason_code
    except Exception:
        result = {"error": "Bounded local document preview failed"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def _extract_process(path: Path, scratch: Path, maximum: int, ocr_pages: int, anchor_pages=None) -> dict:
    package_root = str(Path(__file__).parents[2])
    code = ("import sys;sys.path.insert(0," + repr(package_root) + ");"
            "from buenapro_worker.prod2.document_previews import _preview_worker;_preview_worker()")
    process = None
    try:
        # A dedicated group lets timeout terminate conversion/OCR descendants too.
        arguments = [sys.executable, "-c", code, str(path), str(scratch), str(maximum), str(ocr_pages)]
        if anchor_pages is not None:
            arguments.append(json.dumps(anchor_pages, separators=(",", ":")))
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            ready, _, _ = select.select([process.stdout], [], [], min(remaining, 1))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), min(65536, 65537 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 65536:
                raise ValueError()
        if process.wait(timeout=max(.1, deadline - time.monotonic())) != 0:
            raise ValueError()
        result = json.loads(output)
    except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired):
        raise DocumentPreviewError("Bounded local document preview failed") from None
    finally:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if process.stdout:
                process.stdout.close()
    if not isinstance(result, dict) or "error" in result:
        reason = result.get("reason_code") if isinstance(result, dict) else None
        if reason not in _PREVIEW_REASONS.values():
            reason = None
        message = "Bounded local document preview failed" + (": " + reason if reason else "")
        raise DocumentPreviewError(message, reason_code=reason)
    return result


def _validate(result: dict, sha: str, maximum: int, ocr_pages: int, *, anchor_source_sha256=None, anchor_pages=None) -> dict:
    anchor_sha, anchors = _anchor_options(anchor_source_sha256, anchor_pages)
    policy = ANCHORED_PREVIEW_POLICY if anchors is not None else PREVIEW_POLICY
    if (not isinstance(result, dict) or result.get("source_sha256") != sha
            or result.get("preview_policy") != policy
            or result.get("max_characters") != maximum or result.get("max_ocr_pages") != ocr_pages
            or result.get("method") not in {"native_text", "OCR", "word_text"}
            or result.get("sample_only") is not True
            or not isinstance(result.get("preview_text"), str)
            or len(result["preview_text"]) > maximum
            or _clean(result["preview_text"], maximum) != result["preview_text"]
            or not isinstance(result.get("preview_pages"), list)
            or len(result["preview_pages"]) > MAX_NATIVE_SCAN_PAGES):
        raise DocumentPreviewError("Document preview cache contract mismatch")
    if anchors is not None:
        if (anchor_sha != sha or result.get("anchor_source_sha256") != sha
                or result.get("anchor_pages") != anchors or result.get("extension") != ".pdf"
                or type(result.get("page_count")) is not int
                or any(number > result["page_count"] for number in anchors)
                or any(number not in anchors for number in result["preview_pages"])
                or result["preview_pages"] != sorted(set(result["preview_pages"]))):
            raise DocumentPreviewError("Explicit preview anchor cache contract mismatch")
    elif "anchor_pages" in result or "anchor_source_sha256" in result:
        raise DocumentPreviewError("Legacy preview cannot inherit anchored sample fields")
    count = result.get("page_count")
    if count is not None and (type(count) is not int or not 0 < count <= MAX_PAGES):
        raise DocumentPreviewError("Document preview page count is invalid")
    if any(type(number) is not int or not count or not 1 <= number <= count
           for number in result["preview_pages"]):
        raise DocumentPreviewError("Document preview sampled pages are invalid")
    body = {key: value for key, value in result.items() if key != "content_hash"}
    if canonical_hash(body) != result.get("content_hash"):
        raise DocumentPreviewError("Document preview cache hash mismatch")
    return result


def document_preview(path: Path, workdir: Path, *, max_characters: int = 3500,
                     max_ocr_pages: int = 3, anchor_source_sha256=None, anchor_pages=None) -> dict:
    """Return a sanitized cached sample tied to exact original bytes and policy.

    PDF default: up to 40 native-text page samples and up to 3 opening OCR pages.
    Explicit anchors: one to three global PDF pages bound to its original SHA.
    DOCX: body text only; embedded images need a later complete document reader.
    DOC: isolated local PDF conversion. No preview proves documentary completeness.
    """
    if (type(max_characters) is not int or not 200 <= max_characters <= 4000
            or type(max_ocr_pages) is not int or not 0 <= max_ocr_pages <= 3):
        raise DocumentPreviewError("Document preview sampling options exceed policy")
    anchor_sha, anchors = _anchor_options(anchor_source_sha256, anchor_pages)
    path, folder = _safe_path(path, source=True), _safe_path(workdir)
    if path.suffix.lower() not in {".pdf", ".doc", ".docx"}:
        raise DocumentPreviewError("Document preview format is unsupported")
    sha = _source_sha(path)
    if anchors is not None and (path.suffix.lower() != ".pdf" or anchor_sha != sha):
        raise DocumentPreviewError("Explicit preview anchors do not bind the actual original PDF SHA")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not folder.is_dir():
        raise DocumentPreviewError("Document preview workdir must be a private directory")
    cache_dir = _safe_path(folder / "document-preview-cache")
    cache_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(cache_dir, 0o700)
    identity = {"source_sha256": sha, "preview_policy": ANCHORED_PREVIEW_POLICY if anchors is not None else PREVIEW_POLICY,
                "extension": path.suffix.lower(), "max_characters": max_characters,
                "max_ocr_pages": max_ocr_pages}
    options = {}
    if anchors is not None:
        identity.update(anchor_source_sha256=sha, anchor_pages=anchors)
        options = {"anchor_source_sha256": sha, "anchor_pages": anchors}
    cache_path = _safe_path(cache_dir / (canonical_hash(identity) + ".private.json"))
    if cache_path.exists():
        if not cache_path.is_file() or cache_path.stat().st_size > 65536:
            raise DocumentPreviewError("Document preview cache exceeds size policy")
        try:
            result = _validate(json.loads(cache_path.read_text(encoding="utf-8")),
                               sha, max_characters, max_ocr_pages, **options)
        except (ValueError, UnicodeError):
            raise DocumentPreviewError("Document preview cache verification failed") from None
        os.chmod(cache_path, 0o600)
    else:
        with tempfile.TemporaryDirectory(prefix="document-preview-", dir=cache_dir) as directory:
            scratch = Path(directory)
            os.chmod(scratch, 0o700)
            if anchors is None:
                result = _extract_process(path, scratch, max_characters, max_ocr_pages)
            else:
                result = _extract_process(path, scratch, max_characters, max_ocr_pages, anchors)
        result.update(identity, sample_only=True)
        result["content_hash"] = canonical_hash(result)
        _validate(result, sha, max_characters, max_ocr_pages, **options)
        if _source_sha(path) != sha:
            raise DocumentPreviewError("Document preview source changed during extraction")
        descriptor, temporary = tempfile.mkstemp(prefix="preview-", suffix=".private.json", dir=cache_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            os.replace(temporary, cache_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if _source_sha(path) != sha:
        raise DocumentPreviewError("Document preview source changed during extraction")
    return result
