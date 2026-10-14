"""Read-only adoption of verified page OCR across annotation-policy revisions.

The V2 receipt remains immutable. Only identical source bytes, page mappings,
OCR options and tool versions can reuse usable text. Known bad text is retried
locally; a historical receipt is never relabelled as a fresh provider result.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

from .local_text import _file_sha, _safe

_TSV_ROW = re.compile(r"(?:^|\n)\s*[1-5]\t(?:\d+\t){9}-?\d+(?:\.\d+)?\t")
_POLICIES = {"prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"}


def contains_raw_tsv_rows(text):
    return isinstance(text, str) and bool(_TSV_ROW.search(text))


def adopt_page_ocr(source_folders, *, source_binding, config, versions, number):
    """Return a good exact prior page plus provenance, or None without side effects."""
    from .embedding_corpus import CorpusError, canonical_hash, _config, _strong_ocr_text
    if config.get("ocr_textual_scan_policy") != "prod2_confident_text_layout_review_v3":
        raise CorpusError("OCR adoption requires its explicit V3 policy")
    if not isinstance(source_folders, (list, tuple)) or len(source_folders) > 10:
        raise CorpusError("OCR cache origins must be a bounded literal list")
    if type(number) is not int or not 1 <= number <= source_binding["page_count"]:
        raise CorpusError("OCR adoption page is outside its bound source")
    for origin in source_folders:
        folder = _safe(origin)
        path = folder / f"ocr-page-{number:04d}.private.json"
        if not path.exists():
            continue
        file_sha, _ = _file_sha(path, 16 * 1024 * 1024)
        try:
            pinned = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise CorpusError("Prior OCR page has invalid JSON integrity") from None
        if (not isinstance(pinned, dict)
                or set(pinned) != {"identity", "identity_hash", "result", "cache_hash"}
                or pinned["identity_hash"] != canonical_hash(pinned["identity"])
                or pinned["cache_hash"] != canonical_hash({k: v for k, v in pinned.items() if k != "cache_hash"})):
            raise CorpusError("Prior OCR page integrity changed")
        old = pinned["identity"]
        if (old.get("version") != "prod2_local_page_ocr_cache_v1"
                or old.get("global_page") != number
                or old.get("source_binding") != source_binding
                or old.get("versions") != versions):
            continue
        old_config = _config(old.get("config"))
        if old_config["ocr_textual_scan_policy"] not in _POLICIES:
            continue
        # The one-attempt policy changes failed-page handling, not pixel/engine
        # settings. A previously strong result remains usable with exact proof.
        from .partial_reading import is_partial_corpus
        ignored = {"ocr_textual_scan_policy"}
        if is_partial_corpus(config):
            ignored.add("page_failure_policy")
        options_old = {k: v for k, v in old_config.items() if k not in ignored}
        options_new = {k: v for k, v in config.items() if k not in ignored}
        if options_old != options_new:
            continue
        result = pinned["result"]
        if not _strong_ocr_text(result, config) or contains_raw_tsv_rows(result.get("text")):
            continue
        proof = {"policy_version": "prod2_page_ocr_adoption_v1",
                 "source_receipt_path": str(path), "source_receipt_sha256": file_sha,
                 "source_identity_hash": pinned["identity_hash"],
                 "source_cache_hash": pinned["cache_hash"],
                 "original_ocr_policy": old_config["ocr_textual_scan_policy"]}
        return deepcopy(result), proof
    return None
