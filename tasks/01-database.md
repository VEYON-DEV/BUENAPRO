# Tareas - Database

- [x] Aplicar y verificar únicamente 0046 para categorías PROD2 por título (20/10/2026): tres tablas separadas y migración registrada atómicamente; 1.018 recibos done y 1.135 filas con título exacto, hashes, costos y uso verificados por PG fresco. Sin alterar extracciones ni 144 matches existentes. Evidencia docs/prod2-title-categories-execution-20261020.json.

- [x] Crear, validar y aplicar0043 para corpus/chunks/vectores/retrieval y reservas embeddings PROD2 (08/10/2026): tablas aditivas sin extensión ni originalesR2; cuatro contratos PostgreSQL reales en schema aislado bajo executor, preparación exclusiva idempotente, columnas/PK/FK/status y recibos/pagos exactos verificados. Smoke integrado real confirma texto reducido durable y replay sin nueva llamada con API simulada. Cero proveedores/fuente durante migración; guía `docs/prod2-embeddings-runtime-20261008.md`.

- [x] Crear `saved_opportunities` (migración `0044_saved_opportunities.sql`, 13/10/2026): marcadores por workspace sobre `opportunities.id`, PK `(tenant_id, opportunity_id)`, índice por fecha; aditiva. Aplicada a la BD de producción por solicitud del usuario solo esta versión (sin `migrate.py`, porque `0042` sigue pendiente a propósito) y registrada en `schema_migrations`; prueba real guardar→Gantt→quitar dejó la tabla en 0 filas.
- [ ] Validar en PostgreSQL aislado y aplicar migración 0042 para referencias de originales 7z de PROD2: ampliar únicamente el CHECK de formato de `prod2_document_downloads`, verificar contrato/ledger idempotente bajo coordinador y conservar revisiones existentes. Implementación preparada para recuperación opt-in de 1043284; todavía sin aplicar a producción.

- [x] Modelar recursos/publicaciones PROD2 con migración0041 aplicada en producción (06/10/2026): prod2_documents mantieneUUID único; prod2_document_events conserva etapa/tipo/fecha/identidad e historial listado. Backfill solo del evento previamente conocido, ledger/contrato verificados y hashes/revisiones anteriores intactos. Parser/paginación/repositorio/LLM1 cubren reutilización y conflictos de nombre/versión. Principal valida prueba PostgreSQL real nueva con rollback y readback productivo; cinco contratos PG existentes aprobados por agente de BD. Recuperadas1006060/1025179/1031165 sin modelos/descargas. Evidencia docs/prod2-document-events-recovery-20261006.md.

- [x] Crear y validar migración0040 para seguimiento de barridos/consultas PROD2: época con ventana fija, identidad keyword/objeto/ejercicio/fechas, estado/owner/claim/intentos/evidencia completa y último timestamp fuente. No guarda candidatos ni infiere vigencia. Modo de preparación exclusivo0040, ledger idempotente y contrato PostgreSQL real en schema propio/rollback: cuatro pruebas del principal aprobadas, sin cambios públicos (06/10/2026).
- [x] Aplicar 0040 al destino productivo para el primer lote de 30 keywords autorizado (06/10/2026): preparación exclusiva, contrato y ledger verificados; ambas tablas comenzaron vacías. Cero llamadas fuente/proveedores durante la migración. La ejecución y sus datos se validan por separado.

- [x] Crear y validar0039/`prod2_document_selection_cache` con hash textual global, FK solo tras aceptación, owner/lease/status/recibo y bloqueo de pagos inciertos. El nuevo script incluye `--prepare-selector-cache`, que aplica solo0039 y registra su versión tras verificar el contrato, sin reejecutar DDL legacy. PostgreSQL shadow real verifica preparación idempotente, claims/reuso/replay/raíz archivada/cambio no ancla y rollback sin tablas públicas/modelos (06/10/2026).
- [x] Aplicar 0039 al destino del nuevo script seleccionado y verificar versión/tabla vacía antes de la primera carga (06/10/2026): preparación exclusiva completada y ledger/tabla confirmados en producción, sin reejecutar DDL legacy ni llamar fuente/proveedores. Primer lote de 30 autorizado por el usuario.

- [x] Validar y aplicar0038 para caché/reserva Jev previa a discovery y miembros de corrida aceptada/rechazada (06/10/2026): sin FK del caché hacia oportunidad, hash semántico con reuso legacy, bloqueo de facturación incierta y promoción exacta>=60.29tests focalizados y contrato PostgreSQL shadow/rollback aprobados; producción registra10aceptadas/1rechazo sin promoción y10 participaciones canónicas vinculadas a extracción/SHA. Pilotos históricos preservados; evidencia `docs/prod2-accepted10-20261006.md`.

- [x] Validar y aplicar migración aditiva 0037 para manifest/checkpoints/leases/observaciones PROD2 y evaluaciones temporales versionadas vinculadas a ficha. Contrato PG aislado de16 candidatos/two keywords con rollback y secuencias públicas invariantes; migración por SHA exacto y transaction/version, preservando10 discoveries/50evaluations previos. Smoke productivo tres cronogramas/evaluaciones temporales, repetición sin duplicados; `docs/prod2-six-months-ingestion.md` (2026-10-06).

- [x] Aplicar migraciones aditivas 0035/0036 para piloto PROD2: discovery canónico, lote limitado a 10, inventarios/versiones de documentos, reservas antes del pago, extracciones/facets y procedencia de matches. Integración PostgreSQL en schema temporal con rollback validada; producción lectura posterior de 10 registros, 10 extracciones, 9 matches y una revisión y repetición sin duplicados. Perfiles originales preservados; evidencia `docs/prod2-pilot10-20261005.md` (2026-10-05). No completa el modelado/planificador general del convenio.

- [x] Aplicar migración0033 de consorcio/subcontratación en opportunities con cuatro estados y evidencia JSON, defaults no identificados sin inferencia histórica; PostgreSQL16 valida constraints/idempotencia y smoke productivo revertido conserva datos (2026-10-02).

- [x] Agregar migración0032 con `schedule_fetched_at` para ambas fuentes, diferenciando refresco parcial de cronograma de frescura del detalle completo; aplicada en QA y producción con respaldo previo de tablas (2026-10-01).
- [x] Ejecutar actualización productiva acotada de cronogramas guardados (2026-10-01): 69 concursos PROD4/556 etapas y 206 menores PROD6 segmentos43/81/412 etapas, dry-run y apply sin fallos, conteos SQL confirmados. Sin históricos adicionales ni PDFs/Gemini; respaldo `schedule-before-20261001.dump` preservado.

## Base del proyecto

- [ ] Reconciliar `schema_migrations` de producción con las tablas existentes para `0009` y `0011`-`0014`; verificar estructura antes de registrar versiones y evitar reejecutar DDL ya aplicado.
- [x] Elegir herramienta de migraciones: Prisma, Drizzle, node-pg-migrate o SQL plano.
- [x] Crear configuracion de conexion a PostgreSQL.
- [x] Crear script de migracion inicial.
- [x] Habilitar extensiones necesarias: `pgcrypto`, `citext`, `pg_trgm`.
- [x] Definir convencion de timestamps: guardar UTC, renderizar America/Lima.

## Tenancy y usuarios

- [x] Crear tabla `tenants`.
- [x] Crear tabla `users`.
- [x] Crear tabla `tenant_members`.
- [x] Definir roles iniciales: `owner`, `admin`, `member`.
- [x] Preparar tablas necesarias para auth si se usa Auth.js/NextAuth.

## Catalogos

- [x] Crear tabla `cat_cubso_segmentos`.
- [x] Crear tabla `cat_entidades`.
- [x] Crear tabla `cat_ubigeo`.
- [x] Crear tabla o configuracion versionada para segmentos CUBSO habilitados en el MVP.
- [x] Crear seed inicial de objetos SEACE: Bien, Servicio, Obra, Consultoria de Obra.
- [x] Crear seed inicial de estados SEACE: Vigente, En Evaluacion, Culminado.
- [x] Crear job o script para sincronizar segmentos CUBSO por anio.
- [x] Crear seed inicial de segmentos objetivo MVP: tecnologia, transporte y legal, despues de validar catalogo 2026.
- [x] Agregar migración reproducible con los 56 segmentos CUBSO 2026 para validar perfiles, manteniendo `mvp_enabled_cubso_segments` como alcance operativo de ingesta (`0008_cubso_catalog_2026.sql`).

## Contrataciones SEACE

- [x] Separar histórico analítico del feed operativo con entidades, áreas usuarias, proveedores y resultados culminados; `idContrato` es llave estable y el código SEACE se conserva completo y parseado (`0017_historical_outcomes.sql`, validado localmente el 2026-07-31).
- [x] Persistir checkpoint y estado del backfill histórico por segmento para reanudar barridos largos sin repetir páginas completadas (`0018_historical_backfill_progress.sql`).
- [x] Ampliar `codigo_correlativo` a `BIGINT` para códigos SEACE como `CM-9437000002-2026-OACGD` (`0019_historical_code_correlative_bigint.sql`).
- [x] Normalizar departamento, provincia y distrito del histórico para análisis geográfico; backfill de 4,508 resultados e índices de mercado aplicados en producción (`0021_historical_geography.sql`, 2026-07-31).

- [x] Crear `seace_connections` y auditoría mínima por tenant para credenciales/sesiones cifradas (migración `0009`).
- [x] Crear borradores de postulación normalizados: cabecera, ítems, RTM y documentos (`0010`), más revisión de anexos (`0011`).
- [x] Persistir adjuntos del proveedor en el borrador MVP con aislamiento por aplicación, validación de tipo/tamaño y borrado en cascada (`0012`).
- [x] Crear memoria persistente del copiloto: sesiones por contrato, mensajes, ejecuciones y conjuntos de cambios confirmables con auditoría (`0013`).

- [x] Crear tabla `seace_contracts`.
- [x] Agregar unique constraint por `id_contrato`.
- [x] Agregar unique constraint por `codigo`.
- [x] Agregar columnas `hash_search`, `hash_detail`, `hash_files`.
- [x] Agregar columnas `raw_search_json`, `raw_detail_json`.
- [x] Agregar indices para feed: estado, fecha fin cotizacion.
- [x] Agregar indice full-text para descripcion.

## Documentos

- [x] Crear tabla `contract_documents`.
- [x] Agregar unique constraint por `id_contrato + id_contrato_archivo`.
- [x] Agregar campos para URL original SEACE.
- [x] Agregar campos para preview R2.
- [x] Agregar campos `sha256_original`, `sha256_preview`.
- [x] Agregar campos de tamaño original y preview.

## Extracciones

- [x] Crear tabla `tdr_extractions`.
- [x] Agregar `model`.
- [x] Agregar `prompt_version`.
- [x] Agregar `schema_version`.
- [x] Agregar tokens y costo: `input_tokens`, `output_tokens`, `cost_usd`.
- [x] Agregar `raw_extraction_json`.
- [x] Agregar `summary_json`.
- [x] Agregar `requires_human_review`.
- [x] Agregar constraint para una extraccion current por documento.

## Facets e indice de filtros

- [x] Crear tabla `requirement_facets`.
- [x] Agregar `facet`, `label`, `required`, `details_json`, `evidence_json`.
- [x] Agregar `facet_hash` para diff.
- [x] Crear tabla `contract_filter_index`.
- [x] Agregar arrays `roles_requeridos`, `facets`, `documentos_clave`.
- [x] Crear indices GIN para arrays.

## Perfiles de empresa

- [x] Crear tabla `company_profiles`.
- [x] Crear tabla `business_lines`.
- [x] Crear biblioteca empresarial tenant-safe para datos reutilizables y documentos de postulación (`company_knowledge_items`, `company_documents`) con tags libres, metadata JSON e índices GIN (`0023_company_library.sql`, validada localmente el 2026-08-08).
- [x] Separar frases exactas y términos fuertes de cada línea de negocio (migración `0014`) sin perder compatibilidad con `keywords`.
- [x] Persistir keywords transversales de identidad en `company_profiles.company_keywords` (migración `0015`), sin listas de rubro hardcodeadas.
- [x] Agregar `profile_hash`.
- [x] Agregar campos JSON para identidad, experiencia, equipo, roles contratables y certificaciones.
- [x] Poblar en la VM el workspace de `admin@route.com` con el perfil completo de VEYON SAC y sus 3 líneas tecnológicas desde la copia local; validar integridad y recalcular matches (2026-07-20).
- [x] Agregar `business_lines.is_favorite` (migración `0051`, 31/10/2026): líneas prioritarias que suben las licitaciones mayores. Aplicada solo en la copia local; todas las líneas quedan en `false` y no se recalcula nada.
- [x] `0051` (líneas prioritarias) y `0052` (cola de lectura) aplicadas en producción el 31/10, antes de la web `mayores2w-3ca1917`.

## Matching y seguimiento

- [x] Crear `saved_contracts` por workspace para guardar licitaciones sin crear matches ni alterar el embudo (migración `0016`, aplicada en la VM el 2026-07-20).
- [x] Crear borradores normalizados de postulación (`application_drafts`, ítems, RTM y documentos) sin representar un envío oficial (migración `0010`).
- [x] Crear tabla `matches`.
- [x] Agregar `score`, `verdict`, `breakdown_json`, `missing_actions_json`.
- [x] Agregar seguimiento: `user_state`, `responsable_id`, `monto_ofertado`, `notas`.
- [x] Crear tabla `match_events`.
- [x] Crear tabla `match_tasks` para checklist de postulacion.
- [x] Agregar indices por perfil, verdict y estado de usuario.

## Jobs y observabilidad

- [x] Crear tabla `worker_jobs`.
- [x] Agregar `dedup_key`.
- [x] Agregar indice para claim: `status`, `run_after`, `priority`.
- [x] Crear tabla `pipeline_events`.
- [x] Crear tabla `api_contract_checks`.
- [x] Crear tabla `extractor_golden_cases`.

## Notificaciones

- [x] Persistir reglas de automatizacion por perfil, integracion Telegram cifrada, multiples destinatarios y estado de lectura in-app (`0022_automation_telegram.sql`, validada localmente el 2026-08-03).
- [x] Crear tabla `notification_preferences`.
- [x] Crear tabla `notifications`.
- [x] Definir canales iniciales: `email`, `telegram`, `in_app`.
- [x] Agregar preferencias de digest y max alertas por dia.
- [x] Añadir `chat_artifacts` tenant-scoped para persistir entregables generados por Codex junto al mensaje y run, con contenido durable en PostgreSQL y limite de 15 MB (2026-08-09).

## Modelo universal multi-fuente

- [x] Añadir `objeto_codigo` al histórico y checkpoint compuesto por objeto/segmento; preservar las 5 636 filas de Servicios como objeto 2. Migraciones 0025 y 0026 probadas y aplicadas en producción; 147 años del feed corregidos desde `fec_publica` (2026-09-29).

- [ ] Crear `sources`, capacidades, politicas de acceso y cuentas tenant-scoped cifradas.
- [ ] Crear `ingestion_runs`, particiones, checkpoints y observaciones append-only por fuente.
- [ ] Modelar perfil comercial conjunto del convenio, registro mínimo de discovery PROD2, decisiones Jev y catálogo de candidatos con versiones únicas; conservar procedencia de evidencia por integrante y reutilizar identidad/checkpoints existentes. Diseño `docs/shared-technology-ingestion-architecture.md`.
- [ ] Crear organizaciones universales con identificadores, aliases y roles.
- [x] Crear `opportunities` con UUID canonico y `opportunity_sources` unico por fuente, clase de ID e ID externo; separar tipo de registro, objeto, metodo, etapa, acceso y accionabilidad (`0027`, PostgreSQL aislado y produccion el 2026-09-29).
- [x] Migrar los contratos PROD6 existentes a identidad canonica con referencia por fuente y comprobar integridad e idempotencia del backfill (`0027`; respaldo previo, 1 963 contratos / oportunidades / fuentes, 0 sin UUID en produccion el 2026-09-29).
- [x] Vincular todos los resultados historicos PROD6 a la misma identidad canonica, sin duplicar los IDs que coinciden con contratos activos (`0028`; respaldo previo y prueba con rollback, 6 938 resultados / 0 sin UUID / 0 divergentes, 2026-09-29).
- [ ] Modelar releases, eventos, fechas, lotes, items, adjudicaciones y contratos 1:N.
- [x] Crear tablas aditivas de PROD4 por procedimiento, ítem CUBSO, metadatos de documentos y cronograma; preservar identidad canónica, objeto bien/servicio y salida de ventana sin inferir adjudicación (`0029`, PostgreSQL 16 aislado y producción tras respaldo verificado, 2026-09-29).
- [x] Separar afinidad preliminar PROD4 y evaluación documental final por perfil (`0030`/`0031`), con extracción, facets y `opportunity_matches`; migraciones probadas desde cero y aplicadas en producción tras respaldo (2026-09-29).
- [ ] Crear documentos y extracciones universales con version, procedencia, licencia y visibilidad.
- [ ] Crear esquemas, terminos y mapeos de taxonomias CUBSO, UNSPSC, CPV y privadas.
- [ ] Migrar referencias de `id_contrato` a `opportunity_id` mediante backfill, dual-write y compatibilidad temporal.

- [x] Aplicar únicamente 0047 y verificar la publicación explícita de metadatos de los 157 seleccionados PROD2: 144 registros/UUID nuevos y 13 preservados, sin crear fichas/cronogramas/matches ni sobrescribir históricos. Transacción y readback productivo verificados; web c338422 desplegada desde Git con 603 hashes comprobados, capturas reales en tres tamaños y API200 del perfil DIN (2026-10-20).
