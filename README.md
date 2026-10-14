# BuenaPro

Plataforma de inteligencia para contrataciones publicas.

## Estructura

```text
apps/web/          App web en Next.js
workers/seace/     Worker Python para SEACE, PDFs y extraccion IA
packages/shared/   Tipos/esquemas compartidos
infra/docker/      Dockerfiles y compose base
docs/              Documentacion tecnica
```

## Stack inicial

- Next.js + TypeScript para la web.
- PostgreSQL para datos relacionales y JSONB.
- Python para workers de ingesta/extraccion.
- Cloudflare R2 para PDFs preview/documentos.
- Gemini 3.1 Flash-Lite para extracción de TDRs, onboarding y copiloto.
- Docker Compose para desarrollo/despliegue inicial.

## Flujo del worker

```text
poll_seace
  -> fetch_detail
  -> list_files
  -> download_pdf
  -> upload_preview_to_r2
  -> extract_tdr_json
  -> derive_summary
  -> derive_facets
  -> upsert_filter_index
```

## Evolucion multi-fuente

- [Plan integral de ingesta universal](./docs/universal-ingestion-plan.md)
- [Guias tecnicas por plataforma](./docs/sources/README.md)

## Setup local

```bash
cp .env.example .env.local
```

Instalacion de dependencias queda pendiente hasta definir gestor final y entorno.
