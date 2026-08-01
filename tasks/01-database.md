# Tareas - Database

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
- [x] Separar frases exactas y términos fuertes de cada línea de negocio (migración `0014`) sin perder compatibilidad con `keywords`.
- [x] Persistir keywords transversales de identidad en `company_profiles.company_keywords` (migración `0015`), sin listas de rubro hardcodeadas.
- [x] Agregar `profile_hash`.
- [x] Agregar campos JSON para identidad, experiencia, equipo, roles contratables y certificaciones.
- [x] Poblar en la VM el workspace de `admin@route.com` con el perfil completo de VEYON SAC y sus 3 líneas tecnológicas desde la copia local; validar integridad y recalcular matches (2026-07-20).

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

- [x] Crear tabla `notification_preferences`.
- [x] Crear tabla `notifications`.
- [x] Definir canales iniciales: `email`, `telegram`, `in_app`.
- [x] Agregar preferencias de digest y max alertas por dia.
