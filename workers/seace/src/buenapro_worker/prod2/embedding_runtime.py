"""Explicit paid retrieval stage; prepare_input remains strictly local.

Source text and vectors are durable before a reduced context reaches LLM1.
Unknown payments, unusable OCR and overlarge contexts never trigger a full-PDF
generation. Legacy input contracts are left untouched.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from .batch_runner import private_write
from .selected_control import LimitReached
from .partial_reading import (PAGE_FAILURE_POLICY, SOURCE_COVERAGE_POLICY,
                             SOURCE_COVERAGE_POLICY_KEY)

POLICY = "prod2_embedding_reading_runtime_v1"
INPUT_POLICY = "prod2_embedding_retrieved_text_v1"
SHA = re.compile(r"[a-f0-9]{64}")


class EmbeddingPreparationReview(RuntimeError):
    """The inputs remain available for review; do not repay automatically."""


def _stable_proof(value):
    if isinstance(value, dict):
        return {k: _stable_proof(v) for k, v in value.items() if k != "verified_at"}
    if isinstance(value, list):
        return [_stable_proof(v) for v in value]
    return value


def _write_verified_once(path, value):
    if path.exists():
        if path.is_symlink() or _stable_proof(json.loads(path.read_text())) != _stable_proof(value):
            raise ValueError("Existing embedding persistence proof changed")
    else:
        private_write(path, value)


def load_config(path):
    path = Path(path).absolute()
    if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
            or not path.is_file() or path.stat().st_size > 128 * 1024):
        raise ValueError("Embedding configuration must be a bounded literal file")
    data = json.loads(path.read_text())
    if (data.get("schema_version") != POLICY or data.get("status") != "runtime_pilot"
            or data.get("embedding", {}).get("model") != "text-embedding-3-small"
            or data["embedding"].get("dimensions") != 1536
            or data["embedding"].get("tokenizer") != "cl100k_base"
            or data["embedding"].get("price_per_million_usd") != 0.02
            or not isinstance(data.get("retrieval", {}).get("queries"), list)
            or len(data["retrieval"]["queries"]) != 10):
        raise ValueError("Unsupported versioned embedding configuration")
    ids = [q.get("id") for q in data["retrieval"]["queries"]]
    if (len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids)
            or any(not isinstance(q.get("query"), str) or not q["query"].strip()
                   for q in data["retrieval"]["queries"])):
        raise ValueError("Embedding qualification queries require exact identities")
    _partial_config(data)
    return data


def _partial_config(config):
    reading = config.get("reading", {})
    source_policy = reading.get("source_coverage_policy")
    failure_policy = config.get("corpus", {}).get("page_failure_policy")
    if source_policy is None and failure_policy is None:
        return False
    if (source_policy != SOURCE_COVERAGE_POLICY or failure_policy != PAGE_FAILURE_POLICY
            or reading.get("transport_policy") != "prod2_retrieved_compact_metadata_v4"
            or config["corpus"].get("ocr_textual_scan_policy") != "prod2_confident_text_layout_review_v3"):
        raise ValueError("Partial source reading needs its paired versioned corpus/prompt configuration")
    return True


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def validate_frozen_config(manifest):
    if manifest.get("llm1_input_policy") != INPUT_POLICY:
        if "embedding_reading" in manifest:
            raise ValueError("Embedding stage needs its explicit reading contract")
        return None
    bound = manifest.get("embedding_reading")
    if (not isinstance(bound, dict) or set(bound) != {"config", "config_sha256"}
            or bound["config_sha256"] != config_hash(bound["config"])
            or bound["config"].get("schema_version") != POLICY):
        raise ValueError("Frozen embedding configuration differs from its identity")
    visual_v2 = bound["config"]["corpus"]["ocr_textual_scan_policy"] in {
        "prod2_confident_text_layout_review_v2", "prod2_confident_text_layout_review_v3"}
    if (manifest.get("llm1_visual_review_policy") == "prod2_ocr_text_layout_review_v2") != visual_v2:
        raise ValueError("OCR text coverage requires its explicit visual-review prompt")
    transport = bound["config"].get("reading", {}).get("transport_policy")
    if (transport not in {None, "prod2_retrieved_compact_metadata_v4"}
            or manifest.get("llm1_retrieved_transport_policy") != transport):
        raise ValueError("Retrieved prompt transport differs from the frozen configuration")
    partial = _partial_config(bound["config"])
    if manifest.get(SOURCE_COVERAGE_POLICY_KEY) != (SOURCE_COVERAGE_POLICY if partial else None):
        raise ValueError("Partial source policy differs from the frozen configuration")
    return deepcopy(bound["config"])


@contextmanager
def embedding_permission(limits, *, stop_requested=lambda: False):
    # Reserve capacity before durable vector claims, issue at the HTTP boundary.
    permit = limits.reserve_request("embedding")
    try:
        def issue():
            if stop_requested():
                raise LimitReached("Coordinator stopped before embedding generation")
            permit.issue()
        yield issue
    finally:
        permit.close()


def prepare_retrieved_input(conn, settings, prepared, folder, limits, config, *,
                            convocation_id, stop_requested=lambda: False, memoize_expansion=False):
    from .embedding_client import EmbeddingClient
    from .embedding_corpus import build_corpus
    from .embedding_retrieval import retrieve_context
    from .embedding_store import EmbeddingStore, save_corpus, save_retrieval
    folder = Path(folder).absolute()
    if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):
        raise ValueError("Embedding case requires a literal private path before paid calls")
    if prepared["selection_context"].get("llm1_input_policy") != INPUT_POLICY:
        raise ValueError("Retrieved input cannot be attached to a full-source contract")
    partial = _partial_config(config)
    if prepared["selection_context"].get(SOURCE_COVERAGE_POLICY_KEY) != (SOURCE_COVERAGE_POLICY if partial else None):
        raise ValueError("Partial source policy differs from the prepared input")
    key = getattr(settings, "openai_api_key", "")
    if not key:
        raise ValueError("Embedding configuration requires its provider credential")
    origins = prepared.get("embedding_ocr_cache_sources", ())
    options = {"ocr_cache_sources": origins} if origins else {}
    corpus = build_corpus(prepared, folder, config=config["corpus"], stop_requested=stop_requested, **options)
    if not corpus.get("chunks"):
        _write_verified_once(folder / "embedding-preparation-review.private.json", {
            "reason": "source_has_no_usable_text", "corpus_hash": corpus["corpus_hash"],
            "unresolved_pages": corpus.get("unresolved_pages", []),
            "provider_calls": 0, "full_pdf_fallback": False})
        raise EmbeddingPreparationReview("Source has no usable text; no paid fallback")
    model = config["embedding"]["model"]
    dimensions = config["embedding"]["dimensions"]
    corpus_proof = save_corpus(conn, corpus, convocation_id=convocation_id,
        model=model, dimensions=dimensions)
    _write_verified_once(folder / "embedding-corpus-persistence.private.json", corpus_proof)
    if corpus.get("coverage_complete") is not True and not partial:
        _write_verified_once(folder / "embedding-preparation-review.private.json", {
            "reason": "incomplete_source_text", "corpus_hash": corpus["corpus_hash"],
            "unresolved_pages": corpus.get("unresolved_pages", []),
            "provider_calls": 0, "full_pdf_fallback": False})
        raise EmbeddingPreparationReview("Complete usable source text needs review")
    queries = config["retrieval"]["queries"]
    texts = [chunk["text"] for chunk in corpus["chunks"]] + [q["query"] for q in queries]
    store = EmbeddingStore(conn, model=model, dimensions=dimensions,
        price_per_million=config["embedding"]["price_per_million_usd"])
    def permission(request_hash):
        return embedding_permission(limits, stop_requested=stop_requested)
    with limits.model("embedding:" + corpus["corpus_hash"]):
        client = EmbeddingClient(api_key=key, model=model, dimensions=dimensions,
            price_per_million=config["embedding"]["price_per_million_usd"])
        handoff_guard = getattr(limits, "handoff_guard", None)
        if handoff_guard is not None:
            client = handoff_guard.embedding_client(client)
        try:
            vector_result = store.ensure_vectors(texts, client=client, folder=folder,
                before_request_factory=permission)
        finally:
            client.close()
    size = len(corpus["chunks"])
    vectors = vector_result["vectors"]
    if len(vectors) != size + len(queries):
        raise ValueError("The exact corpus and query vectors were not confirmed")
    retrieval = retrieve_context(corpus, vectors[:size], vectors[size:], config=config["retrieval"],
        **({"memoize_expansion": True} if memoize_expansion else {}))
    if (not retrieval.get("context_text") or retrieval.get("context_tokens", 0) >
            config["retrieval"].get("context_maximum_tokens", 30000)):
        _write_verified_once(folder / "embedding-preparation-review.private.json", {
            "reason": "retrieval_context_needs_expansion_or_review",
            "corpus_hash": corpus["corpus_hash"],
            "retrieval_hash": retrieval.get("retrieval_hash"),
            "embedding_cost_usd": vector_result["known_cost_usd"],
            "full_pdf_fallback": False})
        raise EmbeddingPreparationReview("Retrieved context exceeds the economical reading contract")
    durable = save_retrieval(conn, corpus["corpus_hash"], retrieval,
        convocation_id=convocation_id)
    private = folder / ("embedding-context-" + retrieval["retrieval_hash"])
    if private.is_symlink():
        raise ValueError("Retrieved context path must not be a symlink")
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / "context.private.txt"
    content = retrieval["context_text"].encode()
    if (hashlib.sha256(content).hexdigest() != retrieval["context_sha256"]
            or len(content) != retrieval["context_bytes"]):
        raise ValueError("Retrieved context bytes differ from their durable identity")
    if path.exists() and (path.is_symlink() or path.read_bytes() != content):
        raise ValueError("An existing retrieved context changed")
    if not path.exists():
        path.write_bytes(content)
        path.chmod(0o600)
    result = {**retrieval, "context_text_path": str(path.absolute()), "durable_proof": durable}
    _write_verified_once(private / "manifest.private.json", {"analysis_sha256": prepared["analysis_sha256"],
        "corpus_hash": corpus["corpus_hash"], "retrieval_hash": retrieval["retrieval_hash"],
        "context_sha256": retrieval["context_sha256"], "context_bytes": len(content),
        "durable_proof": durable})
    stage = {
        "policy_version": POLICY, "corpus_hash": corpus["corpus_hash"],
        "retrieval_hash": retrieval["retrieval_hash"], "durable_proof": durable,
        "source_tokens": retrieval["source_tokens"], "context_tokens": retrieval["context_tokens"],
        "chunk_count": size, "model": model, "dimensions": dimensions,
        "known_cost_usd": vector_result["known_cost_usd"],
        "cached_count": vector_result["cached_count"],
        "new_token_count": vector_result["new_token_count"],
        "attempt_ids": vector_result["attempt_ids"], "full_pdf_sent": False}
    marker = folder / "embedding-stage.private.json"
    if marker.exists():
        previous = json.loads(marker.read_text())
        if any(previous.get(key) != stage[key] for key in ("corpus_hash", "retrieval_hash", "model", "dimensions")):
            raise ValueError("Replayed embedding stage changed its source/context")
        _write_verified_once(folder / ("embedding-stage-replay-" + str(time.time_ns()) + ".private.json"), stage)
    else:
        private_write(marker, stage)
    output = deepcopy(prepared)
    output["retrieval_input_result"] = result
    return output
