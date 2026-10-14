"""Versioned partial local reading; omitted pages remain explicit evidence gaps."""
from copy import deepcopy

PAGE_FAILURE_POLICY = "prod2_skip_failed_ocr_pages_v5"
SOURCE_COVERAGE_POLICY = "prod2_partial_source_text_v5"
SOURCE_COVERAGE_POLICY_KEY = "llm1_source_coverage_policy"


def is_partial_corpus(config):
    return config.get("page_failure_policy") == PAGE_FAILURE_POLICY


def source_capture(corpus):
    return {
        "policy_version": SOURCE_COVERAGE_POLICY,
        "total_pages": corpus["page_count"],
        "captured_pages": [p["global_page"] for p in corpus["pages"] if p["acceptable"]],
        "omitted_pages": [{"global_page": p["global_page"], "reasons": deepcopy(p["reasons"]),
            "source_pages": [{k: m[k] for k in ("candidate_id", "document_uuid", "source_page")}
                             for m in p["source_mappings"]]}
            for p in corpus["pages"] if not p["acceptable"]],
        "text_coverage_complete": corpus["coverage_complete"],
    }


def validate_source_capture(value, page_count, manifest, indexed_complete):
    if (not isinstance(value, dict) or set(value) != {"policy_version", "total_pages",
            "captured_pages", "omitted_pages", "text_coverage_complete"}
            or value["policy_version"] != SOURCE_COVERAGE_POLICY
            or type(value["total_pages"]) is not int or value["total_pages"] != page_count
            or type(value["text_coverage_complete"]) is not bool
            or value["text_coverage_complete"] is not indexed_complete
            or not isinstance(value["captured_pages"], list)
            or not isinstance(value["omitted_pages"], list)):
        raise ValueError("Partial source capture identity is invalid")
    captured, omitted = value["captured_pages"], value["omitted_pages"]
    if (any(type(p) is not int or not 1 <= p <= page_count for p in captured)
            or captured != sorted(set(captured))):
        raise ValueError("Partial captured page list is invalid")
    omitted_numbers = []
    for row in omitted:
        if (not isinstance(row, dict) or set(row) != {"global_page", "reasons", "source_pages"}
                or type(row["global_page"]) is not int or not 1 <= row["global_page"] <= page_count
                or not isinstance(row["reasons"], list) or not row["reasons"]
                or any(not isinstance(r, str) or not r or len(r) > 128 for r in row["reasons"])):
            raise ValueError("Partial omitted page diagnostics are invalid")
        page = row["global_page"]
        expected = [{"candidate_id": m["candidate_id"], "document_uuid": m.get("document_uuid"),
            "source_page": page - m["start_page"] + 1} for m in manifest
            if m["start_page"] <= page <= m["end_page"]]
        if not expected or row["source_pages"] != expected:
            raise ValueError("Partial omitted page provenance changed")
        omitted_numbers.append(page)
    if (omitted_numbers != sorted(set(omitted_numbers))
            or set(captured) & set(omitted_numbers)
            or sorted(captured + omitted_numbers) != list(range(1, page_count + 1))
            or indexed_complete is not (not omitted)):
        raise ValueError("Partial source capture contradicts page coverage")
    return value


def compact_source_capture(value):
    numbers = [p["global_page"] for p in value["omitted_pages"]]
    ranges = []
    for page in numbers:
        if ranges and ranges[-1][1] + 1 == page:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return {"policy_version": SOURCE_COVERAGE_POLICY, "total_pages": value["total_pages"],
        "captured_page_count": len(value["captured_pages"]), "omitted_page_count": len(numbers),
        "omitted_global_page_ranges": ranges,
        "omission_reasons": sorted({r for p in value["omitted_pages"] for r in p["reasons"]}),
        "text_coverage_complete": value["text_coverage_complete"],
        "absence_in_partial_text_means_unknown": True}
