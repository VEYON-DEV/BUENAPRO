"""Durable exact-text cache, paid ownership, corpus and evidence readbacks.

Each provider reservation commits BEFORE dispatch. Use a dedicated IDLE
connection: a nested savepoint cannot prove ownership durable to other workers.
Unknown requests remain blocked even when a later caller changes batch layout.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
from hashlib import sha256
import json
import math
from pathlib import Path
from uuid import uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .embedding_client import (MODEL, DIMENSIONS, MAX_BATCH_INPUTS, MAX_BATCH_TOKENS,
                              MAX_INPUT_TOKENS, EmbeddingRequestError, safe_request_error_facts,
                              token_count, validate_vector)

MIGRATION = "0043_prod2_embeddings.sql"


class EmbeddingPersistenceError(RuntimeError):
    pass


class EmbeddingBlocked(EmbeddingPersistenceError):
    def __init__(self, attempt_ids):
        super().__init__("Embedding ownership blocked; reconcile without automatic repayment")
        self.attempt_ids = list(attempt_ids)


class EmbeddingIncomplete(EmbeddingPersistenceError):
    def __init__(self, receipt):
        super().__init__("Embedding response incomplete; usable vectors persisted, no automatic retry")
        self.receipt = receipt


EXPECTED_COLUMNS = {
    "prod2_embedding_attempts": {"attempt_id", "claim_token", "model", "dimensions", "request_hash", "input_json", "status", "input_tokens", "cost_usd", "provider_usage_known", "request_sent", "result_json", "error_code", "claimed_at", "completed_at"},
    "prod2_embedding_vectors": {"text_sha256", "model", "dimensions", "exact_text", "token_count", "status", "attempt_id", "vector", "response_sha256", "completed_at"},
    "prod2_embedding_corpora": {"corpus_id", "corpus_hash", "convocation_id", "payload_sha256", "corpus_json", "chunk_count", "vector_model", "vector_dimensions", "created_at"},
    "prod2_embedding_chunks": {"corpus_hash", "ordinal", "chunk_id", "text_sha256", "exact_text", "location_json"},
    "prod2_embedding_retrievals": {"retrieval_id", "retrieval_hash", "corpus_hash", "convocation_id", "payload_sha256", "retrieval_json", "created_at"},
}
EXPECTED_PRIMARY_KEYS = {
    "prod2_embedding_attempts": "PRIMARYKEY(attempt_id)",
    "prod2_embedding_vectors": "PRIMARYKEY(text_sha256,model,dimensions)",
    "prod2_embedding_corpora": "PRIMARYKEY(corpus_hash)",
    "prod2_embedding_chunks": "PRIMARYKEY(corpus_hash,ordinal)",
    "prod2_embedding_retrievals": "PRIMARYKEY(retrieval_hash)",
}


def _schema_contract(cur):
    for table, expected in EXPECTED_COLUMNS.items():
        cur.execute("""SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(%s)
            AND attnum>0 AND NOT attisdropped""", (table,))
        if {row["attname"] for row in cur.fetchall()} != expected:
            raise EmbeddingPersistenceError("Embedding schema columns incompatible")
        cur.execute("""SELECT contype,convalidated,pg_get_constraintdef(oid) AS definition
            FROM pg_constraint WHERE conrelid=to_regclass(%s)""", (table,))
        constraints = cur.fetchall()
        primary = ["".join(row["definition"].split()) for row in constraints if row["contype"] == "p"]
        if primary != [EXPECTED_PRIMARY_KEYS[table]] or any(not row["convalidated"] for row in constraints):
            raise EmbeddingPersistenceError("Embedding schema ownership constraints incompatible")
        if table in ("prod2_embedding_attempts", "prod2_embedding_vectors"):
            if not any(row["contype"] == "c" and all(status in row["definition"] for status in
                       ("running", "done", "partial_known", "uncertain", "failed_pre_send")) for row in constraints):
                raise EmbeddingPersistenceError("Embedding state constraints incompatible")
        if table == "prod2_embedding_vectors" and not any(row["contype"] == "f" and
                "REFERENCES prod2_embedding_attempts(attempt_id)" in row["definition"] for row in constraints):
            raise EmbeddingPersistenceError("Embedding vector owner relation missing")


def require_schema(conn):
    _idle(conn)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT to_regclass('schema_migrations') AS relation")
        if not cur.fetchone()["relation"]:
            raise EmbeddingPersistenceError("Apply migration0043 before embeddings provider calls")
        cur.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,))
        if not cur.fetchone():
            raise EmbeddingPersistenceError("Apply migration0043 before embeddings provider calls")
        _schema_contract(cur)
    return {"status": "embedding_schema_ready", "migration": MIGRATION}


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise EmbeddingPersistenceError("Invalid SHA256 identity")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _text_sha(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _idle(conn):
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise EmbeddingPersistenceError("Embedding durability requires an IDLE dedicated connection")


def _private_write(path, payload):
    """Atomic private receipt; exact input is retained for reconciliation."""
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    raw = _json(payload).encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)
    return {"path": str(path), "sha256": sha256(raw).hexdigest(), "bytes": len(raw)}


def _attach_attempts(cur, rows):
    # Fetch each 64-vector response ONCE, not once per joined vector row.
    # Joining JSON responses multiplies memory by the batch size.
    ids = sorted({str(row["attempt_id"]) for row in rows})
    if not ids:
        return
    cur.execute("""SELECT attempt_id,input_json,result_json FROM prod2_embedding_attempts
        WHERE attempt_id=ANY(%s::uuid[]) FOR SHARE""", (ids,))
    attempts = {str(row["attempt_id"]): row for row in cur.fetchall()}
    for row in rows:
        attempt = attempts.get(str(row["attempt_id"]))
        if not attempt:
            raise EmbeddingPersistenceError("Embedding paid attempt relation missing")
        row["attempt_input"], row["attempt_response"] = attempt["input_json"], attempt["result_json"]


def _verify_vector_receipt(row):
    """A DONE float array must equal its immutable received answer and input."""
    source, response = row.get("attempt_input"), row.get("attempt_response")
    if not isinstance(source, dict) or not isinstance(response, dict):
        raise EmbeddingPersistenceError("Embedding cached provider receipt missing")
    key = _sha(row["text_sha256"])
    try:
        index = source["text_sha256"].index(key)
        received = response["vectors_by_index"].get(str(index), response["vectors_by_index"].get(index))
        if (source["texts"][index] != row["exact_text"] or _text_sha(row["exact_text"]) != key
                or source["model"] != row["model"] or source["dimensions"] != row["dimensions"]
                or response["model"] != row["model"] or response["dimensions"] != row["dimensions"]
                or response["response_sha256"] != row["response_sha256"]
                or validate_vector(received, row["dimensions"]) != validate_vector(row["vector"], row["dimensions"])):
            raise EmbeddingPersistenceError("Embedding cached evidence changed")
    except (KeyError, ValueError, IndexError, TypeError):
        raise EmbeddingPersistenceError("Embedding cached evidence incomplete") from None


def prepare_schema(conn, migration: Path):
    _idle(conn)
    if migration.name != MIGRATION:
        raise EmbeddingPersistenceError("Expected dedicated embeddings migration0043")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('prod2_accepted_executor'),0) AS locked")
        if not cur.fetchone()["locked"]:
            raise EmbeddingPersistenceError("Another PROD2 coordinator owns the executor")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_embedding_schema'),0)")
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("SELECT version FROM schema_migrations WHERE version=%s", (MIGRATION,))
        if not cur.fetchone():
            cur.execute(migration.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (MIGRATION,))
        _schema_contract(cur)
    return {"status": "embedding_schema_ready", "migration": MIGRATION}


class EmbeddingStore:
    def __init__(self, conn, *, model: str = MODEL, dimensions: int = DIMENSIONS,
                 price_per_million: float = .02):
        self.conn, self.model, self.dimensions = conn, model, dimensions
        self.price_per_million = price_per_million
        if not isinstance(model, str) or not model or type(dimensions) is not int or not 1 <= dimensions <= 3072:
            raise EmbeddingPersistenceError("Invalid embedding cache configuration")

    def _rows(self, keys):
        _idle(self.conn)
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM prod2_embedding_vectors
                WHERE text_sha256=ANY(%s) AND model=%s AND dimensions=%s""",
                (keys, self.model, self.dimensions))
            rows = {row["text_sha256"]: row for row in cur.fetchall()}
            _attach_attempts(cur, list(rows.values()))
            for row in rows.values():
                if row["status"] == "done":
                    _verify_vector_receipt(row)
            return rows

    def _claim(self, texts):
        """Reserve all missing keys atomically; sorted locks avoid split ownership."""
        _idle(self.conn)
        identity = {"model": self.model, "dimensions": self.dimensions,
                    "texts": texts, "text_sha256": [_text_sha(text) for text in texts]}
        request_hash, owner, attempt = _digest(identity), str(uuid4()), str(uuid4())
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            for key in sorted(identity["text_sha256"]):
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('prod2_embedding_vector'),hashtext(%s))", (key,))
            cur.execute("""SELECT * FROM prod2_embedding_vectors WHERE text_sha256=ANY(%s)
                AND model=%s AND dimensions=%s FOR UPDATE""", (identity["text_sha256"], self.model, self.dimensions))
            rows = {row["text_sha256"]: row for row in cur.fetchall()}
            for key, text in zip(identity["text_sha256"], texts):
                if key in rows and rows[key]["exact_text"] != text:
                    raise EmbeddingPersistenceError("Embedding text collision")
            blocked = {str(row["attempt_id"]) for row in rows.values()
                       if row["status"] not in ("done", "failed_pre_send")}
            if blocked:
                raise EmbeddingBlocked(sorted(blocked))
            missing = [text for text in texts if _text_sha(text) not in rows or rows[_text_sha(text)]["status"] != "done"]
            if not missing:
                return None
            identity.update(texts=missing, text_sha256=[_text_sha(t) for t in missing])
            request_hash = _digest(identity)
            cur.execute("""INSERT INTO prod2_embedding_attempts
                (attempt_id,claim_token,model,dimensions,request_hash,input_json,status)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,'running')""",
                (attempt, owner, self.model, self.dimensions, request_hash, _json(identity)))
            for key, text in zip(identity["text_sha256"], missing):
                cur.execute("""INSERT INTO prod2_embedding_vectors
                    (text_sha256,model,dimensions,exact_text,token_count,status,attempt_id)
                    VALUES(%s,%s,%s,%s,%s,'running',%s)
                    ON CONFLICT(text_sha256,model,dimensions) DO UPDATE
                    SET status='running',attempt_id=EXCLUDED.attempt_id,completed_at=NULL
                    WHERE prod2_embedding_vectors.status='failed_pre_send'""",
                    (key, self.model, self.dimensions, text, token_count(text), attempt))
        return {"attempt_id": attempt, "claim_token": owner, "request_hash": request_hash,
                "input": identity, "version": "prod2_embedding_claim_v1"}

    def _complete(self, claim, response):
        _idle(self.conn)
        if (response.get("model"), response.get("dimensions")) != (self.model, self.dimensions):
            raise EmbeddingPersistenceError("Embedding response contract changed")
        # JSON maps stringify index keys. Restore them strictly, not by coercing floats.
        vectors = {}
        for index, vector in response.get("vectors_by_index", {}).items():
            if type(index) is int:
                index_number = index
            elif isinstance(index, str) and index.isdigit() and str(int(index)) == index:
                index_number = int(index)
            else:
                raise EmbeddingPersistenceError("Embedding response index invalid")
            if not 0 <= index_number < len(claim["input"]["texts"]):
                raise EmbeddingPersistenceError("Embedding response index outside request")
            vectors[index_number] = validate_vector(vector, self.dimensions)
        known = response.get("provider_usage_known") is True
        tokens, cost = response.get("input_tokens"), response.get("cost_usd")
        if known and (type(tokens) is not int or tokens < 0 or type(cost) not in (int, float)
                      or not math.isfinite(cost) or cost != tokens * self.price_per_million / 1_000_000):
            raise EmbeddingPersistenceError("Embedding usage mismatch")
        if not known and (tokens is not None or cost is not None):
            raise EmbeddingPersistenceError("Unknown embedding usage cannot be zero")
        complete = len(vectors) == len(claim["input"]["texts"]) and response.get("complete") is True
        status = "done" if complete and known else ("partial_known" if known else "uncertain")
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM prod2_embedding_attempts WHERE attempt_id=%s FOR UPDATE", (claim["attempt_id"],))
            row = cur.fetchone()
            if (not row or str(row["claim_token"]) != claim["claim_token"] or row["request_hash"] != claim["request_hash"]
                    or row["input_json"] != claim["input"]):
                raise EmbeddingPersistenceError("Embedding response owner mismatch")
            normalized = json.loads(_json(response))
            if row["result_json"] is not None:
                if row["result_json"] != normalized:
                    raise EmbeddingPersistenceError("Embedding response is immutable")
                return
            cur.execute("""UPDATE prod2_embedding_attempts SET status=%s,provider_usage_known=%s,
                input_tokens=%s,cost_usd=%s,request_sent=TRUE,result_json=%s::jsonb,completed_at=clock_timestamp()
                WHERE attempt_id=%s""", (status, known, tokens, cost, _json(response), claim["attempt_id"]))
            for index, key in enumerate(claim["input"]["text_sha256"]):
                vector = vectors.get(index)
                cur.execute("""UPDATE prod2_embedding_vectors SET status=%s,vector=%s,response_sha256=%s,
                    completed_at=clock_timestamp() WHERE text_sha256=%s AND model=%s AND dimensions=%s AND attempt_id=%s""",
                    ("done" if vector is not None else status, vector,
                     _sha(response["response_sha256"]) if vector is not None else None,
                     key, self.model, self.dimensions, claim["attempt_id"]))
                if cur.rowcount != 1:
                    raise EmbeddingPersistenceError("Embedding vector ownership changed")

    def _failed(self, claim, error):
        _idle(self.conn)
        sent = error.request_sent
        state = "uncertain" if sent else "failed_pre_send"
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""UPDATE prod2_embedding_attempts SET status=%s,error_code=%s,
                request_sent=%s,provider_usage_known=%s,input_tokens=%s,cost_usd=%s,completed_at=clock_timestamp()
                WHERE attempt_id=%s AND claim_token=%s AND status='running' AND result_json IS NULL""",
                (state, error.code, sent, not sent, None if sent else 0, None if sent else 0,
                 claim["attempt_id"], claim["claim_token"]))
            cur.execute("""UPDATE prod2_embedding_vectors SET status=%s WHERE attempt_id=%s AND status='running'""",
                        (state, claim["attempt_id"]))

    def _recover_owned(self, rows, folder):
        for attempt in sorted({str(row["attempt_id"]) for row in rows.values() if row["status"] in ("running", "uncertain")}):
            stem = Path(folder) / ("embedding-" + attempt)
            claim_path, receipt_path = Path(str(stem) + ".claim.private.json"), Path(str(stem) + ".response.private.json")
            if not claim_path.exists() or not receipt_path.exists():
                continue
            claim, receipt = json.loads(claim_path.read_text()), json.loads(receipt_path.read_text())
            if (claim.get("attempt_id") != attempt or receipt.get("attempt_id") != attempt
                    or receipt.get("request_hash") != claim.get("request_hash")
                    or receipt.get("claim_token") != claim.get("claim_token")):
                raise EmbeddingPersistenceError("Embedding local ownership receipt mismatch")
            self._complete(claim, receipt["response"])

    def ensure_vectors(self, texts: list[str], *, client, folder, before_request=None,
                       before_request_factory=None) -> dict:
        _idle(self.conn)
        if not isinstance(texts, list) or not texts:
            raise EmbeddingPersistenceError("Embedding text list required")
        if (client.model, client.dimensions, client.price_per_million) != (self.model, self.dimensions, self.price_per_million):
            raise EmbeddingPersistenceError("Embedding client/cache contract differs")
        counts = [token_count(t) for t in texts]
        if max(counts) > MAX_INPUT_TOKENS:
            raise EmbeddingPersistenceError("Embedding text exceeds token limit")
        unique = dict(zip([_text_sha(t) for t in texts], texts))
        rows = self._rows(list(unique))
        for key, row in rows.items():
            if row["exact_text"] != unique[key]:
                raise EmbeddingPersistenceError("Embedding exact cache text differs")
        self._recover_owned(rows, folder)
        rows = self._rows(list(unique))
        blocked = sorted({str(row["attempt_id"]) for row in rows.values() if row["status"] not in ("done", "failed_pre_send")})
        if blocked:
            raise EmbeddingBlocked(blocked)
        cached_count = sum(_text_sha(t) in rows and rows[_text_sha(t)]["status"] == "done" for t in texts)
        pending = [text for key, text in unique.items() if key not in rows or rows[key]["status"] != "done"]
        batches, batch, tokens = [], [], 0
        for text in pending:
            count = token_count(text)
            if batch and (len(batch) >= MAX_BATCH_INPUTS or tokens + count > MAX_BATCH_TOKENS):
                batches.append(batch)
                batch, tokens = [], 0
            batch.append(text)
            tokens += count
        if batch:
            batches.append(batch)
        attempts, receipts, cost, new_tokens, unknown_attempts = [], [], 0.0, 0, 0
        for batch in batches:
            batch_identity = {"model": self.model, "dimensions": self.dimensions,
                              "texts": batch, "text_sha256": [_text_sha(t) for t in batch]}
            permission = before_request_factory(_digest(batch_identity)) if before_request_factory else nullcontext(None)
            with permission as issue:
                def dispatch():
                    if issue:
                        issue()
                    if before_request:
                        before_request()
                claim = self._claim(batch)
                if claim is None:
                    continue
                attempt = claim["attempt_id"]
                attempts.append(attempt)
                stem = Path(folder) / ("embedding-" + attempt)
                # Complete exact inputs are already durable in PG. Retain a private
                # owner copy so a received answer can be reconciled after a crash.
                try:
                    _private_write(str(stem) + ".claim.private.json", claim)
                    started = {"provider": "embedding", "attempt_id": attempt, "status": "reserved",
                               "request_hash": claim["request_hash"], "model": self.model, "dimensions": self.dimensions,
                               "usage_known": False, "provider_usage_known": False, "cost_usd": None,
                               "started_at": datetime.now(timezone.utc).isoformat()}
                    _private_write(str(stem) + ".started.private.json", started)
                except Exception:
                    self._failed(claim, EmbeddingRequestError("embedding_receipt_pre_send", request_sent=False))
                    raise EmbeddingPersistenceError("Embedding input receipt unavailable before dispatch") from None
                try:
                    response = client.embed(claim["input"]["texts"], before_request=dispatch)
                except EmbeddingRequestError as exc:
                    self._failed(claim, exc)
                    outcome = {**started, "status": "uncertain" if exc.request_sent else "failed_pre_send",
                               "error_code": exc.code, "request_sent": exc.request_sent,
                               **safe_request_error_facts(exc),
                               "usage_known": not exc.request_sent, "provider_usage_known": not exc.request_sent,
                               "input_tokens": None if exc.request_sent else 0, "cost_usd": None if exc.request_sent else 0}
                    _private_write(str(stem) + ".outcome.private.json", outcome)
                    raise
                except Exception:
                    # An arbitrary injected client failure has no proof of not sending.
                    exc = EmbeddingRequestError("embedding_client_unknown", request_sent=True)
                    self._failed(claim, exc)
                    _private_write(str(stem) + ".outcome.private.json", {**started, "status": "uncertain", "request_sent": True})
                    raise exc from None
                response_receipt = {"attempt_id": attempt, "claim_token": claim["claim_token"],
                                    "request_hash": claim["request_hash"], "response": response}
                receipts.append(_private_write(str(stem) + ".response.private.json", response_receipt))
                self._complete(claim, response)
                outcome = {**started, "status": ("done" if response["complete"] else "partial_known") if response["provider_usage_known"] else "uncertain",
                           "request_sent": True, "usage_known": response["provider_usage_known"],
                           "provider_usage_known": response["provider_usage_known"],
                           "input_tokens": response["input_tokens"], "cost_usd": response["cost_usd"],
                           "response_sha256": response["response_sha256"], "complete": response["complete"]}
                receipts.append(_private_write(str(stem) + ".outcome.private.json", outcome))
                cost += response["cost_usd"] or 0
                new_tokens += response["input_tokens"] or 0
                unknown_attempts += not response["provider_usage_known"]
                if not response["complete"]:
                    raise EmbeddingIncomplete(outcome)
        rows = self._rows(list(unique))
        if any(key not in rows or rows[key]["status"] != "done" for key in unique):
            raise EmbeddingPersistenceError("Embedding committed cache readback failed")
        vectors = [validate_vector(rows[_text_sha(t)]["vector"], self.dimensions) for t in texts]
        return {"vectors": vectors, "known_cost_usd": cost, "new_token_count": new_tokens,
                "cached_count": cached_count, "usage_unknown_attempts": unknown_attempts, "deduplicated_count": len(texts) - len(unique),
                "attempt_ids": attempts, "receipts": receipts, "model": self.model,
                "dimensions": self.dimensions, "vector_text_sha256": [_text_sha(t) for t in texts],
                "pg_verified": True}


def _corpus_chunks(corpus):
    if not isinstance(corpus, dict):
        raise EmbeddingPersistenceError("Corpus object required")
    _sha(corpus.get("corpus_hash"))
    chunks = corpus.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise EmbeddingPersistenceError("Corpus chunks required")
    ids = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str) or not chunk["text"].strip():
            raise EmbeddingPersistenceError("Corpus exact text missing")
        cid = _sha(chunk.get("chunk_id"))
        if cid in ids or _sha(chunk.get("text_sha256")) != _text_sha(chunk["text"]):
            raise EmbeddingPersistenceError("Corpus chunk identity differs")
        ids.add(cid)
    return chunks


def save_corpus(conn, corpus, *, convocation_id, model=MODEL, dimensions=DIMENSIONS):
    _idle(conn)
    if type(convocation_id) is not int or convocation_id <= 0:
        raise EmbeddingPersistenceError("Corpus convocation identity required")
    corpus = {key: value for key, value in corpus.items() if key != "cache_hit"}
    chunks, payload = _corpus_chunks(corpus), _json(corpus)
    digest = sha256(payload.encode("utf-8")).hexdigest()
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""INSERT INTO prod2_embedding_corpora(corpus_hash,convocation_id,payload_sha256,
            corpus_json,chunk_count,vector_model,vector_dimensions) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT(corpus_hash) DO NOTHING""", (corpus["corpus_hash"], convocation_id, digest, payload, len(chunks), model, dimensions))
        cur.execute("SELECT * FROM prod2_embedding_corpora WHERE corpus_hash=%s FOR UPDATE", (corpus["corpus_hash"],))
        row = cur.fetchone()
        if (row["convocation_id"], row["payload_sha256"], row["corpus_json"], row["vector_model"], row["vector_dimensions"]) != (convocation_id, digest, corpus, model, dimensions):
            raise EmbeddingPersistenceError("Persisted corpus is immutable")
        for ordinal, chunk in enumerate(chunks):
            location = {key: value for key, value in chunk.items() if key != "text"}
            cur.execute("""INSERT INTO prod2_embedding_chunks
                (corpus_hash,ordinal,chunk_id,text_sha256,exact_text,location_json) VALUES(%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(corpus_hash,ordinal) DO NOTHING""",
                (corpus["corpus_hash"], ordinal, chunk["chunk_id"], chunk["text_sha256"], chunk["text"], _json(location)))
        _verify_chunks(cur, row)
    # A second transaction checks the committed rows, not just a savepoint.
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM prod2_embedding_corpora WHERE corpus_hash=%s FOR SHARE", (corpus["corpus_hash"],))
        row = cur.fetchone()
        if not row:
            raise EmbeddingPersistenceError("Committed corpus readback missing")
        _verify_chunks(cur, row)
    return {"version": "prod2_embedding_corpus_receipt_v1", "corpus_id": row["corpus_id"], "corpus_hash": corpus["corpus_hash"],
            "corpus_payload_sha256": digest, "chunk_count": len(chunks), "blocks_persisted": True,
            "pg_verified": True, "verified_at": datetime.now(timezone.utc).isoformat()}


def _verify_chunks(cur, row):
    corpus = row["corpus_json"]
    expected = _corpus_chunks(corpus)
    if row["payload_sha256"] != _digest(corpus) or row["chunk_count"] != len(expected):
        raise EmbeddingPersistenceError("Persisted corpus payload changed")
    cur.execute("SELECT * FROM prod2_embedding_chunks WHERE corpus_hash=%s ORDER BY ordinal FOR SHARE", (row["corpus_hash"],))
    chunks = cur.fetchall()
    if len(chunks) != len(expected):
        raise EmbeddingPersistenceError("Persisted corpus chunk count changed")
    for ordinal, (stored, chunk) in enumerate(zip(chunks, expected)):
        location = {key: value for key, value in chunk.items() if key != "text"}
        if (stored["ordinal"], stored["chunk_id"], stored["text_sha256"], stored["exact_text"], stored["location_json"]) != (ordinal, chunk["chunk_id"], chunk["text_sha256"], chunk["text"], location):
            raise EmbeddingPersistenceError("Persisted chunk evidence changed")
    return chunks


def save_retrieval(conn, corpus_hash, retrieval, *, convocation_id):
    _idle(conn)
    _sha(corpus_hash)
    if not isinstance(retrieval, dict) or type(convocation_id) is not int or convocation_id <= 0:
        raise EmbeddingPersistenceError("Retrieval object and convocation required")
    key = _sha(retrieval.get("retrieval_hash"))
    text = retrieval.get("context_text")
    if not isinstance(text, str) or _sha(retrieval.get("context_sha256")) != _text_sha(text):
        raise EmbeddingPersistenceError("Retrieval context hash differs")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM prod2_embedding_corpora WHERE corpus_hash=%s", (corpus_hash,))
        corpus_row = cur.fetchone()
        if not corpus_row or corpus_row["convocation_id"] != convocation_id:
            raise EmbeddingPersistenceError("Retrieval corpus ownership mismatch")
        ids = retrieval.get("selected_chunk_ids")
        known_ids = {chunk["chunk_id"] for chunk in corpus_row["corpus_json"]["chunks"]}
        if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(cid not in known_ids for cid in ids):
            raise EmbeddingPersistenceError("Retrieval chunks outside corpus")
        cur.execute("""INSERT INTO prod2_embedding_retrievals(retrieval_hash,corpus_hash,convocation_id,payload_sha256,retrieval_json)
            VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT(retrieval_hash) DO NOTHING""",
            (key, corpus_hash, convocation_id, _digest(retrieval), _json(retrieval)))
        cur.execute("SELECT * FROM prod2_embedding_retrievals WHERE retrieval_hash=%s", (key,))
        row = cur.fetchone()
        if (row["corpus_hash"], row["convocation_id"], row["retrieval_json"], row["payload_sha256"]) != (corpus_hash, convocation_id, retrieval, _digest(retrieval)):
            raise EmbeddingPersistenceError("Persisted retrieval is immutable")
    return verified_corpus_retrieval(conn, corpus_hash, key)


def verified_corpus_retrieval(conn, corpus_hash, retrieval_hash, *, allow_transaction=False, expected_input=None):
    if not allow_transaction:
        _idle(conn)
    _sha(corpus_hash), _sha(retrieval_hash)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM prod2_embedding_corpora WHERE corpus_hash=%s FOR SHARE", (corpus_hash,))
        corpus_row = cur.fetchone()
        cur.execute("SELECT * FROM prod2_embedding_retrievals WHERE retrieval_hash=%s AND corpus_hash=%s FOR SHARE", (retrieval_hash, corpus_hash))
        retrieval_row = cur.fetchone()
        if not corpus_row or not retrieval_row or corpus_row["convocation_id"] != retrieval_row["convocation_id"]:
            raise EmbeddingPersistenceError("Corpus/retrieval durable relation missing")
        chunks = _verify_chunks(cur, corpus_row)
        retrieval = retrieval_row["retrieval_json"]
        if (retrieval_row["payload_sha256"] != _digest(retrieval)
                or retrieval.get("retrieval_hash") != retrieval_hash
                or retrieval.get("context_sha256") != _text_sha(retrieval.get("context_text", ""))):
            raise EmbeddingPersistenceError("Persisted retrieval evidence changed")
        if expected_input is not None:
            if not isinstance(expected_input, dict):
                raise EmbeddingPersistenceError("Expected retrieval input object required")
            fields = ("route", "analysis_sha256", "page_count", "source_manifest_hash", "corpus_hash",
                      "retrieval_hash", "context_sha256", "context_bytes", "context_tokens", "source_tokens",
                      "selected_chunk_ids", "selected_blocks", "coverage_by_field", "indexed_complete",
                      "citations", "needs_review", "reasons")
            if "source_capture" in retrieval or "source_capture" in expected_input:
                fields += ("source_capture",)
            if any(key not in expected_input or key not in retrieval or expected_input[key] != retrieval[key] for key in fields):
                raise EmbeddingPersistenceError("Sent retrieved evidence differs from durable context")
            if expected_input.get("retrieval_policy_version") != retrieval.get("policy_version"):
                raise EmbeddingPersistenceError("Sent retrieval policy differs from durable context")
        keys = [chunk["text_sha256"] for chunk in chunks]
        cur.execute("""SELECT * FROM prod2_embedding_vectors
            WHERE text_sha256=ANY(%s) AND model=%s AND dimensions=%s AND status='done'""",
            (keys, corpus_row["vector_model"], corpus_row["vector_dimensions"]))
        vectors = {row["text_sha256"]: row for row in cur.fetchall()}
        _attach_attempts(cur, list(vectors.values()))
        # Match completion's lock order: attempt, then vector. Reversing it can
        # deadlock a reader against an in-flight response commit.
        cur.execute("""SELECT text_sha256,attempt_id,exact_text,vector,response_sha256
            FROM prod2_embedding_vectors WHERE text_sha256=ANY(%s) AND model=%s
            AND dimensions=%s AND status='done' FOR SHARE""",
            (keys, corpus_row["vector_model"], corpus_row["vector_dimensions"]))
        locked = {row["text_sha256"]: row for row in cur.fetchall()}
        if set(locked) != set(vectors) or any(any(locked[key][field] != vectors[key][field] for field in
                ("attempt_id", "exact_text", "vector", "response_sha256")) for key in locked):
            raise EmbeddingPersistenceError("Corpus vector changed during readback")
        for chunk in chunks:
            vector = vectors.get(chunk["text_sha256"])
            if not vector or vector["exact_text"] != chunk["exact_text"]:
                raise EmbeddingPersistenceError("Corpus vector durable cache missing")
            _verify_vector_receipt(vector)
    return {"version": "prod2_embedding_durable_proof_v1", "corpus_id": corpus_row["corpus_id"],
            "retrieval_id": retrieval_row["retrieval_id"], "corpus_hash": corpus_hash,
            "retrieval_hash": retrieval_hash, "context_sha256": retrieval["context_sha256"],
            "corpus_payload_sha256": corpus_row["payload_sha256"],
            "retrieval_payload_sha256": retrieval_row["payload_sha256"],
            "chunk_count": len(chunks), "blocks_persisted": True, "vectors_persisted": True,
            "pg_verified": True, "verified_at": datetime.now(timezone.utc).isoformat()}
