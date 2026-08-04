# Tareas - Worker

## Estructura

- [x] Reorganizar `workers/seace/src/buenapro_worker` por dominios: `queue`, `seace`, `documents`, `extraction`, `normalization`, `matching`, `notifications`, `storage`, `db`, `observability`.
- [x] Crear CLI real con comandos: `run`, `schedule`, `poll-once`, `contract-test`.
- [x] Crear settings con `.env.local`.
- [x] Configurar logging estructurado.

## Queue sobre Postgres

- [x] Implementar `queue.repository` para crear jobs con `dedup_key`.
- [x] Implementar claim atomico con `FOR UPDATE SKIP LOCKED`.
- [x] Implementar complete/fail/dead.
- [x] Implementar backoff exponencial con jitter.
- [x] Implementar runner por colas: `io`, `llm`, `match`, `notify`.
- [x] Separar servicios Docker por cola: scheduler, io, llm, match, notify.
- [x] Implementar scheduler que encole polling cada 30 min.
- [x] Implementar scheduler para lifecycle cada 6-12 h.
- [x] Implementar scheduler para contract test cada hora.

## Cliente SEACE

- [x] Implementar cliente HTTP con `httpx`.
- [x] Agregar User-Agent claro.
- [x] Agregar timeouts.
- [x] Agregar retries con backoff.
- [x] Implementar `search_contracts`.
- [x] Implementar `contract_detail`.
- [x] Implementar `list_files`.
- [x] Implementar `download_file`.
- [x] Implementar schemas Pydantic para validar shape esperado.
- [x] Implementar contract test de endpoints clave.

## Polling vigente

- [x] Implementar job `poll_search`.
- [x] Configurar alcance MVP de ingesta desde settings/env: vigentes + servicios + segmentos permitidos.
- [x] Consultar solo `lista_estado_contrato=2`.
- [x] Consultar solo `lista_codigo_objeto=2` para el MVP.
- [x] Cargar segmentos CUBSO permitidos desde configuracion, no hardcodeados en la logica.
- [x] Definir lista inicial de segmentos permitidos para tecnologia, transporte y legal usando el catalogo 2026.
- [x] Enviar `segmento` en el query cuando el polling corra por segmento permitido.
- [x] Omitir y loggear oportunidades fuera del alcance MVP si aparecen por cambios de payload.
- [x] Procesar page 1 con `page_size=100`.
- [x] Calcular `hash_search`.
- [x] Insertar contratos nuevos.
- [x] Actualizar contratos con hash cambiado.
- [x] Cortar cuando una pagina completa no tenga cambios.
- [x] Encolar `process_contract` para nuevos/cambiados.
- [x] Agregar modo de primera corrida controlada con `poll-once --limit`.
- [x] Agregar batch MVP 150 por rubro con `batch_id`.
- [x] Documentar protocolo de diagnostico de workers y BD.

## Lifecycle

- [x] Corregir seguimiento post-cierre: refresco real de detalle, fechas desde etapa de cotización, rotación por `detail_fetched_at`, cobertura completa de ventana reciente y promoción automática al histórico (2026-07-31).
- [x] Añadir `recent-closures` cada 2 h con ventana configurable de 15 días y persistencia histórica al detectar culminado/adjudicado/desierto; corrida controlada de 2 contratos validada el 2026-07-31.
- [x] Añadir backfill histórico sin LLM ni descarga de TDR, comandos `historical-backfill`, `historical-stats` y reglas determinísticas de resultado. Segmento 81 validado con 20 contratos (8 adjudicados, 12 desiertos) y segunda corrida idempotente.
- [x] Validar el barrido histórico completo de tecnología (`81` y `43`) en producción; checkpoint por página y reconciliación final guardaron 4,451 resultados únicos, recuperaron 20 IDs desplazados/fallidos y cerraron con 0 pendientes (2026-07-31).

- [x] Implementar job `poll_lifecycle`.
- [x] Reconsultar contratos no cerrados.
- [x] Detectar cambios de estado.
- [x] Detectar cambios de cronograma.
- [x] Detectar cambios de archivos.
- [x] Capturar resultado/adjudicatario si SEACE lo expone.
- [x] Encolar cascada de cambios cuando aplique.

## Ingesta v2 (2026-07-10)

- [x] Listar archivos de categorias 1 y 2: cat 1 (TDR/requerimiento) se descarga y extrae; cat 2 (anexos, formato de cotizacion) solo se lista con descarga directa SEACE y `doc_class` propio (`anexo`/`cotizacion`; migracion 0007).
- [x] Detalle SEACE una sola vez en la ingesta (llena ubicacion/CUBSO/cronograma) + `detail_fetched_at` (migracion 0006); el lifecycle ya no repite el GET de detalle por ciclo.
- [x] Refresh del detalle bajo demanda desde la web al abrir la vista (TTL 6 h, `server/services/seaceDetail.ts`); ante error de red sirve el cache.
- [x] `cotizar` como senal viva: capturado del buscador en cada poll y derivado a estado operativo (abierta / abre el X / cerrada / no cotizable) visible en feed, panel y detalle.
- [x] Comparar 5 licitaciones cotizables (2026-07-12): formatos no uniformes entre MIDAGRI, INICTEL, SEDAPAR y DIRESA; priorizar `idTipoArchivo=5` y fallback por oferta/cotización/estructura de costos. Dos INICTEL reutilizan exactamente el mismo DOCX.
- [x] Wipe y re-poblado 2026-07-10: solo bucket tecnologia (segmentos 43 y 81), 158 contratos vigentes disponibles, pipeline v2 completo. Validacion previa con lote de 5.
- [x] Operacion 24/7 (2026-07-14): scheduler fuera del profile opcional (corre siempre con `docker compose up -d`), `restart: unless-stopped` en todos los servicios, `DATABASE_URL` parametrizable en el compose (`${DATABASE_URL:-postgres local}`) para apuntar a una BD en la nube sin editar YAML, y alcance fijado a tecnologia con `SEACE_ALLOWED_SEGMENTS=43,81` en `.env.local`. Primer ciclo automatico: 182 contratos vistos, upsert sin duplicados.
- [x] Carga controlada transporte/legal en VM (2026-07-16): segmentos 78 y 80, solo Servicio (`objeto=2`) Vigente (`estado=2`), batches `transporte-20260716` y `legal-20260716`. Se procesaron 458 contratos (122 transporte + 336 servicios profesionales), 457 descargas, 452 TDR PDF extraidos y normalizados, 21 extracciones reutilizadas, 1 marcada para revision humana y 0 jobs `dead` nuevos. Duracion 24m56s; costo Gemini USD 1.643652. Excepciones documentales: 3 DOCX y 2 RAR no se extraen por el guard PDF-only; 2 PDFs de transporte no generaron facets.

## Documentos y R2

R2 retirado el 2026-07-14: la web hace preview y descarga en streaming directo desde SEACE (HTTP Range), la extraccion re-descarga de SEACE, y nadie consumia los previews subidos. `download_file` quedo reducido a identificar el archivo (sha256/MIME/clase) y disparar la extraccion. Se elimino `storage/r2.py`, la dependencia `boto3`, los settings `r2_*`/`pdf_preview_*` y las funciones de compresion de preview.

- [x] Implementar descarga temporal de PDF.
- [x] Calcular `sha256_original`.
- [x] Detectar MIME real por magic bytes.
- [x] Clasificar documento: TDR/requerimiento/otro.
- [x] ~~Comprimir y subir previews a R2~~ (retirado 2026-07-14: sin consumidores; preview en vivo desde SEACE).
- [x] Mantener URL original de SEACE para descarga.

## Extraccion IA

- [x] Migrar prompt `tdr_extraction_v1` al worker.
- [x] Implementar cliente Gemini.
- [x] Implementar `countTokens`.
- [x] Actualizar extracción primaria, onboarding y copiloto a `gemini-3.1-flash-lite`; modelo confirmado en la API oficial y tarifa estándar registrada (`$0.25` entrada / `$1.50` salida por 1M tokens) el 2026-07-12.
- [x] Implementar fallback a `gemini-2.5-flash`.
- [x] Reutilizar extracciones por `sha256_original` para PDFs duplicados entre contratos.
- [x] Guardar tokens y costo por extraccion.
- [x] Guardar `prompt_version`.
- [x] Guardar `schema_version`.
- [x] Manejar respuestas truncadas aumentando `maxOutputTokens`.
- [x] Marcar `requires_human_review` si falla validacion.
- [x] Prompt `tdr_extraction_v2` con schema fijo: `response_schema` Pydantic (`TdrExtractionV2`) en la llamada Gemini, prompt como `system_instruction`, enums para tipo_pago/penalidades/facets y campos numericos (plazo_dias, tope_pct, monto). Salida estructuralmente identica en cada extraccion; validado en vivo con los 5 PDFs del golden set (2026-07-09).
- [x] Restringir reuso por `sha256_original` a extracciones del mismo `prompt_version` vigente.

## Validacion y golden set

- [x] Crear modelos Pydantic para JSON de extraccion.
- [x] Validar JSON estricto.
- [x] Implementar schema repair simple si falta campo opcional.
- [x] Crear golden set con los 5 TDRs iniciales.
- [x] Guardar expected JSON revisado manualmente.
- [x] Crear comando para correr golden set.
- [x] Regenerar golden set expected con schema v2 (2026-07-09, costo USD 0.0054) y adaptar `run_golden.py` a la proyeccion v2.
- [x] Guard de extraccion vacia (2026-07-10): si Gemini devuelve `{}` (PDF escaneado/ilegible), reintentar con el modelo fallback y marcar `requires_human_review`.
- [x] Extraccion solo para PDFs (`mime = application/pdf`); TDRs .docx no van a Gemini (evita dead-letter por MIME no soportado).
- [x] La categoria 1 de SEACE manda sobre el nombre de archivo: cat-1 clasificado como `otro` se trata como `tdr` (los TDR reales llegan como "P513.pdf").
- [x] Comparar campos criticos: penalidades, experiencia economica, roles, forma de pago.

## Normalizacion

- [x] Implementar `derive_summary`.
- [x] Implementar `derive_facets`.
- [x] Implementar `contract_filter_index`.
- [x] Implementar `facet_hash`.
- [x] Implementar `diff_facets`.
- [x] Reextraer y rematchear solo si diff no esta vacio.

## Matching

Rediseno 2026-07-09: el motor deterministico fue reemplazado por analisis LLM bajo demanda (decision de producto: comparar 1 perfil vs 1 TDR al hacer click, no matching masivo). El fit de rubro (segmento CUBSO + keywords) es la senal barata del feed.

- [x] ~~Motor deterministico con reglas por facet~~ (retirado: producia verdes falsos con score 0 y gris masivo; ver `docs/qa`)
- [x] Implementar `analyze_match` (jobs/analyze_match.py): analisis LLM 1 perfil vs 1 contrato con `response_schema` (`MatchAnalysisV1`): por requisito estado/critico/gap/accion + veredicto + score + resumen + acciones recomendadas.
- [x] Prompt `match_analysis_v1` con reglas de veredicto (critico no_cumple -> rojo; papeleo no quita verde; penalidades no se evaluan) y bandas de score por veredicto (verde 85-100, ambar 50-84, gris 30-49, rojo 0-29).
- [x] Guard de re-analisis por hash: solo re-analiza si cambian facets o perfil (breakdown_json.meta).
- [x] `match_contract`/`match_profile` delegan a `analyze_match` sobre matches existentes (los endpoints web no cambiaron).
- [x] Persistencia en `matches` (breakdown_json = analisis completo + meta de costo/modelo; missing_actions_json = gaps con accion).
- [x] Notificaciones: new_match y verdict_change se mantienen.
- [x] Limpieza de 419 matches auto-generados por el motor viejo (sin datos de usuario).
- [x] Validado en vivo: VEYON vs 78188 (rojo 25, gap "S/ 24,000 vs S/ 120,000") y E2E en navegador via boton Evaluar (~USD 0.0007 por analisis).
- [x] Prompt `match_analysis_v2` (2026-07-10): estilo telegrafico con limites de palabras (gap <=10, accion <=8 imperativa, resumen <=2 frases, max 4 acciones) + truncado defensivo al persistir. Campos veredicto/score/resumen/requisitos REQUERIDOS en el schema (con default Gemini los omitia y caian al default gris/0).
- [x] Re-analisis incremental (2026-07-10): al regenerar, el analisis previo (veredicto/score/estados por requisito) se pasa como base; el modelo mantiene lo que no cambio y ajusta solo diferencias. Verificado estable: rojo 29 en corridas consecutivas.
- [x] Clamp determinista del score a la banda del veredicto (verde 85-100, ambar 50-84, gris 30-49, rojo 0-29) en analyze_match: un "rojo 35" ya no puede existir.
- [x] Reglas deterministas sobre el juicio del LLM (2026-07-10): experiencia economica se corrige por aritmetica (cubre todo -> cumple; >=30% -> accionable "Formar consorcio para cubrir S/ X"; <30% -> gap duro) y el veredicto se agrega por reglas desde los estados por requisito. Ancla dura: si ningun requisito cambio de estado vs el analisis previo, veredicto y score se mantienen identicos. Validado: CM-495-SERVIR paso de rojo 20 a ambar 50 (su unico critico era el monto); CM-7282 sigue rojo honesto (falta experiencia especifica).

## Notificaciones

- [x] Enrutar TDRs validados por el fit real de cada perfil, autoevaluar nivel 2+ y notificar scores estrictamente mayores a 50 sin una llamada LLM adicional; smoke real Gemini guardo 84/100 y encolo la alerta (2026-08-03).
- [x] Ejecutar barrido inicial controlado de oportunidades vigentes para VEYON en produccion: 3 contratos enrutados, 2 evaluados, alerta 84/100 enviada in-app y Telegram, y colas `match`, `llm` y `notify` en `done` (2026-08-03).
- [x] Implementar preferencias de notificacion.
- [x] Implementar canal email/Gmail.
- [x] Implementar canal Telegram.
- [x] Implementar in-app notifications.
- [x] Implementar regla anti-ruido.
- [x] Notificar solo nuevas oportunidades relevantes.
- [x] Notificar cambios de veredicto.
- [x] Implementar digest si supera max alertas por dia.
- [x] Respetar afinidad mínima `verde/ambar` y deduplicar por usuario, contrato, canal y motivo antes de encolar una alerta (2026-07-31).

## Observabilidad

- [x] Registrar `pipeline_events` por etapa.
- [x] Medir duracion por job.
- [x] Registrar errores por etapa.
- [x] Crear alerta tecnica si contract test SEACE falla.
- [x] Crear alerta si `poll_search` no corre por mas de 90 min.
