# Tareas - Frontend

Este documento controla el desarrollo de la interfaz de BuenaPro.

Regla: antes de implementar una vista, leer su spec en `docs/frontend/` y el sistema visual en [docs/design-patterns.md](../docs/design-patterns.md).

## Arquitectura frontend

- [x] Crear estructura `apps/web/features`.
- [x] Crear estructura `apps/web/lib/api`.
- [x] Crear estructura `apps/web/lib/format`.
- [x] Crear estructura `apps/web/lib/constants`.
- [x] Crear estructura `apps/web/styles`.
- [x] Mover rutas de `app/*/page.tsx` a paginas delgadas que consumen features.
- [x] Definir convencion de componentes: carpeta por componente con `tsx`, `module.css`, `index.ts`.
- [x] Crear API client base con manejo de errores.
- [x] Crear tipos frontend para contracts, matches, profile, tracking y notifications.
- [x] Centralizar formato de fechas en America/Lima.
- [x] Centralizar formato de moneda PEN.

Referencia: [Frontend README](../docs/frontend/README.md)

## Design system base

- [x] Implementar tokens CSS desde [Design Patterns](../docs/design-patterns.md).
- [x] Implementar light mode terracota + carbon.
- [x] Preparar variables para dark mode sin activarlo por defecto.
- [x] Crear `Button`.
- [x] Crear `IconButton`.
- [x] Crear `Input`.
- [x] Crear `Select`.
- [x] Crear `Checkbox`.
- [x] Crear `Switch`.
- [x] Crear `Tabs`.
- [x] Crear `Badge`.
- [x] Crear `Table`.
- [x] Crear `Drawer`.
- [x] Crear `Dialog`.
- [x] Crear `Toast`.
- [x] Crear `Skeleton`.
- [x] Crear `EmptyState`.
- [x] Crear `Pagination`.
- [x] Crear `Tooltip`.
- [x] Crear `SegmentedControl`.
- [x] Crear wrapper `AppIcon`.

Referencia: [Componentes base](../docs/frontend/09-componentes-base.md)

## App Shell

- [x] Implementar `features/shell`.
- [x] Crear `AppShell` nuevo.
- [x] Crear `TopBar`.
- [x] Crear `IconRail`.
- [x] Crear navegacion activa.
- [x] Crear `UserMenu`.
- [x] Agregar boton visible de cerrar sesion.
- [x] Crear `NotificationBell`.
- [x] Adaptar shell a mobile.
- [x] Reemplazar shell actual en todas las rutas privadas.
- [x] Reducir navegación principal a Oportunidades, Mercado y Postulaciones; mover Perfil/Alertas al avatar y ocultar Admin/Docs del producto visible (2026-07-31).
- [x] Añadir al pie del sidebar un acceso compacto a `VEYON SAC · Mi empresa y radar`, oculto en mobile para conservar la navegación de tres destinos (2026-08-01). Evidencia: `docs/qa/screenshots/sidebar-company-shortcut-desktop.png` y `docs/qa/screenshots/sidebar-company-shortcut-mobile.png`.
- [x] Documentar el contexto funcional completo de BuenaPro para exploración externa de diseño, incluyendo rutas, recorridos, formularios, tablas, estados y límites sin prescribir estilos (2026-08-01). Documento: `docs/stitch/buenapro-functional-context.md`.

Referencia: [App Shell](../docs/frontend/00-app-shell.md)

## Auth

- [x] Redisenar `/login`.
- [x] Redisenar `/registro`.
- [x] Mejorar errores de login.
- [x] Redirigir a perfil si no existe `GET /api/profile`.
- [x] Evitar textos largos o look de landing.
- [x] Separar `/registro` de `/login` y crear onboarding asistido de 4 pasos: empresa/web, líneas con keywords propias, capacidad (monto/equipo/recursos) y revisión. El análisis web es opcional, las sugerencias IA requieren confirmación y el guardado respeta `company_profiles` + `business_lines`. Evidencia: `docs/qa/screenshots/onboarding-company-desktop-2026-07-10.png`, `onboarding-company-mobile-2026-07-10.png`.
- [x] Crear recurso visual propio para las 4 etapas del onboarding e integrarlo sin alterar el sistema terracota/carbón; versión web optimizada a 180 KB: `apps/web/public/onboarding/onboarding-suite.jpg`.
- [x] Asociar cada línea generada por IA con 1–3 segmentos CUBSO válidos, mostrar nombre/cobertura, permitir corrección y exigir segmento antes de guardar. Persistencia validada contra catálogo 2026. Evidencia: `docs/qa/screenshots/onboarding-cubso-lines-2026-07-11.png`, `onboarding-cubso-lines-mobile-2026-07-11.png`.

Referencia: [Auth](../docs/frontend/01-auth.md)

## Inicio

- [x] Retirar el dashboard duplicado del flujo principal y redirigir `/` a `/feed`; conservar `/api/dashboard` y componentes históricos sin exponer una cuarta área (2026-07-31).

- [x] Rework de Inicio como `Hoy en tu radar`: cierres próximos, postulaciones en curso, mercado comparable y proveedores frecuentes; métricas accionables sin dashboard BI decorativo (build y smoke SSR validados el 2026-07-31).
- [x] Reestructurar Inicio como dashboard profesional: KPIs con conteos completos, tendencia mensual adjudicados/desiertos, composición histórica, ranking de empresas y mesas operativas de cierres/postulaciones; build y SSR con PostgreSQL real validados el 2026-07-31.
- [x] Capturar y revisar en desktop/mobile el dashboard profesional de Inicio. Evidencia Playwright: `docs/qa/screenshots/dashboard-market-desktop.png`, `dashboard-market-mobile.png` (2026-07-31).
- [x] Crear `/mercado` como explorador histórico: búsqueda por keywords, alcance perfil/todo SEACE, filtros por segmento, resultado, región, entidad, año y precio, más vistas de contratos y empresas adjudicadas.
- [x] Crear detalle de empresa `/mercado/empresas/[ruc]` con montos, entidades, regiones, servicios y licitaciones ganadas.
- [x] Validar `/mercado` desktop/mobile y detalle de proveedor con datos reales de producción. Evidencia: `docs/qa/screenshots/market-intelligence-desktop.png`, `market-intelligence-mobile.png`, `market-supplier-desktop.png`.

- [x] Implementar `features/dashboard`.
- [x] Crear vista `/`.
- [x] Mostrar estado de perfil.
- [x] Mostrar oportunidades recientes.
- [x] Mostrar oportunidades que cierran pronto.
- [x] Mostrar postulaciones en preparacion.
- [x] Soportar estado sin perfil.
- [x] Consumir `GET /api/contracts`, `GET /api/feed`, `GET /api/tracking`.

Referencia: [Inicio](../docs/frontend/02-inicio.md)

## Oportunidades

- [x] Mantener el histórico fuera del feed de Oportunidades: sin filtros, métricas ni señales históricas en tabla o vista rápida; los comparables viven únicamente en el detalle (ajustado el 2026-07-31).

- [x] Añadir marcador reversible en cada fila y preview, estado visual guardado/no guardado y filtro rápido `Guardadas` que conserva filtros del formulario; integrado con API real y accesibilidad de teclado/lector (2026-07-20).

- [x] Implementar `features/opportunities`.
- [x] Crear toolbar de busqueda.
- [x] Crear filtros base.
- [x] Crear filtros inteligentes.
- [x] Crear filtros rapidos: no cerradas, 24 h, semana, cerradas, verdes, revision, con monto y cotizables.
- [x] Normalizar filtro `deadline` en valores estables: `open`, `all`, `24h`, `week`, `closed`.
- [x] Crear tabla/lista de oportunidades.
- [x] Mostrar todas las filas de la pagina actual, sin limite artificial en frontend.
- [x] Mostrar tabla completa con monto, cierre y score visual.
- [x] Permitir seleccionar una fila para cargar metadata por `GET /api/contracts/:id` y mostrar vista rapida lateral antes de abrir detalle.
- [x] Listar contratos del rubro con `GET /api/contracts` y adjuntar score/verdict cuando exista match, sin ocultar oportunidades sin match.
- [x] Mantener `GET /api/feed` para API priorizada, pero no usarlo como fuente unica de la tabla si deja fuera contratos del rubro.
- [x] Mostrar verdict/score si existe match.
- [x] Permitir marcar `interesada` con `POST /api/contracts/:id/track`.
- [x] Permitir pasar a `en_preparacion`.
- [x] Implementar paginacion con conteo total y etiqueta `Pagina X de Y`.
- [x] Sin perfil: mostrar CTA a Perfil sin bloquear exploracion.
- [x] Refactor visual 2026-07-09: vista rapida lateral en panel claro (paleta terracota/carbon), acciones Abrir detalle/Seguir arriba del panel, columnas Plazo y Match con datos derivados reales. Evidencia: `docs/qa/screenshots/feed-redesign-preview-2026-07-09.png`, `feed-redesign-mobile-2026-07-09.png`.
- [x] Mapeo robusto de summary/facets via `lib/extraction/opportunity.ts` (summary derivado -> filter index -> extraccion cruda); validado contra JSON real de 5 contratos.
- [x] Priorizar todas las oportunidades por afinidad absoluta: conservar rubro exacto, relacionado y general, ordenar de mayor a menor `fit_points` y exponer el puntaje numérico al pasar sobre los puntos. SQL y render SSR validados el 2026-07-16.
- [ ] Capturar y revisar el feed priorizado en desktop y mobile; bloqueo 2026-07-16: navegador interno sin instancias disponibles (`iab` no disponible).

Referencia: [Oportunidades](../docs/frontend/03-oportunidades.md)

## Detalle de oportunidad

- [x] Añadir Histórico comparable al detalle con rango, mediana, adjudicados/desiertos, proveedores, razones de similitud y enlace a TDR antiguo cuando exista.

- [x] Implementar `features/opportunity-detail`.
- [x] Redisenar `/oportunidad/[id]`.
- [x] Mostrar header con codigo, entidad, cierre, estado y score.
- [x] Mostrar resumen compacto.
- [x] Mostrar tabs: Resumen, Requisitos, Documentos, Seguimiento, Historial.
- [x] Mostrar checklist de requisitos/facets.
- [x] Agrupar requisitos/facets repetidos para que el detalle no duplique documentos de propuesta.
- [x] Mostrar metadata operativa: score, objeto, roles detectados, documentos clave, pago y condiciones.
- [x] Mostrar evidencia expandible.
- [x] Mostrar PDF preview y descarga original.
- [x] Permitir `Evaluar con mi perfil` si no hay match.
- [x] Analisis LLM bajo demanda (2026-07-09): barra "¿Puedes postular?" + boton `Evaluar con mi perfil` (AnalyzeButton con polling y refresh), panel "Análisis con tu perfil" (resumen + acciones recomendadas), rail "Qué te falta" con gap exacto y accion por requisito, Re-evaluar con force. Evidencia: `docs/qa/screenshots/detail-llm-analysis-2026-07-09.png`.
- [x] Fit de rubro en feed y vista rapida (chip "Tu rubro exacto / Muy relacionado / Rubro general" cuando no hay analisis). Evidencia: `docs/qa/screenshots/feed-fit-llm-match-2026-07-09.png`.
- [x] Match preview numerico (2026-07-10): anillo `~fit` en tabla/panel/detalle con tonos neutros (sage/gris, sin colores de semaforo); tras el analisis IA el tile de Score compara "▲/▼ vs preview". Fit ponderado: keywords especificas 15 / genericas 6 (tope 45) + factor economico ±8 (monto exigido vs facturacion; moderado porque un consorcio puede cubrirlo). Umbrales: exacto >=80, relacionado >=62.
- [x] Columna "Exp. econ." en el feed con el monto de experiencia economica exigida (52 de 73 requisitos economicos traen monto numerico v2); tile en detalle reemplaza al "Valor estimado" (SEACE no lo publica en <=8 UIT). Evidencia: `docs/qa/screenshots/feed-econ-factor-2026-07-10.png`.
- [x] Perfil (2026-07-10): lineas de negocio muestran keywords como chips; guardar el formulario hace merge sobre la metadata rica (team_json con grado/carrera/skills, econ por rubro, CIIU) en vez de pisarla; campo de experiencia economica lee claves por rubro. Keywords de VEYON re-derivadas de los terminos reales de SEACE y matching de fit insensible a tildes. Evidencia: `docs/qa/screenshots/perfil-keywords-2026-07-10.png`, `feed-fit-keywords-2026-07-10.png`.
- [x] Refactor visual 2026-07-09: breadcrumb + header jerarquizado, franja de decision (valor/pago/plazo/penalidad o veredicto/score/falta), secciones ordenadas que omiten campos sin dato, rail derecho con seguimiento y TDR. Evidencia: `docs/qa/screenshots/detail-redesign-2026-07-09.png`, `detail-redesign-mobile-2026-07-09.png`.
- [x] Permitir iniciar preparacion.
- [x] Permitir registrar monto ofertado.
- [x] Permitir completar tareas.
- [x] Consumir `GET /api/contracts/:id`, `POST /api/contracts/:id/track`, `GET /api/matches/:id/tasks`.
- [ ] Reestructurar el detalle como mesa de decisión: lectura ejecutiva unificada, requisitos prioritarios, ficha técnica progresiva, preparación lateral y actividad/comentarios. Implementación y `npm run build` validados el 2026-07-12; captura desktop/mobile pendiente porque el navegador interno no estuvo disponible en la sesión.
- [ ] Corregir preview del TDR: miniatura PNG real en el rail, PDF inline en modal, fallback y estados accesibles. Build, PNG real `1059×1497` y PDF inline de 9 páginas validados el 2026-07-12; captura del componente pendiente por navegador interno no disponible.
- [ ] Añadir conexión SEACE separada en Perfil y workspace lazy `Consultas oficiales / Preparar cotización` en detalle. Login real corregido (el formulario retenía `event.currentTarget` después del await), SSR/build/API validados; captura desktop/mobile pendiente.
- [x] Retirar del detalle la tarjeta repetitiva `Preparación` (monto, notas y checklist) y mover únicamente la asignación de responsable al rail de postulación con autosave y opción de desasignar. TypeScript y PATCH real asignar/limpiar validados el 2026-07-12; QA visual permanece cubierto por la captura pendiente del workspace.
- [x] Simplificar `Evaluar con mi perfil`: esperar una única respuesta del análisis directo, refrescar el detalle y retirar polling de hasta 2 minutos. Flujo real validado con contrato 78764; no cambia el layout del botón.

Referencia: [Detalle de oportunidad](../docs/frontend/04-detalle-oportunidad.md)

## Perfil de empresa

- [x] Implementar `features/profile`.
- [x] Crear wizard real para `/perfil`.
- [x] Crear seccion Identidad.
- [x] Crear editor de lineas de negocio.
- [x] Crear editor de experiencia economica.
- [x] Crear editor de contratos previos.
- [x] Crear editor de equipo/personas.
- [x] Crear editor de roles contratables.
- [x] Crear editor de equipamiento.
- [x] Crear editor de certificaciones/seguros.
- [x] Mostrar completitud del perfil.
- [x] Mostrar perfil activo, facturacion, equipo y estadisticas de matching.
- [x] Guardar con `PUT /api/profile`.
- [x] Crear/editar lineas con `/api/lines`.
- [x] Usar catalogos CUBSO/ubigeo.
- [x] Avisar que el matching se recalcula.
- [x] Rework profesional de `/perfil` (2026-07-11): jerarquía centrada en líneas de negocio, resumen compacto de empresa/capacidad, editor secundario progresivo y eliminación del mosaico repetitivo de cards. Evidencia: `docs/qa/screenshots/profile-rework-desktop-2026-07-11.png`, `profile-rework-mobile-2026-07-11.png`.
- [x] Editor inline completo para líneas: crear/editar nombre, 1–3 segmentos CUBSO, hasta 30 keywords por chips, estado de cobertura y feedback de rematch. PATCH real validado sin alterar los datos. Evidencia: `docs/qa/screenshots/profile-line-editor-2026-07-11.png`.
- [x] Simplificar capacidad económica: retirar facturación anual de perfil/completitud, conservar solo experiencia económica acreditable y mostrar `Guardar cambios` únicamente cuando el formulario esté modificado. Evidencia: `docs/qa/screenshots/profile-clean-state-2026-07-11.png`.
- [x] Añadir `Identidad del radar` editable en onboarding y perfil: keywords transversales separadas de cada línea, chips, empty/error/saving states y aviso de raíces lingüísticas. QA desktop/mobile: `docs/qa/screenshots/profile-company-keywords-desktop.png`, `profile-company-keywords-mobile.png`.
- [x] Corregir pérdida de foco al editar el nombre de una línea en onboarding: cada editor usa una clave estable y el identificador visual no se envía al backend. TypeScript validado el 2026-07-17.
- [ ] Capturar QA visual del nombre de línea con texto de varias palabras; navegador interno no disponible en la sesión del 2026-07-17.

Referencia: [Perfil de empresa](../docs/frontend/05-perfil-empresa.md)

## Seguimiento

- [x] Renombrar la cartera visible a Postulaciones en `/postulaciones`; `/seguimiento` redirige para conservar enlaces existentes (2026-07-31).

- [x] Implementar `features/tracking`.
- [x] Redisenar `/seguimiento`.
- [x] Crear vista tabla.
- [x] Crear vista kanban o toggle preparado.
- [x] Consumir `GET /api/tracking`.
- [x] Cambiar estado con `PATCH /api/matches/:id`.
- [x] Asignar responsable.
- [x] Registrar monto ofertado.
- [x] Editar notas.
- [x] Mostrar checklist por match.
- [x] Crear tarea manual.
- [x] Completar tarea.
- [x] Mostrar historial de eventos.
- [x] Abrir detalle desde seguimiento.
- [ ] Rework de Seguimiento como cartera compacta: una tabla, urgencia, progreso, responsable y próximo paso; filas con borrador abren `/postulaciones/[matchId]`. Build/SSR validados; captura desktop/mobile pendiente.

## Postulación

- [x] Añadir `Precio y mercado` a `/postulaciones/[matchId]`, contraste de oferta contra rango histórico, comparables usados y acceso read-only a consultas oficiales; envío SEACE permanece deshabilitado hasta confirmar endpoint.

- [ ] Crear workspace `/postulaciones/[matchId]` con oferta por ítems, RTM dinámicos, vigencia/contacto, archivos y rail de progreso. Persistencia real y build validados; captura desktop/mobile pendiente.
- [ ] Simplificar el workspace como mesa única: oferta, RTM y carga de propuesta en una sola vista; leer formatos SEACE deja de ser un paso. Implementación pendiente de build y QA visual desktop/mobile.
- [x] Mantener `Enviar a SEACE` fuera de alcance hasta validar el POST oficial, múltiples ítems y un tipo de cotización distinto de 2.
- [ ] Integrar panel `Preguntar a BuenaPro` en detalle y postulación: historial, fuentes, sugerencias, comparación y confirmación manual. Build/TypeScript y flujo funcional validados; captura desktop/mobile pendiente porque el navegador interno no estuvo disponible.

Referencia: [Seguimiento](../docs/frontend/06-seguimiento.md)

## Configuracion

- [x] Reemplazar Configuración visible por `/alertas`: activación, afinidad mínima, límite diario e historial; `/configuracion` redirige y workspace/miembros quedan fuera de navegación (2026-07-31).
- [x] Validar navegación simplificada, menú de cuenta y Alertas desktop/mobile. Evidencia: `docs/qa/screenshots/account-menu-desktop.png`, `alerts-center-desktop.png`, `alerts-center-mobile.png`, `applications-navigation-desktop.png`, `navigation-simplified-mobile.png`.

- [x] Implementar `features/settings`.
- [x] Redisenar `/configuracion`.
- [x] Mostrar workspace.
- [x] Editar nombre del tenant.
- [x] Listar miembros.
- [x] Agregar miembro.
- [x] Cambiar rol.
- [x] Configurar email/telegram/in_app.
- [x] Configurar realtime/digest.
- [x] Configurar max alertas por dia.
- [x] Configurar horario silencioso.
- [x] Consumir `/api/tenant`, `/api/tenant/members`, `/api/notifications/prefs`.

Referencia: [Configuracion](../docs/frontend/07-configuracion.md)

## Admin tecnico

- [x] Implementar `features/admin`.
- [x] Redisenar `/admin`.
- [x] Mostrar estado de batch.
- [x] Mostrar worker jobs.
- [x] Mostrar pipeline events.
- [x] Mostrar requires review.
- [x] Mostrar contract checks.
- [x] Permitir retry/dead de jobs.
- [x] Confirmar acciones sensibles.

Referencia: [Admin tecnico](../docs/frontend/08-admin-tecnico.md)

## QA frontend

- [x] Probar desktop 1440px. Evidencia: `docs/qa/screenshots/frontend-refactor-feed.png`, `frontend-refactor-detail.png`.
- [x] Probar feed con tabla completa, filtros rapidos y preview lateral bajo demanda. Evidencia: `docs/qa/screenshots/feed-no-preview-filters.png`, `feed-click-preview-metadata.png`.
- [x] Probar laptop 1280px. Evidencia: `docs/qa/screenshots/frontend-refactor-feed-1280.png`.
- [x] Probar mobile 390px. Evidencia: `docs/qa/screenshots/frontend-refactor-feed-mobile.png`.
- [x] Verificar que textos largos no rompan tablas/listas en feed y detalle.
- [ ] Verificar estados loading/empty/error en cada vista.
- [x] Verificar navegacion completa: login -> perfil -> oportunidades -> detalle. Evidencia: `docs/qa/screenshots/functional-profile-veyon.png`, `functional-opportunity-detail-metadata-grouped.png`.
- [ ] Verificar contraste de colores.
- [x] Verificar que no haya cards dentro de cards en las vistas refactorizadas.
- [x] Verificar que no haya exceso de texto en primer pantallazo de login/feed.
- [x] Actualizar screenshots o evidencia de QA.
- [x] Probar onboarding inicial en desktop 1440×1000 y mobile 390×844. Build de producción y endpoint de sugerencias con Gemini validados; bloqueo de URL privada verificado. Evidencia: `docs/qa/screenshots/onboarding-company-desktop-2026-07-10.png`, `onboarding-company-mobile-2026-07-10.png`.
- [x] Probar clasificación CUBSO del onboarding con rubros distintos: VEYON → `43,81`; restaurante/catering → `50,90`. Revisar editor de segmentos desktop/mobile y confirmar que ninguna línea existente quede sin segmento.
