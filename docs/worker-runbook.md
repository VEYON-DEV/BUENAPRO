# Worker SEACE - runbook operativo

Este runbook es para la primera carga controlada del worker. La idea es procesar pocos contratos,
mirar logs y verificar BD antes de activar el scheduler recurrente.

## Alcance MVP

El worker operativo consulta:

- estado SEACE `2`: vigente
- objetos SEACE `1` (Bien) y `2` (Servicio); `3` (Obra) y `4` (Consultoría de Obra) están en el catálogo pero no se barren mientras no se configuren
- segmentos CUBSO configurados en `SEACE_ALLOWED_SEGMENTS`
  - `43`: tecnologia / telecomunicaciones
  - `81`: ingenieria, investigacion y tecnologia
  - `78`: transporte
  - `80`: servicios profesionales amplio para legal

El scheduler crea un job independiente por objeto y segmento, con deduplicación
`poll_search:<año>:<objeto>:<segmento>`. La clave `SEACE_ALLOWED_CODIGO_OBJETO`
selecciona los objetos; no reemplaza los registros de Servicios existentes.

Para cargar Bienes vigentes de tecnología/telecomunicaciones de forma controlada:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker-io \
  buenapro-worker poll-once --objects 1 --segments 43,81 --year 2026 --limit 5
```

Los resultados culminados se guardan por separado del feed. Se filtran por la
fecha real de publicación de los últimos tres años, pues `anio` del buscador
PROD6 no es confiable como filtro. El feed también guarda `anio` derivado de
`fec_publica` en horario de Lima; el año solicitado solo sirve de fallback si
la fuente no publica esa fecha. Reanudar después de una interrupción no
reinicia páginas completadas; `--restart` sí reinicia el checkpoint del par
objeto/segmento.

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker-io \
  buenapro-worker historical-backfill --object 1 --segment 43 --year 2026 --page-size 50
docker compose -f infra/docker/docker-compose.yml run --rm worker-io \
  buenapro-worker historical-backfill --object 1 --segment 81 --year 2026 --page-size 50
```

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

## PROD4: bases oficiales y puntaje final

Tras aplicar las migraciones `0030` y `0031`, el polling PROD4 calcula la afinidad
preliminar por perfil sin Gemini para todos los procesos tecnológicos. El worker
extrae automáticamente **solo** un PDF oficial de bases integradas, bases
administrativas o TDR/EETT por procedimiento con afinidad preliminar de nivel
`2+`. Gemini estructura sus requisitos y después compara el perfil para guardar
el puntaje final en `opportunity_matches`. Si el PDF no contiene requisitos
sustantivos verificables, no se genera veredicto final.

La activación por defecto es `PROD4_DOCUMENT_ANALYSIS_ENABLED=true`. Los límites
por defecto son `PROD4_DOCUMENT_ENQUEUE_LIMIT_PER_POLL=3`,
`PROD4_DOCUMENT_DAILY_LIMIT=10`, `PROD4_PROFILE_EVALUATIONS_DAILY_DEFAULT=10`
y `PROD4_MAX_ANALYSIS_PDF_BYTES=20971520` (20 MiB). Los PDFs grandes o no-PDF
quedan marcados `skipped` con motivo; no se suben a almacenamiento propio. El
poll diario de fichas detecta reemplazos de bases e invalida scores finales
anteriores. El barrido inicial avanza automáticamente en cada poll, no encola
las 72 descargas a la vez.

Comprobación productiva del 2026-09-29: el origen oficial `prod1.seace.gob.pe`
devolvió `403` desde la VM (aunque una descarga local con las mismas cabeceras
respondió `200`). El worker registra `official_document_access_denied`, no
envía ese PDF a Gemini y conserva solamente la afinidad preliminar. Antes de
prometer puntajes finales en producción hay que resolver el acceso oficial a
documentos desde la VM; no usar CAPTCHA ni evadir el bloqueo de origen.

Para adelantar un lote manual sin saltarse los límites diarios:

```bash
docker compose -f infra/docker/docker-compose.yml run --rm worker-io \
  buenapro-worker prod4-document-sweep --limit 10
```

Monitoreo de cobertura/costo:

```sql
SELECT document_analysis_status, document_analysis_reason, count(*)
FROM prod4_processes
WHERE technology_relevant = true AND missing_since IS NULL
GROUP BY 1, 2 ORDER BY 1, 2;

SELECT count(*) AS extracciones, COALESCE(sum(cost_usd), 0) AS usd
FROM prod4_document_extractions;

SELECT count(*) AS puntajes_finales FROM opportunity_matches;
```

Por ahora no se emiten alertas Telegram por `opportunity_matches`; el veredicto
queda en la BD/API. Los documentos superiores a 20 MiB necesitan un flujo
específico de reducción de páginas o subida a Gemini Files antes de obtener
puntaje final; la interfaz debe mantener solo la afinidad preliminar en ellos.

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

## Ingesta PROD6 por perfil (opt-in)

`PROD6_PROFILE_POLL_ENABLED=true` habilita `poll_prod6_profiles` en el mismo ciclo
de 30 minutos del polling convencional. Solo toma perfiles activos con
`identity_json.profile_ingestion_enabled=true`; no modifica el alcance legado.
Los segmentos, frases y términos salen de sus líneas activas en PostgreSQL.

Solo guarda bienes/servicios vigentes publicados en el año consultado y con una
frase configurada o dos términos distintos de la misma línea en la descripción.
El descubrimiento no afirma cumplimiento ni reemplaza leer las bases. Encola
`process_contract` (flujo documental/LLM1), sin ejecutar LLM2 en el poll.
Los IDs oficiales deduplican las oportunidades compartidas entre perfiles.

Por defecto recorre hasta diez páginas de cien filas por objeto/segmento y
guarda hasta doscientos candidatos por perfil/ciclo. `capped_segments` y
`limit_reached` indican cobertura parcial; no hay barrido histórico automático.
También se puede llamar `poll_prod6_profile(..., profile_id=..., anio=2026,
max_pages_per_segment=10, max_candidates=200, batch_id=...)` para una corrida
controlada. El payload del job recurrente admite los mismos límites y batch.

### Decisiones por empresa (`prod6_profile_decisions`)

Cada contrato visto por `poll_prod6_profile` queda con una decisión por empresa,
**aprobada o descartada**, clave `(id_contrato, profile_id, hash de la ficha de búsqueda,
hash del perfil, versión de política)`: con la misma clave no se reevalúa ni se vuelve a
guardar (`decisions_reused`). El prefiltro duro (vigente, bien/servicio, publicado en el
año, no cerrado, segmento CUBSO de la empresa) descarta con motivos estables; las palabras
clave solo se registran en `keyword_lines` y ya no aceptan. Los rechazados y pendientes se
guardan solo en `prod6_profile_decisions` (con su `listing_json` de la búsqueda), sin tocar
`seace_contracts` ni `opportunities`: solo un contrato aceptado se guarda como contrato y se encola.

### Afinidad JEV por empresa (M2, política `prod6_screening_v2`)

Con la política v2 el poll deja todo contrato que pasa el prefiltro como `pending`
(`decided_by='jev'`); nunca acepta. En cada ciclo del runtime, después del descubrimiento
de todas las empresas y antes del procesamiento IO, `prod6/jev_screening.py` puntúa los
pendientes:

- Una sola consulta JEV (`jev-1.13.0`, `POST https://api.typesafe.ai/v1/systemone`) por
  contrato y ficha de búsqueda, con todas las empresas pendientes de ese contrato. El
  `state` lleva `descripcion` (texto del listado + entidad + objeto, máx. 20000 caracteres)
  y `empresas` con un alias estable por empresa (`e1`, `e2`... según el orden de
  `profile_id`) y su descripción y líneas. Preguntas: `dominio`, `informacion`,
  `subcategoria` (taxonomía `SUBCATEGORIES`, editable) y una `afinidad_<alias>` por empresa,
  que puntúa solo contra `state.empresas.<alias>` (sin unión ni suma de capacidades).
  El puntaje 0-4 de JEV se guarda como índice 0-100 (x25).
- `score >= umbral` (60 por defecto) → `accepted`; menor → `rejected`. Siempre se guarda
  (puntaje, umbral, dominio, subcategoría, información, modelo, hash de la solicitud,
  tokens, costo y la respuesta cruda en `result_json`). El costo de la consulta
  (`tokens de entrada x US$0.042 / 1M`) y los tokens se reparten entre las filas del grupo;
  el total queda en `result_json.query_cost_usd`.
- Solo se puntúan filas cuyo `profile_hash` coincide con el perfil actual y que no tengan
  una ficha más nueva (`skipped_stale`: el poll las vuelve a decidir); las cerradas
  (`fec_fin_cotizacion` vencida) se saltan sin gastar (`skipped_closed`).
- Un fallo de JEV (HTTP distinto de 200, error de transporte o respuesta inválida) **nunca
  descarta**: la fila sigue `pending` con `attempts+1` y `last_error` con solo el tipo de
  error (p. ej. `JevHttpError status=503`). Se reintenta en el siguiente ciclo hasta
  `max_attempts=5` (`exhausted` cuenta las agotadas y quedan pendientes para revisión manual).
  Tres fallos seguidos cortan el pase del ciclo para no gastar intentos durante una caída.
- Los contratos aceptados por al menos una empresa se admiten una sola vez
  (`admit_contract`: `upsert_search_item` + `process_contract` si no tiene lectura vigente).
  Si un ciclo cayó entre guardar la decisión y admitir, el siguiente admite lo que quedó
  aceptado y sin contrato. La lectura de TDR con Gemini sigue igual.
- Una decisión `accepted`/`rejected` no se vuelve a consultar: la segunda pasada no llama a JEV.

Runtime (`python -m buenapro_worker.prod6.runtime`):

- `--profile-id` se repite por empresa (`--profile-id <veyon> --profile-id <din>`); con uno
  solo funciona como antes. El primero es el alcance de la cola de jobs (la lectura documental
  es compartida por contrato). Las empresas con `profile_ingestion_enabled=false` se omiten.
- `--max-jev-requests` (200) consultas JEV por ciclo; `--jev-threshold` (60) índice mínimo.
  `--max-candidates` ya no limita nada con la política v2 (el poll no acepta).
- `TYPESAFE_API_KEY` en el entorno del contenedor; fuera de él también se lee de
  `.env.jev.local` en la raíz del repo, como los scripts de PROD2. Sin llave el runtime no
  admite nada nuevo, deja todo `pending`, termina el ciclo con `status: "jev_unavailable"`
  (`jev.reason: missing_api_key`) en `state.json`, sale con código 2 en `--once` y avisa por
  `notify_ops` (Telegram) una vez por transición, y otra al recuperarse.
- `state.json` agrega `profile_ids`, `source_by_profile` (desglose por empresa), `discovery` (rotación) y `jev`
  (`requests`, `accepted`, `rejected`, `failed`, `skipped_closed`, `skipped_stale`,
  `exhausted`, `cost_usd`, `aborted`, `admission`).

#### Ritmo lento, tope por ciclo y pausa ante 403 (PROD6)

Un ciclo real local hizo unas 18 peticiones a PROD6 en tres minutos y el WAF de SEACE devolvió
403 a toda la IP (búsqueda y descargas) durante un buen rato. Producción sale por la misma IP
peruana, así que el runtime va lento y, ante un 403, se detiene mucho tiempo.

- **Ritmo fijo y bajo** (`seace/pacing.py`): un pacer por proceso, compartido por todos los
  `SeaceClient` y todos los hilos, que separa dos peticiones consecutivas (búsqueda, detalle,
  listado de archivos, descarga y cada reintento) al menos `PROD6_MIN_REQUEST_INTERVAL_SECONDS`
  (6 por defecto; `0` lo desactiva). Con `--http-workers`/`--io-workers` mayores a uno el
  intervalo global se mantiene: los hilos solo se reparten las ranuras. **No hay jitter ni
  rotación de IP/User-Agent**: es un ritmo constante y bajo, no una evasión.
- **Una sola búsqueda por ciclo para todas las empresas** (`poll_prod6_profiles`): el ciclo
  recorre una vez los streams (objeto 1 y 2 × segmento) de la **unión** de los segmentos de las
  empresas activas, y cada página alimenta el prefiltro y `record_decision` de cada empresa
  cuyo conjunto de segmentos incluye ese segmento (una empresa sin ese segmento no recibe fila
  de ese ítem). La semántica por empresa no cambia (una decisión por contrato × empresa × hash
  de ficha × hash de perfil × política; un contrato aceptado por varias se admite una vez;
  `--max-candidates` aplica por empresa). `poll_prod6_profile(profile_id=...)` sigue
  existiendo como envoltorio de una sola empresa. `state.json` lleva `source` (contadores
  sumados) y `source_by_profile` (desglose por empresa: `seen`, `decisions_recorded`,
  `decisions_reused`, `prefilter_rejected`, `candidates`, `changed`, `enqueued`, `skipped`,
  `limit_reached`).
- **Tope por ciclo y reserva para IO**: `--max-source-requests` (por defecto
  `PROD6_MAX_SOURCE_REQUESTS` o 40) cuenta las peticiones PROD6 de todo el ciclo y
  `--max-discovery-requests` (24, nunca más que el tope del ciclo) limita las del
  descubrimiento en los ciclos que buscan (ver "Cadencia de la búsqueda"), así que lo que
  resta queda para `process_contract` y descargas; un ciclo sin búsqueda deja las 40 al IO. Debe cubrir
  `--max-pages` (por defecto 5, páginas por stream y ciclo) o se rechaza el arranque. Al llegar
  al límite de descubrimiento la búsqueda se corta sin error (`source.discovery_limit_reached`,
  cobertura `bounded_partial`), conserva las decisiones ya registradas y
  `state.json.discovery` guarda `next_stream_index` (y `next_stream`, `stream_count`,
  `complete`): el siguiente ciclo, incluso tras reiniciar, empieza en ese stream y da la
  vuelta, de modo que ningún stream queda sin recorrer siempre. Un stream cortado a mitad se
  reinicia desde su primera página; tras una pasada completa se vuelve al índice 0. Con
  `--http-workers` mayor a uno, qué streams se cortan puede depender de los tiempos, pero el
  cursor siempre apunta al primero que no terminó.
- **Tope agotado**: el ciclo deja de pedir a PROD6 sin error; los jobs de IO pendientes
  (un contrato necesita hasta 3 peticiones, así que no se inicia uno que no quepa) esperan
  al siguiente ciclo. El estado pasa a `source_budget_reached` y `--once` sale con código 0.
  `state.json` guarda `source_pacing` (intervalo y tope) y `source_requests` (`used`, `max`,
  `budget_reached`).
- **Estimación con VEYON + DIN** (base local, consulta de solo lectura): VEYON SAC activo con
  ingesta tiene los segmentos 43 y 81 (2 × 2 = 4 streams) y DIN AUTOMATIZACION S.A.C. tiene
  23, 32, 39, 41, 43, 72, 73, 81 y 86 (9 × 2 = 18 streams); la unión son **9 segmentos = 18
  streams** (antes 22 búsquedas mínimas por ciclo, con 43 y 81 repetidos). Una pasada completa
  cuesta como mínimo 18 peticiones (una página de 100 por stream) y hasta 90 con `--max-pages 5`;
  con el límite de 24 una pasada cabe en un ciclo si casi todos los streams tienen una sola
  página y, si no, se reparte en varios ciclos con la rotación. Un ciclo completo son hasta
  24 de descubrimiento más hasta 16 de IO (unos 5 contratos nuevos con detalle, dos listados y
  una descarga cada uno, o más si solo hay descargas): 40 peticiones máximas, unos 4 minutos a 6 s.
  En régimen, con pocos contratos nuevos, un ciclo suele quedar en 18 a 30 peticiones.
- **Circuito ante 403/429**: cualquier 403 o 429 de PROD6 (búsqueda, detalle, listado o
  descarga) abre el circuito de la fuente. Nada vuelve a salir hacia PROD6 en ese ciclo (los
  hilos que esperaban su ranura tampoco envían) y el 403/429 nunca se reintenta con tenacity.
  `state.json` guarda `source_blocked_until` (ahora + `PROD6_BLOCK_BACKOFF_SECONDS`, 10800 = 3 h)
  y `source_block` (`status`, `reason`, `since`, `until`, `alerted`); se lee al arrancar, así que
  la pausa sobrevive a reinicios (con `--once` desde cron o con el bucle). Los ciclos
  siguientes, antes de esa hora, **no hacen ninguna petición PROD6** (estado `source_blocked`,
  `--once` sale con 2) pero sí trabajo local: JEV de pendientes, `derive_summary` y lecturas
  cuyo documento ya está descargado. Pasada la hora, el siguiente ciclo es la sonda: si el
  descubrimiento pasa, se cierra el bloqueo; si vuelve un 403, la pausa se renueva. `notify_ops`
  avisa una vez al abrirse el bloqueo (una vez por episodio, aunque la sonda falle de nuevo) y
  otra al recuperarse.
- **Jobs afectados**: el job que recibió el 403/429 y los que el circuito rechazó vuelven a
  `pending` con `run_after = source_blocked_until`, devuelven su intento (`attempts-1`, para
  que el guard de lecturas pagadas no vea un intento) y quedan en `state.source_deferred`; no
  son `manual_review_required` ni fallo del documento. Solo la respuesta 403/429 que recibió la
  petición propia del job suma a `payload.source_block_count`; al tercer bloqueo del mismo job
  (descarga, listado de contrato o relectura) pasa a `manual_review_required` como hoy
  (`blocked_documents`), para no reintentar eternamente un documento prohibido. Los 401/404/410
  de una descarga siguen yendo a revisión manual de inmediato.
- **Variables**: `PROD6_MIN_REQUEST_INTERVAL_SECONDS` (6), `PROD6_MAX_SOURCE_REQUESTS` (40, valor
  por defecto de `--max-source-requests`), `PROD6_DISCOVERY_INTERVAL_SECONDS` (7200, valor por
  defecto de `--discovery-interval-seconds`), `PROD6_BLOCK_BACKOFF_SECONDS` (10800). Se leen del
  entorno del proceso (como las del proxy de salida), no de `.env.local`; un valor inválido
  detiene el arranque antes de abrir la base. El intervalo afecta a todo `SeaceClient`: los
  workers legados que lo usen (p. ej. `schedule-refresh`) también irían a un ritmo de 6 s por
  petición salvo que su entorno lo cambie.

#### Cadencia de la búsqueda (PROD6)

La búsqueda unificada gastaba ~22 de las 40 peticiones de **cada** ciclo (cada 30 min) aunque
casi todo ya estuviera decidido, y a los contratos aprobados por JEV les quedaban ~18 peticiones
para `process_contract` y descargas. Ahora la búsqueda corre solo cuando toca y el resto de los
ciclos entrega todo el tope a IO.

- **`--discovery-interval-seconds`** (por defecto `PROD6_DISCOVERY_INTERVAL_SECONDS` o 7200;
  `0` busca en todos los ciclos, como antes). Un ciclo busca si ocurre cualquiera de:
  1. nunca hubo una vuelta completa (primer arranque, carpeta de recibos nueva o `state.json`
     sin `last_discovery_completed_at`; también el primer ciclo tras actualizar desde una versión
     anterior): `discovery.due=true`, razón `never_completed`;
  2. quedó una vuelta a medias (`discovery.complete=false`: tope de descubrimiento, tope de
     candidatos, 403/429 o presupuesto): continúa en el **siguiente ciclo sin esperar el
     intervalo**, razón `partial_pass_pending`, y solo recorre los streams que le faltan a esa
     vuelta (`discovery.pass_origin_index`, `pass_streams_searched`, `pass_started_at`; el poll
     recibe `max_streams`), de modo que termina donde empezó en vez de repetir lo ya buscado;
  3. la última vuelta completa terminó hace al menos el intervalo (`interval_elapsed`).
  Una vuelta completa es la que recorrió todos los streams, en uno o varios ciclos; al cerrarla
  se guarda `last_discovery_completed_at` (nivel superior de `state.json`, sobrevive a reinicios).
- **Ciclos sin búsqueda**: se salta solo el descubrimiento. El chequeo de salida (egress), la
  puntuación JEV de pendientes y la admisión (no gastan peticiones a PROD6), el IO, las lecturas
  y los resúmenes corren igual. Como la reserva `--max-discovery-requests` solo existe donde se
  busca, el IO puede usar las 40 peticiones (con el pacer de 6 s, unos 4 min). Si el circuito
  estaba abierto y ya venció, la primera petición de IO es la sonda y cierra el bloqueo con su
  aviso de recuperación, igual que lo hacía la búsqueda.
- **`state.json`**: `discovery.due` (si ese ciclo debía buscar), `discovery.skipped_reason`
  (`not_due_until <iso>` cuando no toca, `source_blocked` si tocaba pero el circuito estaba
  abierto, `null` si buscó), `discovery.next_due_at` (el siguiente momento previsto; `null`
  mientras la próxima búsqueda sea simplemente el siguiente ciclo), `discovery.interval_seconds`
  y `source_coverage: "discovery_not_due"` en un ciclo sin búsqueda ni topes. El resto de
  `discovery` (`next_stream_index`, `complete`, ...) no cambia. El estado `source_blocked`, la
  pausa de lecturas Gemini, las alertas y `--once` se comportan como antes; un `--once` sin
  estado previo busca, y con una vuelta reciente solo hace IO/JEV.
- **Granularidad**: el intervalo se evalúa al inicio de cada ciclo, que dura el tiempo de
  trabajo más `--interval-seconds` (30 min), así que con 7200 s la búsqueda real cae cada 4 a 5
  ciclos (~2 h 20 min con ciclos de ~35 min).
- **Estimación de contratos procesables por hora** (tope de peticiones, no incluye la lectura
  Gemini, que no usa PROD6 y sigue en serie). Un contrato nuevo cuesta ~4 peticiones (3 de
  `process_contract` más ~1 descarga de TDR) y hay ~1,7 ciclos por hora:
  - Antes: la búsqueda gastaba ~22 de 40 en cada ciclo, quedaban 18 para IO: ~4,5 contratos por
    ciclo, **~8 contratos/hora**, y ~37 de las ~68 peticiones por hora iban a buscar.
  - Después: de cada 5 ciclos, 4 dan 40 peticiones a IO (10 contratos) y 1 da ~18 (4,5): media
    ~35,6 peticiones, ~8,9 contratos por ciclo, **~15 contratos/hora** (los ciclos sin búsqueda
    solos: ~17/hora), y la búsqueda baja a ~9 peticiones por hora. Es un máximo con cola
    suficiente; con poca cola el ciclo usa menos de 40.
  - Costo: los contratos nuevos se descubren con hasta ~2 h de retraso en vez de ~30 min, y una
    empresa o segmento recién agregado no se busca hasta la siguiente vuelta (o poner el
    intervalo en 0 / borrar `last_discovery_completed_at` para forzarla).

#### Descarga única

`download_file` guarda lo que descargó (solo PDF de TDR/EETT que van a lectura) como temporal
privado en `<receipt_dir>/source-cache/<sha256>` (carpeta 0700, archivo 0600) y `extract_tdr` lo
lee en vez de volver a pedirlo: una petición por documento en lugar de dos. Es un temporal de
análisis, no una copia permanente: al leerse se verifica su sha256 (si falta o no coincide se
borra y entonces sí se descarga, con ritmo y circuito) y el runtime lo elimina cuando la lectura
quedó guardada (después del commit), cuando el job termina sin leer (lectura vigente, contrato
cerrado, intento histórico), cuando el documento cambió de hash o pasa a revisión manual. Si la
lectura falla, la copia se conserva para reanudar. No altera el guard de lecturas pagadas: la
copia solo se consulta después de `recover_before_download`, que sigue decidiendo si un job con
intento previo puede enviar algo; nunca autoriza un nuevo envío pagado.

#### Errores de Gemini, pausa de lecturas y alerta de parada (PROD6)

Una lectura que falla ya no apaga el servicio por cualquier causa. El lector distingue dos casos
(`prod6/reading.py`):

- **Gemini respondió un error HTTP** (`google.genai.errors.APIError`: `ClientError` 4xx o
  `ServerError` 5xx con código). No hay respuesta facturable, así que no se abre el guard: se
  escribe `<key>.provider_error.private.json` (solo código HTTP, palabra de estado de la API como
  `RESOURCE_EXHAUSTED`, fechas y la identidad de la petición; nunca el mensaje del proveedor, que
  puede repetir contenido o llaves), se retira el `.started.private.json` de ese intento y se
  lanza `ProviderRejectedError(code, status)`, distinta de `GuardedReadingError`.
- **Cualquier otra falla tras enviar** (timeout, corte de conexión, `KeyboardInterrupt`/`SystemExit`,
  fallo al escribir la respuesta o el recibo del error) sigue siendo incierta: guard abierto,
  `started` sin respuesta y `GuardedReadingError`. El guard, `started` sin respuesta o una respuesta
  pendiente de revisión siguen bloqueando todo envío nuevo.

Un `started` solo queda resuelto por un recibo `provider_error` válido del mismo intento (mismo
`started_at`; cubre un corte entre ambos pasos). El recibo habilita un nuevo envío de esa misma
petición, también para un job que `recover_before_download` marcaría como solo-reproducción
(`attempts > 1`); al iniciar el nuevo envío el recibo se consume, de modo que si ese intento queda
incierto ya no autoriza otro.

El runtime (`prod6/runtime.py`) trata `ProviderRejectedError` así:

- El job vuelve a `pending` y devuelve su intento (`attempts-1`, como los bloqueos de fuente).
  Se distinguen dos clases de error:
  - **Errores del documento o pasajeros** (400, 404, 413, 422, 5xx y cualquier otro código):
    suman a `payload.provider_error_count`, el job se reprograma para el siguiente ciclo
    (`run_after = ahora + --interval-seconds`), sin pausa global ni alerta. Al **tercero** del mismo
    job pasa a `manual_review_required` (`source_document_block.provider_http_status`), el ciclo
    continúa y no se encolan lecturas nuevas de ese documento.
  - **Errores de la cuenta** (401, 402, 403, 429): no dicen nada del documento, así que **no suman**
    a `provider_error_count` y nunca lo mandan a revisión manual, por muchas veces que se repitan.
    Pausan la lectura como se describe a continuación.
- **401/403, 402 y 429** ("Gemini: llave inválida o sin permiso (401/403)", "Gemini: créditos
  agotados (402)", "Gemini: límite de uso (429)") pausan **todas** las lecturas pagadas durante
  `PROD6_READING_BACKOFF_SECONDS` (3600 por defecto): el job vuelve con `run_after` al fin de la
  pausa, `extract_tdr` no se reclama mientras dure, y búsqueda, JEV, descargas, listados y
  `derive_summary` siguen. `state.json` guarda `reading_pause` (`code`, `status`, `reason`, `since`,
  `until`, `alerted`) y `reading_paused_until`; sobrevive a reinicios. El estado del ciclo es
  `reading_paused` (`--once` sale con 2). `notify_ops` avisa **una vez por episodio**
  con ese motivo legible; pasada la hora, la siguiente
  lectura es la sonda: si vuelve a fallar la pausa se renueva sin otra alerta, y la primera lectura
  en que Gemini respondió (no una reutilizada ni reproducida de recibo) cierra el episodio con
  un aviso de reanudación.
- Reanudar manualmente tras recargar crédito: borrar `reading_pause` y `reading_paused_until` de
  `state.json` con el servicio detenido, o esperar a que termine la pausa.

**Alerta de parada.** Cuando el proceso termina en `attention_required` (lectura incierta, fallo de
un job de IO, fallo del ciclo o fallo de arranque) envía por `notify_ops` el tipo de excepción y el
tipo de job o fase, sin mensajes del proveedor ni datos del documento:
`BuenaPro PROD6: servicio detenido (attention_required): GuardedReadingError en extract_tdr;
requiere revisión.` Se avisa una vez por parada: `state.json` guarda `attention_alert`
(`<error>|<job o fase>`) y un reinicio que se detiene por la misma causa no repite la alerta; un
ciclo que termina sin parada cierra el episodio. Los fallos de arranque (antes de tomar el
candado) no pueden deduplicarse y avisan en cada intento.

**Variable**: `PROD6_READING_BACKOFF_SECONDS` (3600), del entorno del proceso; un valor inválido
detiene el arranque. `pacing.py` ya se copia en la imagen PROD6 y el resto del cambio vive en
`prod6/`, así que `infra/docker/prod6.Dockerfile` no necesita archivos nuevos.

La imagen PROD6 copia todo `prod6/` (incluye `source_cache.py`) y, de fuera de esa carpeta,
`seace/client.py`, `seace/pacing.py`, `seace/egress.py`, `jobs/download_file.py`,
`jobs/process_contract.py`, `jobs/extract_tdr.py` y `jobs/poll_prod6_profile.py`; las
constantes JEV se importan de `prod2/analysis.py` de la imagen base (sin cambios desde el 1.er sync).

## Salida peruana para SEACE (proxy egress)

SEACE bloquea con 403 la IP de la VM. Los workers pueden enviar **solo** el tráfico
a `seace.gob.pe` y `*.seace.gob.pe` por un proxy HTTP en Perú (tinyproxy, solo
`CONNECT` a `*.seace.gob.pe:443`); LLM, Telegram, Redis y el resto salen directo.
Se configura únicamente por entorno (no por `Settings`):

- `SEACE_EGRESS_PROXY`: URL del proxy, p. ej. `http://10.161.181.75:8888`. Vacío = salida directa.
- `SEACE_EGRESS_REQUIRED`: `1`/`true` hace obligatorio el proxy; si falta, el worker
  falla al construir el cliente (`EgressNotConfigured`) en vez de salir por la IP de la VM.
- `TELEGRAM_BOT_TOKEN` + `OPS_TELEGRAM_CHAT_ID`: alerta de operación al caer y al
  recuperarse la salida (una vez por transición). Sin alguno de los dos no se alerta.

Los clientes SEACE ignoran `HTTP_PROXY`/`HTTPS_PROXY` del entorno (`trust_env=False`).
La caída del proxy (`httpx.ProxyError`, o no poder conectar con el proxy) no es un
bloqueo de SEACE y nunca escribe `source_access_blocked` ni cuenta como 403/429:

- **PROD2** (`prod2.service`): antes de cada ciclo abre una conexión TCP al proxy
  (no envía nada a SEACE). Si falla, el estado pasa a `egress_unavailable`, alerta una vez,
  espera `interval_seconds` y repite el chequeo. Si el ejecutor selected termina con
  código `75` (`EGRESS_EXIT_CODE`), el servicio renombra la carpeta parcial del epoch
  a `<epoch>.egress-<ns>` y reintenta con una carpeta nueva; no pasa por el gate. Un 403/429 real
  sigue siendo `source_access_blocked` y `attention_required`.
- **PROD6** (`prod6.runtime`): chequeo previo por ciclo y estado `egress_backoff` (sin contar
  como `source_backoff` ni revisión manual). Los jobs de IO afectados quedan `pending` con
  `run_after` del siguiente ciclo. Un 403/429 de PROD6 abre la pausa de fuente (ver "Ritmo lento,
  tope por ciclo y pausa ante 403"); los 401/404/410 de descargas siguen en revisión manual.

La imagen PROD6 (`infra/docker/prod6.Dockerfile`) copia también `seace/egress.py` y `seace/pacing.py`.

## Pausa de emergencia

```bash
docker compose -f infra/docker/docker-compose.yml stop scheduler worker-io worker-llm worker-match worker-notify
```

La cola queda en Postgres. Al volver a levantar workers, siguen desde `worker_jobs`.
