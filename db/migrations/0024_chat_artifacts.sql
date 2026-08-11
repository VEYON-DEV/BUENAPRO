-- Documentos generados por el agente y asociados a una respuesta del chat.
-- PostgreSQL conserva la copia durable; el workspace Codex sigue siendo temporal.

CREATE TABLE chat_artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  message_id UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
  mime_type TEXT NOT NULL CHECK (mime_type IN (
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  )),
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 15728640),
  content BYTEA NOT NULL CHECK (octet_length(content) = size_bytes),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, filename)
);

CREATE INDEX ix_chat_artifacts_message
  ON chat_artifacts (message_id, created_at, id);
CREATE INDEX ix_chat_artifacts_tenant_session
  ON chat_artifacts (tenant_id, session_id, created_at DESC);
