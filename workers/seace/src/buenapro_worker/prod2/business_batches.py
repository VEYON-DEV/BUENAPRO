"""Versioned business routing; batch names never change paid semantic inputs."""
from __future__ import annotations

import re

from .batch_plan import configured_keywords, digest

POLICY = "prod2-business-batches-v1"


def select_business_batches(config: dict, inventory: dict, *, batch_ids: list[str] | None = None,
                            scope: str | None = None) -> dict:
    """Validate a complete partition, then union primary/related search signals.

    One keyword can describe several lines. Its primary owner is stable; an
    isolated batch also searches related signals. A combined run searches each
    exact keyword once, using the same joint commercial profile for every batch.
    """
    if config.get("policy") != POLICY or config.get("source_inventory_hash") != digest(inventory):
        raise ValueError("Business batch inventory is stale or has an unsupported policy")
    keywords = configured_keywords(inventory)
    expected = set(keywords)
    members = config.get("members")
    if (not isinstance(members, dict) or set(members) != {"veyon", "dinaut"}
            or any(not isinstance(value, str) or not value for value in members.values())
            or len(set(members.values())) != len(members)):
        raise ValueError("Business batch members must map to the two declared companies")
    member_codes = {value: key for key, value in members.items()}
    origins, declared = {}, {keyword: set() for keyword in keywords}
    for line in inventory["member_keyword_inventory"]:
        key = (member_codes.get(line["member"]), line["line"])
        if key[0] is None or key in origins or not line["keywords"]:
            raise ValueError("Invalid declared business line inventory")
        if any(keyword not in expected for keyword in line["keywords"]):
            raise ValueError("Declared business line contains an unconfigured keyword")
        origins[key] = set(line["keywords"])
        for keyword in line["keywords"]:
            declared[keyword].add(key[0])
    if any(not codes for codes in declared.values()):
        raise ValueError("Every configured keyword needs declared company provenance")

    batches = config.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ValueError("Business batches must be a nonempty ordered list")
    primary, associations, line_owners, known_ids = {}, {}, {}, set()
    for batch in batches:
        batch_id = batch.get("id")
        if (not isinstance(batch_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", batch_id)
                or batch_id in known_ids or batch.get("scope") not in {"digitales", "industriales"}
                or not isinstance(batch.get("label"), str) or not batch["label"]):
            raise ValueError("Invalid or duplicate business batch")
        known_ids.add(batch_id)
        owned = batch.get("primary_keywords")
        if not isinstance(owned, list) or not owned:
            raise ValueError("A business batch needs primary keywords")
        for keyword in owned:
            if keyword not in expected or keyword in primary:
                raise ValueError("Primary business keywords must form a unique configured partition")
            primary[keyword] = batch_id
        related = set(owned)
        source_lines = batch.get("source_lines")
        if not isinstance(source_lines, list) or not source_lines:
            raise ValueError("A business batch needs declared source lines")
        for line in source_lines:
            key = (line.get("member"), line.get("line"))
            if key not in origins or key in line_owners:
                raise ValueError("Business batch source line is missing or assigned twice")
            line_owners[key] = batch_id
            related.update(origins[key])
        associations[batch_id] = related
    if set(primary) != expected or set(line_owners) != set(origins):
        raise ValueError("Business batches must cover all configured keywords and declared lines")
    broad = config.get("broad_keywords", [])
    if (not isinstance(broad, list) or any(keyword not in expected for keyword in broad)
            or len(broad) != len(set(broad))):
        raise ValueError("Broad keyword flags must refer to unique configured keywords")
    if batch_ids and scope:
        raise ValueError("Choose business batch IDs or a business scope")
    if scope is not None and scope not in {"digitales", "industriales", "todas"}:
        raise ValueError("Unknown business scope")
    if batch_ids is not None and (not batch_ids or set(batch_ids) - known_ids):
        raise ValueError("Unknown business batch")
    selected = [batch for batch in batches if (batch_ids and batch["id"] in batch_ids)
                or (not batch_ids and (scope in {None, "todas"} or batch["scope"] == scope))]

    # Keep complete source variants; broad/ambiguous signals run last in each batch.
    selected_ids = {batch["id"] for batch in selected}
    searchable = set().union(*(associations[batch["id"]] for batch in selected))
    search_owner = {keyword: primary[keyword] if primary[keyword] in selected_ids else
        next(batch["id"] for batch in selected if keyword in associations[batch["id"]])
        for keyword in searchable}
    ordered, seen = [], set()
    for batch in selected:
        batch_id = batch["id"]
        related = [kw for kw in keywords if kw in associations[batch_id] and kw not in batch["primary_keywords"]]
        candidates = batch["primary_keywords"] + related
        for keyword in sorted(candidates, key=lambda kw: kw in broad):
            if search_owner[keyword] == batch_id and keyword not in seen:
                seen.add(keyword)
                ordered.append(keyword)
    index = {keyword: {
        "primary_batch_id": primary[keyword], "search_batch_id": search_owner[keyword],
        "related_batch_ids": [batch["id"] for batch in batches if keyword in associations[batch["id"]]],
        "declared_members": [code for code in members if code in declared[keyword]],
        "literal_shared": len(declared[keyword]) == len(members), "broad": keyword in broad,
    } for keyword in ordered}
    summaries = [{"id": batch["id"], "label": batch["label"], "scope": batch["scope"],
        "declared_line_members": [code for code in members if any(line["member"] == code for line in batch["source_lines"])],
        "primary_keyword_count": len(batch["primary_keywords"]),
        "related_keyword_count": len(associations[batch["id"]]),
        "search_keyword_count": sum(value["search_batch_id"] == batch["id"] for value in index.values()),
        "literal_shared_primary_count": sum(len(declared[kw]) == len(members) for kw in batch["primary_keywords"]),
    } for batch in selected]
    return {"keywords": ordered, "business_plan": {"policy": POLICY, "config_hash": digest(config),
        "source_inventory_hash": digest(inventory), "batch_ids": [batch["id"] for batch in selected],
        "batches": summaries, "keyword_index": index,
        "selected_unique_keyword_count": len(ordered),
        "selected_literal_shared_keyword_count": sum(value["literal_shared"] for value in index.values()),
        "same_joint_commercial_profile": True, "routing_is_business_classification": False}}


def report_business_batches(manifest: dict, coverage: list[dict], outcomes: list[dict]) -> list[dict]:
    """Attribute once by first discovery query, never imply semantic fit/coverage."""
    plan = manifest.get("business_plan")
    if not plan:
        return []
    index = plan["keyword_index"]
    reports = []
    for batch in plan["batches"]:
        batch_id = batch["id"]
        queries = [row for row in coverage if index[row["query"]["keyword"]]["search_batch_id"] == batch_id]
        records = [row for row in outcomes if row.get("business_batch_id") == batch_id]
        years, objects = manifest.get("resolved_years"), manifest.get("objects")
        expected = batch["search_keyword_count"] * len(years) * len(objects) if years and objects else None
        planned = {(keyword, year, object_type) for keyword, value in index.items()
            if value["search_batch_id"] == batch_id for year in (years or []) for object_type in (objects or [])}
        observed = {(row["query"]["keyword"], row["query"].get("year"), row["query"].get("object_type")) for row in queries}
        reports.append({**batch, "queries_total": len(queries),
            "queries_expected": expected,
            "queries_complete": sum(row["coverage_complete"] for row in queries),
            "source_coverage_complete": (len(queries) == expected and observed == planned
                and all(row["coverage_complete"] for row in queries))
                if expected is not None else None,
            "processed_first_discovery": len(records), "accepted": sum(row["accepted"] for row in records),
            "llm1_complete": sum(row["status"] == "llm1_complete" for row in records),
            "accepted_unread": sum(row["status"] == "accepted_unread" for row in records)})
    return reports


def select_keyword_preset(presets: dict, config: dict, inventory: dict, preset_id: str) -> dict:
    if (presets.get("policy") != "prod2-keyword-presets-v1"
            or presets.get("source_inventory_hash") != digest(inventory)
            or presets.get("business_config_hash") != digest(config)):
        raise ValueError("Keyword preset is stale or has an unsupported policy")
    preset = presets.get("presets", {}).get(preset_id)
    if not isinstance(preset, dict):
        raise ValueError("Unknown keyword preset")
    full = select_business_batches(config, inventory, scope=preset["business_scope"])
    keywords = preset.get("keywords")
    if (not isinstance(keywords, list) or not keywords or len(keywords) != len(set(keywords))
            or set(keywords) - set(full["keywords"])):
        raise ValueError("Keyword preset must be a unique configured business subset")
    selected = set(keywords)
    full["keywords"] = [kw for kw in full["keywords"] if kw in selected]
    plan = full["business_plan"]
    plan["keyword_index"] = {kw: row for kw, row in plan["keyword_index"].items() if kw in selected}
    for batch in plan["batches"]:
        batch["search_keyword_count"] = sum(row["search_batch_id"] == batch["id"] for row in plan["keyword_index"].values())
        batch["selected_primary_keyword_count"] = sum(row["primary_batch_id"] == batch["id"] for row in plan["keyword_index"].values())
    plan["batches"] = [batch for batch in plan["batches"] if batch["search_keyword_count"]]
    plan["batch_ids"] = [batch["id"] for batch in plan["batches"]]
    plan["selected_unique_keyword_count"] = len(selected)
    plan["selected_literal_shared_keyword_count"] = sum(row["literal_shared"] for row in plan["keyword_index"].values())
    return {**full, "keyword_preset": {"id": preset_id, "label": preset["label"], "config_hash": digest(presets), "keyword_count": len(selected)}}
