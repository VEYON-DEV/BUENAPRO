# Tareas - Backend/Web

- [x] Publicar categorías preliminares PROD2 (20/10/2026): precedencia documental, fallback por título/hash/recibo done/proyección v2 y filtro antes de count/página. Web ad74430 publicada desde Git limpio, imagen title-categories-ad74430, 602 hashes y salud 200. Seis totales públicos verificados y 144 matches intactos; menores sin backfill pagado. Evidencia docs/prod2-title-categories-execution-20261020.json.

- [x] Exponer las seis categorías simples compartidas en ambas fuentes (20/10/2026): commercial_category validado, OR por CSV en API y AND con criterios anteriores; filtro/count antes de página sobre clasificación vigente válida con raw prioritario. Listas/detalles/feed añaden commercial_categories derivado del JSON común, sin inferir desde título/score ni modificar notas/permisos/procedencia. SQL y proyecciones coinciden en 41 fixtures por fuente; siete pruebas PG desechables, servicios 128 pass/1 skip y 32 focales del principal solapadas. TypeScript y arquitectura16 aprobados; código c1f1802. Sin migración, backfill, pagos ni despliegue; publicación pendiente en la tarea existente. Evidencia docs/simple-categories-20261020.json.

- [x] Exponer clasificación comercial común en APIs locales de PROD2, contratos menores y feed (20/10/2026): catálogo compartido, parámetros CSV/repetidos validados, OR dentro de eje y AND entre ejes antes de count/paginación; mantiene tenant/perfil, permisos y scores. Extracción vigente elegible más reciente, raw prioritario y Sin clasificar para bloques ausentes/invalidos, sin rescatar tags antiguos; auditoría comercial V5 degrada a revisión antes del filtro. JSONB existente evita migración/backfill y llamadas para históricos. CASE exterior impide validar repetidamente raw sin tags: EXPLAIN read-only conserva resultados y reduce conteo combinado 7979→445 ms en la muestra; no es SLA. Código 20dea69/2a0f646; 119 pruebas de servicios aprobadas/1 omitida, seis PostgreSQL temporales (35 fixtures por fuente y cortocircuito), TypeScript y arquitectura16. Implementación validada localmente; publicación se verifica aparte. Evidencia tmp/commercial-filters-20261020/ y docs/commercial-classification-20261020.json.

- [ ] Publicar y verificar el selector simple Categoría y lectores preparados en producción desde Git limpio cuando se solicite: desplegar solo servicios autorizados, registrar commit/imagen y repetir lectura/API/QA. Desplegar código no autoriza iniciar lectores ni usar el opt-in sobre recibos históricos; nuevas lecturas requieren su configuración/namespace apropiados. No completar etiquetas mediante relecturas automáticas ni ejecutar el lote IA/industrial pendiente como parte del despliegue web.

- [x] Publicar orden «Buscador SEACE → Contratos menores» del Cronograma (16/10/2026): commit `f8504fb317fbaf00d0b71df1a09a6a87edfb2fd9` pushed, build Git limpio/OCI y588 hashes exactos; imagen `buenapro-web:timeline-tabs-f8504fb` / `sha256:27a717b1e1b2f4fddbf1e97c339e3123419050f30b848fa384bce67c8988df1a`. Solo web recreada, salud local200 y seis capturas públicas autenticadas desktop/laptop/mobile verifican orden, selección, teclado y periodo sin overflow/errores/writes. Entorno/configuración/monturas/puertos y ocho contenedores ajenos intactos;0modelos/migraciones. Primer intento con ubuntu no pudo leer dos objetos Git root0400: hashes descomprimidos correctos y fsck0; repetido con la cuenta de publicación, sin reemplazar objetos ni cambiar permisos. Limpieza581.526.747bytes lógicos entre worktrees propios y dos capturas iniciales duplicadas; finales/recibos/rollback y entradas PROD6 retenidos. Evidencia `tmp/timeline-tab-order-20261016/`; cierre en `docs/deployment-workflow.md`.

- [ ] Permitir seguimiento en Gantt de etapas posteriores al cierre de ofertas de las oportunidades PROD2 guardadas: `normalizeTimelineRows` actualmente elimina la oportunidad al vencer ofertas aunque quede buena pro u otra etapa. Detectado en la verificación solicitada el 16/10/2026; sin cambiar comportamiento porque el alcance fue solo verificar. Ver `docs/seace-saved-gantt-verification-20261016.md`.

- [x] Crear y verificar la cuenta administradora de DIN solicitada el 16/10/2026: un usuario nuevo con contraseña bcrypt y una única membresía admin en el tenant existente del perfil DIN; dueño/membresía anterior preservados, sin crear otro tenant. Contraseña aleatoria guardada solo en `.env.local` ignorado/0600, sin secretos en Git/recibos. Login real CSRF→Credentials→sesión (sin sesión QA fabricada) confirma rol admin, tenant/perfil DIN, acceso a 74 puntajes SEACE y miembros correctos. Sin modelos, notificaciones, migraciones ni despliegues. Evidencia privada `tmp/din-admin-account-20261016/`; 0 bytes de temporales retirados, se retienen recibos compactos y credenciales privadas necesarias.

- [x] Publicar desde Git el orden por puntaje del perfil activo y filtro «Con puntaje» de ambas fuentes (15/10/2026): commit05db6c9 pushed y build canónico Git limpio, imagen919b9e38/OCI exacta y585 hashes. Salud local/pública200, 51 valores de entorno/archivos privados/puertos/restart y ocho servicios ajenos preservados; HostConfig coincide exactamente al normalizar solo orden de dos monturas. Límite auditado: Config previo conservado solo como hash; igualdad material comprobada, igualdad binaria total no acreditada ni atribuida a una causa no probada. APIs auténticas y seis capturas productivas sin errores/escrituras verifican perfil/orden/filtro/paginación; sin migraciones ni reiniciar workers. Recibos tmp/score-first-build-20261015 y tmp/dinaut-two-dot-50-20261015/qa-production; rollback retenido.

- [x] Revisar, subir y desplegar los cambios locales del 14/10/2026: 48 archivos subidos a `main` en `72e3114`, perfil maestro-detalle, guardadas PROD2/cronograma y mejoras responsive. 233 Node/5 skip, TypeScript/arquitectura16 y build61 desde source confirmado; 34 capturas locales y 22 productivas, latencia/foco/dataset comprobados y 15 análisis guardados exactos. Corregidos overflow BIGINT y fallback solo tabla de marcadores, respuesta tardía/estado del editor y clave de ranking. PG read-only confirmó 0044 ya aplicada; sin nuevas migraciones/modelos/writes de negocio. Imagen canónica `f3233498`, etiqueta OCI y 546 hashes exactos, health200/configuración/ocho contenedores preservados. Limpieza propia 410602996 bytes locales/90867094 remotos; capturas/recibos/rollback/entradas pendientes retenidos y servidor local del usuario preservado. Cierre en `docs/deployment-workflow.md` y recibos privados `tmp/latest-local-release-20261014/`.

- [x] Regularizar el trabajo local acumulado en Git y publicación reproducible (13/10/2026 Lima): source/tests/config subidos a `main` en `d046863`, evidencia privada excluida del repositorio público. Build canónico desde worktree limpio del commit, imagen `ed8c43fb`, etiqueta OCI y 542 hashes runtime verificados; salud 200, 47 variables de aplicación y ocho servicios preservados. Se restauró únicamente `NEXT_TELEMETRY_DISABLED=1` desde la imagen anterior en la configuración privada. Quince resultados guardados exactos y ocho capturas productivas revisadas; sin migraciones ni nuevas inferencias. Limpieza propia: 238417 bytes locales y 90796380 remotos; recibos, capturas, rollback y pendientes conservados. Flujo y cierre en `docs/deployment-workflow.md`.

- [x] Exponer puntaje0–100 provisional por petición humana (13/10/2026): `provisional_score` desde score guardado en revisión/rango, separado del campo final protegido; PROD2 lista/process/match y menores lista/detalle. Sin match/inválido/null no inventa cero, conserva0real; provenance/perfil/fuente/final-score/orden/metadata intactos. Principal31testsbackend+3overlay y15APIs locales/productivas exactas con resultados originales. Doce archivos web publicados selectivamente, imagen f59038d8, /login200 y ocho contenedores/entorno/transportedocumental preservados. Cero modelos/DBwrites/reintentos/workers. `docs/provisional-score-visible-20261013.{md,json}`; limpieza33935bytes, QApropio detenido y evidencia/rollback retenidos. No se aprueban notas ni fuentes por esta exposición provisional.

- [x] Exponer disponibilidad del análisis aun con puntaje retenido y filtrar Con análisis (13/10/2026): PROD2 match_id/match_review_required en listado/detalle y analysis=done bajo mismo tenant/perfil/procedencia vigente antes de count/paginación; menores conserva scopelegacytenant, añade flag y filtro m.id. Listado general/ordenkeywords/scoreguard/cero validado intactos. Principal27testsbackend+3overlay, APIs auténticas locales/productivas:5PROD2/10menores=9no cerrados; revisiónfuentes/notes intactas. Doce archivos web selectivos, imagen d0349562, entorno/ocho contenedores preservados y /login200; overlay conserva transporteactual. `docs/feed-analysis-visible-20261013.{md,json}`. Cero modelos/mutaciones negocio/reinicios worker; limpieza26306bytes y rollback/evidencia retenidos.

- [x] Integrar y desplegar publicación segura de los nuevos análisis Dinaut PROD2 (13/10/2026): listado/detalle exponen `meta.score_validated=false` como score nulo/veredicto gris y ordenan con esa misma nota pública; conservan match, explicación y checklist. Legado sin flag/cero validado intactos y facets vinculadas a la extracción exacta. Principal19tests PROD2/3overlay, TypeScript/arquitectura/build y diez PG/API exactos para 5PROD2/5menores. Web selectiva cuatro archivos, imagen de811117, /login200, ocho contenedores/entorno preservados; overlay sobre runtime real conserva transporte R2/publicaciones y excluye cambios locales ajenos de descarga. Diez análisis provisionales guardados, cinco previos intactos, fuentes en revisión y notas no validadas retenidas. Ocho capturas locales/productivas revisadas; `docs/dinaut-ten-read-matches-20261013.{md,json}`. Limpieza69889bytes, inputs/recibos/revisiones/rollback conservados.

- [x] Validar e integrar análisis web LLM2 sustantivo y contexto compacto (13/10/2026): RNP/RUC/impedimentos/formularios en `administrative_checklist` sin impacto en puntaje; requisitos técnicos/experiencia separados, sin totales globales ni consorcio supuesto, incertidumbre sin tope artificial 49. Proyección `compact_match_context_v1` conserva 43 fuentes/157 importes/329 hechos y citas sin respaldos duplicados; perfil explícito tenant-scoped y lectura/facets de la misma extracción en análisis y detalle. Fuente/índice/referencias/categoría/criticidad vinculados al requisito original; aprobación económica por partida, moneda exigida, cita/período sin duplicados validada. Modelos sin comparación monetaria suficiente quedan `score_validated=false`, nota bruta solo auditable y APIs publican score nulo/veredicto gris. Caché exige SHA real del prompt y `economic_comparison_guard_v1`; sin fallback pagado. Principal valida veinte pruebas backend, TypeScript, 84 worker y cinco detalles/lista productivos contra PG. Trece archivos web desplegados selectivamente sobre imagen vigente, ocho contenedores/entorno intactos y /login200; no worker ajeno. Cinco nuevas llamadas autorizadas con respuestas durables y notas inválidas ocultas; revisión humana pendiente. Evidencia `docs/dinaut-five-substantive-compact-20261013.json` y ocho capturas locales/productivas. Limpieza59694bytes, recibos/inputs/rollback retenidos.

- [x] Persistir lectura documental Dinaut en cada archivo y experiencia (13/10/2026): PG atómico y readback exacto 43 documentos/43 experiencias/43 áreas; texto íntegro por página, resumen y evidencia económica sin agregación/conversión, 43 SHA originales preservados. Dos referencias ausentes archivadas; hash de perfil actualizado con ingesta desactivada. Copiloto conserva 43 fuentes/157 evidencias monetarias y JSON completo: contexto fresco con contrato real 105611/128000 caracteres, seis SELECT read-only / 0 modelos / 0 jobs. Diez tests Node y TypeScript/build aprobados; cinco archivos runtime desplegados selectivamente y QA autenticado productivo 43/43. `docs/dinaut-document-reading-20261013.json`; limpieza local confirmada 209370238 bytes, recibos/backup conservados.

- [x] Resolver enlaces activos de las dos referencias antiguas huérfanas de Dinaut (13/10/2026): b6d24cb8-c7e7-4f71-8b41-5babdc7b4e69 (AIR PRODUCT/DCS) y 7ca19414-a020-47b5-8c8f-6594fd3c98e9 (PETRO4200087970), previamente inexistentes en PG/HTTP404. Retiradas de `documents[]` y archivadas con UUID/referencia/motivo/no lectura en metadata y backup. Cada contrato conserva su PDF vigente verificado; no se equipararon nombres a SHA ni se recuperaron los originales ausentes. Readback exacto de 43 experiencias sin borrar archivos/recibos ni repetir pagos. `docs/dinaut-document-reading-20261013.json` y auditoría privada.

- [x] Calcular afinidad léxica actual y orden global PROD2 por perfil autorizado (13/10/2026): raíces españolas/frases15/términos10/empresa10, caps30/45 y dedup; mejor línea activa sin sumar entre líneas. Listado/detalle entregan puntos/nivel/línea/hits y mantienen Jev y matches documentales vinculados. Orden SQL antes de paginar, sin inferir CUBSO/economía ni guardar scores nuevos. Validación34tests incl.PG temporal, PostgreSQL16read-only1126fichas y100filas ordenadas/0,9329s en corte; scope prod no altera descargas/publicaciones locales pendientes. `docs/new-style/qa/prod2-keyword-affinity-release-20261013.json`. Web desplegada, workers/DB/Redis preservados. Limpieza 48949688 bytes lógicos; servidor/PG QA propios retirados, capturas/recibos/paquete remoto/rollback conservados.

- [x] Exponer `participation_extraction` compacta y ligada a la extracción vigente de cada convocatoria/procedimiento/documento en listado/detalle PROD2/PROD4/PROD6; preservar calidad/revisión y ámbito combinado cuando corresponde, sin fallback canónico ni JSON completo nuevo en listados. 24 pruebas de web/regresiones y SQL real read-only sobre 15 extracciones aprobados; solo local, sin cambios de BD/modelos. Evidencia `docs/participation-columns-qa-20261013.md` (2026-10-13).

- [x] Resolver descargas PROD2 desde referencia oficial SEACE por solicitud, con UUID listado, JSON/JSONP acotado64KiB, HTTPS/host permitido y timeout10s; `download_url` disponible sin R2, ticket fresco sin caché/log/persistencia y errores400/404/503 seguros. Conserva fallback histórico R2 verificado sin crear copias nuevas. Principal veinte pruebas offline y TypeScript aprobados; código local sin despliegue/SEACE real, acceso VM todavía no certificado. Guía `docs/prod2-official-downloads-20261007.md` (07/10/2026).

- [x] Añadir publicaciones documentales1:N y enlace oficial por archivo a APIs read-only PROD2, conservando documents_count como recursos únicos y ocultando JSON interno/storage_key. Once regresiones API y TypeScript aprobados por principal/agente de lectores (06/10/2026). Código local probado, sin despliegue web; API productiva existente confirma nuevas fichas3/3HTTP200. Evidencia docs/prod2-document-events-recovery-20261006.md.

- [x] Integrar y desplegar APIs read-only PROD2 de listado/detalle/PDF con perfil tenant-scoped, procedencia y firma R2 de 60 s. Nueve pruebas, TypeScript/build y revisión del agente principal aprobados; smoke productivo con 10 registros y 10 PDF HTTP 200 y perfil ajeno HTTP 404, sin escrituras ni modelos. Evidencia `docs/prod2-web-integration-qa-20261005.md` y `docs/prod2-pilot10-20261005.md` (2026-10-05).

- [x] Bloquear nuevos análisis manuales PROD4 por retiro de fuente (2026-10-05): POST autenticado devuelve 410/source_retired antes de DB/body/params y guard del servicio evita encolado incluso con force; GET e histórico conservados. Siete pruebas de rechazo/lectura aprobadas y TypeScript sin emisión ni incremental. Cambio local no desplegado; app-paths-manifest productivo confirma que la nueva ruta manual no existe allí. Retiro operativo y límites en `docs/prod4-retirement-20261005.md`; sin modificar UI ni datos.
- [x] Guardar descripción comercial de VEYON principal y DINAUT basada en todas sus líneas/keywords, indicando actividades y oportunidades buscadas. Actualización productiva de `identity_json.descripcion` con backup privado, lectura posterior 2/2 y comprobación de que los demás campos se preservan; sin ejecutar LLM ni modificar puntajes. Textos versionados en `scripts/seeds/company-commercial-descriptions.json` (2026-10-05).

- [x] Crear `GET /api/cronograma` tenant-scoped en lote para menores/SEACE, con match documental vigente prioritario sobre fit, exclusión rojo/gris/vencidos y todos los eventos oficiales con precisión/fuente. Ocho pruebas backend, consultas read-only reales con 11 menores/23 SEACE y aislamiento de tenants aprobadas; sin LLM ni cambios DB (2026-10-02).

- [x] Corregir «Revisar en SEACE»: API genera ficha oficial por id_procedimiento, migración0034 repara94/94 URLs y worker mantiene enlace por concurso. Desplegado3609491 web/worker-io; test backend, TypeScript/build y175 pruebas worker; ficha1254603 verificada en navegador normal, sin cambio visual ni auth (2026-10-02).

- [x] Exponer consortium_status, subcontracting_status y participation_terms_json en detalle API de contratos menores/concursos SEACE, conservando payload previo. TypeScript/build aprobados y web desplegada24577ed; sin cambios visuales (2026-10-02).

- [x] Exponer análisis documental PROD4 en detalle tenant-scoped: extracción vigente, facets, match del perfil activo y rechazo de puntajes de otra versión documental; seis regresiones aprobadas sin alterar campos existentes (2026-10-02).

## Pendientes del diagnóstico documental y cronograma (2026-10-01)

- [x] Corregir refresco web PROD6 para escribir fechas de cotización junto con cronograma/raw/hash, evitando que el worker vea hash nuevo y conserve cierre viejo. Fecha ausente/sin hora limpia el instante normalizado; regresión con dependencias HTTP/DB aisladas verifica ampliación, ausencia, caché y error de origen (2026-10-01).
- [x] Añadir normalización determinista de cronogramas PROD4/PROD6 y `official_schedule` en API detalle PROD4 sin romper `schedule`; preserve día sin inventar hora, clasifica consultas/observaciones/absolución y conserva todas las etapas. 12 pruebas del normalizador/refresco, TypeScript y build webpack aprobados, smoke API sobre PostgreSQL QA aislado (2026-10-01).
- [ ] Exponer detalle completo PROD4 con resumen/facets versionados, estado y motivo de análisis documental; resolver por identidad/fuente sin confundir `idProcedimiento` con `idContrato`.

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
- [x] Exponer `opportunity_id`, `source_key`, tipo de registro, objeto, modalidad, etapa, acceso y capacidad de acción canónicos en feed, contratos, dashboard y tracking; conservar `id_contrato` y rutas PROD6. TypeScript, arquitectura frontend, SQL aislado y build de producción validados; web desplegada y OpenAPI 200 (2026-09-29).
- [x] Exponer los mismos metadatos canónicos en contratos del mercado historico y proveedor, conservando los campos actuales; TypeScript, arquitectura frontend y build de producción verificados (2026-09-29).
- [x] Exponer listado/detalle autenticado de PROD4 solo para procesos tecnológicos con filtros de bienes/servicios, estado de ventana y texto; incluir ítems, documentos, cronograma, clasificación canónica y proxy PDF oficial controlado, sin alterar rutas PROD6 (TypeScript, PostgreSQL aislado y despliegue web verificados; el PDF oficial responde `403` desde la VM, 2026-09-29).
- [x] Personalizar listado y detalle PROD4 por tenant con afinidad preliminar y campos de score/veredicto final solo si existe match documental; 72/72 concursos tienen afinidad para VEYON SAC. OpenAPI, TypeScript, build y web productiva verificados (2026-09-29).
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
- [ ] Crear endpoint asincrono `POST /api/contracts/:id/refresh-official`: consultar OCDS primero, encolar fallback `prod2` solo si faltan cronograma/canal o hay inconsistencias, reutilizar job activo y exponer estado/ultimo refresh/cooldown.
- [x] Conexión SEACE persistente: AES-256-GCM, access/refresh token, renovación y re-login automático; login real y endpoints read-only de consultas/contexto de cotización validados el 2026-07-12 (`200`).
- [x] Preview PDF sin R2 desde URL SEACE con streaming HTTP Range (`206`) y rechazo explícito de DOCX/XLSX (`415`).
- [x] API de borradores de postulación: crear desde contrato, sincronizar contexto SEACE, leer/editar cabecera, ítems, RTM y revisión de documentos; sin envío oficial. Validado con contrato 78753.
- [x] API de borradores de postulación: iniciar desde contrato, snapshot autenticado SEACE, ítems/RTM/documentos dinámicos y edición tenant-safe; sin envío oficial.
- [x] API tenant-safe para subir, descargar y eliminar adjuntos del borrador (PDF/Word/Excel, máximo 10 MB, PostgreSQL sin R2). Ciclo completo y aislamiento entre tenants validados con match 21.
- [x] Registrar una postulación como presentada de forma tenant-safe y transaccional: exige oferta/contacto/RTM/adjunto completos, actualiza borrador a `submitted` y match a `postulada`, y audita que el canal fue SEACE manual sin simular envío oficial. Flujo real y restauración de datos QA validados con match 21 (2026-08-21).
- [x] API tenant-safe de biblioteca empresarial en Perfil: listar, crear/editar/borrar datos reutilizables, subir/descargar/editar/borrar documentos de respaldo y exponer metadata al contexto del copiloto. Smoke crear/borrar validado localmente (2026-08-08).
- [x] Copiloto tenant-safe por licitación: memoria PostgreSQL, Gemini con citas, contexto JSON persistido/perfil/borrador, compactación y change sets con confirmación manual; nunca envía a SEACE. Agente real y confirm/reject validados con contrato 78753.
- [x] Simplificar copiloto IA para conversar contra JSON estructurado ya guardado (`summary_json`, `raw_extraction_json`, facets, perfil y borrador), sin descargar ni leer PDF/DOCX en vivo durante el chat. Validado con build web local (2026-08-08).
- [x] Añadir puente Codex CLI para el copiloto con sesión caliente Redis/TTL, workspace por tenant, endpoint SSE, validación de JSON/citas/IDs y persistencia final en PostgreSQL. Validado con dev smoke del endpoint streaming (`401` esperado sin sesión) y documentación en `docs/codex-agent-bridge.md` (2026-08-08).
- [x] Habilitar prueba experimental de lectura por `downloadUrl` público SEACE en contexto Codex, con fallback explícito a JSON extraído si el acceso falla. Validado con links `200` desde host local, corrida Codex reportando fallo DNS interno y build web OK (2026-08-08).
- [x] Invalidar sesión caliente Codex cuando cambian prompt, política de workspace o schema de salida, para no reanudar threads con instrucciones antiguas. Validado con `tsc` y `next build` (2026-08-08).
- [x] Configurar modelo y sandbox del puente Codex por entorno (`CODEX_AGENT_MODEL`, `CODEX_AGENT_SANDBOX_MODE`) para permitir lectura agentica directa de documentos SEACE en VM aislada. Validado con `gpt-5.6-sol`, `danger-full-access` y endpoint SSE real (2026-08-08).
- [x] Persistir Word/PDF/XLSX generados por Codex como artefactos tenant-scoped, exponer descarga autenticada y limpiar el archivo temporal despues del commit. Validado con `.docx` real generado por `gpt-5.5`, respuesta SSE, PostgreSQL, descarga HTTP y estructura OOXML (2026-08-09).
- [x] Robustecer el flujo documental Codex: inventario y deduplicacion de originales, cache tenant-scoped acotado con allowlist SEACE, clasificación de plantillas editables, `AGENTS.md` operativo y obligación de preguntar datos faltantes antes de editar todas las plantillas requeridas (2026-08-09).
- [x] Empaquetar Codex CLI `0.147.0` y herramientas documentales en la imagen web, con autenticacion, workspace y Redis persistentes mediante volumenes Docker. Compose, build de imagen y `codex --version` validados localmente (2026-08-11).
- [x] Aislar el workspace Codex por `tenantId/chatSessionId` para que una conversación nueva no comparta contexto, temporales, documentos ni entregables con otra sesión; TypeScript y build web validados (2026-08-21).
- [x] Mantener `CODEX_AGENT_SANDBOX_MODE` al reanudar threads Codex, pasando la opción global antes de `resume` para evitar el fallback a `workspace-write`/`bwrap` dentro de Docker; pruebas unitarias, TypeScript y build web validados (2026-08-21).
- [x] Entregar solicitudes documentales como archivos descargables directos, sin crear `agent_change_sets` ni mencionar cambios del borrador; conservar confirmación manual solo para modificaciones estructuradas de la postulación. Detección, respuesta directa, TypeScript, build y 7 pruebas específicas validados localmente y dentro del contenedor productivo `8bfd891` (2026-08-21).
- [x] Hacer resiliente la entrega documental SSE: heartbeats, buffering desactivado, desconexiones sin cancelar la persistencia, recuperación de archivos existentes referenciados por Codex y nombres finales sin `borrador`. Validado con 8 pruebas, arquitectura frontend, TypeScript y build local (2026-08-21).
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

- [x] Exponer análisis manual PROD4 por tenant en `POST/GET /api/prod4/opportunities/[id]/analyze` (2026-10-05): POST `{force:boolean}` devuelve 202 `{data:{job_id,status}}`; GET `?job_id=` consulta estado propio y distingue `skipped` cuando no existe evaluación de la extracción/perfil actual. Reutiliza `analyze_prod4_match` en cola `llm`, deduplica pending/claimed, rechaza ausencia de perfil o bases LLM1 validadas y no expone errores internos. `force` reevalúa sin saltar requisitos documentales. Validado con 14 pruebas de cola, 19 de worker y TypeScript; sin ejecutar evaluaciones pagadas ni escribir en producción.

- [x] Validar montos económicos por rubro/especialidad en `econ_experience_json.areas` antes de guardar perfil: monto no negativo, moneda explícita y hasta 100 áreas, preservando claves históricas y metadatos (2026-10-05). Cinco pruebas unitarias; sin migración ni recálculo LLM.

- [x] Crear cuenta independiente de demostración para Dinaut en producción (2026-10-02): RUC 20343877294, 8 líneas industriales y 140 señales únicas, sin modificar VEYON. Seed sin secretos: `scripts/seeds/dinaut-demo.json`. Acceso autenticado y APIs de perfil/líneas verificados; capturas: `docs/new-style/qa/dinaut-demo-production-20261002.png` y `dinaut-demo-team-production-20261002.png`. Seis roles sugeridos NO acreditados; supuesto S/ 5 millones solo en metadata/descripción, experiencia acreditable vacía y automatización LLM desactivada. Pendiente ampliar ingesta industrial fuera de los segmentos cubiertos actualmente; no se afirma cobertura completa.

- [x] Crear vista interna de `worker_jobs`.
- [x] Ver jobs pendientes/fallidos/dead.
- [x] Reintentar job manualmente.
- [x] Crear vista de `pipeline_events`.
- [x] Crear vista de `requires_review`.
- [x] Crear vista de contract tests SEACE.

- [x] Proyectar afinidad comercial JEV exclusiva del perfil DIN vigente en feed/detalle PROD2 mediante política e identidad de título/fila/perfil/tenant; campo separado de matches documentales y orden global antes de paginar. 72 pruebas Node, cuatro fixtures SQL reales, TypeScript y arquitectura aprobados; despliegue y capturas se verifican por separado (2026-10-20).
