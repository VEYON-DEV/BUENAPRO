# BuenaPro

Plataforma de inteligencia para contrataciones publicas.

El [contexto actual del proyecto](./CONTEXTO.md) reúne arquitectura, decisiones,
despliegues registrados, resultados y pendientes para retomar el trabajo.
La [guía completa de flujos y trabajo realizado](./FLUJOS_Y_TRABAJO_BUENAPRO.md)
añade los recorridos LLM1/LLM2, diagramas y la entrega documental a la VM.

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
- Cloudflare R2 para usos auxiliares y archivos históricos existentes; los lectores nuevos analizan originales temporales de SEACE.
- Gemini 3.1 Flash-Lite para extracción de TDRs, onboarding y copiloto.
- Docker Compose para desarrollo/despliegue inicial.

## Lectores de SEACE

Hay dos servicios de ingesta independientes. Ambos guardan sus resultados en
PostgreSQL para la web; la evaluación de capacidad DIN se ejecuta por separado.

```text
Buscador SEACE (PROD2)
  -> búsqueda y selección -> ficha/cronograma -> documentos -> LLM1 -> PostgreSQL

Contratos menores (PROD6)
  -> búsqueda por perfil -> ficha/archivos -> PDF TDR/EETT -> LLM1 -> PostgreSQL
```

La [guía de arquitectura de ambos lectores](./docs/guia-lectores-seace-menores.md)
explica los ciclos automáticos, colas, modelos, filtros y evaluación DIN.
Incluye el estado comprobado el 21/10/2026: ambos servicios preparados pero
detenidos, con activación y validación de acceso pendientes. El mapeador
paralelo de metadatos PROD2 es otra herramienta y no ejecuta lecturas.

## Evolucion multi-fuente

- [Plan integral de ingesta universal](./docs/universal-ingestion-plan.md)
- [Guias tecnicas por plataforma](./docs/sources/README.md)

## Setup local

```bash
cp .env.example .env.local
```

Instalacion de dependencias queda pendiente hasta definir gestor final y entorno.
