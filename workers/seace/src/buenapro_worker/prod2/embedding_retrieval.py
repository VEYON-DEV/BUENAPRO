"""Exact in-corpus cosine plus literal evidence and conservative structural expansion.

Scores rank retrieval seeds. They are not commercial affinity, compliance or a
probability. No providers, PG writes, full-PDF fallback or legal classification.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import math
import re
import unicodedata

from .embedding_corpus import (canonical_hash, canonical_json, token_count, text_hash,
                               validate_corpus)
from .partial_reading import is_partial_corpus, source_capture, compact_source_capture

RETRIEVAL_POLICY = "prod2_qualification_retrieval_v1"
EXPANDED_CONTEXT_POLICY = "prod2_explicit_expanded_context_v1"
DEFAULT_CONFIG = {
    "policy_version": RETRIEVAL_POLICY,
    "semantic_seed_top_k_per_query": 6,
    "context_target_tokens": 15000,
    "context_maximum_tokens": 30000,
    "neighbor_pages": 1,
    "expand_complete_sections": True,
    "include_complete_tables": True,
    "include_literal_matches": True,
    "queries": [
        {"id": "contract", "fields": ["contract", "summary"],
         "query": "Objeto y alcance específico de la contratación, entidad, lugar, plazo, valor estimado, moneda, modalidad y condiciones de inicio.",
         "literal_anchors": ["objeto", "alcance", "valor estimado", "valor referencial", "plazo", "lugar"]},
        {"id": "provider_requirements", "fields": ["requirements.provider"],
         "query": "Requisitos de calificación y habilitación del postor: capacidad legal, RNP, licencias, certificaciones, giro y documentos para acreditar cada requisito.",
         "literal_anchors": ["requisitos de calificación", "habilitación", "capacidad legal", "RNP", "licencia", "certificación"]},
        {"id": "provider_experience", "fields": ["requirements.provider"],
         "query": "Experiencia general, específica y económica del postor: monto facturado, moneda, periodo computable, contratos similares, acreditación, MYPE, alternativas y excepciones.",
         "literal_anchors": ["experiencia del postor", "facturación", "monto facturado", "similares", "MYPE", "acreditación"]},
        {"id": "personnel", "fields": ["requirements.key_personnel"],
         "query": "Personal clave y profesionales exigidos por perfil: cantidad, formación, títulos, colegiatura, habilitación, experiencia, cursos y acreditación.",
         "literal_anchors": ["personal clave", "personal", "formación académica", "colegiatura", "profesional", "capacitación"]},
        {"id": "equipment_insurance", "fields": ["requirements.equipment", "requirements.insurance"],
         "query": "Equipos, vehículos, infraestructura y seguros que el proveedor debe acreditar: cantidades, características, coberturas, disponibilidad y condiciones.",
         "literal_anchors": ["equipamiento", "infraestructura", "vehículos", "seguro", "póliza", "cobertura"]},
        {"id": "consortium", "fields": ["participation.consorcio"],
         "query": "Cláusulas aplicables de consorcio: autorización o prohibición, integrantes, porcentajes de participación y ejecución, experiencia acreditable, límites, condiciones y excepciones.",
         "literal_anchors": ["consorcio", "consorcios", "consorciado", "consorciados", "participación mínima"]},
        {"id": "subcontracting", "fields": ["participation.subcontratacion"],
         "query": "Cláusulas aplicables de subcontratación: permiso o prohibición, porcentaje máximo, autorización previa, actividades permitidas, condiciones y excepciones.",
         "literal_anchors": ["subcontratación", "subcontratacion", "subcontratar", "subcontratista", "subcontratistas"]},
        {"id": "execution", "fields": ["execution"],
         "query": "Actividades y entregables específicos, resultados, productos, lugares y plazos de ejecución por item, etapas y condiciones de entrega.",
         "literal_anchors": ["actividades", "entregables", "producto", "plazo de ejecución", "cronograma", "lugar de entrega"]},
        {"id": "goods", "fields": ["goods"],
         "query": "Bienes e items por adquirir: cantidades, unidades, especificaciones técnicas completas, entrega, garantía, cobertura y condiciones por item.",
         "literal_anchors": ["especificaciones técnicas", "cantidad", "unidad", "ítem", "garantía", "ficha técnica"]},
        {"id": "penalties_management", "fields": ["penalties", "contract_management"],
         "query": "Penalidades aplicables con fórmula, tope y base; conformidad, confidencialidad, vicios ocultos y controversias con sus condiciones.",
         "literal_anchors": ["penalidad", "penalidades", "conformidad", "confidencialidad", "vicios ocultos", "controversias"]},
    ],
}


class RetrievalError(ValueError):
    """Retrieval configuration or vector/source identity is untrustworthy."""


def _config(config):
    result = deepcopy(DEFAULT_CONFIG)
    if config is not None:
        if not isinstance(config, dict) or set(config) - (set(result) | {"context_expansion_policy"}):
            raise RetrievalError("Unknown retrieval configuration field")
        result.update(deepcopy(config))
    # The absent flag preserves every historical config/hash and the original
    # 100k hard bound. An explicit, separately frozen recovery may admit the
    # complete expanded context up to 350k; this does not relax source coverage.
    if ("context_expansion_policy" in result
            and result["context_expansion_policy"] != EXPANDED_CONTEXT_POLICY):
        raise RetrievalError("Unsupported explicit expanded context policy")
    maximum_context = 350000 if result.get("context_expansion_policy") == EXPANDED_CONTEXT_POLICY else 100000
    if result["policy_version"] != RETRIEVAL_POLICY:
        raise RetrievalError("Unsupported retrieval policy")
    for name in ("semantic_seed_top_k_per_query", "context_target_tokens", "context_maximum_tokens", "neighbor_pages"):
        if type(result[name]) is not int or result[name] < (0 if name == "neighbor_pages" else 1):
            raise RetrievalError("Invalid retrieval bounded configuration")
    if not (result["semantic_seed_top_k_per_query"] <= 50
            and result["context_target_tokens"] <= result["context_maximum_tokens"] <= maximum_context
            and result["neighbor_pages"] <= 3):
        raise RetrievalError("Retrieval limits exceed their hard bounds")
    # These are invariants, not switches that allow an inexpensive omission.
    for name in ("expand_complete_sections", "include_complete_tables", "include_literal_matches"):
        if result[name] is not True:
            raise RetrievalError("Required structural or literal expansion cannot be disabled")
    queries = result["queries"]
    if not isinstance(queries, list) or not 1 <= len(queries) <= 30:
        raise RetrievalError("Retrieval queries must be versioned and bounded")
    ids = set()
    for query in queries:
        if (not isinstance(query, dict) or set(query) != {"id", "fields", "query", "literal_anchors"}
                or not isinstance(query["id"], str) or not query["id"] or query["id"] in ids
                or not isinstance(query["query"], str) or not 0 < len(query["query"]) <= 4000
                or not isinstance(query["fields"], list) or not query["fields"]
                or not all(isinstance(value, str) and value for value in query["fields"])
                or not isinstance(query["literal_anchors"], list)
                or not all(isinstance(value, str) and 0 < len(value) <= 200 for value in query["literal_anchors"])):
            raise RetrievalError("Retrieval query schema is invalid")
        ids.add(query["id"])
    return result


def retrieval_queries(config=None):
    """Exact query texts/IDs for durable embedding reservation and caching."""
    return deepcopy(_config(config)["queries"])


def _normal(text):
    # Remove line-end hyphenation for lexical discovery only; the evidence sent
    # below always uses original exact text, including OCR and layout whitespace.
    text = re.sub(r"(?<=\w)[-\u00ad]\s*\n\s*(?=\w)", "", text)
    return " ".join("".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                            if not unicodedata.combining(char)).split())


def _literal_matches(blocks, query, *, memo=None):
    normal = memo.normal if memo is not None else _normal
    patterns = [re.compile(r"(?<!\w)" + re.escape(normal(anchor)) + r"(?!\w)")
                for anchor in query["literal_anchors"]]
    return {block["id"] for block in blocks
            if any(pattern.search(normal(block["text"])) for pattern in patterns)}


def _vector(value):
    if not isinstance(value, (list, tuple)) or not value or len(value) > 3072:
        raise RetrievalError("Missing or unbounded retrieval vector")
    if any(isinstance(number, bool) or not isinstance(number, (int, float))
           or not math.isfinite(number) for number in value):
        raise RetrievalError("Retrieval vector contains invalid numbers")
    norm = math.sqrt(math.fsum(float(number) * float(number) for number in value))
    if not norm or not math.isfinite(norm):
        raise RetrievalError("Retrieval vector has zero or invalid norm")
    return tuple(float(number) / norm for number in value)


def _vectors(values, identifiers):
    if isinstance(values, dict):
        if set(values) != set(identifiers):
            raise RetrievalError("Vector identities differ from the exact corpus or queries")
        ordered = [values[identifier] for identifier in identifiers]
    elif isinstance(values, (list, tuple)) and len(values) == len(identifiers):
        ordered = list(values)
    else:
        raise RetrievalError("Vectors must cover every exact corpus chunk or query")
    return [_vector(value) for value in ordered]


def _page_refs(text):
    # References retain a document-local page, not a guessed global page. The
    # caller resolves it only within the same official candidate.
    return {int(match) for match in re.findall(
        r"\b(?:p[aá]gina(?:s)?|p[aá]g\.?)[ \t]*(\d{1,4})\b", text, re.I)}


def _named_refs(text):
    refs = set()
    for kind, number in re.findall(r"\b(anexo|ap[eé]ndice|numeral|secci[oó]n|cap[ií]tulo)\s+(?:[Nn][.°ºo]*\s*)?"
                                  r"(\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])(?=\s|[).,:;-]|$)", text, re.I):
        refs.add((_normal(kind), number.casefold()))
    return refs


def _reference_heading(block, kind, number, *, normal=None):
    # A mention alone is not a resolved target. Require an actual heading line.
    normal = _normal if normal is None else normal
    target = re.compile(r"^\s*" + re.escape(kind) + r"\s+" + re.escape(number) + r"(?:\W|$)", re.I)
    numerical = re.compile(r"^\s*" + re.escape(number) + r"(?:[.)]\s*|\s+)", re.I)
    return any(target.search(normal(line)) or (kind in {"numeral", "seccion", "capitulo"}
               and numerical.search(normal(line))) for line in block["text"].splitlines())


class _ExpansionMemo:
    """Finite caches of pure decisions, owned by exactly one retrieval call.

    No cache crosses a corpus, request, thread, or retrieval-policy boundary.
    Eviction repeats the original function; it never omits evidence or changes
    expansion. Standard LRU locking also permits safe concurrent lookups.
    """
    def __init__(self, blocks):
        self.normal = lru_cache(maxsize=32768)(_normal)
        self.page_refs = lru_cache(maxsize=8192)(_page_refs)
        self.named_refs = lru_cache(maxsize=8192)(_named_refs)
        by_id = {block["id"]: block for block in blocks}
        normal = self.normal

        @lru_cache(maxsize=65536)
        def heading(block_id, kind, number):
            return _reference_heading(by_id[block_id], kind, number, normal=normal)

        self._heading = heading

    def reference_heading(self, block, kind, number):
        return self._heading(block["id"], kind, number)


def _expand(seed_ids, blocks, config, *, memo=None):
    by_id = {block["id"]: block for block in blocks}
    normal = memo.normal if memo is not None else _normal
    page_refs = memo.page_refs if memo is not None else _page_refs
    named_refs = memo.named_refs if memo is not None else _named_refs
    reference_heading = memo.reference_heading if memo is not None else _reference_heading
    selected = set(seed_ids)
    reasons, references, unresolved = {}, [], set()
    for identifier in selected:
        reasons[identifier] = {"seed"}
    # Expansion is monotonic: complete seed sections, adjacent pages in the same
    # document, tables, and recursively resolved reference sections. Neighbors
    # are not recursively expanded into every subsequent page of the source.
    seed_sections = {(by_id[identifier]["candidate_id"], by_id[identifier]["section_id"])
                     for identifier in selected}
    seed_pages = {(by_id[identifier]["candidate_id"], by_id[identifier]["source_page"])
                  for identifier in selected}
    for block in blocks:
        identifier = block["id"]
        section = (block["candidate_id"], block["section_id"])
        if section in seed_sections:
            selected.add(identifier)
            reasons.setdefault(identifier, set()).add("complete_seed_section")
        if any(candidate == block["candidate_id"] and abs(page - block["source_page"]) <= config["neighbor_pages"]
               for candidate, page in seed_pages):
            selected.add(identifier)
            reasons.setdefault(identifier, set()).add("neighbor_page")
    # Native tables split by PDF page commonly repeat their column headers.
    # Follow every matching continuation, even beyond the one-page neighbor.
    # No maximum row count silently cuts this expansion.
    expanded_tables = set()
    while True:
        tables = [by_id[key] for key in selected - expanded_tables if by_id[key]["kind"] == "table"]
        if not tables:
            break
        expanded_tables.update(table["id"] for table in tables)
        for table in tables:
            rows = table.get("table_rows") or []
            if not rows:
                continue
            header = normal(" | ".join(rows[0]))
            for other in blocks:
                other_rows = other.get("table_rows") or []
                if (other["kind"] == "table" and other["candidate_id"] == table["candidate_id"]
                        and abs(other["source_page"] - table["source_page"]) == 1
                        and other_rows and normal(" | ".join(other_rows[0])) == header):
                    selected.add(other["id"])
                    reasons.setdefault(other["id"], set()).add("complete_continued_table")
    # Follow references from the resulting context, including a reference inside
    # an expanded exception/note. The finite block set prevents cyclic recursion.
    checked, seen_refs = set(), set()
    while selected - checked:
        wave = sorted(selected - checked)
        checked.update(wave)
        new_targets = set()
        for identifier in wave:
            source = by_id[identifier]
            for page in page_refs(source["text"]):
                key = (source["candidate_id"], "page", str(page))
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                targets = [block for block in blocks if block["candidate_id"] == source["candidate_id"]
                           and block["source_page"] == page]
                references.append({"from_block_id": identifier, "kind": "source_page", "reference": page,
                                   "resolved_block_ids": [block["id"] for block in targets]})
                if targets:
                    new_targets.update(block["id"] for block in targets)
                else:
                    unresolved.add(canonical_hash(key))
            for kind, number in named_refs(source["text"]):
                key = (source["candidate_id"], kind, number)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                targets = [block for block in blocks if reference_heading(block, kind, number)]
                # An annex may be a different selected official document. Keep
                # every candidate/version match; do not guess authority.
                references.append({"from_block_id": identifier, "kind": kind, "reference": number,
                                   "resolved_block_ids": [block["id"] for block in targets]})
                if targets:
                    target_sections = {(block["candidate_id"], block["section_id"]) for block in targets}
                    new_targets.update(block["id"] for block in blocks
                                       if (block["candidate_id"], block["section_id"]) in target_sections)
                else:
                    unresolved.add(canonical_hash(key))
        for identifier in new_targets:
            selected.add(identifier)
            reasons.setdefault(identifier, set()).add("resolved_reference_section")
    # A multi-page table may have a separate native table ID on each page. Whole
    # neighboring pages preserve repeated headers and continuation rows. Nothing
    # slices the complete table block to hit a token budget.
    return selected, {key: sorted(value) for key, value in reasons.items()}, references, sorted(unresolved)


def _marked_context(blocks, visual_coverage_warnings=None, partial_capture=None):
    # Bind each official source once. Repeating UUID/SHA/alias metadata beside
    # every small paragraph can cost more tokens than the source itself.
    source_map = {}
    for block in blocks:
        for location in block["locations"]:
            metadata = {key: value for key, value in location.items() if key != "source_page"}
            key = canonical_hash(metadata)
            if key not in source_map:
                source_map[key] = {"label": f"F{len(source_map) + 1}", "metadata": metadata}
    parts = ["[FUENTES OFICIALES DEL CONTEXTO " + canonical_json(
        {row["label"]: row["metadata"] for row in source_map.values()}) + "]"] if blocks else []
    if partial_capture is not None:
        parts.insert(0, "[COBERTURA PARCIAL DE LA FUENTE " + canonical_json(
            compact_source_capture(partial_capture)) + "]")
    if visual_coverage_warnings:
        parts.insert(0, "[ADVERTENCIAS DE COBERTURA DEL CONTEXTO " + canonical_json({
            "text_capture_is_not_visual_certainty": True, "claims_visual_complete": False,
            "require_human_review_of_layout_relations": True,
            "pages": visual_coverage_warnings}) + "]")
    for block in blocks:
        locations = []
        for location in block["locations"]:
            metadata = {key: value for key, value in location.items() if key != "source_page"}
            locations.append(source_map[canonical_hash(metadata)]["label"] + ":" + str(location["source_page"]))
        parts.append(f"[BLOQUE {block['id']}; PAGINA GLOBAL={block['global_page']}; "
                     f"FUENTES={','.join(locations)}; TIPO={block['kind']}]\n" + block["text"] + "\n[FIN BLOQUE]")
    return "\n\n".join(parts)


def retrieve_context(corpus, chunk_vectors, query_vectors, *, config=None, memoize_expansion=False):
    """Build a provenance-bound LLM subset without cutting requirements to save tokens.

    Top-k is a seed count, not a context cap. If the complete expanded evidence
    exceeds the configured budget, return it with needs_review rather than
    silently truncate it or fall back to the costly full PDF.

    Optional per-call memoization accelerates repeated text/reference checks;
    it does not change policy, limits, selected text, coverage, or result hashes.
    """
    if type(memoize_expansion) is not bool:
        raise RetrievalError("Retrieval memoization requires an explicit boolean")
    validate_corpus(corpus)
    limits = _config(config)
    chunks, blocks, queries = corpus["chunks"], corpus["blocks"], limits["queries"]
    memo = _ExpansionMemo(blocks) if memoize_expansion else None
    vectors = _vectors(chunk_vectors, [chunk["chunk_id"] for chunk in chunks])
    questions = _vectors(query_vectors, [query["id"] for query in queries])
    dimensions = {len(vector) for vector in [*vectors, *questions]}
    if len(dimensions) != 1:
        raise RetrievalError("Corpus and query vectors have different dimensions")
    coverage = {}
    union, all_references, unresolved_refs = set(), [], set()
    all_expansions = {}
    for query, question in zip(queries, questions):
        ranking = sorted(((math.fsum(left * right for left, right in zip(vector, question)), index)
                          for index, vector in enumerate(vectors)), key=lambda row: (-row[0], row[1]))
        seeds = ranking[:limits["semantic_seed_top_k_per_query"]]
        semantic = {identifier for _, index in seeds for identifier in chunks[index]["block_ids"]}
        literal = _literal_matches(blocks, query, memo=memo)
        selected, expansions, references, unresolved = _expand(semantic | literal, blocks, limits, memo=memo)
        union.update(selected)
        unresolved_refs.update(unresolved)
        for key, values in expansions.items():
            all_expansions.setdefault(key, set()).update(values)
        all_references.extend(references)
        coverage[query["id"]] = {"fields": query["fields"], "query_text_sha256": text_hash(query["query"]),
            "search_executed": True, "semantic_seed_chunk_ids": [chunks[index]["chunk_id"] for _, index in seeds],
            "semantic_scores": [{"chunk_id": chunks[index]["chunk_id"], "cosine": score} for score, index in seeds],
            "literal_block_ids": sorted(literal), "expanded_block_ids": sorted(selected),
            "literal_evidence_identified": bool(literal),
            "absence_semantics": "not_identified_not_permission_or_prohibition" if not literal else None,
            "complete_material_coverage_proven": False, "unresolved_reference_ids": unresolved}
    selected = [block for block in blocks if block["id"] in union]
    layout_review = corpus["config"]["ocr_textual_scan_policy"] in {
        "prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"}
    selected_pages = {block["global_page"] for block in selected}
    visual_coverage_warnings = [{"global_page": page["global_page"],
        "warnings": page.get("visual_warnings", []), "method": page["method"]} for page in corpus["pages"]
        if page["global_page"] in selected_pages and page.get("visual_warnings")] if layout_review else None
    partial_capture = source_capture(corpus) if is_partial_corpus(corpus["config"]) else None
    text = _marked_context(selected, visual_coverage_warnings, partial_capture)
    context_tokens = token_count(text)
    reasons = []
    if not corpus["coverage_complete"]:
        reasons.append("corpus_page_coverage_unresolved")
    if not selected:
        reasons.append("retrieved_context_empty")
    if unresolved_refs:
        reasons.append("referenced_evidence_unresolved")
    if context_tokens > limits["context_maximum_tokens"]:
        reasons.append("expanded_context_exceeds_budget_without_truncation")
    selected_blocks = [{"block_id": block["id"], "text": block["text"],
        "text_sha256": block["text_sha256"], "global_page": block["global_page"],
        "page_start": block["global_page"], "page_end": block["global_page"],
        "locations": deepcopy(block["locations"]), "section_id": block["section_id"],
        "table_id": block["table_id"]} for block in selected]
    citations = [{"block_id": block["id"], "global_page": block["global_page"],
                  **deepcopy(location)} for block in selected for location in block["locations"]]
    # Identity includes the actual vectors used, query text, complete selected text
    # and every expansion. A cached context cannot masquerade as another ranking.
    result = {"policy_version": RETRIEVAL_POLICY, "route": "retrieved_text", "config": limits,
        "analysis_sha256": corpus["analysis_sha256"], "page_count": corpus["page_count"],
        "source_manifest_hash": corpus["source_manifest_hash"], "corpus_hash": corpus["corpus_hash"],
        "vector_set_hash": canonical_hash({"chunks": chunk_vectors, "queries": query_vectors}),
        "context_text": text, "context_sha256": text_hash(text), "context_bytes": len(text.encode("utf-8")),
        "context_tokens": context_tokens, "token_count": context_tokens, "source_tokens": corpus["source_tokens"],
        "selected_chunk_ids": [chunk["chunk_id"] for chunk in chunks if any(key in union for key in chunk["block_ids"])],
        "selected_blocks": selected_blocks, "citations": citations,
        "coverage_by_field": coverage, "coverage": deepcopy(coverage),
        "indexed_complete": corpus["coverage_complete"], "unresolved_pages": corpus["unresolved_pages"],
        "visual_warnings": corpus.get("visual_warnings", []),
        "human_review_recommended": corpus.get("human_review_recommended", False),
        "references": sorted({canonical_hash(value): value for value in all_references}.values(), key=canonical_hash),
        "unresolved_reference_ids": sorted(unresolved_refs),
        "expansion_reasons": {key: sorted(value) for key, value in sorted(all_expansions.items())},
        "target_budget_exceeded": context_tokens > limits["context_target_tokens"],
        "needs_review": bool(reasons), "review_reasons": reasons, "reasons": list(reasons),
        "claims_exhaustive_read": False, "scores_are_compliance_probabilities": False,
        "source_to_context_token_ratio": context_tokens / corpus["source_tokens"] if corpus["source_tokens"] else None,
        "unverified_retrieval_projects_canonical_participation": False}
    if layout_review:
        result.update(text_coverage_complete=corpus["coverage_complete"], claims_visual_complete=False,
                      text_capture_policy=corpus["config"]["ocr_textual_scan_policy"],
                      visual_coverage_warnings=visual_coverage_warnings)
    if partial_capture is not None:
        result["source_capture"] = partial_capture
    result["retrieval_hash"] = canonical_hash(result)
    return result


def validate_retrieval(result, corpus):
    """Verify the exact context, citations and selected blocks before transport."""
    validate_corpus(corpus)
    body = {key: value for key, value in result.items() if key != "retrieval_hash"}
    if (result.get("policy_version") != RETRIEVAL_POLICY or result.get("corpus_hash") != corpus["corpus_hash"]
            or result.get("retrieval_hash") != canonical_hash(body)
            or result.get("context_sha256") != text_hash(result["context_text"])
            or result.get("context_bytes") != len(result["context_text"].encode("utf-8"))
            or result.get("context_tokens") != token_count(result["context_text"])):
        raise RetrievalError("Retrieved context no longer matches its bound identity")
    by_id = {block["id"]: block for block in corpus["blocks"]}
    partial_capture = source_capture(corpus) if is_partial_corpus(corpus["config"]) else None
    if ((partial_capture is not None and result.get("source_capture") != partial_capture)
            or (partial_capture is None and "source_capture" in result)):
        raise RetrievalError("Partial retrieval source coverage changed")
    if partial_capture is not None and (result.get("indexed_complete") is not corpus["coverage_complete"]
            or (not corpus["coverage_complete"] and (result.get("needs_review") is not True
                or "corpus_page_coverage_unresolved" not in result.get("reasons", [])))):
        raise RetrievalError("Partial retrieval cannot claim complete source coverage")
    selected = []
    for block in result["selected_blocks"]:
        original = by_id.get(block["block_id"])
        if (not original or block["text"] != original["text"]
                or block["text_sha256"] != original["text_sha256"]
                or block["locations"] != original["locations"]
                or block["page_start"] != original["global_page"]
                or block["page_end"] != original["global_page"]):
            raise RetrievalError("Retrieved block no longer matches corpus evidence")
        selected.append(original)
    if _marked_context(selected, result.get("visual_coverage_warnings"), partial_capture) != result["context_text"]:
        raise RetrievalError("Transported context differs from complete selected blocks")
    expected_citations = [{"block_id": block["id"], "global_page": block["global_page"],
                           **deepcopy(location)} for block in selected for location in block["locations"]]
    if result.get("citations") != expected_citations:
        raise RetrievalError("Retrieved citations differ from exact transported provenance")
    return result
