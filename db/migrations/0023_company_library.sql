CREATE TABLE company_knowledge_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  profile_id UUID REFERENCES company_profiles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'nota',
  title TEXT NOT NULL,
  description TEXT,
  value_text TEXT,
  tags TEXT[] NOT NULL DEFAULT '{}',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  usable_for_applications BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_company_knowledge_items_tenant
  ON company_knowledge_items (tenant_id, updated_at DESC);

CREATE INDEX ix_company_knowledge_items_tags
  ON company_knowledge_items USING GIN (tags);

CREATE TABLE company_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  profile_id UUID REFERENCES company_profiles(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 10485760),
  content BYTEA NOT NULL,
  document_type TEXT NOT NULL DEFAULT 'documento',
  description TEXT,
  tags TEXT[] NOT NULL DEFAULT '{}',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  valid_until DATE,
  amount NUMERIC(14,2),
  entity_name TEXT,
  usable_for_applications BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_company_documents_tenant
  ON company_documents (tenant_id, updated_at DESC);

CREATE INDEX ix_company_documents_tags
  ON company_documents USING GIN (tags);

COMMENT ON TABLE company_knowledge_items IS
  'Memoria empresarial libre y estructurable para postulación: CCI, notas, metodología, consorcios, plantillas y datos reutilizables.';

COMMENT ON TABLE company_documents IS
  'Biblioteca documental tenant-safe del proveedor para sustentar postulaciones y alimentar agentes bajo demanda.';
