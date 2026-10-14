-- Text/vectors are durable derived evidence; no source binaries or credentials.
-- No pgvector extension is required: search is exact within one corpus.
CREATE TABLE prod2_embedding_attempts (
    attempt_id UUID PRIMARY KEY,
    claim_token UUID NOT NULL,
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    dimensions INTEGER NOT NULL CHECK (dimensions BETWEEN 1 AND 3072),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    input_json JSONB NOT NULL CHECK (jsonb_typeof(input_json) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('running','done','partial_known','uncertain','failed_pre_send')),
    input_tokens BIGINT CHECK (input_tokens >= 0),
    cost_usd NUMERIC(20,12) CHECK (cost_usd >= 0),
    provider_usage_known BOOLEAN NOT NULL DEFAULT FALSE,
    request_sent BOOLEAN NOT NULL DEFAULT FALSE,
    result_json JSONB,
    error_code TEXT,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CHECK ((provider_usage_known AND input_tokens IS NOT NULL AND cost_usd IS NOT NULL)
           OR (NOT provider_usage_known AND input_tokens IS NULL AND cost_usd IS NULL))
);
CREATE INDEX prod2_embedding_attempts_request_idx ON prod2_embedding_attempts(request_hash);
CREATE TABLE prod2_embedding_vectors (
    text_sha256 TEXT NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions BETWEEN 1 AND 3072),
    exact_text TEXT NOT NULL CHECK (length(exact_text) > 0),
    token_count INTEGER NOT NULL CHECK (token_count BETWEEN 1 AND 8192),
    status TEXT NOT NULL CHECK (status IN ('running','done','partial_known','uncertain','failed_pre_send')),
    attempt_id UUID NOT NULL REFERENCES prod2_embedding_attempts(attempt_id),
    vector DOUBLE PRECISION[],
    response_sha256 TEXT CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (text_sha256,model,dimensions),
    CHECK ((status='done' AND vector IS NOT NULL AND cardinality(vector)=dimensions AND response_sha256 IS NOT NULL)
           OR (status<>'done' AND vector IS NULL))
);
CREATE INDEX prod2_embedding_vectors_attempt_idx ON prod2_embedding_vectors(attempt_id);
CREATE TABLE prod2_embedding_corpora (
    corpus_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE NOT NULL,
    corpus_hash TEXT PRIMARY KEY CHECK (corpus_hash ~ '^[0-9a-f]{64}$'),
    convocation_id BIGINT NOT NULL CHECK (convocation_id > 0),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    corpus_json JSONB NOT NULL CHECK (jsonb_typeof(corpus_json)='object'),
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    vector_model TEXT NOT NULL,
    vector_dimensions INTEGER NOT NULL CHECK (vector_dimensions BETWEEN 1 AND 3072),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE prod2_embedding_chunks (
    corpus_hash TEXT NOT NULL REFERENCES prod2_embedding_corpora(corpus_hash),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    chunk_id TEXT NOT NULL CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    text_sha256 TEXT NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    exact_text TEXT NOT NULL CHECK (length(exact_text) > 0),
    location_json JSONB NOT NULL CHECK (jsonb_typeof(location_json)='object'),
    PRIMARY KEY (corpus_hash,ordinal),
    UNIQUE (corpus_hash,chunk_id)
);
CREATE TABLE prod2_embedding_retrievals (
    retrieval_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE NOT NULL,
    retrieval_hash TEXT PRIMARY KEY CHECK (retrieval_hash ~ '^[0-9a-f]{64}$'),
    corpus_hash TEXT NOT NULL REFERENCES prod2_embedding_corpora(corpus_hash),
    convocation_id BIGINT NOT NULL CHECK (convocation_id > 0),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    retrieval_json JSONB NOT NULL CHECK (jsonb_typeof(retrieval_json)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
