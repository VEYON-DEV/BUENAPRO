-- Separate downloadable resources from official stage publication rows.
-- Requires 0036; it neither copies file bytes nor modifies download history.
CREATE TABLE IF NOT EXISTS prod2_document_events (
  id_convocatoria BIGINT NOT NULL,
  codigo_alfresco UUID NOT NULL,
  event_hash TEXT NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
  filename TEXT NOT NULL,
  seace_version TEXT NOT NULL CHECK (seace_version = '3'),
  document_type TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  published_at TIMESTAMPTZ,
  published_at_text TEXT NOT NULL,
  is_listed BOOLEAN NOT NULL DEFAULT true,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (id_convocatoria, codigo_alfresco, event_hash),
  FOREIGN KEY (id_convocatoria, codigo_alfresco)
    REFERENCES prod2_documents(id_convocatoria, codigo_alfresco) ON DELETE CASCADE,
  CHECK (last_seen_at >= first_seen_at),
  CHECK (event_hash = encode(sha256(convert_to(
    'prod2_document_event_v1:'
    || octet_length(codigo_alfresco::text)::text || ':' || codigo_alfresco::text
    || octet_length(seace_version)::text || ':' || seace_version
    || octet_length(filename)::text || ':' || filename
    || octet_length(stage_name)::text || ':' || stage_name
    || octet_length(document_type)::text || ':' || document_type
    || octet_length(published_at_text)::text || ':' || published_at_text,
    'UTF8')), 'hex'))
);
CREATE INDEX IF NOT EXISTS ix_prod2_document_events_listed
  ON prod2_document_events(id_convocatoria, is_listed);

-- Preserve only the single publication actually recorded by the legacy model.
-- Do not invent earlier stage history or replace the original ficha hash.
INSERT INTO prod2_document_events(id_convocatoria,codigo_alfresco,event_hash,
  filename,seace_version,document_type,stage_name,published_at,published_at_text,
  is_listed,first_seen_at,last_seen_at)
SELECT id_convocatoria,codigo_alfresco,
  encode(sha256(convert_to(
    'prod2_document_event_v1:'
    || octet_length(codigo_alfresco::text)::text || ':' || codigo_alfresco::text
    || '1:3'
    || octet_length(name)::text || ':' || name
    || octet_length(stage_name)::text || ':' || stage_name
    || octet_length(document_type)::text || ':' || document_type
    || octet_length(published_at_text)::text || ':' || published_at_text,
    'UTF8')), 'hex'),
  name,'3',document_type,stage_name,published_at,published_at_text,
  is_listed,first_seen_at,last_seen_at
FROM prod2_documents
ON CONFLICT(id_convocatoria,codigo_alfresco,event_hash) DO NOTHING;

COMMENT ON TABLE prod2_document_events IS
  'Official publication metadata per resource and stage/date; complete inventories reconcile listed events, partial captures never unlist unseen events. Downloaded revision hashes/storage references remain in prod2_document_downloads; file contents are not stored in these metadata tables.';
