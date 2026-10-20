# Tareas - Frontend

- [x] Mostrar Categoría y Por título en PROD2 y reutilizar filtro simple de seis opciones (20/10/2026): 12 capturas locales/6 productivas en desktop/laptop/mobile revisadas contra Glass, selección/foco/perfil/query conservados y badges dentro de columnas tras un único ajuste. Dos rondas visuales; API pública y código ad74430 publicados. Validación técnica distingue bloqueo de analítica externa del navegador de un error de aplicación. Evidencia docs/prod2-title-categories-execution-20261020.{md,json}.

- [x] Simplificar clasificación a un selector Categoría en SEACE y menores por petición humana (20/10/2026): Todas + Licencias y suscripciones, Desarrollo de software, IA y automatización, Industrial, Cloud e infraestructura y Soporte y mantenimiento. Sustituye los cuatro grupos comerciales visibles; conserva controles normales de vigencia/puntaje/guardadas. URLs anteriores mantienen sus valores bajo Selección anterior hasta elegir categoría/Todas; búsqueda/tipo/fuente/limpieza conservan perfil y criterios aplicados. Principal revisa cinco capturas representativas contra Glass04; matriz de 12 PNG desktop/laptop/mobile y once grupos funcionales con datos reales read-only, teclado/foco/transición legacy correctos, sin overflow/errores. 28 pruebas de modelo UI, 32 focales mixtas solapadas, TypeScript/arquitectura16/build aislado24.92s y detector0 aprobados. Código c1f1802; evidencia tmp/commercial-category-20261020/qa-real-final/ y docs/simple-categories-20261020.json. Preparado localmente, sin publicación ni etiquetas históricas inventadas.

- [x] Conectar filtros comerciales locales en Buscador SEACE y Contratos menores (20/10/2026): rubro, trabajo y especialidad multiselección, estado y Quitar clasificación conservando perfil/otros criterios. Panel plegable dentro del toolbar Glass, sin columnas extra; búsqueda/tipo/vistas rápidas/fuente/paginación preservan CSV. Filtro/count SQL antes de paginar y Sin clasificar explícito, separado de afinidad y modalidad jurídica. Principal revisa capturas representativas contra referencia04; matriz completa de 18 PNG desktop1600/laptop1024/mobile390 en ambas fuentes y diez grupos funcionales aprobados, teclado/foco/labels/sin overflow/errores. 26 pruebas de modelo UI, TypeScript, arquitectura16 y build aislado27s; detector estático sin hallazgos. Código UI 20dea69 sin cambios visuales posteriores, evidencia tmp/commercial-filters-20261020/qa-reviewed-real/. Sin publicación, etiquetas históricas ni modelos; cierre de código/limpieza en docs/commercial-classification-20261020.json.

- [x] Ordenar fuentes del Cronograma como «Buscador SEACE → Contratos menores» (16/10/2026): mismo orden visual y de teclado en desktop/laptop/mobile, selección e iconos conservados, enlaces mantienen source/start/days. Altura mínima de 44 px en ambos enlaces corrige el target móvil de 38 px detectado durante QA. Principal revisa seis capturas locales con datos reales y prueba los seis cruces por teclado, foco/aria-current/periodo/sin overflow ni errores; PG read-only y proveedores desactivados. 14 pruebas focales, TypeScript, arquitectura16 y build aislado aprobados; revisión independiente confirma scope. Código `f8504fb`; evidencia `tmp/timeline-tab-order-20261016/qa-local-real/`. Publicación y limpieza se registran al cierre Backend/Web.

- [x] Revisar e integrar bordes de los cambios locales del 14/10/2026 antes de publicación: guardado aislado por identidad frente a respuestas tardías; Inicio describe solo contratos menores guardados y deriva SEACE al Cronograma; editor reinicia dataset, grupos con aria-pressed, foco móvil restaurado y reduced-motion. Inspector menores expone aria-expanded según visibilidad/resize y cierre limpia apertura; ranking proveedor usa clave código/nombre. Principal verifica 34 capturas locales, 15 análisis exactos y pruebas funcionales de latencia/foco/dataset con escrituras simuladas; TypeScript/arquitectura y 233 Node aprobados. Impeccable sin hallazgos en seis componentes corregidos. Sin modelos/mutaciones de negocio. Evidencia privada en `tmp/latest-local-release-20261014/`; despliegue y limpieza se registran al cierre Backend/Web.

- [x] Aplicar colores por puntaje en ambas fuentes según solicitud humana (13/10/2026): helper transversal scoreTone, rojo<30/amarillo30–80inclusive/verde>80 en listas, inspectores y DecisionOverview; notas finales/provisionales independientes del verdict, Provisional/Por validar permanece incluso enverde. Cero real visible, inválidos/sin análisis no inventan cifras; mismo Glass/layout. Principal44pruebasUI/SSR/presentación, TypeScript/arquitectura16/build61; veinte capturas reales locales/productivas con revisión desktop/laptop/mobile representativa, bandasDOM/contraste/foco y ceroerrores/overflow/escrituras. Publicación selectiva diezarchivos, imagenf3da622e, quince resultados previos exactos y cinco revisiones de fuente preservadas;0modelos. `docs/score-bands-concrete-prompt-20261013.{md,json}`. Limpieza36403byteslocales/29717remotos, QA/túnel propios detenidos y evidencia/rollback/entradas pendientes conservados.

- [x] Mostrar puntaje0–100 almacenado como provisional en listas/inspectores/detalles de ambas fuentes, según solicitud humana (13/10/2026): campoAPIprovisional explícito, anillo neutral/Provisional/Por validar; DecisionOverview muestra cifra/100 y EstimaciónIA·pendientevalidación. Sin dato mantiene estado pendiente; zero real visible y notaslegado intactas. Principal27tests UI/SSR/overview/detalle, TypeScript/arquitectura16/build61;20capturas reales locales/productivas desktop/laptop/mobile revisadas contra Glass y15API resultados exactos. Publicado imagen f59038d8; fuentes/metadata/gradefinal/orden preservados. Impeccable0hallazgos;0modelos/reintentos. `docs/provisional-score-visible-20261013.{md,json}`. Limpieza33935bytes, QApropio detenido y capturas/recibos/rollback retenidos; aprobación semántica continúa pendiente.

- [x] Corregir desbordes y vistas responsive del diagnóstico (14/10/2026): auditoría automática de 14 rutas × desktop1440/laptop1024/mobile390/girado844×390 pasa de 3 vistas con overflow a 0 y sin errores de cliente. Cronograma girado sin scroll anidado (la página desplaza, el Gantt solo horizontal) y columna de oportunidad 210–230 px; rail lateral compacto para alturas ≤560 px; gráfico «Actividad del mercado» ajustado con container query (antes salía 31 px); Inicio con columnas de igual alto como `03-inicio.png`, competidores en filas de una línea y estado vacío con acción; Mercado › Empresa con columnas `minmax(0,1fr)`, tabla con scroll propio y filas estructuradas en mobile; vista rápida de Contratos menores como panel flotante ≤1500 px y hoja inferior en mobile solo al tocar una fila (Esc cierra, también en Buscador SEACE) y título de inspector visible en paneles de alto fijo. 203 tests web, TypeScript, arquitectura16, build e Impeccable 0 hallazgos. Evidencia `docs/new-style/qa/responsive-*-20261014.png`.

- [x] Guardar procedimientos del Buscador SEACE y verlos en el Cronograma (13/10/2026, pedido del usuario: «en SEACE ya no hay botón de guardar» y faltaba en el Gantt). Marcador en cada fila PROD2 (columna propia, fuera del botón de fila), en la vista rápida y en el detalle («Guardar oportunidad»); `PUT/DELETE /api/prod2/opportunities/:id/saved` sobre `saved_opportunities`. Cronograma con pestaña «Buscador SEACE» (guardadas con etapas oficiales) y guardadas de Contratos menores siempre visibles con marca «Guardada». `EmptyState` usa icono Lucide en vez del cuadrado y su acción es un enlace con estilo de botón (sin `<button>` dentro de `<a>`). 203 tests web (+5 nuevos), TypeScript, arquitectura16 y build OK; capturas desktop/laptop/mobile sin overflow ni errores y prueba real guardar→Gantt→quitar (tabla de vuelta en 0). Evidencia `docs/new-style/qa/seace-save-gantt-*-20261013.png`; spec `docs/frontend/13-cronograma.md`.

- [x] Rediseñar formularios de `/perfil` como maestro-detalle (13/10/2026, por diagnóstico del usuario: Experiencia con contrato abierto medía 11.908 px desktop / 23.513 px mobile). Lista con búsqueda (sin acentos/mayúsculas) agrupada por rubro, unificando variantes como «OIL & GAS»/«Oil & Gas» solo en la vista; editor fijo por pasos Contrato · Monto · Fechas · Archivos; Experiencia separada en Contratos / Montos por área; Equipo con pasos Datos · Experiencia · Certificados · Archivos; barra «Guardar cambios» fija al haber cambios; mobile alterna lista/editor con «Volver a la lista». Radar con segmentos CUBSO en píldoras y keywords plegadas (+N). Sin cambios de API/JSONB ni migración. 12 tests de modelo + 12 TS, TypeScript y arquitectura16 aprobados; QA local con datos reales de lectura y escrituras bloqueadas en desktop1440/laptop1024/mobile390 sin overflow ni errores (Experiencia 1.531 px desktop). Impeccable detect 0 hallazgos. Capturas `docs/new-style/qa/profile-master-detail-*-20261013.png`.

- [x] Mostrar Analizado·Puntaje por validar en filas/inspectores y Con análisis en ambas fuentes (13/10/2026): identifica matchguardado por ID explícito, distingue gris sin análisis, conserva score0legacy y puntoskeywords de noanalizados; URLperfil/query/tipo/plazo persistidos y page reset. BarraPROD2 fija también perfilresuelto. Principal10SSRhelper/TypeScript/arquitectura16/build61 y15capturas reales locales/productivas desktop/laptop/mobile contra Glass; teclado/foco/copy/overflow revisados y producción5SEACE/9menoresno cerrados,10incl cerrados. Impeccable0hallazgos; publicado selectivamente sin workers/modelos/recalcular. `docs/feed-analysis-visible-20261013.{md,json}`. Limpieza26306bytes, QApropio detenido y capturas/recibos/rollback conservados; notas semánticas pendientes preservadas.

- [x] Integrar y desplegar política sustantiva V4 en detalle Buscador SEACE PROD2 (13/10/2026): cobertura/brechas de capacidad, checklist administrativo plegado sin penalización y Puntaje por validar sin número; conserva deseables/evidencia/perfil/atribución histórica por miembro. MYPE muestra Condicional·MYPE y no se suma al umbral general. Principal5SSR/11presentación, TypeScript/arquitectura16/build61; ocho capturas reales locales/productivas desktop1440/laptop1024/mobile390 y menores revisadas contra Glass. Teclado/checklist/nota retenida y cero overflow/errores/escrituras; Impeccable0hallazgos. Lote5PROD2/5menores exacto en APIs, no Por leer, cinco fuentes en revisión preservadas; web selectiva de cuatro archivos activa. Informe `docs/dinaut-ten-read-matches-20261013.{md,json}`; limpieza69889bytes, QA propio detenido, recibos/revisiones/rollback retenidos. Aprobación semántica de puntajes sigue pendiente en QA.

- [x] Separar brechas de capacidad y «Documentos para presentar» en detalle de menores con la política LLM2 sustantiva (13/10/2026): checklist administrativo plegado/neutro, RNP/RUC/anexos sin reducir cobertura, licencias y certificados especializados conservados; contador desde `breakdown.requisitos` y estados `cumple`, re-evaluación vinculada al perfil del match. V4 conserva la clasificación autorizada y propuestas técnicas API/SLA sin reclasificarlas como formatos. Puntaje retenido muestra «Puntaje por validar» sin número ni gauge, conservando análisis/cobertura/brechas. Principal verifica once tests UI, TypeScript, arquitectura16dominios, build61páginas y ocho capturas reales locales/productivas desktop1440/laptop1024/mobile390 + API/SLA. Checklist plegado/teclado, cero overflow/errores/escrituras. Capturas `docs/new-style/qa/dinaut-substantive-{local,production}-*-20261013.png` revisadas contra Glass; Impeccable0hallazgos. Web desplegada selectivamente; notas del modelo no validadas permanecen ocultas. Informe `docs/dinaut-five-substantive-compact-20261013.json`; limpieza59694bytes, inputs/recibos/rollback conservados.

- [x] Corregir el contador «Requisitos cubiertos» del detalle de menores: excluir penalidades/condiciones no evaluables y calcular estados sobre los requisitos del match, conservando el total de facets detectadas por separado. Caso 101535, match913: 8 requisitos LLM2 en revisión y 1 facet de penalidad excluida muestran incorrectamente 1/9 cubierto. Captura productiva `docs/new-style/qa/dinaut-five-llm2-server-production-20261013.png`; no interpretar ese contador como acreditación. Cierre13/10: V4 calcula solo capacidad sustantiva; servidor0/1 y SERFOR0/3 en datos productivos. Formularios/penalidades excluidos, cero notas inventadas. Once tests UI y capturas locales/productivas revisadas en tres tamaños; `docs/dinaut-five-substantive-compact-20261013.json`.

- [x] Mostrar resumen y texto íntegro por documento en biblioteca Dinaut (13/10/2026): descripción completa y texto por página en disclosures independientes cerrados, montaje del texto solo al abrir, scroll acotado/wrapping/teclado/foco y métricas OCR. Datos productivos 43 documentos/571 páginas exactos; doce capturas locales/productivas desktop1440/laptop1024/mobile390 revisadas contra Glass, sin overflow/errores ni escrituras. TypeScript/arquitectura16dominios/build61páginas aprobados; web desplegada selectivamente en cinco archivos, workers preservados. `docs/dinaut-document-reading-20261013.json` y `docs/new-style/qa/company-reading-{local,production}-*-20261013.png`. Limpieza local confirmada 209370238 bytes; servidor QA propio detenido, originales/recibos/capturas/rollback conservados.

- [x] Mostrar afinidad por palabras clave en Buscador SEACE con tres puntos y orden análogo a menores (13/10/2026): niveles alta/media/baja, línea/puntos/coincidencias en inspector y detalle; Jev histórico deja de dirigir UI, anillo documental válido conserva prioridad. Perfil/líneas ausentes distinto de cero coincidencias, materiales Glass y dos pestañas actuales preservados. TypeScript/arquitectura16dominios/build61páginas y18capturas desktop/laptop/mobile locales/productivas revisadas; sin overflow/errores/escrituras browser. Release selectivo de seis archivos sobre imagen vigente, sin publicar cambios ajenos de documentos. `docs/new-style/qa/prod2-keyword-affinity-release-20261013.json`. Limpieza 48949688 bytes lógicos; servidor/PG QA propios retirados, capturas/recibos/paquete remoto/rollback conservados.

- [x] Retirar «Concursos SEACE» de la navegación y desplegar solo «Buscador SEACE»/«Contratos menores» (13/10/2026): feed con dos pestañas, acceso PROD4 del cronograma retirado, URLs guardadas redirigen a PROD2/PROD6 y enlaces de retorno históricos ajustados. Mantener Glass y datos/APIs históricos. Release selectivo de seis archivos sobre imagen productiva, sin cambios locales ajenos de análisis manual; arquitectura16dominios/TypeScript/build61páginas, Impeccable0hallazgos y24capturas locales/productivas desktop/laptop/mobile revisadas. Teclado/selección/44px/redirecciones y ausencia de overflow/errores/escrituras comprobados. Workers/PG/Redis/entorno preservados. `docs/new-style/qa/two-sources-release-20261013.json`; limpieza 972394 bytes lógicos, capturas/recibos/paquete remoto y rollback conservados.

- [x] Integrar y desplegar fuente PROD2 en feed/detalle Glass, afinidad comercial separada de requisitos, evidencia por integrante, deseables, cronograma aun sin match y PDF privado. Arquitectura/TypeScript/build y nueve pruebas aprobadas; nueve capturas fixture y seis reales desktop/laptop/mobile revisadas por agente principal, con 10 registros, nueve matches y una revisión. Evidencia `docs/prod2-web-integration-qa-20261005.md` y `docs/new-style/qa/prod2-production-*-20261005.png` (2026-10-05). Impeccable ausente: se aplicaron las otras skills/referencias documentadas.

- [x] Añadir «Analizar con IA» como CTA principal en detalle SEACE/PROD4 y mover «Revisar en SEACE» al encabezado sin cambiar su URL directa. Encola LLM2 existente, permite reevaluar, evita doble clic y consulta estado/refresh; explica bases pendientes y errores. QA desktop1440/laptop1024/mobile390, teclado, carga/success/error y bases faltantes con datos reales de lectura y todas las escrituras interceptadas; seis capturas `docs/new-style/qa/prod4-analyze-*-20261005.png` revisadas. Arquitectura/TypeScript/build y 14 pruebas de cola + 38 worker aprobadas. Solo local, requiere despliegue web/worker-llm (2026-10-05).
- [ ] Desplegar botón de análisis manual PROD4 y soporte force en web/worker-llm; verificar desde sesión autenticada de producción sin recalcular masivamente.

- [x] Añadir múltiples montos de experiencia económica por área en `/perfil`: rubro/especialidad/tipo/monto/moneda/respaldo, agregar/editar/quitar registros con confirmación y monto visible en fila plegada; preservar valores generales y contratos. Veinte tests TS, 238 worker, build y arquitectura aprobados. Guardado browser QA con escrituras interceptadas, desktop/laptop/mobile sin overflow; capturas `docs/new-style/qa/profile-economic-areas-*-20261005.png` revisadas (2026-10-05). No calcula admisibilidad por TDR ni suma monedas/áreas.
- [x] Desplegar web y protección económica de worker por áreas (`33ce09a`): contenedores web/worker-llm running, `/login` HTTP200 y smoke Python confirma que las áreas no usan el máximo global. Sin modificación de montos de usuarios ni llamadas LLM manuales (2026-10-05).

- [x] Ampliar Experiencia en `/perfil` con contratos por rubro/especialidad, actividades, tipo, moneda/monto, participación y alcance, fechas y respaldo; agrupar edición en tres secciones y conservar montos generales/metadata legacy sin sumarlos automáticamente. Validación cliente/API, 15 tests, arquitectura, TypeScript y build aprobados; QA local desktop/laptop/mobile con lectura real y todas las escrituras interceptadas. Capturas `docs/new-style/qa/profile-experience-{desktop,laptop,mobile}-20261005.png` revisadas (2026-10-05). No modifica scoring ni requiere migración SQL.
- [x] Desplegar y verificar en producción los nuevos campos de experiencia por contrato: commit `506962d`, contenedor web en ejecución y `/login` HTTP 200; QA funcional/visual realizado localmente (2026-10-05).

- [x] Desplegar rework de perfil en producción por Git (`c01e54b`, 2026-10-02): build Docker/TypeScript aprobado, web running sin reinicios, login HTTP200 y `/perfil` sin sesión redirige a login. Sin migración de DB ni cambios de workers. QA visual responsive aprobado en local con datos reales de lectura; no se verificó sesión autenticada productiva ni subida real de QA a R2.

- [x] Rework de `/perfil` por Radar/Equipo/Experiencia/Recursos/Documentos/Empresa: fichas agregables de profesionales, experiencia y certificados con respaldo en biblioteca; reemplazar CSV/pipe por registros, preservar metadata/strings existentes, validación oculta y guardado seguro. Ocho tests, arquitectura/TypeScript/build, QA responsive con datos reales de lectura y escrituras interceptadas, diez capturas revisadas (2026-10-02). Evidencia `docs/new-style/qa/profile-*-20261002.png`; spec `docs/frontend/05-perfil-empresa.md`.

- [x] Implementar `/cronograma` en sidebar con Gantt amplio PROD6/PROD4, afinidad 2–3 sin evaluar o amarillo/verde documental, etapas oficiales sin solapamiento, Hoy/rangos/búsqueda/detalle y agenda móvil. Diez pruebas, TypeScript/arquitectura, capturas reales desktop1600/laptop1024/mobile390 revisadas, teclado y empty/error aprobados (2026-10-02). Evidencia `docs/new-style/qa/gantt-prod{4,6}-{desktop,laptop,mobile}-20261002.png`.
- [x] Publicar Gantt en producción por Git (`8c19b47`, 2026-10-02): build Docker/TypeScript aprobado, web running sin reinicios, rutas `/cronograma` y `/api/cronograma` registradas, OpenAPI público actualizado y acceso privado sin sesión redirige a login. Sin cambios de DB/workers ni Gemini. QA visual con datos reales aprobada en local; no se verificó sesión autenticada productiva.

- [x] Desplegar detalle PROD4 mediante Git y build Docker web (`702ccda`, 2026-10-02); servicio running, cero reinicios, nueva ruta registrada y acceso sin sesión redirige a login. PostgreSQL confirma 10 extracciones y 27 matching.
- [ ] Validar visualmente el nuevo detalle PROD4 con sesión autenticada en producción; navegador de QA llega a login. Capturas responsive aprobadas corresponden a app local con datos reales de producción.

- [x] Implementar `/oportunidad/seace/[id]` y conectar Ver detalle completo del radar PROD4: lectura ejecutiva, evaluación personalizada, brechas, evidencia original, documentos oficiales, cronograma y ejecución/ítems; estados pendientes sin puntuación inventada. Reutiliza Glass y componentes aprobados; QA local con datos de producción, nueve capturas responsive y navegación aprobada (2026-10-02).
- [x] Corregir contador del nuevo detalle PROD4 usando requisitos evaluados con estado `cumple`, no facets menos brechas; conservar cálculo heredado PROD6 pendiente de revisión específica (2026-10-02).

- [ ] Corregir Requisitos cubiertos para contar estados cumple del análisis personalizado y total de requisitos evaluados, no facets.length menos missing_actions.length; distinguir dato no acreditado de incumplimiento comprobado. Diagnóstico 2026-10-01, sin cambio de UI todavía.

- [x] Publicar por Git la sección Cronograma y timeline oficial de ambas fuentes en producción (`b4e9c28`, 2026-10-01): build Docker con TypeScript y 58 páginas aprobado, web activa y conexión a PostgreSQL verificada para ficha 96189 con dos etapas. QA responsive local previo conserva evidencia.
- [ ] Capturar y revisar visualmente Cronograma en la sesión autenticada de producción: el navegador conectado llega a login; se pidió al usuario iniciar sesión. No marcar QA visual productivo sin acceso y captura real.

Este documento controla el desarrollo de la interfaz de BuenaPro.

Regla: antes de implementar una vista, leer su spec en `docs/frontend/`, el sistema visual en [DESIGN.md](../DESIGN.md) y la guia de adopcion en [docs/new-style/](../docs/new-style/README.md).

## Arquitectura frontend

- [x] Eliminar la especificacion visual anterior y todas sus referencias activas; `DESIGN.md` queda como unica fuente normativa (2026-08-03).
- [x] Formalizar y aplicar arquitectura frontend por dominios: paginas en `pages/`, componentes privados en `components/`, API/model por dominio, barrels publicos y chequeo automatizado de limites (2026-08-03). Documento: `docs/frontend/architecture.md`.

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

- [x] Adoptar la identidad orbital de BuenaPro como SVG compartido en auth, onboarding, shell, Inicio y favicon; sustituir la fotografia y tarjeta promocional del acceso por una escena radar SVG propia y validar desktop/mobile (2026-08-03). Evidencia: `docs/new-style/qa/auth-radar-desktop.png`, `auth-radar-mobile.png`, `register-radar-desktop.png`, `register-radar-mobile.png`.
- [x] Refinar la marca orbital y la escena de acceso con arcos geometricamente concentricos; usar aro blanco en el favicon para fondos oscuros, liberar `/icon.svg` del middleware y renovar evidencia visual (2026-08-03). Evidencia: `docs/new-style/qa/auth-radar-concentric-desktop.png`, `auth-radar-concentric-mobile.png`, `favicon-radar-white-ring.png`.

- [x] Generar una coleccion de 10 referencias visuales BuenaPro Glass para Login, Registro, Inicio, Oportunidades, Detalle, Mercado, Postulaciones, Workspace, Perfil y Alertas; archivos e indice en `docs/new-style/generated-glass/` (2026-08-03).
- [x] Sustituir la norma visual anterior por BuenaPro Glass en `AGENTS.md` y `DESIGN.md`: lienzo lavanda, vidrio ambiental, superficies de trabajo blancas, CTA negro, seleccion violeta, radios amplios e iconografia Lucide (2026-08-03).
- [x] Crear el catalogo interno modular `/design-system` con materiales, color, botones, controles, estados, metricas, decision y tabla compartida. Arquitectura, TypeScript y QA desktop/mobile validados (2026-08-03). Evidencia: `docs/new-style/qa/design-system-desktop.png`, `design-system-mobile.png`.
- [x] Migrar el shell productivo a BuenaPro Glass y Lucide, manteniendo solo Oportunidades, Mercado y Postulaciones como navegacion primaria y Perfil/Alertas en cuenta (2026-08-03).

- [x] Preparar y aplicar el reemplazo integral del sistema visual con `PRODUCT.md`, `DESIGN.md`, registro de referencias y mapa de adopcion en `docs/new-style/` (2026-08-03).
- [x] Implementar los tokens CSS BuenaPro Glass y mantener aliases tecnicos solo para consumidores existentes.
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

- [x] Aplicar BuenaPro Glass a Login y Registro; la primera iteracion con fotografia fue sustituida por la identidad orbital SVG y el radar propio documentados en Design system base (2026-08-03).
- [x] Redisenar `/login`.
- [x] Redisenar `/registro`.
- [x] Mejorar errores de login.
- [x] Redirigir a perfil si no existe `GET /api/profile`.
- [x] Evitar textos largos o look de landing.
- [x] Separar `/registro` de `/login` y crear onboarding asistido de 4 pasos: empresa/web, líneas con keywords propias, capacidad (monto/equipo/recursos) y revisión. El análisis web es opcional, las sugerencias IA requieren confirmación y el guardado respeta `company_profiles` + `business_lines`. Evidencia: `docs/qa/screenshots/onboarding-company-desktop-2026-07-10.png`, `onboarding-company-mobile-2026-07-10.png`.
- [x] Crear recurso visual propio para las 4 etapas del onboarding; version web optimizada a 180 KB: `apps/web/public/onboarding/onboarding-suite.jpg`.
- [x] Asociar cada línea generada por IA con 1–3 segmentos CUBSO válidos, mostrar nombre/cobertura, permitir corrección y exigir segmento antes de guardar. Persistencia validada contra catálogo 2026. Evidencia: `docs/qa/screenshots/onboarding-cubso-lines-2026-07-11.png`, `onboarding-cubso-lines-mobile-2026-07-11.png`.

Referencia: [Auth](../docs/frontend/01-auth.md)

## Inicio

- [x] Crear Inicio diario contra `03-inicio.png`: alerta de cierres sobre guardadas, KPIs actualizados al cargar, mesa exclusiva de `saved_contracts`, contexto de mercado y competidores; añadir Inicio a la navegación y fijar empresa/perfil al pie del sidebar. QA desktop/mobile sin overflow (2026-08-03). Evidencia: `docs/new-style/qa/home-daily-desktop.png`, `home-daily-mobile.png`.
- [x] Restaurar `/` como dashboard ejecutivo BuenaPro Glass conectado a `getDashboard`: KPIs, actividad mensual, composicion historica, cierres y ranking de proveedores; arquitectura, TypeScript y QA desktop/mobile validados (2026-08-03). Evidencia: `docs/new-style/qa/dashboard-glass-desktop.png`, `dashboard-glass-mobile.png`.
- [x] Rework de Inicio como `Hoy en tu radar`: cierres próximos, postulaciones en curso, mercado comparable y proveedores frecuentes; métricas accionables sin dashboard BI decorativo (build y smoke SSR validados el 2026-07-31).
- [x] Reestructurar Inicio como dashboard profesional: KPIs con conteos completos, tendencia mensual adjudicados/desiertos, composición histórica, ranking de empresas y mesas operativas de cierres/postulaciones; build y SSR con PostgreSQL real validados el 2026-07-31.
- [x] Capturar y revisar en desktop/mobile el dashboard profesional de Inicio. Evidencia Playwright: `docs/qa/screenshots/dashboard-market-desktop.png`, `dashboard-market-mobile.png` (2026-07-31).
- [x] Crear `/mercado` como explorador histórico: búsqueda por keywords, alcance perfil/todo SEACE, filtros por segmento, resultado, región, entidad, año y precio, más vistas de contratos y empresas adjudicadas.
- [x] Refinar las visualizaciones de Mercado contra `06-mercado.png`: actividad mensual con columnas cápsula violeta/verde, guías discretas y rankings comparables con posición, escala y valor; aplicar la misma paleta al resumen gráfico de Inicio. QA desktop/mobile sin overflow (2026-08-03). Evidencia: `docs/new-style/qa/market-charts-desktop.png`, `market-charts-mobile.png`.
- [x] Simplificar filtros de Mercado: búsqueda, resultado y departamento visibles; segmento, entidad, año y precios bajo `Filtros avanzados`, con reapertura automática cuando existen valores activos. Interacción y responsive validados (2026-08-03). Evidencia: `docs/new-style/qa/market-filter-desktop.png`, `market-filter-advanced-desktop.png`, `market-filter-mobile.png`.
- [x] Añadir filtro `Tipo` (Todos/Bienes/Servicios) a Mercado y conservarlo en las tres vistas; QA con datos reales, capturas desktop/laptop/mobile y sin overflow a 390 px (2026-09-29).
- [x] Crear detalle de empresa `/mercado/empresas/[ruc]` con montos, entidades, regiones, servicios y licitaciones ganadas.
- [x] Validar `/mercado` desktop/mobile y detalle de proveedor con datos reales de producción. Evidencia: `docs/qa/screenshots/market-intelligence-desktop.png`, `market-intelligence-mobile.png`, `market-supplier-desktop.png`.

- [x] Implementar `features/dashboard` como resumen ejecutivo separado del explorador detallado `market-intelligence`.
- [x] Crear vista `/`.
- [x] Mostrar estado de perfil.
- [x] Mostrar oportunidades recientes.
- [x] Mostrar oportunidades que cierran pronto.
- [x] Mostrar postulaciones en preparacion.
- [x] Soportar estado sin perfil.
- [x] Consumir `GET /api/contracts`, `GET /api/feed`, `GET /api/tracking`.

Referencia: [Inicio](../docs/frontend/02-inicio.md)

## Oportunidades

- [x] Añadir columnas «Subcontratación» y «Consorcio» en PROD2/PROD4/PROD6, estados explícitos por lectura vigente y evidencia/condiciones en vista rápida y detalle; preservar revisión humana, desconocidos e identidad al cambiar fila. 24 tests, arquitectura, TypeScript/build, PostgreSQL read-only de 15 lecturas y 33 capturas desktop/laptop/mobile verificadas. Solo local; evidencia `docs/participation-columns-qa-20261013.md` (2026-10-13).
- [x] Desplegar las columnas de participación y verificar listado/detalle en sesión productiva conservando pendientes y revisiones; sin recalcular lecturas para el smoke. Release selectivo de 18 archivos sobre la imagen productiva, 17 pruebas offline y build de 61 páginas. APIs/identidad/aislamiento y 18 capturas 1440/1024/390 verificadas; fechas largas corregidas sin solapamiento. PostgreSQL/Redis y workers preservados. Evidencia `docs/participation-columns-qa-20261013.md`; limpieza local/VM 15.343.075 bytes y archivos de rollback retenidos (2026-10-13).
- [x] Actualizar Impeccable a 4.5.2 en el proyecto tras autorización humana, sin hooks ni cambios a la copia desactivada del usuario; versión/instalación verificadas y ejecutable local excluido de Git (2026-10-13).

- [x] Replantear `/feed` contra la composicion de `docs/new-style/generated-glass/04-oportunidades.png`: busqueda dominante, solo 3 vistas rapidas, filtros secundarios progresivos sin presets hardcodeados de transporte/legal, tabla de 6 columnas utiles y vista rapida glass. QA desktop/mobile sin overflow y prueba funcional del disclosure de filtros (2026-08-03). Evidencia: `docs/new-style/qa/opportunities-reference-match-desktop.png`, `opportunities-reference-match-mobile.png`.
- [x] Añadir filtro `Tipo` (Todos/Bienes/Servicios) a Oportunidades y conservarlo en búsqueda y vistas rápidas; QA con datos reales, capturas desktop/laptop/mobile y sin overflow a 390 px (2026-09-29).
- [x] Hacer que `Tipo` en Oportunidades se aplique con un clic mediante opciones visibles, sin depender del desplegable ni del botón `Aplicar`; validar cambio Bienes/Servicios, persistencia en filtros y capturas desktop/laptop/mobile sin overflow (2026-09-29).
- [x] Simplificar la vista rapida de Oportunidades: mantener visibles entidad, cierre y experiencia economica; mover ubicacion, pago, personal y penalidad a `Mas informacion`, y alinear la paginacion bajo la tabla (2026-08-03).
- [x] Restaurar la semantica de afinidad en Oportunidades: tres puntos por niveles antes de evaluar y anillo numerico solo cuando existe un score real generado por el analisis IA (2026-08-03).

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
- [x] Refactor visual 2026-07-09: vista rapida lateral, acciones Abrir detalle/Seguir arriba del panel y columnas Plazo y Match con datos derivados reales. Evidencia: `docs/qa/screenshots/feed-redesign-preview-2026-07-09.png`, `feed-redesign-mobile-2026-07-09.png`.
- [x] Mapeo robusto de summary/facets via `lib/extraction/opportunity.ts` (summary derivado -> filter index -> extraccion cruda); validado contra JSON real de 5 contratos.
- [x] Priorizar todas las oportunidades por afinidad absoluta: conservar rubro exacto, relacionado y general, ordenar de mayor a menor `fit_points` y exponer el puntaje numérico al pasar sobre los puntos. SQL y render SSR validados el 2026-07-16.
- [x] Capturar y revisar el feed priorizado en desktop y mobile, sin señales historicas y sin overflow (2026-08-03). Evidencia: `docs/new-style/qa/opportunities-desktop.png`, `opportunities-mobile.png`.

- [x] Exponer Concursos SEACE PROD4 en `/feed?source=prod4` sin mezclar IDs de contratos menores: pestaña de fuente, filtro bienes/servicios, búsqueda, vista rápida con cronograma y PDF oficial. Build, arquitectura, interacción y QA desktop/laptop/mobile con 72 procesos tecnológicos reales; capturas `docs/new-style/qa/prod4-opportunities-{desktop,laptop,mobile}.png` (2026-09-29).
- [x] Mostrar afinidad preliminar de 1–3 puntos y reservar el anillo 0–100 para evaluación documental del worker; orden por afinidad de VEYON SAC, vista rápida y responsive desktop/laptop/mobile revisados en producción (2026-09-29). El PDF oficial aún responde `403` desde la VM y no hay scores finales reales.
- [ ] Añadir navegación al detalle completo PROD4 y mostrar resumen/requisitos/estado documental desde API; permitir ver todos los documentos/ítems/etapas (la vista rápida limita cada sección a seis). Separar registro de presentación y señalar hora inferida, actualización y conflictos documentales. Implementación y capturas QA pendientes; diagnóstico de código completado el 2026-10-01.
- [ ] Añadir alternativa explícita «Abrir PDF en SEACE» para PROD4 usando enlace oficial verificado (navegación directa del usuario, no proxy de VM), conservando autenticación para archivos privados y sin prometer descarga si SEACE deniega acceso. El botón actual pasa por API de BuenaPro y conserva el bloqueo 403 de VM. Validar PDF real desde navegador y completar QA visual antes de marcar done (2026-10-01).
- [x] Mostrar sección oficial determinista de cronograma en ambas fuentes: pestaña `Cronograma` del detalle PROD6 y composición compacta PROD4 con preguntas/ofertas visibles y las demás etapas desplegables sin truncarlas. Consultas, observaciones, respuestas, registro y ofertas separados; fechas/hora/fuente/consulta visibles, sin inventar hora cuando solo hay día. 12 pruebas, TypeScript, build webpack y arquitectura aprobados; QA fixtures locales aislados en desktop1440, laptop1024 y mobile390 (sin overflow), teclado Enter/Space y datos faltantes. Evidencia `docs/new-style/qa/schedule-prod{4,6}-{desktop,laptop,mobile}-20261001.jpg`. No desplegado a producción (2026-10-01).
- [x] Añadir timeline oficial conectado y estados temporales en PROD4/PROD6 (2026-10-01): todas las etapas en orden, etapas simultáneas, fecha/hora Lima, día sin hora incierto, ventanas ampliadas recalculadas y aviso de que vencimiento no acredita ejecución. 16 tests web, arquitectura, TypeScript/build y capturas desktop/laptop/mobile revisadas con fixtures aisladas. Evidencia `docs/new-style/qa/timeline-prod{4,6}-{desktop,laptop,mobile}-20261001.jpg`. UI local, pendiente despliegue.
- [ ] Añadir alertas de cambios/ampliaciones de consultas y ofertas; no inferir disponibilidad solo por presencia en radar.

Referencia: [Oportunidades](../docs/frontend/03-oportunidades.md)

## Detalle de oportunidad

- [x] Rework integral de `/oportunidad/[id]` como mesa de decision BuenaPro Glass: gauge de score IA, progreso de requisitos, cierre y referencia de mercado; navegacion reducida; superficies independientes; secciones operativas plegables; brechas prioritarias con expansion; historico compacto; y Copilot contextual reconstruido como drawer accesible (2026-08-03). Evidencia: `docs/new-style/qa/detail-rework-desktop.png`, `detail-rework-mobile.png`, `detail-rework-requirements.png`, `detail-rework-documents.png`, `detail-rework-history.png`, `detail-rework-copilot.png`.
- [x] Sustituir el scroll acumulativo del detalle por navegación horizontal real entre Decisión, Requisitos, Documentos, Histórico y Ejecución; mantener la pestaña en el hash, plegar categorías y SEACE por defecto, y conservar el rail común. Altura desktop de la vista inicial reducida de 4,583 px a 1,538 px sin eliminar información (2026-08-03).

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
- [ ] Añadir `Actualizar datos oficiales` en el detalle con estados `Actualizando`, `Actualizado`, `Sin cambios` y `No disponible`; refrescar OCDS primero y mostrar el fallback `prod2` sin bloquear la vista ni duplicar acciones.
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
- [x] Reestructurar el detalle como mesa de decisión: lectura ejecutiva unificada, requisitos prioritarios, ficha técnica progresiva, preparación lateral y actividad/comentarios. QA renovado desktop/mobile sin overflow (2026-08-03). Evidencia: `docs/new-style/qa/detail-desktop.png`, `detail-mobile.png`.
- [x] Corregir preview del TDR: miniatura PNG real en el rail, PDF inline en modal, fallback y estados accesibles. Build, PNG real `1059×1497` y PDF inline de 9 páginas validados; captura renovada del rail (2026-08-03): `docs/new-style/qa/detail-desktop.png`.
- [ ] Añadir conexión SEACE separada en Perfil y workspace lazy `Consultas oficiales / Preparar cotización` en detalle. Login real corregido (el formulario retenía `event.currentTarget` después del await), SSR/build/API validados; captura desktop/mobile pendiente.
- [x] Retirar del detalle la tarjeta repetitiva `Preparación` (monto, notas y checklist) y mover únicamente la asignación de responsable al rail de postulación con autosave y opción de desasignar. TypeScript y PATCH real asignar/limpiar validados el 2026-07-12; QA visual permanece cubierto por la captura pendiente del workspace.
- [x] Simplificar `Evaluar con mi perfil`: esperar una única respuesta del análisis directo, refrescar el detalle y retirar polling de hasta 2 minutos. Flujo real validado con contrato 78764; no cambia el layout del botón.

Referencia: [Detalle de oportunidad](../docs/frontend/04-detalle-oportunidad.md)

## Perfil de empresa

- [x] Uniformizar `/perfil` con BuenaPro Glass: resumen claro, completitud violeta, superficies de trabajo blancas, chips violetas y rail de capacidad/SEACE consistente. QA desktop/mobile sin overflow (2026-08-03). Evidencia: `docs/new-style/qa/profile-unified-desktop.png`, `profile-unified-mobile.png`.
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
- [x] Añadir biblioteca de postulación en `/perfil` para datos libres y documentos de respaldo con tags creados por el usuario, búsqueda, subida/descarga/borrado y formulario simplificado a nombre/tags/descripcion. Arquitectura, build, smoke API y QA desktop/laptop/mobile validados (2026-08-08). Evidencia: `docs/new-style/qa/profile-library-simple-desktop.png`, `profile-library-simple-laptop.png`, `profile-library-simple-mobile.png`.
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
- [x] Rework de Seguimiento como cartera compacta: una tabla, urgencia, progreso, responsable y próximo paso; filas con borrador abren `/postulaciones/[matchId]`. Build/SSR y captura desktop/mobile validados (2026-08-03). Evidencia: `docs/new-style/qa/applications-glass-desktop.png`, `applications-glass-mobile.png`.

## Postulación

- [x] Añadir `Precio y mercado` a `/postulaciones/[matchId]`, contraste de oferta contra rango histórico, comparables usados y acceso read-only a consultas oficiales; envío SEACE permanece deshabilitado hasta confirmar endpoint.

- [x] Crear workspace `/postulaciones/[matchId]` con oferta por ítems, RTM dinámicos, vigencia/contacto, archivos y rail de progreso. Persistencia real, build y QA desktop/mobile con borrador real validados (2026-08-03). Evidencia: `docs/new-style/qa/workspace-desktop.png`, `workspace-mobile.png`.
- [x] Simplificar el workspace como mesa única: oferta, RTM, mercado y carga de propuesta en una sola vista; contenido operativo primero en mobile y rail después, sin solaparse con navegación/copiloto (2026-08-03).
- [x] Mantener `Enviar a SEACE` fuera de alcance hasta validar el POST oficial, múltiples ítems y un tipo de cotización distinto de 2.
- [x] Añadir `Registrar como presentada` al rail del workspace cuando oferta, contacto, RTM y propuesta estén completos; mostrar `¡Felicidades! Tu licitación quedó registrada` tras persistir y aclarar que el cargo oficial sigue en SEACE. Autoguardado acumulativo, subida sin pérdida de campos, arquitectura, TypeScript, build y QA desktop/laptop/mobile validados (2026-08-21). Evidencia: `docs/new-style/qa/application-submit-*.png`.
- [x] Conectar panel `Preguntar a BuenaPro` al endpoint SSE del copiloto para recibir user message, estado de sesión Codex y assistant message persistido sin bloquear la UI. Arquitectura frontend y smoke dev del endpoint validados (2026-08-08).
- [x] Mostrar actividad intermedia del agente Codex en el panel `Preguntar a BuenaPro` durante lectura de documentos: sesión, descarga, validación y extracción aparecen como pasos compactos mientras llega la respuesta final. TypeScript, arquitectura, build y capturas desktop/mobile validados (2026-08-09). Evidencia: `docs/new-style/qa/copilot-agent-activity-desktop.png`, `docs/new-style/qa/copilot-agent-activity-mobile.png`.
- [x] Añadir `Nueva conversación` al panel `Preguntar a BuenaPro`: crea una sesión limpia, inicia un thread Codex independiente y usa workspace aislado por conversación. Arquitectura, TypeScript, build y QA desktop/laptop/mobile validados. Evidencia: `docs/new-style/qa/copilot-new-conversation-*.png` (2026-08-21).
- [ ] Mostrar documentos generados debajo de la respuesta con nombre, tamaño y descarga accesible; implementación, TypeScript, arquitectura, build y prueba end-to-end completos, captura desktop/mobile pendiente porque el navegador interno no estuvo disponible (2026-08-09).
- [ ] Ocultar `Cambios listos para revisar` en respuestas de generación/edición documental y mostrar el archivo descargable como resultado principal; implementación, pruebas, arquitectura, build y despliegue productivo `8bfd891` completos. QA visual pendiente porque el navegador interno no pudo conectarse en esta sesión (2026-08-21).
- [ ] Integrar panel `Preguntar a BuenaPro` en detalle y postulación: historial, fuentes, sugerencias, comparación y confirmación manual. Build/TypeScript y flujo funcional validados; captura desktop/mobile pendiente porque el navegador interno no estuvo disponible.

Referencia: [Seguimiento](../docs/frontend/06-seguimiento.md)

## Configuracion

- [x] Añadir `/configuracion` con radar automatico y multiples chats de Telegram, y enriquecer `/alertas` con score, modalidad, fortalezas, riesgo y CTA. QA desktop/mobile: `docs/new-style/qa/automation-*.png` (2026-08-03).
- [x] Reemplazar Configuración visible por `/alertas`: activación, afinidad mínima, límite diario e historial; `/configuracion` redirige y workspace/miembros quedan fuera de navegación (2026-07-31).
- [x] Validar navegación simplificada, menú de cuenta y Alertas desktop/mobile. Evidencia: `docs/qa/screenshots/account-menu-desktop.png`, `alerts-center-desktop.png`, `alerts-center-mobile.png`, `applications-navigation-desktop.png`, `navigation-simplified-mobile.png`.
- [x] Retirar el dominio frontend `settings` sin ruta activa; la configuracion vigente pertenece a `alerts` y al perfil de empresa (2026-08-03).

- [x] Implementar `features/settings` en v1, posteriormente retirado al simplificar la navegacion.
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

- [x] Mostrar la lectura completa guardada y desplegar el texto íntegro de cada brecha/requisito de PROD2, incluidas las adicionales, sin reanalizar ni modificar puntajes. Vinculación única por título/tipo, protegida ante órdenes históricos distintos de facets. Validación: 229 Node/5 skip, TypeScript, arquitectura16 y builds61 local/producción; seis capturas locales y seis productivas desktop/laptop/mobile, expansión por teclado y sin overflow ni errores. Caso1047247: resumen582 y requisitos1497/334/411 caracteres. Webc3aa3a4, imagena3f7e539a9da,588hashes; solo web recreada,8 servicios y entorno preservados. QA privada tmp/readable-detail-20261016. El detector señaló únicamente el borde preexistente del blockquote, sin nuevos hallazgos.

- [x] Priorizar puntajes documentales válidos del perfil activo antes de afinidad y paginación en ambos feeds; cápsula «Con puntaje» (with_score=true), incluidos cero/provisionales y filtro legacy analysis=done preservado. Perfil activo/propio consistente entre pestañas, afinidad, inspector y detalle. Principal 224 Node/5 skip, TypeScript/arquitectura16/build61; 12 capturas locales reales y seis productivas desktop1600/laptop1024/mobile390 revisadas contra Glass, sin overflow, errores ni escrituras. APIs verifican orden entre páginas, perfil ajeno404 e inválido400. Web05db6c9/imagen919b9e38 con585 hashes exactos. Evidencia privada tmp/dinaut-two-dot-50-20261015/qa-local-real y qa-production; los nuevos análisis se verifican por separado.

- [x] Unificar Decisión y Requisitos en una sola sección navegable del detalle; lectura ejecutiva primero y categorías documentales plegables debajo (2026-08-03).

- [x] Comparar las diez vistas productivas contra `docs/new-style/generated-glass/` en desktop 1440 y mobile 390, sin overflow horizontal: Login, Registro, Inicio, Oportunidades, Detalle, Mercado, Postulaciones, Workspace, Perfil y Alertas (2026-08-03). Evidencia consolidada: `docs/new-style/qa/*-glass-*.png`.

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

- [ ] Verificar con capturas desktop/laptop/mobile y en producción la afinidad comercial DIN 0–100 por título, preservando el análisis documental y los estados de metadatos de los 157 seleccionados (2026-10-20).
