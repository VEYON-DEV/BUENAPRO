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

El hash de la ficha (`listing_hash` en `prod6/screening.py`, también usado en el `dedup_key` de
`process_contract`) excluye los campos volátiles de `VOLATILE_LISTING_FIELDS` (hoy `secuencia`,
la posición del contrato en la lista de resultados, que cambia cada vez que se publican
otros): un contrato con el mismo contenido conserva su decisión y JEV no vuelve a puntuarlo.
Las filas guardadas antes de este cambio llevan el hash de la ficha completa. Al registrar la
primera decisión con el hash estable, `record_decision` busca en esas filas (mismo contrato,
empresa, hash del perfil y política) la primera decisión de JEV (`accepted` o `rejected`,
menor `id`) cuya ficha, sin `secuencia`, sea la misma, y la copia a la fila nueva (puntaje,
umbral, clasificación y `result_json` con `inherited_from` = id de la fila origen; tokens y
costo quedan vacíos porque la copia no se pagó). Las filas de otro perfil se
ignoran, las antiguas no se modifican, y si no hay ninguna el contrato queda `pending` como
siempre.

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

#### Calibración de JEV (M2b)

- Anclaje por línea: `company_payload` envía a JEV, por línea, el nombre y hasta 12 frases/términos normalizados (frases y términos intercalados), con tope de 120 por empresa repartido por turnos entre líneas. El `profile_hash` ya cubre esas palabras. Costo estimado: +~1 450 tokens por consulta con ambas empresas (~US$ 0,00006). Cambiar `SUBCATEGORIES`, `QUESTION_INSTRUCTIONS` o el anclaje cambia `request_hash` solo de las consultas nuevas; las decisiones guardadas conservan el suyo.
- Subcategoría `servicios_informacion` (suscripciones o acceso a bases de datos, información crediticia o comercial, consultas): no es software; `licencias_software` la excluye. La subcategoría es descriptiva y no modifica por sí sola el puntaje.
- Estado `superseded` (migración 0049, idempotente): una fila `pending` con una fila más nueva para el mismo contrato y empresa (otra ficha u otro perfil) pasa a `superseded`. Lo hace el poll al registrar una fila nueva y `score_pending` al inicio de cada pasada (`stats["superseded"]`). Nunca toca `accepted` ni `rejected`. Si la ficha o el perfil vuelve a un hash anterior, `record_decision` revive esa fila con la decisión nueva. La web excluye `superseded`. Aplicar 0049 antes de desplegar este código.
- El umbral sigue en 60. Simulación sobre 220 decisiones reales (25/10/2026): 55 sumaría 3 contratos eléctricos de DIN; 65 quitaría 2 de DIN y 1 de VEYON; "65 si confianza de dominio < 0,9" solo quitaría uno de DIN.

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

#### Búsqueda nacional por palabras clave (PROD6, desde el 30/10/2026)

Los segmentos CUBSO de las líneas siguen siendo la base: todo lo vigente de esos segmentos pasa a
JEV como antes. El runtime agrega al final de la lista dos streams sin segmento (`KEYWORD_SEGMENT`,
`0`), uno por objeto, que recorren todo SEACE vigente del año (el 30/10/2026: 1090 bienes y 1542
servicios, 27 páginas de 100).

- Ahí un contrato solo llega a la decisión de una empresa si su título nombra una línea de negocio
  de esa empresa (una frase o dos términos de la misma línea, `line_matches`), sin mirar segmentos.
  Lo que no nombra ninguna no recibe decisión (`keyword_misses`): si es de un segmento del perfil,
  lo decide su propio stream.
- Lo ya decidido por su segmento se reutiliza (`decisions_reused`). Lo nuevo se guarda con
  `cubso_segmento = '0'` (la búsqueda sin segmento no lo informa) y `keyword_lines`.
- Estos streams leen hasta `max_keyword_pages` páginas, igual al tope de descubrimiento del ciclo
  (24), para que una pasada siempre pueda terminar uno. Con los 18 de segmento, una vuelta son
  ~49 peticiones: se reparte en 2 o 3 ciclos seguidos (`partial_pass_pending`).
- Los streams de segmento también leen hasta ese tope por defecto (`--max-pages` sin valor; antes
  5 páginas de 100), así que un segmento que crezca no se corta.
- La evaluación (LLM2) ata un contrato con segmento `0` a la línea que nombró su título; las listas
  de menores de la web (`/api/contracts` y `/api/feed`) lo muestran a la empresa cuyo JEV lo aceptó.
- Imagen `buenapro-worker-prod6:allkw-c30ad0c` (sobre `shared-gate-9afb7f8`); volver atrás es
  restaurar `prod6-mvp.env.bak-<sello>` (sello en `~/buenapro-releases/allkw-c30ad0c-prod6-backup-stamp`)
  y repetir el `up` de `deploy.sh`.

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
  Se distinguen tres clases de error:
  - **Errores del documento** (400, 404, 413, 422 y cualquier otro 4xx que no sea de cuenta):
    suman a `payload.provider_error_count`, el job se reprograma para el siguiente ciclo
    (`run_after = ahora + --interval-seconds`), sin pausa global ni alerta. Al **tercero** del mismo
    job pasa a `manual_review_required` (`source_document_block.provider_http_status`), el ciclo
    continúa y no se encolan lecturas nuevas de ese documento.
  - **Errores de la cuenta** (401, 402, 403, 429): no dicen nada del documento, así que **no suman**
    a `provider_error_count` y nunca lo mandan a revisión manual, por muchas veces que se repitan.
    Pausan la lectura como se describe a continuación.
  - **Saturación del proveedor** (500, 502, 503, 504 y 529): pasajera y ajena al documento (en una
    prueba real Gemini devolvió 503 `UNAVAILABLE` en 5 de 7 intentos), así que tampoco **suma** a
    `provider_error_count` ni manda el documento a revisión manual. Aplica una pausa **corta** de
    lecturas pagadas para no martillar al proveedor; ver más abajo.
- **401/403, 402 y 429** ("Gemini: llave inválida o sin permiso (401/403)", "Gemini: créditos
  agotados (402)", "Gemini: límite de uso (429)") pausan **todas** las lecturas pagadas durante
  `PROD6_READING_BACKOFF_SECONDS` (3600 por defecto): el job vuelve con `run_after` al fin de la
  pausa, `extract_tdr` no se reclama mientras dure, y búsqueda, JEV, descargas, listados y
  `derive_summary` siguen. `state.json` guarda `reading_pause` (`kind`, `code`, `status`, `reason`,
  `since`, `until`, `alerted`) y `reading_paused_until`; sobrevive a reinicios. El estado del ciclo es
  `reading_paused` (`--once` sale con 2). `notify_ops` avisa **una vez por episodio**
  con ese motivo legible; pasada la hora, la siguiente
  lectura es la sonda: si vuelve a fallar la pausa se renueva sin otra alerta, y la primera lectura
  en que Gemini respondió (no una reutilizada ni reproducida de recibo) cierra el episodio con
  un aviso de reanudación.
- **500/502/503/504/529** ("Gemini saturado (503)") usan el mismo mecanismo `reading_pause` (`kind`
  `transient`) pero con `PROD6_READING_TRANSIENT_BACKOFF_SECONDS` (900 por defecto), y **sin alerta**
  en la primera ocurrencia: una saturación breve se resuelve sola. Cada sonda fallida renueva la
  pausa; si el episodio sigue renovándose de forma continua por **más de 2 horas** (`since` es el
  inicio del episodio) se envía **una** alerta ("Gemini saturado desde hace >2 h") y el campo
  `alerted` pasa a `true`; el aviso de reanudación solo se envía si hubo alerta, y la primera lectura
  en que Gemini respondió cierra el episodio. Una pausa corta que venció sin sonda durante más de
  el doble de `max(pausa corta, --interval-seconds)` no cuenta como continua y el siguiente fallo
  abre un episodio nuevo. **Una pausa de cuenta vigente manda**: la saturación no la acorta ni la
  reemplaza; si la sonda tras vencer una pausa de cuenta recibe saturación, el episodio continúa como
  corto conservando su alerta ya enviada (y su aviso de reanudación), y un error de cuenta durante
  una saturación pasa a la pausa de cuenta con su alerta. Un `reading_pause` guardado sin `kind` (de
  antes de este cambio) se lee como pausa de cuenta.
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

**Variables**: `PROD6_READING_BACKOFF_SECONDS` (3600, errores de cuenta) y
`PROD6_READING_TRANSIENT_BACKOFF_SECONDS` (900, saturación), del entorno del proceso; un valor
inválido detiene el arranque. `pacing.py` ya se copia en la imagen PROD6 y el resto del cambio vive en
`prod6/`, así que `infra/docker/prod6.Dockerfile` no necesita archivos nuevos.

#### Calidad de las lecturas PROD6: revisión determinista, modalidad, plazo y documento (PROD6)

Origen: la revisión de las 23 primeras lecturas reales (25/10/2026). Las 23 salían con
`quality='auto'` y `requires_human_review=false` aunque cinco se contradecían (NAS de GRTCLL con
título 2.2 GHz/8 GB y especificaciones 1.7 GHz/4 GB; CEPLAN con pago mensual como "armadas" y 1 de
12 cuotas; SEDAPAR con `plazo_dias` nulo y entrega de 20 días; AGROMERCADO con plazo 375 sumado;
SEAL con experiencia en USD y moneda PEN), `modalidad` era siempre `no_indicada` y los bienes no
tenían plazo. Todo se corrige sin nuevas lecturas pagadas.

**Reglas de revisión (`prod6/review.py`, sin LLM).** `derive_summary_job` las aplica a cada lectura
y guarda en `summary_json`: `review_reasons` (lista de `{code, message}`; código estable en inglés,
mensaje corto en español), `review_rules_version` (`reading_review_v1`) y `review_inherited`.
`requires_human_review = hay razones OR review_inherited`. Códigos:

| Código | Cuándo |
| --- | --- |
| `term_missing` | `contract.plazo_dias` vacío pero `plazo_texto` o `goods.entrega` indican un plazo |
| `term_conflict` | el plazo del contrato y el de la entrega difieren |
| `term_not_literal` | `plazo_dias` no aparece en el texto del plazo ni como meses/semanas/años (suma o cálculo) |
| `payment_count_mismatch` | el documento declara N pagos (`numero_pagos_declarado` o "doce (12) armadas") y hay otra cantidad de filas |
| `payment_percent_sum` | los porcentajes de las filas no suman 100 (±1) |
| `payment_type_incoherent` | `pago_unico` con varias filas, `armadas` con menos de dos, no mensual con texto de pagos mensuales, o mensual con "pago único" |
| `experience_currency_mismatch` | la moneda de un requisito de experiencia (campo `moneda` o símbolo en el texto: S/, US$, $, USD, €) no es la del contrato |
| `low_evidence` | más de la mitad de los requisitos (sin contar RUC/RNP/CCI/impedimentos; con el prompt nuevo también personal, equipos y seguros) no trae cita textual; mínimo 2 requisitos |
| `title_spec_mismatch` | cifras con unidad del título (GHz, GB/TB/MB, HP, kVA, kW, kV, núcleos) disjuntas de las de `goods.items` (solo bienes) |
| `title_quantity_mismatch` | cantidad "DOS (02)" del título distinta de las cantidades de `goods.items`, su suma o su número |
| `title_model_missing` | un modelo del título (letras y dígitos, p. ej. TS4300) no aparece en la lectura (solo bienes) |
| `review_rule_failed` | una regla no pudo evaluar la forma de la lectura |

`quality` no cambia: sus valores (`auto`/`reviewed`/`corrected`/`failed`) los restringe la base y
significan resultado de revisión humana o fallo, no confianza. La señal es `requires_human_review`
(la cola de administración lista `requires_human_review = true`). Un indicador puesto antes por la
extracción (bienes sin items, modelo de respaldo, clasificación comercial sin respaldo) se conserva
como `review_inherited` y solo lo limpia una persona; una lectura con `quality` `reviewed`/`corrected`
conserva el indicador que dejó la persona. **Efecto:** una lectura marcada queda fuera de LLM2
(`analyze_match` y `matching.readiness` la rechazan) hasta que alguien la revise. Los códigos se
consultan con `summary_json->'review_reasons'`.

No se detecta de forma determinista que el objeto describa mal el producto cuando el título y el
cuerpo coinciden en cifras (caso SBS-530, "Audit PLUS" frente a "ADAudit Plus"): el prompt nuevo pide
que `contract.objeto` salga del cuerpo y que las contradicciones se anoten en `observaciones_clave`.

**Modalidad y plazo (`normalization/facets.py`).** `modalidad` ya no depende de
`contract.modalidad` (el prompt ahí pone el sistema de contratación, "Suma alzada"): decide, en
orden, `contract.forma_ejecucion` (campo nuevo), `contract.modalidad` si nombra una forma,
`contract.lugar_ejecucion`, `execution.lugar` y `goods.entrega.lugar`. Remoto/virtual y presencial a
la vez o "híbrido" es `mixta`; un lugar con local, oficina, sede, almacén o dirección es `presencial`;
un nombre propio sin lugar queda `no_indicada`. `plazo_ejecucion_dias` lee además
`goods.entrega.plazo_dias` y `plazo_texto` de contrato y entrega.

**Recalcular lecturas ya guardadas, sin Gemini** (`prod6/recompute.py`; solo lee
`raw_extraction_json` y el título, no encola nada, no crea filas de `contract_filter_index`, no
toca la lectura cruda; idempotente; dry-run por defecto):

```bash
cd workers/seace
DATABASE_URL=... python -m buenapro_worker.prod6.recompute --since 2026-10-25               # dry-run
DATABASE_URL=... python -m buenapro_worker.prod6.recompute --since 2026-10-25 --apply
DATABASE_URL=... python -m buenapro_worker.prod6.recompute --id 2298 --id 2303 --apply
```

Selectores: `--id` (repetible), `--since` (UTC) o `--all`. Imprime JSON con lecturas cambiadas,
marcadas (`newly_flagged`, `no_longer_flagged`), razones por código y conteo de `modalidad`.
Aplicado en la base local sobre las 23 lecturas (25/10/2026): 5 pasan a revisión, `modalidad` deja
de ser `no_indicada` en 22, y `plazo_ejecucion_dias` se completa en SEDAPAR (20). **No correr
`--all` en producción sin revisar el dry-run**: sobre las 2 100 lecturas vigentes de la copia local
marca 347 (200 por `low_evidence`, porque las lecturas con el prompt antiguo traían la cita como
opcional) y la marca saca esas lecturas de LLM2.

**Prompt nuevo y recibos (`prod6/reading.py`).** El prompt y el esquema pasan a una *generación*
de lectura: `prod6_quality_v1` (prompts `prompts/prod6_tdr_reading_v1.txt` y
`prod6_eett_reading_v1.txt`, versiones `prod6_tdr_reading_v1`/`prod6_eett_reading_v1`, esquemas
`prod6_*_schema_v1`). Los prompts antiguos (`tdr_extraction_v2.txt`, `eett_extraction_v1.txt`) no se
editan: su hash forma parte de la clave de cada recibo. Una lectura nueva usa la generación nueva
(la identidad añade `reading_generation`). Un documento que ya tiene `response` o `result` de la
generación antigua se reproduce localmente sin enviar nada; un `started` sin respuesta de cualquier
generación sigue bloqueando todo envío nuevo; un `provider_error` antiguo permite un reintento con el
prompt nuevo (no hubo respuesta facturable). El marcador `reading-policy.private.json` no cambia y
`reuse_prompt_versions` busca primero el prompt nuevo y luego los antiguos (hasta cuatro versiones
con la taxonomía comercial), así que no se paga de nuevo un documento ya leído con otro prompt.
Reglas nuevas del prompt: cita literal breve y página por requisito (`evidencia`, `pagina`, también
en personal, equipos y seguros), `moneda` por requisito con monto (PEN/USD/EUR, sin convertir),
`plazo_dias` solo si el documento lo da en días (nada de sumas), pago mensual es `mensual`
(`numero_pagos_declarado`, `armadas` vacío), `forma_ejecucion` (remota/presencial/mixta) separada de
`modalidad` (sistema de contratación), sin expandir siglas ni completar datos ausentes. Los campos
nuevos son opcionales y tolerantes (una página o moneda inválida queda `null`, nunca bloquea una
respuesta pagada), y los consumidores de los campos anteriores no cambian.

**Documento a leer (`prod6/document_selection.py`, `jobs/process_contract.py`, `documents/pdf.py`).**
Un PDF de categoría 1 se lee como siempre. Sin PDF de categoría 1 (RAR/ZIP, Word), si un anexo PDF
tiene nombre de TDR/EETT/términos de referencia/especificaciones técnicas/requerimiento y no es un
papel del postor (declaración jurada, cumplimiento, anexo, formato, carta, CCI, oferta...), ese PDF se
descarga y se lee; los dos casos reales: `CM-177-2026-CM-UE008` (el RAR `REQUERIMIENTO.rar` no se
lee; se elige `EETT - SERVIDOR  UE008 v2 F .pdf`, categoría 2) y `CM-5-2026-EPSEL S.A.` (su PDF
`TDR -ADQUISICIÓN DE DOS (02) VARIADORES...` ya era el elegido; no se leyó porque el contrato cerró
su cotización antes de la descarga, `closed_preserved`, y su `doc_class` quedó NULL; al volver a listar
un contrato elegible el listado le pone clase `tdr`/`eett` según el objeto nativo, pero un contrato
cerrado ya no se procesa). El listado también clasifica los anexos por
nombre (antes todo era `anexo`/`otro`) y mejora `otro` a `eett`/`tdr` al volver a listar. Un RAR/ZIP
principal ya no se rotula `eett`/`tdr` (queda `otro`) y no se vuelve a descargar en cada ciclo.
**RAR/ZIP/7z no se abren**: `prod2/archive_inventory.py` inventaría un archivo con UUID padre y SHA
congelados y devuelve miembros ligados a ese inventario, mientras que una lectura PROD6, sus recibos
y `contract_documents.sha256_original` se ligan al SHA del archivo que lista SEACE; leer un miembro
exige su propia fila, identidad y limpieza, y eso es un diseño aparte. Si no hay PDF legible queda una
**revisión manual explícita**: el resultado del job `process_contract` trae `manual_review`
(`primary_document_is_archive`, `primary_document_not_pdf`, `no_primary_document` o
`ambiguous_secondary_documents`, con mensaje y nombres de archivo) y `contract_documents.raw_file_json`
de los archivos principales guarda `_reading_review` (el anexo elegido guarda `_reading`:
`attachment_is_requirement`). Consulta: `SELECT id_contrato, filename, raw_file_json->'_reading_review'
FROM contract_documents WHERE raw_file_json ? '_reading_review'`. En la copia local: 18 contratos
tendrían anexo PDF como requerimiento, 17 quedan en revisión por archivo comprimido y 12 por
documento no PDF.

**Despliegue.** La imagen PROD6 (`infra/docker/prod6.Dockerfile`) copia ahora también
`jobs/derive_summary.py`, `normalization/facets.py`, `documents/pdf.py` y los dos prompts nuevos a
`/app/workers/seace/prompts/`; `reading_schema.py`, `review.py`, `recompute.py` y
`document_selection.py` van dentro de `prod6/`. Verificar contra la imagen base que esos tres
módulos no difieran en otro aspecto antes de construir. Sin migración. Nada de esto autoriza
reiniciar workers, relecturas ni llamadas pagadas.

La imagen PROD6 copia todo `prod6/` (incluye `source_cache.py`) y, de fuera de esa carpeta,
`seace/client.py`, `seace/pacing.py`, `seace/egress.py`, `jobs/download_file.py`,
`jobs/process_contract.py`, `jobs/extract_tdr.py`, `jobs/poll_prod6_profile.py`, `jobs/derive_summary.py`,
`jobs/analyze_match.py`, `matching/analyzer.py`, `normalization/facets.py`, `documents/pdf.py` y los prompts `prod6_*_reading_v1.txt`; las
constantes JEV se importan de `prod2/analysis.py` de la imagen base (sin cambios desde el 1.er sync).

#### Evaluación automática (LLM2) de los aceptados por JEV (PROD6)

Desde 25/10/2026 el runtime PROD6 evalúa solo lo que JEV aceptó. Antes (flujo legacy) la evaluación
se encolaba desde `route_contract_profiles` con un filtro de palabras clave; ese router sigue
suprimido (`suppressed.route_contract_profiles`) y el filtro ahora es la decisión JEV.

**Qué se evalúa.** Un par (contrato, empresa del runtime) se encola como `analyze_match` (cola `llm`,
prioridad 4, `source=automatic`, `origin=prod6_jev_accepted_v1`) solo si se cumple todo:

1. La decisión vigente de esa empresa para ese contrato en `prod6_profile_decisions` (la fila más
   nueva de la política `prod6_screening_v2` que no esté `superseded`) es `accepted` y `decided_by='jev'`
   (puntaje >= umbral 60 al decidir). `rejected`, `pending`, `superseded` y aceptaciones de otro origen
   no se evalúan; el cliente puede evaluar a mano desde la web, que no cambia.
2. `automation_rules` de la empresa: `enabled` (sin fila de reglas se asume habilitada, como en el flujo
   legacy), `max_daily_evaluations` (0 = sin límite; cuenta los jobs `analyze_match` automáticos creados
   hoy en UTC para esa empresa, enviados o no) y `min_hours_before_close` (no evalúa si faltan menos horas;
   sin fecha de cierre no aplica). `min_fit_level` ya no se usa: lo reemplaza JEV.
3. Contrato vigente (estado 2, bien o servicio, cierre futuro o sin cierre).
4. Lectura vigente (la última extracción `is_current` del contrato, la misma que usa `analyze_match`)
   con `quality<>'failed'`, **sin `requires_human_review`** y con requisitos (`requirement_facets`).
   Las lecturas en revisión no se evalúan, igual que antes.
5. Sin match de esa empresa para esa lectura (`matches.breakdown_json.meta.extraction_id` igual a la
   extracción vigente). Si el match viene de una lectura anterior (el documento cambió) se vuelve a
   evaluar desde el gancho; un match de origen desconocido (por ejemplo las recomendaciones de PROD2 de
   DIN) nunca se sobrescribe.

**Cuándo se encola.** (a) Al terminar `derive_summary` de un contrato sin `requires_human_review`
(`state.json.last_outcome.result.evaluations` resume candidatos, encolados, ya encolados, limitados por
día y motivos de descarte). (b) Con el backfill descrito abajo. No hay barrido periódico: un par que el
límite diario dejó fuera, o una empresa activada después, se recupera ejecutando el backfill. La
deduplicación es `prod6-only-v1:<perfil de alcance>:analyze_match:<empresa>:<contrato>` mientras el job
esté `pending` o `claimed`; un job en revisión manual (ver abajo) sigue `pending` y bloquea otro igual.
`ScopedRepository.enqueue` rechaza cualquier otro `analyze_match` (por ejemplo el que `derive_summary`
encola cuando cambian los requisitos) y cualquier empresa fuera de `--profile-id`.

**Ejecución.** `analyze_match` lo reclama solo el coordinador, en serie, junto con `extract_tdr` (una
llamada pagada a la vez); nunca el ejecutor de IO libre. Antes de pagar vuelve a comprobar gratis la
decisión, las reglas, la lectura y el match: un job que esperó en la cola y dejó de calificar termina
como `skipped` con el motivo. No llama a SEACE (solo PostgreSQL y Gemini) y no consume el tope de
peticiones a PROD6. Guarda el match con `source=automatic` (`meta.execution=worker_auto`,
`match_events.event_type=automatic_analysis`) más `meta.evaluation_origin`, `jev_decision_id` y
`jev_score`, con tokens y costo reales. **No crea notificaciones**: `analyze_match_job(...,
notifications=False)` no inserta filas en `notifications` ni jobs `send_notification` (en el flujo legacy
sí lo hacía, aun con `send_notification` suprimido).

**Errores de Gemini.** Mismo trato que las lecturas (ver "Errores de Gemini, pausa de lecturas y alerta de
parada"), con la misma `reading_pause` compartida: 401/402/403/429 pausan todas las llamadas pagadas
(lecturas y evaluaciones) con alerta una vez; 500/502/503/504/529 pausan unos minutos sin penalizar ni
alertar; los demás 4xx suman a `provider_error_count` del job y al tercero pasa a revisión manual
(`source_document_block`, mensaje "Gemini evaluation HTTP ..."); un fallo incierto deja
`attention_required` y alerta una vez (`<error> en analyze_match`). La primera respuesta real de Gemini,
de una lectura o de una evaluación, cierra el episodio de pausa.

**Pago incierto.** `analyze_match` no tenía recibos. `prod6/evaluation_guard.py` (`GuardedMatchAnalyzer`)
aplica el contrato de `reading.py` a su única llamada, con recibos privados en
`<receipt-dir>/evaluations/` (0700/0600; solo la respuesta JSON del modelo y los tokens, nunca la petición
ni el perfil):

- `<clave>.started.private.json` antes de enviar; la clave es el hash del modelo, el texto del prompt y el
  payload exacto (otro perfil, otros requisitos u otro prompt son otra petición).
- Un error HTTP de Gemini (`APIError`) prueba que no hay respuesta facturable: recibo `provider_error`,
  se retira el `started`, sin guard, y se permite reintentar esa petición una vez (el recibo se consume al
  reenviar).
- Cualquier otro fallo tras enviar (timeout, corte, interrupción, fallo al escribir) abre
  `evaluations/guard.json` y lanza `GuardedReadingError`: ningún envío nuevo hasta que un operador lo revise
  (servicio detenido en `attention_required`). Un `started` sin respuesta ni `provider_error` del mismo intento
  también bloquea todo envío.
- La respuesta se guarda (`.response.private.json`, con hash) **antes** de validarla o escribir en
  PostgreSQL. Si la base falla o la respuesta es inutilizable (truncada, JSON o esquema inválido, uso
  desconocido) el guard se abre y la respuesta queda preservada: un job posterior de la misma petición la
  reproduce localmente sin volver a pagar, incluso con el guard abierto.
- Un job reclamado antes (`attempts > 1`) solo puede reproducir su propia respuesta o reintentar tras un
  `provider_error`; nunca envía por primera vez.

**Respuesta incompleta.** Gemini suele omitir los "documentos de propuesta" que solo dicen cómo se
acredita otro requisito (copias de contratos, SCTR, títulos). Esos, y solo esos, pasan a la lista
`administrative_checklist` ("Documentos para presentar", sin efecto en el puntaje) y la evaluación se
guarda; un documento de propuesta que el modelo sí evaluó (SLA, documentación técnica) sigue siendo requisito.
Si omite o reordena cualquier otro requisito (o cita evidencia que no existe), `ModelOutputMismatch`: solo
falla ese par contrato × empresa, el job termina `done` con `runtime_result.outcome = invalid_model_output`,
la respuesta queda en su recibo (temperatura 0: reenviar devolvería lo mismo) y el par no se vuelve a encolar
para esa lectura (`skip_reason = model_output_invalid`). El cliente puede usar "Evaluar con mi perfil".
Antes del 26/10 este caso detenía todo el servicio (`attention_required: ValueError`).

**Ritmo de Gemini.** Cada llamada pagada que Gemini respondió (lectura o evaluación) retrasa el siguiente
envío lo que tardan sus tokens a `PROD6_GEMINI_TOKENS_PER_MINUTE` (150 000; `0` lo apaga): una evaluación de
DIN (~58 000 tokens) espera ~23 s, una de VEYON ~1,6 s. La espera ocurre antes del recibo `started`. Un 429
cuya cuota es por minuto (`quotaId` con `PerMinute`) pausa `PROD6_GEMINI_RATE_BACKOFF_SECONDS` (120) sin
alerta; uno diario o sin detalle sigue siendo pausa de cuenta (1 h, con alerta).

Resolver un guard abierto (servicio detenido): revisar `evaluations/guard.json` y los `started` sin
respuesta, mover el guard a `evaluations/resolved-<fecha>/` y reiniciar el job, como se hizo con las
lecturas el 25/10. Si el envío pudo facturarse, decidir con el panel de uso de Google AI Studio antes de
liberar el guard.

**Costo estimado** (`gemini-3.1-flash-lite`, US$0,25 / 1M entrada y US$1,50 / 1M salida): con las
evaluaciones pasadas de la base local, la salida ronda 500 tokens. VEYON (~4 000 tokens de entrada) cuesta
~US$ 0,0018 por evaluación (histórico v2: US$ 0,0014 promedio, máx. 0,0039). DIN envía un perfil mucho más
grande (~51 500 tokens de entrada medidos con el payload real) y cuesta ~US$ 0,014 (histórico v4: US$ 0,0155).
La salida incluye los tokens de razonamiento, que Google factura como salida.

**Backfill.** `python -m buenapro_worker.prod6.backfill_evaluations --profile-id <primera empresa> --profile-id
<segunda>` (mismo orden que el runtime: la primera es el alcance de la cola). Dry-run por defecto: lista lo
que encolaría (`planned`, el que cierra antes primero), los motivos de descarte y el desglose por empresa;
`--apply` encola (una transacción, idempotente), `--limit N` acota y `--include-stale` incluye matches de
una lectura anterior. Solo escribe en `worker_jobs`; no llama a SEACE, JEV ni Gemini: el pago ocurre después,
una evaluación a la vez, cuando el runtime reclama los jobs. Para activar DIN: poner `automation_rules.enabled=true`
y ejecutar el backfill (el runtime no repasa por sí solo).

#### Calificación manual desde la web (PROD6, desde el 31/10/2026)

Decisión del 30/10: en menores se guarda y se muestra todo; JEV ≥ 60 se lee y evalúa solo (lo de arriba). Lo
demás, incluido el histórico anterior a JEV, se puede calificar con el botón «Calificar» de la ficha.

- **Tabla `manual_grading_requests`** (migración 0050): una fila por pedido (`tenant_id`, `profile_id`,
  `source_key` `seace_prod6`/`seace_prod2`, `source_id`, `mode`, `status`, `reason`, `lima_day`). Un índice
  único impide dos pedidos activos (`pending`, `reading`, `evaluating`) del mismo par.
- **Tope:** 100 pedidos por empresa (tenant) y día de Lima, sumando menores, mayores y re-calificaciones;
  `MANUAL_GRADING_DAILY_LIMIT` en el entorno de la web. Un pedido que falla también cuenta; uno sin cambios
  (`skipped: unchanged`) no. La web cuenta e inserta bajo un lock consultivo por tenant.
- **Con lectura válida** (vigente, no `failed`, sin revisión humana) la web evalúa en el momento
  (`analyzeMatchDirect`, modo `direct`). **Sin lectura** deja el pedido `queued`/`pending` y el runtime:
  1. lo reclama cada ~10 s mientras procesa; en espera revisa cada 30 s y adelanta el ciclo si llegó uno;
  2. encola `process_contract` con la misma carga que `admit_contract` (aunque el contrato esté cerrado) →
     `reading`;
  3. tras `derive_summary`, con lectura válida encola `analyze_match` con `origin=prod6_manual_request_v1`
     (`source=manual_request`, prioridad 3) → `evaluating` → `done`; en revisión o fallida → `failed`.
- La evaluación manual no exige JEV aceptado, `automation_rules`, tope diario automático ni contrato abierto;
  sí empresa activa del runtime y lectura válida. Comparte la deduplicación `analyze_match:<empresa>:<contrato>`
  con la automática, así que un par se paga una vez.
- **Motivos de falla:** `contract_not_found`, `profile_inactive`, `reading_requires_review`, `reading_failed`,
  `no_documents`, `invalid_model_output` y `reading_timeout` (24 h en `reading` o `evaluating`).

**Despliegue.** La imagen completa de `worker.Dockerfile` ya lo incluye; `infra/docker/prod6.Dockerfile` copia además
`jobs/analyze_match.py` y `matching/analyzer.py`. Sin migración. Actualizar el código no autoriza reiniciar el
runtime ni encolar: sin `--apply` y sin reinicio no hay llamadas pagadas.

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

- **Web** (`apps/web/server/services/seaceEgress.ts`): la vista previa del TDR (miniatura y visor) y
  el refresco del detalle de la ficha usan las mismas `SEACE_EGRESS_PROXY`, `SEACE_EGRESS_REQUIRED` y
  `SEACE_USER_AGENT`. Como comparte la IP de salida con el worker, pide poco: un turno cada
  `WEB_SEACE_MIN_INTERVAL_SECONDS` (8), tope `WEB_SEACE_MAX_REQUESTS_PER_HOUR` (20) y espera máxima
  `WEB_SEACE_MAX_WAIT_SECONDS` (20); un 403/429 pausa toda la salida web
  `WEB_SEACE_BLOCK_BACKOFF_SECONDS` (10800). Cada PDF se descarga una vez y queda en
  `SEACE_DOCUMENT_CACHE_DIR` (por defecto el tmp del contenedor) `SEACE_DOCUMENT_CACHE_HOURS` (24) horas;
  una respuesta que no empieza con `%PDF-` nunca se guarda. Si SEACE no está disponible, el visor muestra
  "Vista previa no disponible" con el enlace "Abrir en SEACE" (el navegador del usuario descarga directo),
  con 503 propio en vez del 502 que Cloudflare reemplazaba. El refresco del detalle nunca espera turno:
  si la salida está ocupada sirve lo guardado. El estado (ritmo, pausa) es por proceso de la web.
  Por la misma salida pasan también las acciones que el usuario pide a propósito: el enlace oficial de
  los documentos PROD2 (`prod2OfficialDocument.ts`), el PDF de PROD4 (`/api/prod4/opportunities/[id]/documents/[code]`),
  la sesión SEACE del tenant (`seaceAuthenticated.ts`: conectar, consultas oficiales, contexto de cotización y
  comenzar postulación) y la descarga de originales del agente documental (`codexAgent.ts`). Ninguna sale
  directo: la VM recibe 403 de SEACE. Esas acciones (`interactive`) comparten el ritmo, el turno compartido
  y la pausa ante 403/429 con todo lo demás, pero tienen cupo propio para no agotar el de las vistas previas:
  `WEB_SEACE_INTERACTIVE_MAX_REQUESTS_PER_HOUR` (60) y espera máxima
  `WEB_SEACE_INTERACTIVE_MAX_WAIT_SECONDS` (45). Conectar la cuenta son 2 peticiones y cada consulta
  autenticada 1 o 2, así que con el ritmo de 8 s cada acción tarda unos 8 s más. Con la salida cerrada,
  en pausa o saturada, esas rutas responden 503 (`code: SEACE_UNAVAILABLE`) y los PDF de PROD4 muestran la
  página "Abrir en SEACE"; la conexión guardada no se marca como inválida por eso. Un 403 de la API de
  SEACE (también por un token vencido) abre la misma pausa de 3 h: si apareciera con sesiones vencidas, hay
  que distinguirlo del bloqueo de la IP.

### Freno compartido (PROD6 + PROD2 + web)

Los tres procesos salen por la misma IP peruana, y SEACE corta la IP completa ante una ráfaga.
Con `SEACE_SHARED_GATE=1` y `REDIS_URL` en los tres, comparten en Redis un solo turno y una
sola pausa (`seace/shared_gate.py` y `apps/web/server/services/seaceSharedGate.ts`, mismo protocolo):

- `seace:egress:next_slot_ms`: siguiente turno libre (reloj de Redis), reservado con un script Lua
  atómico. Cada petición espera además su ritmo local (PROD6 6 s, web 8 s, PROD2 `--request-interval`).
- `seace:egress:blocked`: JSON `{"status","source","at"}` escrito con `NX` y TTL ante cualquier 403/429.
  Mientras existe nadie envía. El primero que lo escribe gana: no se pisa ni se alarga.
- `SEACE_SHARED_MIN_INTERVAL_SECONDS` (6): separación mínima entre peticiones de todos los procesos.
  `SEACE_SHARED_BLOCK_SECONDS` (10800): duración de la pausa compartida. Deben ser iguales en los tres.

Con el freno activo, si Redis falla nadie envía: PROD6 y PROD2 lo tratan como salida caída
(`egress_backoff` / `egress_unavailable`), y la web responde "ocupado". Escribir la pausa es
de mejor esfuerzo. Si el freno está desactivado (o falta `REDIS_URL`) todo funciona como antes;
ese es el kill switch.

Reacción de cada proceso ante una pausa abierta por otro:

- **PROD6**: abre su propio breaker como si hubiera recibido el 403 y persiste `source_blocked_until`.
- **PROD2 (hijo selected)**: lo trata como el 403 que representa (`source_<status>`), y el ciclo
  termina en `attention_required`.
- **PROD2 (servicio)**: antes de lanzar cada ciclo, si hay pausa espera en `source_paused`
  (`reason` = origen, `resume_at`) y alerta una vez. También alerta en `attention_required` y en
  `complete`.
- **Web**: sirve lo guardado o muestra "Abrir en SEACE".

Diagnóstico: `docker exec docker-redis-1 redis-cli GET seace:egress:blocked`, `PTTL` de la misma
clave y `GET seace:egress:next_slot_ms`. Borrar la pausa (`DEL seace:egress:blocked`) solo si se
comprobó que SEACE responde.

La imagen PROD6 copia también `seace/shared_gate.py`.

## Operación PROD2 (licitaciones mayores)

Imagen `buenapro-worker-prod2:<tag>`, construida con `infra/docker/prod2.Dockerfile` sobre la base
`docker-worker-io:latest`. En la VM:

- **Datos privados:** `~/buenapro/runtime/prod2-pilot-20261027/` contiene `runtime.env`,
  `service.private.json` y `state/`.
  - Dueño `ubuntu`, porque `docker compose` lee el `env_file` como el usuario que lo ejecuta;
    con root 600 falla con *permission denied*.
  - Permisos 600 para los archivos y 700 para los directorios. El servicio no acepta permisos
    abiertos ni symlinks.
- **Rutas:** `PROD2_*` van en `<recibos>/prod2-pilot.env`, junto al compose `prod2.compose.yml`.
- **Arranque:**
  `docker compose --env-file <recibos>/prod2-pilot.env -f infra/docker/docker-compose.yml -f <recibos>/prod2.compose.yml --profile prod2-only up -d --no-deps prod2-ingest`.
  Nunca usar `--remove-orphans`: borraría `prod6-ingest`, que vive en otro compose.
- **Preflight:** solo GET y sin base ni proveedores, pero exige `--folder`. Comando:
  `run --rm --no-deps --entrypoint python prod2-ingest /app/scripts/run_prod2_selected.py --preflight --folder /var/lib/buenapro/prod2/preflight --keyword software --years 2026 --request-interval 10`.
- **Ventana:** `window.first_to` del primer ciclo debe ser la fecha de hoy en Lima. En los ciclos
  siguientes se calcula sola.
- **Topes:** `--max-candidates`, `--max-accepted`, `--max-jev-calls`, `--max-llm1-calls` y
  `--max-embedding-calls` son frenos de seguridad, no recortes, y se cuentan por ciclo.
  - Si la búsqueda llega al tope de candidatas (convocatorias únicas, antes de JEV), se corta: el
    ciclo termina en `attention_required` (`final_report_source_incomplete`) y no se reintenta.
  - Si JEV acepta más que el tope, quedan en cola (`final_report_queue_incomplete`). Cuentan también
    las aceptadas que ya tienen lectura.
  - Los topes de JEV, LLM1 y embeddings solo dejan casos en revisión.
  - Deben cubrir todo el alcance; el gasto real lo fija la búsqueda.
- **`--document-previews`:** con él el selector lee el contenido de PDF, DOC/DOCX y miembros de
  ZIP/RAR. Sin él solo ve nombres, y los expedientes en RAR quedan en `selection_needs_review`.
- **Reintentar tras `attention_required`:** exige un state dir nuevo. El anterior se conserva como
  evidencia.
- **Velocidad de lectura (31/10/2026):** el ritmo con SEACE no cambia (10 s entre peticiones y el turno
  compartido en Redis). Lo que frenaba cada lectura era la CPU del servidor: el OCR, ~44 % del tiempo, y
  armar el contexto tras los embeddings, ~26 %.
  - `run_prod2_selected.py` arma el contexto con `memoize_expansion=True`. Da el mismo contexto y el
    mismo `retrieval_hash`, pero ya no busca las referencias («ver anexo N», «numeral X») de nuevo por
    cada pregunta. En un corpus real de 249 páginas bajó de 403 s a 13 s.
  - El diario corre con `--processing-workers 2`: mientras una licitación espera a SEACE, la otra hace
    OCR. Los hilos comparten el pacer del proceso, así que el ritmo con SEACE no sube.
  - `prod2.compose.yml` limita `prod2-ingest` y `prod2-manual` a `cpus: 1.5`. Así la web, Postgres y
    menores conservan medio núcleo.
  - El OCR no se puede saltar. Ya usa `tessdata_fast` y procesa una página a la vez. En los escaneos
    con encabezado digital, el OCR aporta la mayor parte del texto.

### Servicio diario (desde el 28/10/2026)

Mismo contenedor y receta de arranque. Desde el 28/10/2026 09:00 Lima corre la config final en
`~/buenapro/runtime/prod2-service-final-20261028/` (imagen `buenapro-worker-prod2:final-ecc71be`,
recibos en `~/buenapro-releases/prod2-final-ecc71be-receipts/`): 10 s entre peticiones,
`max_cycles` 10000 y `"evaluation": {"max_calls": 60}`. Las carpetas del piloto y de la observación
(`prod2-service-20261028/`, 1 ciclo a 15 s) quedan como evidencia. Cambiar la config de un estado en
curso lo detiene (`active_configuration_changed`): un cambio de config exige otra carpeta de estado, y
detener el contenedor deja el estado anterior en `attention_required` (`operator_stop_requires_review`)
con su aviso. Config:

- Preset `--keyword-preset convenio60`: las `digitales30` más 30 señales industriales para DIN.
  - Cada keyword cuesta unas 12 peticiones a SEACE por año: dos objetos (bien y servicio), y en
    cada uno sesión, búsqueda y una segunda sesión que confirma la primera página.
  - Las 60 suman unas 720 peticiones por ciclo, más fichas y descargas de lo aceptado.
  - `--business-scope todas` serían 166 keywords, unas 2 000 peticiones.
- `--years window`: el servicio busca los años que abarca la ventana; el cambio de año no requiere
  tocar la config.
- `start_time` `"01:00"` (Lima): cada ciclo siguiente arranca a esa hora, al menos
  `interval_seconds` después del anterior. Con `start_time`, `interval_seconds` (900) solo marca
  cada cuánto se revisa la salida caída.
- Ventana de 3 días: cubre publicaciones que SEACE indexa tarde.
  - Cada ciclo vuelve a buscar todas las keywords: no hay caché entre ciclos.
  - Lo ya leído (`--preserve-prior-readings`) no se descarga ni se paga otra vez.
  - Los casos sin lectura vuelven a pasar por ficha y descargas mientras estén en la ventana.
- Lectura v6 con `--llm1-model-routing fixed`. La ruta `pages20` manda a `gemini-2.5-flash-lite`,
  retirado para nuestra llave.

Desde el 30/10/2026 busca **todas** las palabras clave de DIN y VEYON (`--business-scope todas`,
167 tras sumar «radar», igual que los perfiles en PostgreSQL; la lista de `docs/sources` es una copia
que hay que actualizar cuando cambian los perfiles):

- Imagen `buenapro-worker-prod2:allkw-d924d0f`, recibos en
  `~/buenapro-releases/prod2-allkw-d924d0f-receipts/`.
- Recuperación única de 30 días en `~/buenapro/runtime/prod2-catchup-all-20261030/`
  (`max_cycles` 1, contenedor `docker-prod2-catchup-1`), para lo publicado antes que sigue abierto.
- Diario en `~/buenapro/runtime/prod2-service-all-daily/` (env `prod2-daily.env`): topes 2000
  candidatas y llamadas JEV, 400 aceptadas, 120 LLM1, 1000 embeddings y `max_calls` 120 de evaluación.
  `start-daily-after-catchup.sh` lo arranca solo si la recuperación quedó `complete` (log
  `start-daily.log`) y fija `first_to` en la fecha de Lima de ese momento.
- Cada consulta tarda ~1 min con 10 s entre peticiones y el freno compartido: 334 consultas son
  ~5,5 h por ciclo, más fichas y lecturas.

Desde el 31/10/2026 el diario **lee solo desde afinidad 85** (`--llm1-min-affinity 85`; decisión del 30/10
por el volumen de mayores):

- JEV sigue aceptando desde 60 (`accepted_runner`, `ACCEPTANCE_THRESHOLD`, `jev_threshold` 60 sin cambios):
  todo lo aceptado se promueve y guarda su ficha (`prod2_details`), así que se lista.
- De 60 a 84 termina `accepted_unread`: sin descarga, selector, embeddings ni LLM1, y sin gastar sus topes.
  Cuenta para `--max-accepted`. La web lo muestra «Por calificar» (sin lectura ni evaluación) y su ficha se
  vuelve a consultar en cada ciclo que la redescubre dentro de la ventana.
- `accepted_unread` es un resultado cerrado: el ciclo queda `complete` si `llm1_complete + accepted_unread`
  iguala lo aceptado. El aviso de Telegram suma "N sin leer".
- El valor 60 (por defecto) no entra al manifiesto ni a la identidad de la cola: las carpetas anteriores
  siguen iguales. Otro valor sí entra, y una carpeta congelada no acepta cambiarlo. El modo de importación
  por IDs exige el valor por defecto.
- Las listas de mayores en la web muestran solo lo aceptado (afinidad JEV de la unión ≥ 60 o sin fila JEV
  previa); las «Descartadas en la revisión previa» ya no salen en ninguna vista. Las dos listas muestran
  «JEV N».

Qué hace el servicio cuando un ciclo no termina limpio:

- **Bloqueo a mitad del ciclo** (`final_report_source_access_blocked`), sea un 403/429 propio o
  una pausa abierta por PROD6 o la web:
  - la carpeta queda como `<época>.blocked-<ns>`;
  - el servicio espera la pausa compartida (`source_paused`) y repite el ciclo en una carpeta nueva
    con el mismo sweep, así que las consultas ya completas salen de la caché sin pedir a SEACE;
  - hasta 3 veces por ciclo; la cuarta vez se detiene.
- **Búsqueda incompleta** sin llegar al tope de candidatas (`final_report_source_incomplete`):
  espera 30 min en `retry_wait` y reintenta una vez; si se repite, se detiene.
- **Solo se reintenta si los pagos están limpios:** sin intentos de uso desconocido, sin corte de
  embeddings y sin archivos `*guard*`. Cualquier otro motivo se detiene como antes.
- El cupo de reintentos (`retries` en el estado) sobrevive a un reinicio del contenedor.
- Cada ciclo terminado manda a Telegram: candidatas, aceptadas, leídas, en revisión, costo y hora
  del próximo ciclo.

Piloto del 27/10/2026, con «software», 2026, ventana de 7 días y 1 ciclo:

- Terminó en `complete` en 41 min: 8 licitaciones, todas aceptadas por JEV, y 0 respuestas 403/429.
- Costo: US$ 0,0194, repartido en 11 llamadas JEV, 19 de embeddings y 1 de Gemini.
- Solo 1 llegó a lectura LLM1. Las otras 7 quedaron en `EmbeddingPreparationReview`
  (`retrieval_context_needs_expansion_or_review`): el contexto recuperado supera
  `context_maximum_tokens` (30 000) de la config v5. Hoy el servicio acepta la v5 o la v6 congeladas;
  la v6 lee la Sección Específica y recorta por relevancia (máx. 45 000).

### Listado nacional por día, cola de lectura y horario por franjas (descubrimiento rápido)

Separa el diario en dos trabajos: **descubrir** (este servicio, rápido y varias veces al día) y **leer** (otro
worker, desde la cola `prod2_reading_queue`). Las tres piezas son opt-in: sin sus flags ni claves, los
manifiestos, hashes e identidades de cola de las corridas por palabra clave no cambian. Nada de esto se
despliega ni se activa solo; el código no autoriza aplicar migraciones ni arrancar workers.

**Listado nacional (`--national-listing` en `run_prod2_selected.py`).**

- Sustituye a las 167 palabras (~2 000 peticiones, ~5,5 h por vuelta) por **una consulta por día de publicación
  y objeto** (bien y servicio), sin palabra: `{"keyword": "", "object_type", "year": <año del día>,
  "published_from": día, "published_to": día, "seace_version": "3"}`. Un día por consulta porque los barridos
  largos sin palabra fallan (`first_page_reconciliation_changed`: SEACE cambia la primera página mientras se
  pagina).
- Excluyente con `--keyword`, `--lane`, `--business-batch`, `--business-scope`, `--keyword-preset` y
  `--all-keywords`, y con la importación por IDs. Exige `--from-date` y `--to-date` explícitos, de hasta 31
  días, y que `--years` sea `auto` o cubra los años de esos días (con `--years window` del servicio se cumple
  solo).
- **Palabra vacía solo aquí.** `validate_capture(..., allow_empty_keyword=True)` la acepta únicamente para una
  ventana de un día; sin ese parámetro sigue rechazándola. Lo pasan `keyword_sweep.sweep_query`,
  `keyword_tracking`, `accepted_repository.promote_accepted`, `get_detail` y el «Calificar» de mayores
  (`manual_grading.locate_ficha`) cuando la consulta del caso no tiene palabra; así se puede releer un caso del
  listado. El cliente ya admitía la búsqueda sin palabra (`search(..., allow_empty_keyword=True)`).
- **Manifiesto.** Solo con el modo: `"discovery_policy": "prod2_national_listing_v1"`. `keywords` queda `[""]`
  (un pseudo-keyword: el seguimiento, `--keyword-status` y el avance por consulta lo reutilizan) y
  `probe_empty_scopes` queda en falso, porque el sondeo de ámbitos vacíos mira la ventana entera y cada día ya
  es su propia consulta. La política entra también en el alcance de la época de seguimiento y en la identidad
  de la cola, así que una época por palabras y una nacional no se mezclan. Las consultas de un día se aceptan
  dentro de la ventana de la época (las de palabra siguen exigiendo la ventana completa).
- **Sin `business_plan`** no hay `discovery_keyword` ni `business_batch_id` en los resultados, y
  `report_business_batches` no corre.
- **Día con 499 o más resultados** (el tope de SEACE): un día no se puede partir más, así que la consulta queda
  `blocked` (`capped_single_day_requires_review`), el ciclo `final_report_source_incomplete` y el servicio lo
  reintenta una vez a los 30 min antes de detenerse. Igual que antes; hay que mirar ese día a mano.
- **Día en curso** (el último día de la ventana del ciclo, mientras es hoy en Lima; ayer si el ciclo cruzó la
  medianoche). Recibe publicaciones mientras se pagina (~250 al día; una pasada tarda ~2 min), así que su
  primera página o su contador cambian a mitad de la pasada y la consulta falla con
  `first_page_reconciliation_changed` o `counter_changed_during_pass`. Solo en el modo nacional:
  - **Reintento en la misma corrida:** hasta 2 veces más, con 60 s de pausa (STOP y el pacer siguen mandando).
    Cada reintento es un intento nuevo de la misma consulta con su caché; el parcial queda como evidencia.
    El resultado lleva `current_day_retries`.
  - **Tolerancia:** si sigue parcial por esos dos códigos, la consulta se marca `current_day_partial`. Las filas
    ya emitidas se procesan normal y el ciclo **no** la cuenta como incompleta: el reporte trae
    `current_day_partial: [consultas]`, `source_coverage_complete` es verdadero y `assess_final_report` / `_gate`
    del servicio lo dan por completo (sin reintento de 30 min ni `attention_required`). El aviso de Telegram
    agrega «día en curso parcial en N consultas». `queries_complete` sigue contando solo las completas.
  - **Qué no cambia:** cualquier otro código, o un día que no sea el en curso, sigue incompleto como hoy. El
    gate valida la reclamación (política nacional, última fecha de la cobertura, solo esos códigos, consulta sin
    palabra, marcada por la corrida); si no cuadra → `invalid_coverage_evidence`. El modo por palabra no
    reintenta ni tolera nada.
  - **Cobertura posterior:** la corrida de las 01:00 (3 días) y las siguientes vuelven a pedir ese día; la
    tolerancia no lo da por cerrado, solo evita detener el servicio. Lo que llegue después de la pasada parcial
    lo recoge el ciclo siguiente.
- **Antes de lanzar:** `run_prod2_selected.py --folder <tmp> --national-listing --from-date <d1> --to-date <d2>
  --years <año> --dry-run` (sin red, base ni proveedores) imprime `base_queries` (días × 2) y `discovery_policy`.

**Cola de lectura (`--reading-queue`).**

- Exige `--llm1-min-affinity` mayor que 60 (por ejemplo 85) y no se combina con la importación por IDs. Toda
  candidata aceptada por JEV (≥ 60) se procesa con `enrich(..., defer_reading=True)`: guarda ficha y ventana de
  participación y termina `accepted_unread`, sin descargas ni pagos más allá de JEV.
- Si su afinidad es **≥ el mínimo** y no hay lectura previa preservada (`preserve_prior_reading` devolvió
  `None`), inserta en la cola, después de guardar la ficha:

  ```sql
  INSERT INTO prod2_reading_queue (id_convocatoria, affinity_index, enqueued_by)
  VALUES (%s, %s, %s) ON CONFLICT (id_convocatoria) DO NOTHING
  ```

  con `enqueued_by` = `run_id` del ciclo, y marca `"reading_queued": true` en su `outcome`. Una fila ya
  existente (cualquier estado) no se toca; repetir el caso no la duplica. Las de 60 a 84 y las que ya tienen
  lectura quedan `accepted_unread` sin encolar (y la web las sigue mostrando «Por calificar»).
- Exige la migración `0052_prod2_reading_queue.sql`: sin la tabla la corrida se detiene antes de cualquier
  petición o pago (`to_regclass('prod2_reading_queue')`). El servicio no la aplica.
- La clave `"reading_queue": true` entra al manifiesto y a la identidad de la cola solo con el flag; una
  carpeta congelada no acepta cambiarlo.
- **Reporte y servicio.** Son filas `accepted_unread`: cuentan como cerradas en `completion_exit_code`, en
  `keyword_chain.assess_final_report` y en `_gate` del servicio, igual que hoy. El reporte final suma
  `reading_queued` (solo con el flag) y el aviso de Telegram agrega «N en cola de lectura».

**Horario por franjas (`schedule` en `service.private.json`).**

- Opcional y excluyente con `start_time`: lista de `{"start_time": "HH:MM", "window_days": 1..30}` en hora de
  Lima, de 1 a 24 franjas, **horas únicas y en orden ascendente** (cualquier otra cosa →
  `invalid_schedule`; junto a `start_time` → `schedule_excludes_start_time`).
- Cada ciclo siguiente arranca en la primera franja que cae **a partir del fin del ciclo anterior +
  `interval_seconds`** (si el ciclo pasó de una franja, esa se salta; después de la última franja del día va la
  primera del día siguiente, también al cruzar la medianoche de Lima) y busca los `window_days` de **esa**
  franja terminando hoy (Lima). `window.days` sigue siendo obligatorio y solo rige el **primer ciclo**, que
  arranca de inmediato. `interval_seconds` sigue marcando cada cuánto se revisa la salida.
- La franja del ciclo siguiente se guarda en el estado (`next_slot`, junto a `next_cycle_at`), así un reinicio
  calcula exactamente el mismo ciclo; los reintentos de un ciclo (salida caída, pausa, búsqueda incompleta)
  conservan su franja. Si falta o ya no está en la config (`schedule_slot_unavailable`) se detiene para
  revisión. El plan de cada ciclo registra `schedule_slot`. Sin `schedule` el estado y los planes no cambian.
- Como toda config, `schedule` forma parte del hash: cambiarla en un estado en curso lo detiene
  (`active_configuration_changed`); un cambio exige otra carpeta de estado.
- El servicio acepta `--national-listing` como selector de `base_argv` (junto a `--keyword`, `--lane`, etc.).

Ejemplo de `service.private.json` (campos como en el diario; solo cambian el selector, el mínimo, la cola y el
horario):

```json
{
  "version": 1, "enabled": true, "first_epoch_id": "prod2-national-20261101",
  "interval_seconds": 900, "max_cycles": 10000,
  "window": {"policy": "rolling_days", "days": 3, "first_to": "2026-11-01", "zone": "America/Lima"},
  "schedule": [
    {"start_time": "01:00", "window_days": 3},
    {"start_time": "08:00", "window_days": 1}, {"start_time": "12:00", "window_days": 1},
    {"start_time": "16:00", "window_days": 1}, {"start_time": "20:00", "window_days": 1}
  ],
  "base_argv": ["--local", "--preserve-prior-readings", "--national-listing", "--reading-queue",
    "--llm1-min-affinity", "85", "--years", "window", "--document-storage", "official",
    "--llm1-input", "embeddings", "--llm1-scope", "qualification", "--llm1-model-routing", "fixed",
    "--llm1-fallback", "none", "--embedding-config", "docs/sources/prod2-embedding-reading.runtime.v6.json",
    "--max-accepted", "400", "--max-candidates", "2000", "--max-jev-calls", "2000",
    "--max-llm1-calls", "120", "--max-embedding-calls", "1000",
    "--search-workers", "1", "--processing-workers", "2", "--model-workers", "1", "--request-interval", "10"]
}
```

(La ruta de `--embedding-config` en producción es la absoluta de la imagen.) Riesgos conocidos:

- El filtro de año de SEACE es el de la convocatoria: la consulta de un día usa el año de ese día, así que una
  publicación de enero con año de convocatoria anterior no aparecería (las consultas por palabra buscaban cada
  año de la ventana con toda la ventana). Vigilar las primeras semanas de cada año.
- Un día **anterior** que quede parcial por esos mismos códigos (SEACE reordena tarde, p. ej.) sigue siendo una
  fuente incompleta: el ciclo se reintenta una vez a los 30 min y, si se repite, se detiene para revisión.
- Mientras `prod2-manual` solo trabaja con el diario en `waiting` y más de 60 min libres, con varias corridas al
  día esa ventana se acorta: revisar que siga habiendo pasadas.

### Calificación manual de mayores («Calificar», contenedor `prod2-manual`)

Desde el 31/10/2026 la ficha de mayores ofrece «Calificar» cuando la convocatoria tiene JEV ≥ 60, sigue abierta
y no tiene evaluación de la empresa. La web solo deja el pedido (`manual_grading_requests`, `seace_prod2`,
`queued`/`pending`; tope compartido con menores, 100 por empresa y día de Lima). Lo atiende el contenedor
`prod2-manual` (`scripts/grade_prod2_manual.py --loop`, módulo `prod2/manual_grading.py`), con la misma imagen
y entorno que el diario, su propia carpeta de estado y la del diario y su config montadas solo lectura:

- **Cuándo trabaja.** Cada 60 s, solo si:
  - el diario está `waiting` y faltan más de 60 min para `next_cycle_at`;
  - no hay guard abierto ni pausa;
  - la salida responde y el freno compartido está libre;
  - logra sin esperar el lock `prod2_accepted_executor`, que mantiene toda la pasada.

  Un vigilante corta una lectura en curso 10 min antes del ciclo. Atiende hasta 5 pedidos por pasada. Así
  nunca corre junto con un ciclo: los pedidos de madrugada esperan a que el ciclo termine.
- **Qué hace por pedido** (`pending` → `reading` → `evaluating` → `done`/`failed`):
  1. Revisa gratis la convocatoria, la empresa (solo VEYON y DIN), JEV ≥ 60 y que siga abierta.
  2. Si ya hay lectura vigente, salta a evaluar. Si no, rearma el caso desde `prod2_accepted_run_members`,
     congela un manifiesto planeado por el propio servicio desde su config y vuelve a buscar la ficha en
     SEACE.
  3. Con la puerta de ciclo de vida, una convocatoria cerrada o sin apertura confirmada termina
     `convocatoria_cerrada` antes de cualquier pago.
  4. Lee como un ciclo (`enrich`, topes: 2 LLM1, 60 embeddings).
  5. Evalúa solo para la empresa que pidió (`evaluate_manual`). Ignora la regla, el dominio JEV y las
     horas al cierre; respeta lo demás.
  6. La evaluación queda con `evaluation_origin=prod2_manual_request_v1` y no cuenta para el tope diario
     automático.
- **Pagos.** Recibos del pedido en `<estado manual>/cases/<id>/` y de la evaluación en
  `<estado manual>/evaluations/` (no los del diario):
  - un intento pagado previo sin lectura vigente nunca se repaga;
  - una duda de cobro deja el pedido `payment_uncertain`, abre `guard.private.json` y avisa por Telegram;
  - no se envía nada más hasta que un operador revise y retire el guard.
- **Arranque.**

  ```bash
  docker compose -p docker -f docker-compose.yml -f prod2.compose.yml --profile prod2-manual up -d prod2-manual
  ```

  Usa las mismas variables que el diario más `PROD2_MANUAL_STATE_DIR` (carpeta 0700 creada a mano). Reiniciarlo
  no afecta al diario.
- **Efecto lateral esperado.** Una lectura manual deja la lectura vigente, así que el evaluador automático del
  ciclo siguiente puede evaluar también a la otra empresa, con sus propias reglas y tope.

### Evaluación por empresa de mayores (LLM2, `evaluate_prod2_matches.py`)

Evalúa las lecturas PROD2 abiertas para VEYON y DIN con el evaluador de menores (`finish_match`:
8 experiencias más relacionadas, solo requisitos sustantivos y veredicto por reglas) y el prompt
`prod2_match_v1.txt`. Código: `prod2/evaluation.py`.

- **A quién evalúa:** JEV de mayores decide una vez por licitación, para la unión de las dos
  empresas, y nombra el dominio (`prod2_evaluations` de tipo `jev_affinity`).
  - `industrial_electrica`: solo DIN.
  - `informatica` o `ambiguo`: VEYON y DIN, porque DIN también vende software.
  - Exige afinidad ≥ 60, licitación abierta, lectura vigente con requisitos y que la empresa no
    tenga ya una evaluación de esa lectura, perfil y prompt. Desde el 31/10 solo se leen solas las de
    85 o más, así que en la práctica evalúa esas más las que se lean a mano.
- **`automation_rules` de cada empresa:** `enabled`, `min_hours_before_close` y
  `max_daily_evaluations`. El tope diario se comparte con las evaluaciones automáticas de menores.
- **Lecturas en revisión humana:** se evalúan igual; todas las de mayores lo están por diseño. La
  evaluación se guarda con `persist_match(allow_source_review=True)` y la web la muestra como
  "Puntaje provisional".
- **Pagos:**
  - Recibos del guard de menores con su propio tipo (`prod2_match_analysis_v1`) y una reserva
    `gemini_match` en `prod2_evaluations` por par licitación–empresa.
  - Un error HTTP de Gemini devuelve la reserva a `pending` y termina la corrida (exit 3).
  - Si la respuesta omite o reordena requisitos, la reserva queda `failed` con su costo: solo ese
    par.
  - Un envío sin respuesta durable deja el guard abierto y la reserva `uncertain` (exit 2): ninguna
    corrida posterior envía hasta revisarlo.
- **`--plan`:** solo lee PostgreSQL. `--execute` necesita `--max-calls` (1–200) y `--receipts`, una
  carpeta privada que se conserva entre corridas.
- **En el servicio:** el bloque `"evaluation": {"max_calls": N}` de la config lanza el evaluador como
  proceso aparte tras cada ciclo, con recibos en `<state>/evaluations`. Avisa por Telegram el
  resultado o el problema; nunca detiene la búsqueda.

### Releer las revisiones con otra configuración (`reread_prod2_reviews.py`)

Relee, con una config nueva (p. ej. la v6), los casos de una corrida que terminaron en
`EmbeddingPreparationReview` / `retrieval_context_needs_expansion_or_review`. Reutiliza la
preparación retenida, los OCR y los vectores ya pagados: **no compra embeddings** (tope 0; si falta
un vector, el caso falla sin pagar) y no usa SEACE, Redis, selector ni JEV. Hace una llamada LLM1
por caso, sin fallback, y deja un `outcome.private.json` inmutable por caso en `--folder`.

- La imagen debe incluir el script (`prod2.Dockerfile` lo copia a `/app/scripts/`).
- `--plan` es offline: valida la evidencia original y escribe `manifest.private.json`. Un caso que
  no sea la revisión esperada se rechaza con su motivo y no se escribe nada.
  Imprime la edad de la observación «abierto» de cada caso.
- `--execute` exige el mismo manifiesto y los mismos argumentos; cualquier cambio de la evidencia
  lo detiene. Toma el candado global de PROD2: no corre mientras el servicio esté en un ciclo.
- Regla de ciclo de vida (la del seguimiento de sectores): solo se lee un contrato abierto
  (`confirmed_open` y `registration_open`/`submission_open`), con la observación de hasta
  `--max-observation-hours` (6 por defecto) y con el plazo de presentación, tomado de PostgreSQL,
  todavía en el futuro. Solo la edad de la observación se puede ampliar, con un plan nuevo en otra
  carpeta; el plazo no.
- Un caso que falla o vuelve a revisión registra su outcome y no se reintenta. Un caso detenido antes
  de despacharse (tope LLM1, parada) queda sin outcome y un `--execute` posterior lo retoma.

```bash
python /app/scripts/reread_prod2_reviews.py --plan \
  --original /var/lib/buenapro/prod2/runs/pilot2-software-2026-000001 \
  --ids 1051075 1051079 1051128 1051566 1051702 1051736 1051853 \
  --embedding-config /app/docs/sources/prod2-embedding-reading.runtime.v6.json \
  --folder /var/lib/buenapro/prod2/rereads/pilot2-software-2026-000001-v6 \
  --max-llm1-calls 7
```

El comando `--execute` es el mismo con `--execute` en lugar de `--plan`. Se ejecuta con
`docker compose … run --rm --no-deps --entrypoint python prod2-ingest` como el preflight.

## Pausa de emergencia

```bash
docker compose -f infra/docker/docker-compose.yml stop scheduler worker-io worker-llm worker-match worker-notify
```

La cola queda en Postgres. Al volver a levantar workers, siguen desde `worker_jobs`.
