# Worker SEACE - runbook operativo

Este runbook es para la primera carga controlada del worker. La idea es procesar pocos contratos,
mirar logs y verificar BD antes de activar el scheduler recurrente.

## Alcance MVP

El worker inicial debe consultar solo:

- estado SEACE `2`: vigente
- objeto SEACE `2`: servicio
- segmentos CUBSO configurados en `SEACE_ALLOWED_SEGMENTS`
  - `43`: tecnologia / telecomunicaciones
  - `81`: ingenieria, investigacion y tecnologia
  - `78`: transporte
  - `80`: servicios profesionales amplio para legal

## Servicios

```text
postgres       BD
web            Next.js
worker-io      polling, detalle, archivos, descarga
worker-llm     extraccion TDR con Gemini
worker-match   summary, facets, diff, matching
worker-notify  email, Telegram, in-app
scheduler      encola jobs recurrentes cada 30 min / 6 h / 1 h
```

El `scheduler` esta en profile `scheduler` para no activarlo durante pruebas iniciales.

## Primera prueba controlada

Desde la raiz del proyecto:

```bash
docker compose -f infra/docker/docker-compose.yml up -d postgres
python scripts/migrate.py
docker compose -f infra/docker/docker-compose.yml up -d worker-io worker-llm worker-match worker-notify
```

Encolar solo 5 contratos nuevos/cambiados:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker-io \
  buenapro-worker poll-once --year 2026 --limit 5
```

Si se quiere una muestra mas util, usar `--limit 10` o `--limit 20`.

## Ver logs

```bash
docker compose -f infra/docker/docker-compose.yml logs -f worker-io
docker compose -f infra/docker/docker-compose.yml logs -f worker-llm
docker compose -f infra/docker/docker-compose.yml logs -f worker-match
docker compose -f infra/docker/docker-compose.yml logs -f worker-notify
```

Eventos esperados:

- `poll_search_done`
- `process_contract_done`
- `job_completed`
- eventos `ok` en `pipeline_events`

Si aparece `job_failed`, revisar `last_error` en `worker_jobs`.

## Evaluacion automatica y Telegram

El flujo automatico se ejecuta despues de validar el TDR:

```text
TDR validado -> fit deterministico por perfil -> analyze_match -> alerta in-app/Telegram
```

- El fit usa las lineas, segmentos y keywords del perfil activo; no contiene rubros hardcodeados.
- El nivel preliminar por defecto es `2` (Relacionado).
- La alerta se crea solo si el score final es estrictamente mayor a `50`.
- `50` no notifica; `51` si notifica.
- El resumen, modalidad, fortalezas y riesgo salen de la extraccion y evaluacion existentes. No se hace una llamada LLM adicional para redactar la alerta.
- `SETTINGS_ENCRYPTION_KEY` cifra el token de Telegram almacenado en PostgreSQL.
- `APP_BASE_URL` construye el enlace a la oportunidad incluido en cada mensaje.

Barrido inicial de todas las oportunidades vigentes y validadas para un perfil:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker-match \
  buenapro-worker auto-evaluate-current --profile-id PROFILE_UUID
```

El comando solo encola trabajo. Supervisar las colas `match`, `llm` y `notify` hasta que no queden jobs pendientes. Se puede limitar una prueba con `--limit 5`.

Antes de probar un destinatario personal, ese usuario debe abrir el bot y pulsar **Start**. Un mismo bot admite varios `chat_id` activos desde `/configuracion`.

## Verificar BD

```sql
SELECT status, queue_name, job_type, count(*)
FROM worker_jobs
GROUP BY status, queue_name, job_type
ORDER BY status, queue_name, job_type;

SELECT id_contrato, codigo, estado_codigo, objeto_codigo, cubso_segmento, pipeline_state, last_seen_at
FROM seace_contracts
ORDER BY first_seen_at DESC
LIMIT 20;

SELECT id_contrato, filename, doc_class, has_text_layer, size_original_bytes, size_preview_bytes, r2_preview_key
FROM contract_documents
ORDER BY created_at DESC
LIMIT 20;

SELECT
  count(*) AS docs,
  pg_size_pretty(sum(size_original_bytes)::bigint) AS original_total,
  pg_size_pretty(sum(size_preview_bytes)::bigint) AS preview_total,
  round((1 - sum(size_preview_bytes)::numeric / nullif(sum(size_original_bytes), 0)) * 100, 2) AS saved_pct
FROM contract_documents
WHERE size_original_bytes IS NOT NULL AND size_preview_bytes IS NOT NULL;

SELECT model, prompt_version, schema_version, input_tokens, output_tokens, cost_usd, requires_human_review, quality
FROM tdr_extractions
ORDER BY created_at DESC
LIMIT 20;

SELECT
  count(*) FILTER (WHERE reused_from_extraction_id IS NULL) AS gemini_extractions,
  count(*) FILTER (WHERE reused_from_extraction_id IS NOT NULL) AS reused_extractions,
  COALESCE(sum(cost_usd), 0) AS total_cost_usd
FROM tdr_extractions;

SELECT id_contrato, facet, label, required, is_current
FROM requirement_facets
ORDER BY created_at DESC
LIMIT 50;

SELECT id_contrato, valor_estimado, tipo_pago, plazo_ejecucion_dias, roles_requeridos, facets
FROM contract_filter_index
ORDER BY updated_at DESC
LIMIT 20;

SELECT stage, status, count(*)
FROM pipeline_events
GROUP BY stage, status
ORDER BY stage, status;
```

## Criterio para subir el limite

Subir de `5` a `20` o activar scheduler solo si:

- no hay jobs en `dead`
- los jobs `failed` bajan a cero despues de retry
- los contratos llegaron a `files_listed`, `downloaded`, `extracted` o `matched` segun etapa
- existen documentos para los contratos procesados
- las extracciones guardan tokens/costo o `requires_human_review`
- las facets se generan sin romper el schema

## Batch MVP 450

La corrida recomendada para volumen inicial es:

```text
tecnologia  -> segmentos 43,81 -> 150 contratos nuevos/cambiados
transporte  -> segmento 78     -> 150 contratos nuevos/cambiados
legal       -> segmento 80     -> 150 contratos nuevos/cambiados
```

Desde la UI:

```text
/admin -> Batch MVP -> per bucket 150 -> Iniciar
```

Desde API:

```bash
curl -X POST http://localhost:3000/api/admin/batches/start \
  -H "content-type: application/json" \
  -H "x-internal-token: $INTERNAL_JOBS_TOKEN" \
  -d '{"year":2026,"per_bucket":150}'
```

Ver progreso:

```bash
curl http://localhost:3000/api/admin/batches/status \
  -H "x-internal-token: $INTERNAL_JOBS_TOKEN"
```

El panel muestra:

- contratos vistos
- documentos descargados
- TDRs extraidos
- extracciones reutilizadas por `sha256_original`
- costo acumulado
- fallos/dead
- ETA aproximada basada en throughput de jobs

Para esta corrida mantener inicialmente:

```bash
docker compose -f infra/docker/docker-compose.yml up -d --scale worker-io=5 worker-io
docker compose -f infra/docker/docker-compose.yml up -d --scale worker-llm=3 worker-llm
docker compose -f infra/docker/docker-compose.yml up -d --scale worker-match=2 worker-match
```

No activar `scheduler` para esta prueba; el batch ya encola lo necesario.

## Politica de previews R2

El sistema no recorta paginas: el preview debe conservar el mismo numero de paginas que el PDF original.
Para no perder tiempo en archivos pequenos, la compresion se aplica solo si el PDF pesa al menos:

```bash
PDF_PREVIEW_COMPRESS_MIN_BYTES=1000000
```

Luego se acepta la version comprimida solo si reduce el peso al menos:

```bash
PDF_PREVIEW_MIN_REDUCTION_RATIO=0.15
```

Si el PDF esta por debajo del umbral, si Ghostscript no mejora el peso, o si el preview no valida,
se sube el PDF original como preview. Si el mismo PDF ya existe por `sha256_original`, se reutiliza
el `r2_preview_key` existente y no se vuelve a subir a R2.

Esta politica optimiza tres costos a la vez:

- storage en R2: PDFs grandes se reducen antes de subir
- tiempo de subida: se sube menos peso cuando vale la pena
- CPU del worker: PDFs pequenos no pasan por compresion innecesaria

## Activar scheduler recurrente

Cuando la muestra controlada este bien:

```bash
docker compose -f infra/docker/docker-compose.yml --profile scheduler up -d scheduler
```

Para escalar I/O sin tocar codigo:

```bash
docker compose -f infra/docker/docker-compose.yml up -d --scale worker-io=3 worker-io
```

Mantener `worker-llm=1` al inicio para no golpear limites de Gemini. Subirlo solo cuando haya
metricas claras de costo y rate limit.

## Pausa de emergencia

```bash
docker compose -f infra/docker/docker-compose.yml stop scheduler worker-io worker-llm worker-match worker-notify
```

La cola queda en Postgres. Al volver a levantar workers, siguen desde `worker_jobs`.
