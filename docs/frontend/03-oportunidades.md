# Vista 03 - Oportunidades

## Objetivo

Mostrar licitaciones/contratos menores cargados por BuenaPro, con filtros utiles y prioridad por match cuando exista perfil.

## Usuario

Proveedor que revisa oportunidades vigentes. Puede estar antes o despues de configurar su perfil.

## Ruta

```text
/feed
```

Desde el 13/10/2026, por solicitud del usuario, el feed muestra únicamente **Buscador SEACE** (`/feed?source=prod2`) y **Contratos menores** (`/feed`). Se retira la pestaña de Concursos SEACE porque su ingesta PROD4 está detenida. Los enlaces guardados a `/feed?source=prod4` redirigen al buscador sin trasladar filtros exclusivos de la fuente retirada. No se mezclan identidades entre fuentes.

## Concursos SEACE (PROD4, acceso histórico)

Se conservan los datos, APIs y detalles históricos. Sus enlaces de retorno llevan al Buscador SEACE; no existe acceso PROD4 desde las pestañas del feed o del cronograma. La siguiente documentación describe el comportamiento de lectura histórico, no un worker activo.

- `GET /api/prod4/opportunities?object=good|service&q=&state=current|exited|all&page=&page_size=`: solo procedimientos marcados como tecnología por CUBSO; la vista usa `current` por defecto.
- `GET /api/prod4/opportunities/:id`: ficha con ítems, cronograma y metadatos documentales.
- El detalle devuelve `official_schedule`: todas las etapas de `raw_detail.listaCronograma` normalizadas por reglas, además de `process.detail_fetched_at`. Conserva `schedule` para compatibilidad; sus timestamps históricos pueden tener horas inferidas, por lo que la nueva sección usa exclusivamente `official_schedule`.
- `Cronograma oficial` en la vista rápida usa un timeline conectado en el orden publicado: primeras tres etapas y desplegable con todas las siguientes. El resumen identifica todas las ventanas simultáneamente en plazo, o la próxima fecha conocida. Diferencia absolución/respuestas de formulación de consultas y observaciones. Inicio/cierre usan hora de Perú, con fecha y hora separadas; las fechas sin hora indican `Hora no informada` y las ausentes `No informado`. No interviene Gemini.
- Estados temporales: `Plazo finalizado`, `En plazo`, `Próxima`, `Sin precisión suficiente`. Son posiciones en las fechas publicadas, no constancias de ejecución/adjudicación ni elegibilidad. Una fecha sin hora correspondiente al día actual en Lima queda incierta. Se recalculan cada minuto en navegador; no se persisten estados que quedarían obsoletos. La consulta del cronograma usa `schedule_fetched_at` y, si falta, `detail_fetched_at`.
- `GET /api/prod4/opportunities/:id/documents/:code`: proxy autenticado de PDF oficial, sin almacenar el archivo.
- La fecha de cierre de registro no equivale al cierre de presentación de propuestas. Mostrar ambas por separado y priorizar la segunda solo cuando la ficha oficial la indique.
- `exited` significa que salió del listado PROD4, no que fue adjudicado, cancelado ni cerrado. No inferir estado jurídico desde esa ausencia.
- La vista rápida es informativa: no afirma elegibilidad, no ejecuta postulación y remite al módulo oficial para comprobar cronograma y requisitos.
- El listado autenticado compara CUBSO y texto de los ítems con las líneas de negocio activas del perfil. Ordena por afinidad preliminar y muestra 1–3 puntos: general, relacionado o rubro exacto. Si no hay segmento compatible, deja la afinidad sin calcular; no inventa el nivel 1.
- El anillo 0–100 y el veredicto solo aparecen cuando el worker extrajo requisitos de un PDF oficial de bases/EETT/TDR y ejecutó el análisis con Gemini. Los puntos preliminares no significan que el proveedor cumpla los requisitos.

## Dos modos

### Explorar

Usa contratos crudos de SEACE ya cargados.

```text
GET /api/contracts
```

Sirve cuando el usuario aun no tiene perfil o quiere revisar todo.

### Prioritario

Usa matches calculados contra el perfil.

```text
GET /api/feed
```

Sirve cuando ya existe perfil y lineas de negocio.

## Layout

- Header de pagina: titulo corto, contador y accion secundaria.
- Toolbar con busqueda y filtros principales.
- Tabla/lista densa y limpia.
- Drawer o panel lateral para filtros avanzados.

## Columnas del listado

- Código y objeto en una misma columna, con descripción completa en la vista rápida.
- Entidad; ubicación secundaria cuando está disponible.
- Subcontratación.
- Consorcio.
- Cierre o próxima fecha oficial, distinguiendo registro de ofertas.
- Evaluación documental si existe; afinidad preliminar en los demás casos.
- Guardar en contratos menores.

Subcontratación y consorcio usan la extracción vigente de la identidad y fuente de la fila. `participation_extraction` contiene solo `id`, `quality`, `requires_human_review`, `participation` y `page_scope`; no se usa como respaldo el permiso canónico de otra lectura. Una clasificación necesita cita y página válida. Los estados son `Permitida/Permitido`, `No permitida/No permitido`, `Con condiciones`, `Por confirmar` y `Por leer`. Las condiciones limitan un permiso declarado; no se inventa un porcentaje permitido. Una lectura con revisión pendiente conserva `Por revisar` junto al estado detectado.

La vista rápida despliega «Cómo puedes participar», con cita, página y condiciones. La falta de cláusula no equivale a autorización o prohibición. Al cambiar de fila, el detalle anterior se descarta visualmente por identidad desde el primer render. En móvil ambas etiquetas permanecen visibles; en desktop son accesibles aunque ya estén en el encabezado. Pago, plazo, ubicación y datos operativos adicionales siguen en «Más información» o el detalle.

Implementación, despliegue web y QA productivo: [participation-columns-qa-20261013.md](../participation-columns-qa-20261013.md). Publicado el 13/10/2026 en las tres fuentes; fechas largas envuelven dentro de su columna sin invadir afinidad.

## Filtros

Base:

- guardadas del workspace `saved=true`
- texto `q`
- objeto
- estado
- segmento CUBSO
- region
- bucket MVP: tecnologia, transporte, legal
- cierra antes de fecha
- tiene extraccion IA
- con puntaje personalizado (`with_score=true`), final o provisional válido de 0 a 100; incluye cero
- se puede cotizar

El panel de filtros expone `Tipo` como tres opciones visibles: `Todos`, `Bienes` y `Servicios` (`objeto=1/2`). Al elegir una opción se aplica de inmediato y se marca la activa; `Aplicar`, la búsqueda y las vistas rápidas conservan el tipo seleccionado.

Inteligentes:

- verdict
- facet
- role
- tipo_pago

## Componentes

```text
features/opportunities/components/OpportunityToolbar/
features/opportunities/components/OpportunityTable/
features/opportunities/components/OpportunityRow/
features/opportunities/components/OpportunityFiltersDrawer/
features/opportunities/components/OpportunityVerdictBadge/
features/opportunities/components/DeadlineCell/
features/opportunities/components/EmptyOpportunities/
```

## Backend

```text
GET /api/contracts?page=&page_size=&profile_id=&q=&objeto=&estado=&segmento=&region=&bucket=&has_extraction=&cotizar=&with_score=true
PUT /api/contracts/:id/saved
DELETE /api/contracts/:id/saved
GET /api/feed?page=&page_size=&verdict=&q=&objeto=&estado=&segmento=&region=&role=&facet=&tipo_pago=
POST /api/contracts/:id/track
GET /api/catalogs/objects
GET /api/catalogs/states
GET /api/catalogs/cubso/segments
GET /api/catalogs/enabled-cubso-segments
```

## Acciones

- Abrir detalle.
- Guardar o quitar una oportunidad sin incorporarla al seguimiento.
- Marcar como `interesada`.
- Pasar directo a `en_preparacion`.
- Limpiar filtros.

## Estados

- Sin perfil: mostrar modo Explorar y CTA discreta a Perfil.
- Con perfil pero sin matches: mostrar Explorar + aviso de que el match se esta calculando.
- Sin resultados por filtros: permitir limpiar filtros.
- Error de API: retry.

## Criterios de done

- La tabla no se rompe con textos largos.
- Paginacion funciona.
- Los filtros reflejan query params.
- La vista es usable con 0 matches y con matches.

### Buscador público PROD2 (2026-10-05)

`/feed?source=prod2` reutiliza la lista, filtros y vista rápida Glass. La identidad de fila y enlace es `id_convocatoria`; el detalle vive en `/oportunidad/prod2/[id]`. La fuente conserva `seace_prod2` y no se almacena como PROD4. Las fichas consultadas aparecen aunque no tengan match o requieran revisión humana; el listado no afirma que todas estén abiertas.

`GET /api/prod2/opportunities` acepta `q` (máximo 120 caracteres), `object=good|service`, `page`, `page_size` (1–100) y `profile_id` opcional. Devuelve `data`, `total`, `page`, `page_size` y el perfil activo seleccionado. El perfil debe pertenecer al tenant autenticado; no se acepta un UUID de otra empresa. La paginación conserva fuente y perfil. Desde el 13/10/2026, la afinidad visible se calcula por reglas sobre las palabras clave actuales del perfil y la descripción oficial; se presenta con tres puntos, como Contratos menores. Los niveles son **Afinidad alta** (30–45 puntos), **Afinidad media** (10–29) y **Afinidad baja** (0–9). Sin perfil activo o líneas configuradas, muestra **Sin afinidad calculada**. No se infiere un segmento CUBSO ni capacidad económica para PROD2.

Las selecciones publicadas explícitamente en `prod2_feed_publications` también
pueden aparecer con los metadatos del listado mientras no existe ficha consultada.
La publicación debe conservar el hash vigente de la fila; un descubrimiento crudo
sin ficha ni publicación no aparece. `has_official_detail=false` muestra **Solo
datos del listado**, **Ficha por consultar** y la ausencia de documentos/cronograma
registrados, sin crear fechas ni puntajes. `publication_status` y
`publication_status_label` conservan el resultado de la última revisión: descartada
por JEV, cerrada al revisar, apertura por confirmar, lectura por revisar, documento
por seleccionar o lectura previa conservada. Son observaciones del lote, no una
declaración de vigencia actual. Las filas cerradas o descartadas no se ocultan por
ese estado y mantienen los filtros, la identidad, el perfil y la paginación.

El motor comparte las reglas léxicas de menores: raíces españolas, frase en orden 15 puntos, término explícito 10, identidad general de empresa hasta 10; términos limitados a 30 y total a 45. Las palabras cubiertas por una frase no se cuentan otra vez. Se elige una sola línea activa, sin sumar señales de líneas diferentes. El inspector y detalle explican la línea y coincidencias. Cambiar keywords actualiza la siguiente lectura, sin jobs ni modelos.

El orden global se aplica en PostgreSQL antes de paginar: primero el puntaje documental guardado válido de 0 a 100, de mayor a menor, incluyendo provisionales y cero. Después se conservan las palabras clave y fecha/identidad como desempate estable. Las filas sin puntaje siguen disponibles después; la afinidad comercial no se usa como puntaje ni acredita requisitos. El Jev histórico se conserva en la API, pero no dirige los puntos visibles.

Desde el 20/10/2026, las evaluaciones JEV individuales del perfil DIN con política
`din_title_commercial_affinity_v1` pueden aportar `commercial_affinity` como señal
separada: **0–100 · Afinidad comercial · Por título**. Se valida el perfil activo
actual, tenant, hash comercial, versión, recibo conocido, convocatoria y hash del
título/fila antes de presentarla. Los resultados históricos conjuntos no se
reutilizan como afinidad individual de DIN. El orden pasa a puntaje documental
válido, afinidad comercial JEV individual y palabras clave; todos antes de paginar.
El número JEV no crea análisis documental, nota de cumplimiento, match ni
elegibilidad. **Con puntaje** y `analysis=done` mantienen sus condiciones de LLM2.

Desde el 15/10/2026, la cápsula existente se llama **Con puntaje** (`with_score=true`) y selecciona únicamente números documentales válidos de 0 a 100, antes del conteo y paginación. Conserva búsqueda, tipo, perfil y plazo; los menores vencidos requieren «Cualquier cierre». Ambos listados resuelven un único perfil activo del tenant y rechazan perfiles ajenos o inactivos. Las pestañas, búsqueda, paginación, inspector y detalle conservan ese perfil. En menores también se limita afinidad y selección del match al mismo perfil. No se considera «Por leer» como analizado ni se inventa una nota desde afinidad.

El parámetro legacy `analysis=done` continúa disponible en ambas APIs con su semántica original: análisis guardado aunque el número no exista. El nuevo control migra ese parámetro a `with_score`; no modifica resultados ni flags de validación. `with_score=false` u omitido conserva todas las filas con las demás restricciones. Valores distintos de true/false reciben400.

Por solicitud explícita del usuario del 13/10/2026, ambos listados e inspectores muestran el número guardado de 0 a 100 con etiqueta «Provisional / Por validar» cuando `provisional_score` es finito, está en rango y pertenece al análisis vigente en revisión. El anillo usa rojo para menos de 30, amarillo para 30–80 inclusive y verde para más de 80; `scoreTone` aplica el mismo criterio a notas finales y provisionales, independiente del veredicto documental. «Por validar» permanece incluso en verde, sin afirmar aprobación. El puntaje final `score` conserva su protección y no se modifica `score_validated`, fuente, resultado ni orden. Si falta el número, muestra «Analizado · Puntaje por validar»; una oportunidad sin análisis conserva solo afinidad preliminar. Un cero provisional real se muestra como cero y nunca se inventa desde null. Los ceros validados y resultados legacy publicados mantienen su anillo. El inspector ofrece el enlace al análisis completo y conserva el perfil seleccionado. En PROD2, el toolbar fija también el perfil resuelto cuando la URL inicial no lo incluía. El feed sin filtro continúa mostrando todas las fichas consultadas.

La vista rápida consulta `GET /api/prod2/opportunities/:id?profile_id=...`. Incluye cronograma con precisión original, documentos y acceso al detalle. No usa CUBSO o regiones inferidos de palabras ni encola análisis manuales. El enlace oficial abre el buscador estable de PROD2: SEACE requiere una sesión para abrir una ficha, por lo que no se promete un permalink del procedimiento.


### Categoría: filtro simple (20/10/2026)

En Buscador SEACE, la columna **Categoría** y la vista rápida muestran las categorías
efectivas del procedimiento. La clasificación documental válida más reciente tiene
prioridad; únicamente cuando falta se usan etiquetas preliminares calculadas desde
el título completo. Estas últimas llevan **Por título** y no acreditan requisitos,
vigencia ni capacidad de la empresa. La fila muestra una etiqueta compacta y `+N`
cuando existen más; el nombre accesible y la vista rápida conservan todas.
`Otros`, `Por confirmar` y `Por clasificar` distinguen fuera de catálogo,
ambigüedad y ausencia de clasificación. El filtro de las seis categorías usa esa
misma selección antes del conteo y la paginación. Los filtros documentales avanzados
siguen requiriendo evidencia documental y no aceptan etiquetas de título como respaldo.
La API PROD2 añade `commercial_classification_source` (`document`, `title` o null)
y `commercial_classification_state` (`classified`, `other`, `unclear`, `unclassified`)
junto a `commercial_categories`; conserva la clasificación documental original
normalizada en `commercial_classification`. Títulos cambiados o recibos incompletos,
inciertos o de otra política/modelo/catálogo/prompt no habilitan el respaldo por título.

Ambas fuentes comparten **Filtros → Categoría**: un único selector y el botón
Aplicar filtros. Opciones: Todas, Licencias y suscripciones, Desarrollo de software,
IA y automatización, Industrial, Cloud e infraestructura, Soporte y mantenimiento.
Se conservan los controles existentes de vigencia, puntaje y guardadas; la
clasificación comercial detallada deja de aparecer en el flujo normal.

En Buscador SEACE, elegir una categoría la aplica de inmediato y anuncia
**Aplicando…** mientras navega. La selección se serializa después de actualizar
el control y retirar criterios comerciales anteriores; conserva perfil,
búsqueda y tipo, y reinicia la página. Así no queda un borrador seleccionado
frente a una lista con otra categoría. Buscar y los enlaces de tipo/puntaje
comparten también la categoría pendiente: una nueva búsqueda durante la carga
conserva esa selección y el texto recién escrito. Contratos menores conserva el envío
manual con **Aplicar filtros** para combinar sus distintos controles.

El selector envía `commercial_category`. Búsqueda, tipo, vistas rápidas, cambio de
fuente y paginación conservan categoría y perfil. Quitar categoría limpia solo los
criterios comerciales. Un enlace antiguo con ejes detallados muestra Selección
anterior y conserva sus valores; elegir una categoría o Todas los reemplaza.

Las seis categorías se derivan con reglas del catálogo JSON compartido por web y
Python sobre la clasificación LLM1 válida. La API filtra en PostgreSQL antes de
count/paginación; no clasifica una página en React ni llama un modelo adicional.
Una oportunidad puede pertenecer a varias categorías. Todas conserva también
casos históricos sin etiquetas; no se inventa su clasificación desde el título.
No cambia puntaje, afinidad, modalidad jurídica ni guardadas. Publicación y
activación de lectores se verifican aparte. Contrato: [Clasificación comercial](../opportunity-classification.md).

### Afinidad JEV en Contratos menores (25/10/2026)

La lista de `/feed` (Contratos menores) muestra la calificación preliminar de afinidad comercial JEV del **perfil activo** junto al indicador existente de la columna «Análisis / afinidad». Lee `prod6_profile_decisions` en solo lectura; no llama a SEACE, a JEV ni a Gemini, y no cambia filtros ni orden (JEV no reordena la lista en este MVP).

- `GET /api/contracts` añade por fila `jev_score` (0–100), `jev_decision` (`accepted|rejected|pending`), `jev_subcategory`, `jev_threshold` y `jev_decided_at`; todos `null` sin decisión.
- Un par contrato × perfil puede tener varias filas (la clave incluye hashes y versión de política). Se usa la más reciente, con prioridad para `decided_by = 'jev'` y luego `updated_at`/`id` descendentes. La tabla vacía no produce error.
- La fila muestra «**78 / 100** · Afinidad JEV» sobre el indicador actual (puntaje documental, provisional o afinidad por palabras clave), que no cambia. La subcategoría legible, el umbral y la aclaración «no evalúa requisitos ni elegibilidad» viven en la vista rápida. El tooltip de la fila contiene el texto completo.
- Sin puntaje JEV válido (sin decisión, pendiente o decidida por prefiltro) la fila conserva exactamente el comportamiento anterior.
- El violeta identifica afinidad (información); no se aplica la regla rojo/amarillo/verde de puntajes documentales, que sigue reservada a esos puntajes.
- Etiquetas en español en `apps/web/lib/jevAffinity.ts`; componente `features/opportunities/components/JevAffinityMark/`.
- La vista por omisión conserva `has_extraction=true` y `estado=2`; un contrato aprobado por JEV sin extracción aparece al pasar `has_extraction=` vacío.
