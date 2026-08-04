# Tareas - Backend/Web

## Estructura Next.js

- [x] Organizar `apps/web` por features: `feed`, `opportunity`, `profile`, `tracking`, `settings`.
- [x] Crear `components/ui`.
- [x] Crear la capa de layout inicial, posteriormente absorbida por el dominio `features/shell`.
- [x] Crear `server/db`.
- [x] Crear `server/auth`.
- [x] Crear `server/services`.
- [x] Definir estilo visual base de BuenaPro.
- [x] Documentar el sistema visual profesional, actualmente centralizado en `DESIGN.md`.

## Auth y tenancy

- [x] Elegir Auth.js/NextAuth o Better Auth.
- [x] Implementar login.
- [x] Implementar registro inicial.
- [x] Crear tenant al registrar owner.
- [x] Inyectar `tenant_id` en todas las consultas.
- [x] Proteger rutas privadas.
- [x] Crear middleware de autorizacion.
- [x] Restringir `x-tenant-id` y `DEV_TENANT_ID` a desarrollo para impedir suplantación de tenant en producción.

## API interna

- [x] Exponer reglas de automatizacion y configuracion/test de Telegram tenant-safe, sin devolver el token almacenado; GET local, cifrado PostgreSQL y mensaje real validados (2026-08-03).
- [x] Implementar `GET /api/contracts/:id/history` con scoring explicable por CUBSO, keywords, entidad y recencia; desiertos excluidos del precio y umbral endurecido contra comparables amplios.
- [x] Implementar `GET /api/dashboard` con cierres, preparación, mercado del perfil y proveedores frecuentes, más admin interno para iniciar/consultar backfill histórico.
- [x] Implementar `GET /api/market` tenant-safe con alcance de perfil/todo el mercado, búsqueda textual, filtros de segmento, resultado, ubicación, entidad, año y precio; incluye tendencia, rankings, contratos y proveedores (2026-07-31).

- [x] `GET /api/contracts` para explorar contratos cargados aunque no exista match.
- [x] `GET /api/feed`.
- [x] `GET /api/contracts/:id`.
- [x] `POST /api/contracts/:id/track` para asegurar match y mover al embudo.
- [x] `GET /api/contracts/:id/original/:docId`.
- [x] `GET /api/profile`.
- [x] `PUT /api/profile`.
- [x] `PUT /api/profile/keywords` para editar identidad transversal, sanear/deduplicar señales y encolar rematch.
- [x] `GET /api/lines`.
- [x] `POST /api/lines`.
- [x] `PATCH /api/matches/:id`.
- [x] `GET /api/tracking`.
- [x] `GET /api/notifications/prefs`.
- [x] `PUT /api/notifications/prefs`.
- [x] Guardar preferencias de alerta para el usuario autenticado, incluyendo activación, afinidad mínima y límite diario, sin duplicar filas por canal (2026-07-31).
- [x] `GET /api/matches/:id/tasks`.
- [x] `POST /api/matches/:id/tasks`.
- [x] `PATCH /api/matches/:id/tasks/:taskId`.
- [x] `DELETE /api/matches/:id/tasks/:taskId`.
- [x] `POST /api/internal/jobs` protegido para reproc manual.
- [x] Documentar endpoints en OpenAPI/Swagger.
- [x] `POST /api/contracts/:id/analyze` (2026-07-09): encola analisis LLM del contrato vs el perfil activo del tenant (409 si no hay perfil); el resultado se persiste en `matches`.
- [x] `GET /api/contracts` expone `fit_keyword_hits` (keywords de las lineas encontradas en la descripcion) y ordena por score -> fit -> cierre.
- [x] Refresh de detalle SEACE bajo demanda al abrir `/oportunidad/[id]` con cache TTL 6 h (`server/services/seaceDetail.ts`); expone `fec_ini_cotizacion` y tamanio de documentos.
- [x] Conexión SEACE persistente: AES-256-GCM, access/refresh token, renovación y re-login automático; login real y endpoints read-only de consultas/contexto de cotización validados el 2026-07-12 (`200`).
- [x] Preview PDF sin R2 desde URL SEACE con streaming HTTP Range (`206`) y rechazo explícito de DOCX/XLSX (`415`).
- [x] API de borradores de postulación: crear desde contrato, sincronizar contexto SEACE, leer/editar cabecera, ítems, RTM y revisión de documentos; sin envío oficial. Validado con contrato 78753.
- [x] API de borradores de postulación: iniciar desde contrato, snapshot autenticado SEACE, ítems/RTM/documentos dinámicos y edición tenant-safe; sin envío oficial.
- [x] API tenant-safe para subir, descargar y eliminar adjuntos del borrador (PDF/Word/Excel, máximo 10 MB, PostgreSQL sin R2). Ciclo completo y aislamiento entre tenants validados con match 21.
- [x] Copiloto tenant-safe por licitación: memoria PostgreSQL, Gemini con citas, lectura acotada de TDR/DOCX/perfil/borrador, compactación y change sets con confirmación manual; nunca envía a SEACE. Agente real y confirm/reject validados con contrato 78753.
- [x] Ejecutar el análisis manual perfil–licitación directamente en `POST /api/contracts/:id/analyze`, sin `worker-llm`: conserva prompt v2, schema, reglas económicas, score estable, persistencia y costos. Validado con contrato 78764 usando `gemini-3.1-flash-lite`; job pendiente anterior cerrado automáticamente.
- [x] Robustecer onboarding empresa: prompt externo versionado, modelo Gemini configurable, crawl seguro multipágina, evidencia por URL, frases/términos separados y guardas CUBSO por servicio vendido. Motor de fit por línea y segmento con frase exacta 15, término fuerte explícito 10, sin división automática, topes 30/45 y hits explicables.
- [x] Prompt universal v3 sin ejemplos sectoriales: separa términos y frases de identidad de empresa, exige al menos un término autónomo y clasifica CUBSO por obligación/entregable. El motor usa stemming español de PostgreSQL, conserva frases ordenadas, suma identidad una sola vez (10) y evita doble conteo con señales de línea.

## Feed

- [x] Crear pagina `/feed`.
- [x] Mostrar oportunidades por match.
- [x] Mostrar codigo, entidad, descripcion, fecha limite y verdict.
- [x] Agregar filtros base: objeto, estado, segmento, region, fecha.
- [x] Agregar filtros inteligentes: verdict, facets, roles, tipo pago, plazo.
- [x] Agregar busqueda por texto.
- [x] Agregar paginacion.
- [x] Mostrar estado de carga y vacio.

## Detalle de oportunidad

- [x] Exponer guardado reversible de oportunidades con `PUT/DELETE /api/contracts/:id/saved`, `is_saved` en lista/detalle y filtro `saved=true`, aislado por tenant.
- [x] Crear pagina `/oportunidad/[id]`.
- [x] Mostrar ficha de contratacion.
- [x] Mostrar PDF preview desde R2.
- [x] Mostrar boton para descargar original desde SEACE.
- [x] Mostrar summary compacto.
- [x] Mostrar facets agrupados.
- [x] Mostrar breakdown del match.
- [x] Mostrar acciones sugeridas.
- [x] Mostrar penalidades y forma de pago.
- [x] Mostrar evidencia textual expandible.

## Perfil de empresa

- [x] Crear pagina `/perfil`.
- [x] Crear wizard de identidad: RUC, RNP, CCI.
- [x] Crear seccion lineas de negocio.
- [x] Crear seccion experiencia economica.
- [x] Crear seccion contratos previos.
- [x] Crear seccion equipo/personas.
- [x] Crear seccion roles contratables.
- [x] Crear seccion equipos/activos.
- [x] Crear seccion certificaciones/seguros.
- [x] Al guardar perfil, encolar `match_profile`.

## Seguimiento

- [x] Crear pagina `/seguimiento`.
- [x] Mostrar estados del embudo.
- [x] Permitir cambiar `user_state`.
- [x] Permitir asignar responsable.
- [x] Permitir registrar monto ofertado.
- [x] Permitir notas.
- [x] Crear historial de eventos.
- [x] Crear checklist backend de postulacion por match.
- [x] Crear tareas default al pasar a `en_preparacion` o `postulada`.
- [x] Exponer conteo de tareas en `GET /api/tracking`.

## Configuracion

- [x] Crear pagina `/configuracion`.
- [x] Editar preferencias de notificacion.
- [x] Configurar Telegram/email.
- [x] Configurar max alertas por dia.
- [x] Configurar digest.
- [x] Configurar horarios silenciosos.

## Admin tecnico minimo

- [x] Crear vista interna de `worker_jobs`.
- [x] Ver jobs pendientes/fallidos/dead.
- [x] Reintentar job manualmente.
- [x] Crear vista de `pipeline_events`.
- [x] Crear vista de `requires_review`.
- [x] Crear vista de contract tests SEACE.
